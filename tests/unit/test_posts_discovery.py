"""Offline coordinator and storage tests; no TikTok/browser process is launched."""

import copy
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sqlite3

import pytest

import posts_discovery as discovery


NOW = datetime(2026, 8, 31, 7, 0, tzinfo=timezone.utc)


def forbidden(*args, **kwargs):
    raise AssertionError("This test must not launch a process, browser, or network request")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    # Pytest's full test-name directory can itself exceed Windows' path budget.
    root = tmp_path.parent / ("pd_" + discovery._hash(tmp_path.name)[:10])
    root.mkdir()
    (root / "engage_tiktok.py").write_text("# isolated test placeholder\n", encoding="utf-8")
    monkeypatch.setattr(discovery, "WORKSPACE", root)
    monkeypatch.setattr(discovery, "_now", lambda: NOW)
    monkeypatch.setattr(discovery.subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    return root


def make_plan(**kwargs):
    options = {"topic": "coffee shops Jakarta", "posts": 2, "last_hours": 24, "label": "coffee"}
    options.update(kwargs)
    return discovery.plan_posts(**options)


def saved_manifest(result):
    return json.loads(Path(result["paths"]["manifest"]).read_text(encoding="utf-8"))


def save_changed_manifest(result, manifest, *, resign=False):
    if resign:
        manifest["sha256"] = discovery._hash({key: value for key, value in manifest.items() if key != "sha256"})
    Path(result["paths"]["manifest"]).write_text(json.dumps(manifest), encoding="utf-8")


def make_handoff(manifest, *, state="complete"):
    """Use the real guarded operator's naming, intent, and handoff validators."""
    operator = discovery._operator_module()
    paths = operator.OperatorPaths(
        workspace=discovery.WORKSPACE.resolve(), python=discovery.REQUIRED_PYTHON.resolve(),
        engage_script=(discovery.WORKSPACE / "engage_tiktok.py").resolve(),
        master_database=(discovery.WORKSPACE / "comments_data/tiktok_master/state/tiktok_master.sqlite").resolve(),
    )
    window = manifest["publication_window"]
    argv = ["start", "--project", manifest["child_project"], "--topic", manifest["topic"],
            "--posts", str(manifest["requested_posts"]), "--published-after", window["start"],
            "--published-before", window["end"]]
    if manifest["expected_account"]:
        argv += ["--expected-account", manifest["expected_account"]]
    args = operator.build_parser().parse_args(argv)
    mode, target, count, all_posts = operator.validate_start_scope(args)
    operator.freeze_start_options(args, source_mode=mode, source_target=target, requested_count=count)
    naming = operator.explicit_project_naming(manifest["child_project"], timestamp="20260831_070000_000001")
    path, handoff = operator.new_handoff(
        project=manifest["child_project"], source_mode=mode, source_target=target,
        requested_count=count, all_posts=all_posts, args=args, paths=paths, naming=naming,
    )
    source = Path(handoff["database"])
    source.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source)) as db, db:
        db.execute("CREATE TABLE source_marker (value TEXT)")
        db.execute("INSERT INTO source_marker VALUES ('source is not to be modified')")
    handoff["run_id"] = "engage_" + discovery._hash(manifest["run_id"])[:16]
    handoff["frozen_selection"] = {}
    handoff["frozen_selection_hash"] = operator.json_hash({})
    handoff["state"] = state
    operator.write_handoff(path, handoff)
    return path, handoff


def source_row(manifest, handoff, post_id, published_at):
    packet = {"post_id": post_id, "creator": "testcreator", "url": f"https://www.tiktok.com/@testcreator/video/{post_id}",
              "caption": {"status": "available", "text": "Coffee shop test fixture"},
              "source_evidence_hash": "a" * 64}
    if published_at is not None:
        packet["published_at"] = published_at
    return {"schema_version": "tiktok-listen-evidence-export-v1", "project": manifest["child_project"],
            "run_id": handoff["run_id"], "post_id": post_id, "evidence_hash": "a" * 64,
            "evidence_projection_hash": discovery._hash(packet), "evidence_packet": packet}


def poll_result(manifest, path, handoff, state):
    result = {"operator_status": state, "project": manifest["child_project"],
              "requested": manifest["requested_posts"], "run_id": handoff["run_id"],
              "evidence_ready": manifest["requested_posts"] if state == "COMPLETE" else 0,
              "next_action": "validate_or_stop" if state == "COMPLETE" else "preserve_same_handoff",
              "safe_same_handoff_action": None, "replacement_project_allowed": False}
    if state in {"INTERRUPTED", "RESUME_READY"}:
        result["safe_same_handoff_action"] = {
            "operation": "guarded_resume",
            "argv": [str(discovery.REQUIRED_PYTHON.resolve()), str(discovery.OPERATOR.resolve()),
                     "resume", "--handoff", str(path.resolve())],
            "requires_explicit_user_direction": state == "RESUME_READY",
        }
    return result


class FakeOperator:
    """Mock execution only; real persisted handoffs bind every resumed child."""

    def __init__(self, result, *, state="COMPLETE", dates=None, mutate_rows=None,
                 mutate_review=None, mutate_poll=None, start_code=0):
        self.result = result
        self.state = state
        self.dates = dates if dates is not None else ["2026-08-30T08:00:00Z", "2026-08-31T06:00:00Z"]
        self.mutate_rows = mutate_rows
        self.mutate_review = mutate_review
        self.mutate_poll = mutate_poll
        self.start_code = start_code
        self.calls = []
        self.source_before = {}

    def populate(self):
        manifest = saved_manifest(self.result)
        path, handoff = make_handoff(manifest)
        rows = [source_row(manifest, handoff, str(7650000000000000000 + index), date)
                for index, date in enumerate(self.dates)]
        if self.mutate_rows:
            self.mutate_rows(rows)
        Path(handoff["export_file"]).write_text("".join(discovery._json(row) + "\n" for row in rows), encoding="utf-8")
        self.source_before = {key: Path(handoff[key]).read_bytes() for key in ("database", "export_file", "handoff_file")}
        return path, handoff

    def __call__(self, argv, *, foreground=False):
        self.calls.append((list(argv), foreground))
        manifest = saved_manifest(self.result)
        if argv[0] == "start":
            assert foreground is True
            assert manifest["state"] == "launch_requested"
            expected = ["start", "--project", manifest["child_project"], "--topic", manifest["topic"],
                        "--posts", str(manifest["requested_posts"]), "--published-after", manifest["publication_window"]["start"],
                        "--published-before", manifest["publication_window"]["end"]]
            if manifest["expected_account"]:
                expected += ["--expected-account", manifest["expected_account"]]
            assert argv == expected
            self.populate()
            return self.start_code, None
        assert foreground is False
        assert argv[:2] in (["poll", "--handoff"], ["validate", "--handoff"])
        path = Path(argv[2])
        handoff = json.loads(path.read_text(encoding="utf-8"))
        if argv[0] == "poll":
            result = poll_result(manifest, path, handoff, self.state)
            if self.mutate_poll:
                self.mutate_poll(result)
            return 0, result
        review = {"schema_version": "google-music-audit-review-v1", "task_outcome": "COMPLETE", "offline_validation": True,
                  "project": manifest["child_project"], "run_id": handoff["run_id"], "source_mode": "topic",
                  "source_target": manifest["topic"], "workflow": "listen", "collection_policy": "new_only",
                  "intent_hash": handoff["intent_hash"], "database": handoff["database"],
                  "status": {"status": "collection_complete", "evidence_ready": manifest["requested_posts"],
                             "publication_window": manifest["publication_window"],
                             "requested": manifest["requested_posts"]},
                  "artifacts": {"evidence_export": {"path": handoff["export_file"], "records": manifest["requested_posts"],
                                                      "schema_version": "tiktok-listen-evidence-export-v1",
                                                      "sha256": discovery._file_hash(handoff["export_file"])}},
                  "ai_actions": [], "outbound_actions": []}
        if self.mutate_review:
            self.mutate_review(review)
        return 0, review


def test_plan_status_and_report_are_offline_with_default_naming(workspace, monkeypatch):
    monkeypatch.setattr(discovery, "_operator_call", forbidden)
    monkeypatch.setattr(discovery, "_operator_module", forbidden)
    result = make_plan(posts=125, label="coffee_jakarta")
    manifest = saved_manifest(result)
    root = Path(result["run_dir"])
    assert root.parent == workspace / "comments_data/posts_discovery_runs"
    assert discovery.RUN_PATTERN.fullmatch(root.name)
    assert "coffee_jakarta_125p_20260831T070000Z_" in root.name
    assert manifest["artifact_stem"].startswith("posts_coffee_jakarta_20260831_")
    assert result["publication_window"] == {"start": "2026-08-30T07:00:00.000000Z", "end": "2026-08-31T07:00:00.000000Z",
                                            "bounds": "[start,end)", "order": "published_desc"}
    for path in result["paths"].values():
        assert Path(path).parent == root
        assert len(path) < 240
    assert discovery.status_run(root)["current_process_liveness"] == "not_checked"
    assert discovery.report_run(root)["status"] == "collection_incomplete"
    assert not (workspace / "comments_data" / ("project_" + manifest["child_project"])).exists()
    assert not Path(result["paths"]["database"]).exists()
    assert not Path(result["paths"]["export"]).exists()
    with pytest.raises(discovery.PostsDiscoveryError, match="No complete"):
        discovery.validate_run(root)


def test_absolute_window_normalizes_timezone_and_stays_fixed_on_status(workspace, monkeypatch):
    result = make_plan(last_hours=None, since="2026-08-30T14:00:00+07:00", until="2026-08-31T14:00:00+07:00")
    manifest_before = Path(result["paths"]["manifest"]).read_bytes()
    monkeypatch.setattr(discovery, "_now", lambda: datetime(2026, 9, 9, tzinfo=timezone.utc))
    assert discovery.status_run(result["run_dir"])["publication_window"] == result["publication_window"]
    assert Path(result["paths"]["manifest"]).read_bytes() == manifest_before


def test_long_topic_slug_keeps_normal_workspace_default_artifacts_within_windows_budget(workspace):
    result = make_plan(topic="Indonesian indie artists with a very long descriptive category", label="", posts=50)
    root = Path(result["run_dir"])
    manifest = saved_manifest(result)
    assert root.name.startswith("posts_discovery_indonesian_indie_50p_")
    actual_workspace = Path(__file__).resolve().parents[2]
    actual_journal = actual_workspace / "comments_data/posts_discovery_runs" / root.name / (manifest["artifact_stem"] + "_posts.sqlite-journal")
    assert len(str(actual_journal).encode("utf-16-le")) // 2 < 240


@pytest.mark.parametrize("posts", [0, -1, True, "ALL", "2.5", "01"])
def test_invalid_count_never_creates_a_run(workspace, posts):
    with pytest.raises(discovery.PostsDiscoveryError):
        make_plan(posts=posts)
    assert not (workspace / "comments_data").exists()


@pytest.mark.parametrize("window", [
    {"last_hours": None}, {"last_hours": 0}, {"last_hours": -1}, {"last_hours": float("inf")},
    {"last_hours": float("nan")}, {"last_hours": True},
    {"last_hours": None, "since": "2026-08-30T00:00:00Z"},
    {"last_hours": None, "since": "2026-08-30T00:00:00", "until": "2026-08-31T00:00:00"},
    {"last_hours": None, "since": "2026-08-31T00:00:00Z", "until": "2026-08-30T00:00:00Z"},
    {"last_hours": 24, "since": "2026-08-30T00:00:00Z", "until": "2026-08-31T00:00:00Z"},
])
def test_invalid_or_missing_window_never_creates_a_run(workspace, window):
    with pytest.raises(discovery.PostsDiscoveryError):
        make_plan(**window)
    assert not (workspace / "comments_data").exists()


def test_collect_real_handoff_validates_sorted_export_and_never_writes_sources(workspace, monkeypatch):
    result = make_plan(expected_account="@KitaScore")
    fake = FakeOperator(result)
    monkeypatch.setattr(discovery, "_operator_call", fake)
    outcome = discovery.collect_posts(result["run_dir"])
    assert [argv[0] for argv, _ in fake.calls] == ["start", "poll", "validate"]
    assert outcome["status"] == "collection_complete" and outcome["validated"] is True
    assert outcome["accepted_posts"] == outcome["requested_posts"] == 2
    assert outcome["ai_actions"] == outcome["outbound_actions"] == []
    assert outcome["artist_analysis_performed"] is False
    rows = [json.loads(line) for line in Path(outcome["paths"]["export"]).read_text(encoding="utf-8").splitlines()]
    assert [row["post_id"] for row in rows] == ["7650000000000000001", "7650000000000000000"]
    assert all(row["schema_version"] == discovery.EXPORT_SCHEMA for row in rows)
    handoff = json.loads(Path(outcome["handoff"]).read_text(encoding="utf-8"))
    for key, original in fake.source_before.items():
        assert Path(handoff[key]).read_bytes() == original
    with closing(sqlite3.connect(Path(outcome["paths"]["database"]).as_uri() + "?mode=ro", uri=True)) as db:
        assert db.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 2
        assert db.execute("PRAGMA application_id").fetchone()[0] == discovery.APPLICATION_ID
    before = {str(path): path.read_bytes() for path in Path(outcome["run_dir"]).iterdir() if path.is_file()}
    assert discovery.validate_run(outcome["run_dir"])["validation_result"] == "pass"
    assert discovery.collect_posts(outcome["run_dir"])["validated"]
    assert len(fake.calls) == 3
    assert before == {str(path): path.read_bytes() for path in Path(outcome["run_dir"]).iterdir() if path.is_file()}


@pytest.mark.parametrize("state", ["RUNNING", "INTERRUPTED", "RESUME_READY", "BLOCKED", "RESTART_PENDING", "COLLECTION_COMPLETE_NEEDS_FINALIZE"])
def test_second_collect_only_polls_exact_preserved_handoff(workspace, monkeypatch, state):
    result = make_plan()
    fake = FakeOperator(result, state=state)
    monkeypatch.setattr(discovery, "_operator_call", fake)
    first = discovery.collect_posts(result["run_dir"])
    handoff_before = Path(first["handoff"]).read_bytes()
    second = discovery.collect_posts(result["run_dir"])
    assert [argv[0] for argv, _ in fake.calls] == ["start", "poll", "poll"]
    assert first["handoff"] == second["handoff"]
    assert first["child_project"] == second["child_project"]
    assert first["publication_window"] == second["publication_window"]
    assert second["operator_status"] == state and second["status"] == "collection_incomplete"
    assert second["accepted_posts"] == 0
    assert Path(second["handoff"]).read_bytes() == handoff_before
    assert fake.calls[-1][0] == ["poll", "--handoff", second["handoff"]]
    if state in {"INTERRUPTED", "RESUME_READY"}:
        assert second["safe_same_handoff_action"]["argv"][-3:] == ["resume", "--handoff", second["handoff"]]


def test_existing_complete_handoff_is_reused_without_start(workspace, monkeypatch):
    result = make_plan()
    fake = FakeOperator(result)
    path, _ = fake.populate()
    monkeypatch.setattr(discovery, "_operator_call", fake)
    outcome = discovery.collect_posts(result["run_dir"])
    assert outcome["handoff"] == str(path.resolve())
    assert [argv[0] for argv, _ in fake.calls] == ["poll", "validate"]


def test_post_count_above_fifty_has_no_coordinator_ceiling(workspace, monkeypatch):
    result = make_plan(posts=65)
    fake = FakeOperator(result, dates=["2026-08-31T06:00:00Z"] * 65)
    monkeypatch.setattr(discovery, "_operator_call", fake)
    outcome = discovery.collect_posts(result["run_dir"])
    assert outcome["accepted_posts"] == outcome["requested_posts"] == 65
    rows = [json.loads(line) for line in Path(outcome["paths"]["export"]).read_text(encoding="utf-8").splitlines()]
    assert len(rows) == len({row["post_id"] for row in rows}) == 65
    assert [int(row["post_id"]) for row in rows] == sorted((int(row["post_id"]) for row in rows), reverse=True)


def test_start_without_handoff_can_never_start_replacement(workspace, monkeypatch):
    result = make_plan()
    calls = []
    monkeypatch.setattr(discovery, "_operator_call", lambda argv, **kwargs: calls.append(argv) or (1, None))
    with pytest.raises(discovery.PostsDiscoveryError) as first:
        discovery.collect_posts(result["run_dir"])
    assert first.value.code == "handoff_not_created"
    with pytest.raises(discovery.PostsDiscoveryError) as second:
        discovery.collect_posts(result["run_dir"])
    assert second.value.code == "launch_interrupted"
    assert len(calls) == 1


@pytest.mark.parametrize("published", [None, "", "not-a-date", "2026-08-30T06:59:59Z", "2026-08-31T07:00:00Z", "2026-09-01T00:00:00Z"])
def test_counted_unknown_old_or_end_exclusive_dates_fail_before_parent_storage(workspace, monkeypatch, published):
    result = make_plan()
    fake = FakeOperator(result, dates=[published, "2026-08-31T06:00:00Z"])
    monkeypatch.setattr(discovery, "_operator_call", fake)
    with pytest.raises(discovery.PostsDiscoveryError) as error:
        discovery.collect_posts(result["run_dir"])
    assert error.value.code == "source_recency_failed"
    assert not Path(result["paths"]["database"]).exists()
    assert not Path(result["paths"]["export"]).exists()
    assert saved_manifest(result)["state"] == "awaiting_operator"


def test_exact_start_boundary_is_accepted(workspace, monkeypatch):
    result = make_plan()
    fake = FakeOperator(result, dates=["2026-08-30T07:00:00Z", "2026-08-31T06:59:59Z"])
    monkeypatch.setattr(discovery, "_operator_call", fake)
    assert discovery.collect_posts(result["run_dir"])["accepted_posts"] == 2


@pytest.mark.parametrize("alter", [
    lambda rows: rows.pop(),
    lambda rows: rows.append(copy.deepcopy(rows[0])),
    lambda rows: rows[0].update(project="other_project"),
    lambda rows: rows[0]["evidence_packet"].update(caption="changed after evidence projection hash"),
])
def test_invalid_source_cardinality_or_hash_never_produces_complete_output(workspace, monkeypatch, alter):
    result = make_plan()
    fake = FakeOperator(result, mutate_rows=alter)
    monkeypatch.setattr(discovery, "_operator_call", fake)
    with pytest.raises(discovery.PostsDiscoveryError):
        discovery.collect_posts(result["run_dir"])
    assert not Path(result["paths"]["database"]).exists()


@pytest.mark.parametrize("alter", [
    lambda review: review.update(offline_validation=False),
    lambda review: review.update(source_target="unrelated topic"),
    lambda review: review["status"].update(evidence_ready=1),
    lambda review: review.update(ai_actions=["analysis"]),
    lambda review: review["artifacts"]["evidence_export"].update(sha256="0" * 64),
])
def test_exit_zero_does_not_bypass_guarded_review_binding(workspace, monkeypatch, alter):
    result = make_plan()
    fake = FakeOperator(result, mutate_review=alter)
    monkeypatch.setattr(discovery, "_operator_call", fake)
    with pytest.raises(discovery.PostsDiscoveryError):
        discovery.collect_posts(result["run_dir"])
    assert not Path(result["paths"]["database"]).exists()


@pytest.mark.parametrize("resign", [False, True])
def test_changed_manifest_scope_fails_even_with_new_outer_hash(workspace, resign):
    result = make_plan()
    manifest = saved_manifest(result)
    manifest["publication_window"]["start"] = "2026-08-01T00:00:00.000000Z"
    save_changed_manifest(result, manifest, resign=resign)
    with pytest.raises(discovery.PostsDiscoveryError) as error:
        discovery.status_run(result["run_dir"])
    assert error.value.code == "manifest_invalid"


def test_valid_but_different_guarded_child_topic_is_rejected(workspace, monkeypatch):
    result = make_plan()
    fake = FakeOperator(result)
    path, handoff = fake.populate()
    operator = discovery._operator_module()
    handoff["source_target"] = handoff["intent"]["source_target"] = "unrelated topic"
    handoff["intent_hash"] = operator.json_hash(handoff["intent"])
    operator.write_handoff(path, handoff)
    monkeypatch.setattr(discovery, "_operator_call", forbidden)
    with pytest.raises(discovery.PostsDiscoveryError) as error:
        discovery.collect_posts(result["run_dir"])
    assert error.value.code == "child_scope_mismatch"


def test_valid_but_widened_child_window_is_rejected(workspace, monkeypatch):
    result = make_plan()
    fake = FakeOperator(result)
    path, handoff = fake.populate()
    operator = discovery._operator_module()
    window = discovery.normalize_publication_window("2026-08-29T07:00:00Z", "2026-08-31T07:00:00Z")
    handoff["publication_window"] = handoff["intent"]["publication_window"] = window
    handoff["intent_hash"] = operator.json_hash(handoff["intent"])
    operator.write_handoff(path, handoff)
    monkeypatch.setattr(discovery, "_operator_call", forbidden)
    with pytest.raises(discovery.PostsDiscoveryError) as error:
        discovery.collect_posts(result["run_dir"])
    assert error.value.code == "child_scope_mismatch"


def test_tampered_resume_action_is_not_returned_or_executed(workspace, monkeypatch):
    result = make_plan()
    fake = FakeOperator(result, state="RESUME_READY", mutate_poll=lambda payload: payload["safe_same_handoff_action"]["argv"].append("--after-restart"))
    monkeypatch.setattr(discovery, "_operator_call", fake)
    with pytest.raises(discovery.PostsDiscoveryError) as error:
        discovery.collect_posts(result["run_dir"])
    assert error.value.code == "resume_action_invalid"
    assert [argv[0] for argv, _ in fake.calls] == ["start", "poll"]


@pytest.mark.parametrize("artifact", ["export", "database"])
def test_local_artifact_tampering_fails_offline_validation(workspace, monkeypatch, artifact):
    result = make_plan()
    fake = FakeOperator(result)
    monkeypatch.setattr(discovery, "_operator_call", fake)
    outcome = discovery.collect_posts(result["run_dir"])
    path = Path(outcome["paths"][artifact])
    path.write_bytes(path.read_bytes() + b"tampered")
    monkeypatch.setattr(discovery, "_operator_call", forbidden)
    with pytest.raises(discovery.PostsDiscoveryError) as error:
        discovery.validate_run(outcome["run_dir"])
    assert error.value.code == "artifact_integrity_failed"


def test_stale_report_fails_offline_validation(workspace, monkeypatch):
    result = make_plan()
    fake = FakeOperator(result)
    monkeypatch.setattr(discovery, "_operator_call", fake)
    outcome = discovery.collect_posts(result["run_dir"])
    Path(outcome["paths"]["review_markdown"]).write_text("claimed result without evidence", encoding="utf-8")
    monkeypatch.setattr(discovery, "_operator_call", forbidden)
    with pytest.raises(discovery.PostsDiscoveryError) as error:
        discovery.validate_run(outcome["run_dir"])
    assert error.value.code == "report_stale"


def test_completed_collect_repairs_derived_report_without_recollecting(workspace, monkeypatch):
    result = make_plan()
    fake = FakeOperator(result)
    monkeypatch.setattr(discovery, "_operator_call", fake)
    outcome = discovery.collect_posts(result["run_dir"])
    Path(outcome["paths"]["review_markdown"]).write_text("interrupted derived output", encoding="utf-8")
    database_before = Path(outcome["paths"]["database"]).read_bytes()
    monkeypatch.setattr(discovery, "_operator_call", forbidden)
    repaired = discovery.collect_posts(result["run_dir"])
    assert repaired["validated"] is True
    assert Path(repaired["paths"]["database"]).read_bytes() == database_before


def test_cli_incomplete_collect_returns_nonzero_without_auto_resume(workspace, monkeypatch, capsys):
    result = make_plan()
    fake = FakeOperator(result, state="INTERRUPTED")
    monkeypatch.setattr(discovery, "_operator_call", fake)
    assert discovery.main(["collect", "--run-dir", result["run_dir"]]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "collection_incomplete"
    assert [argv[0] for argv, _ in fake.calls] == ["start", "poll"]


def test_legacy_directory_cannot_be_silently_reinterpreted(workspace):
    legacy = workspace / "music_discovery_id_legacy_20260831T070000Z_deadbeef"
    legacy.mkdir()
    with pytest.raises(discovery.PostsDiscoveryError) as error:
        discovery.status_run(legacy)
    assert error.value.code == "not_posts_discovery"


def test_cli_requires_window_and_count_without_creating_state(workspace):
    for argv in (["plan", "--topic", "coffee", "--posts", "2"], ["plan", "--topic", "coffee", "--last-hours", "24"]):
        with pytest.raises(SystemExit) as error:
            discovery.main(argv)
        assert error.value.code == 2
    assert not (workspace / "comments_data").exists()
