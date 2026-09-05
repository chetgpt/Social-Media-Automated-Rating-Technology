"""Offline publication-window guards; these tests never access a browser."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import engage_tiktok as engage
from test_music_audit_operator import ROOT, ReadyPreflight, operator, terminal_record


START = "2026-08-24T00:00:00Z"
END = "2026-08-31T00:00:00Z"


def start_args(*extra):
    args = operator.build_parser().parse_args(
        ["start", "--topic", "kopi Indonesia", "--posts", "2", *extra]
    )
    mode, target, count, _ = operator.validate_start_scope(args)
    operator.freeze_start_options(args, source_mode=mode, source_target=target, requested_count=count)
    return args


def make_handoff(tmp_path, *, window=True):
    paths = operator.OperatorPaths(
        workspace=tmp_path.resolve(),
        python=operator.REQUIRED_PYTHON.resolve(),
        engage_script=(ROOT / "engage_tiktok.py").resolve(),
        master_database=(tmp_path / "comments_data/tiktok_master/state/tiktok_master.sqlite").resolve(),
    )
    args = start_args(*(["--published-after", START, "--published-before", END] if window else []))
    path, handoff = operator.new_handoff(
        project="music_audit_window_test", source_mode="topic", source_target="kopi Indonesia",
        requested_count=2, all_posts=False, args=args, paths=paths,
    )
    return paths, path, handoff, args


def test_absent_window_retains_legacy_handoff_and_argument_shapes(tmp_path):
    _paths, _path, handoff, _args = make_handoff(tmp_path, window=False)
    assert "publication_window" not in handoff
    assert "publication_window" not in handoff["intent"]
    assert "--published-after" not in operator.build_start_arguments(handoff)
    assert "--published-before" not in operator.build_resume_arguments(handoff)
    assert operator.status_summary({"run_id": "engage_test"}) == {"run_id": "engage_test"}


def test_window_is_canonical_hash_bound_and_reused_in_exact_resume(tmp_path):
    paths, path, handoff, _args = make_handoff(tmp_path)
    window = handoff["publication_window"]
    assert window == handoff["intent"]["publication_window"]
    assert window["start"] == "2026-08-24T00:00:00.000000Z"
    assert handoff["intent_hash"] == operator.json_hash(handoff["intent"])
    operator.validate_handoff(handoff, path=path, paths=paths)
    for argv in (operator.build_start_arguments(handoff), operator.build_resume_arguments(handoff)):
        assert argv[argv.index("--published-after") + 1] == window["start"]
        assert argv[argv.index("--published-before") + 1] == window["end"]
        assert "social_browser.py" not in " ".join(argv)


def test_duplicate_start_matching_includes_the_frozen_publication_window(tmp_path):
    paths, _path, _handoff, args = make_handoff(tmp_path)
    kwargs = dict(source_mode="topic", source_target="kopi Indonesia", requested_count=2, all_posts=False, paths=paths)
    with pytest.raises(operator.OperatorError, match="unfinished matching"):
        operator.refuse_automatic_duplicate_start(args, **kwargs)
    different = start_args("--published-after", "2026-08-23T00:00:00Z", "--published-before", END)
    operator.refuse_automatic_duplicate_start(different, **kwargs)
    operator.refuse_automatic_duplicate_start(start_args(), **kwargs)


@pytest.mark.parametrize("argv", [
    ["--topic", "kopi", "--posts", "1", "--published-after", START],
    ["--topic", "kopi", "--posts", "1", "--published-before", END],
    ["--creator", "maker", "--posts", "1", "--published-after", START, "--published-before", END],
    ["--url", "https://www.tiktok.com/@maker/video/123", "--posts", "1", "--published-after", START, "--published-before", END],
    ["--topic", "kopi", "--posts", "1", "--collection-policy", "refresh_known", "--published-after", START, "--published-before", END],
    ["--topic", "kopi", "--posts", "1", "--published-after", END, "--published-before", START],
    ["--topic", "kopi", "--posts", "1", "--published-after", "2026-08-24T00:00:00", "--published-before", END],
])
def test_invalid_window_scope_fails_before_creating_a_project(argv):
    args = operator.build_parser().parse_args(["start", *argv])
    with pytest.raises(operator.OperatorError):
        operator.validate_start_scope(args)


@pytest.mark.parametrize("mutation", ["drop_top", "drop_intent", "empty", "change_top", "add_extra"])
def test_rehashed_handoff_window_mutations_fail(tmp_path, mutation):
    paths, path, handoff, _args = make_handoff(tmp_path)
    if mutation == "drop_top":
        handoff.pop("publication_window")
    elif mutation == "drop_intent":
        handoff["intent"].pop("publication_window")
    elif mutation == "empty":
        handoff["publication_window"] = {}
        handoff["intent"]["publication_window"] = {}
    elif mutation == "change_top":
        handoff["publication_window"] = {**handoff["publication_window"], "start": END}
    else:
        handoff["publication_window"] = {**handoff["publication_window"], "limit": 5}
        handoff["intent"]["publication_window"] = dict(handoff["publication_window"])
    handoff["intent_hash"] = operator.json_hash(handoff["intent"])
    operator.write_handoff(path, handoff)
    with pytest.raises(operator.OperatorError, match="publication window"):
        operator.validate_handoff(handoff, path=path, paths=paths)


class WindowCollector:
    last_diagnostics = {"collection_stop_reason": "requested_count_reached"}

    async def collect(
        self, *, record_callback, candidate_reserver, publication_window,
        publication_exclusion_callback, existing_post_ids=(), initial_evidence_ready_count=0,
        **_kwargs,
    ):
        assert publication_window["start"] == "2026-08-24T00:00:00.000000Z"
        accepted = []
        for post_id, published in (
            ("901", "2026-08-01T00:00:00Z"), ("902", ""),
            ("903", END), ("904", "2026-08-29T00:00:00Z"),
            ("905", "2026-08-30T00:00:00Z"),
        ):
            record = terminal_record(post_id)
            if published:
                record["published_at"] = published
            if candidate_reserver(post_id, record) and record_callback(record):
                accepted.append(record)
        return accepted


@pytest.fixture
def completed_window(tmp_path):
    paths, path, handoff, args = make_handoff(tmp_path)
    conn = engage.connect_database(Path(handoff["database"]), master_database=paths.master_database, sync_master=True)
    try:
        run_id = engage.create_run(
            conn, project=handoff["project"], topic="kopi Indonesia", requested_count=2,
            max_comments=20, max_pages=args.resolved_max_pages, mode="shadow", workflow="listen",
            collection_policy="new_only", source_mode="topic", master_database=str(paths.master_database),
            publication_window=handoff["publication_window"],
        )
        status = asyncio.run(engage.collect_exact(conn, run_id=run_id, preflight=ReadyPreflight(), collector=WindowCollector()))
        assert status["status"] == "collection_complete"
        assert status["evidence_ready"] == 2
        assert engage.export_listen_evidence(conn, run_id, Path(handoff["export_file"])) == 2
    finally:
        conn.close()
    handoff["run_id"] = run_id
    operator.capture_frozen_selection(handoff)
    handoff["state"] = "collection_complete"
    handoff["last_status"] = operator.status_summary(status)
    operator.write_handoff(path, handoff)
    operator.append_ledger(handoff, action="fixture-collection-complete", stage="collection", paths=paths, status=status, next_action="validate")
    return paths, path, handoff


def test_completed_window_validates_dates_order_and_report_counts(completed_window):
    paths, _path, handoff = completed_window
    review = operator.validate_completed_artifacts(handoff, paths=paths)
    assert [post["post_id"] for post in review["posts"]] == ["905", "904"]
    assert all(post["published_at"] for post in review["posts"])
    assert review["status"]["publication_window"] == handoff["publication_window"]
    assert review["status"]["publication_window_exclusions"] == {
        "published_before_window": 1, "publication_time_unknown": 1, "published_at_or_after_window_end": 1,
    }
    markdown = operator.review_markdown(review)
    assert "Inclusive start" in markdown and "Exclusive end" in markdown
    assert "not_all_matching_TikTok_posts" in markdown
    assert "Published at" in markdown


@pytest.mark.parametrize("value", ["{}", "[]", "invalid", '{"start":"2020-01-01T00:00:00Z","end":"2021-01-01T00:00:00Z","bounds":"[start,end)","order":"published_desc"}'])
def test_durable_window_cannot_be_dropped_or_changed(completed_window, value):
    paths, _path, handoff = completed_window
    conn = engage.connect_database(Path(handoff["database"]), sync_master=False)
    try:
        conn.execute("UPDATE engage_tiktok_runs SET publication_window_json=? WHERE run_id=?", (value, handoff["run_id"]))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(operator.OperatorError, match="publication window"):
        operator.validate_run_intent(handoff, paths=paths)


def test_dropping_both_rehashed_handoff_window_fields_cannot_discard_durable_fence(completed_window):
    paths, path, handoff = completed_window
    handoff.pop("publication_window")
    handoff["intent"].pop("publication_window")
    handoff["intent_hash"] = operator.json_hash(handoff["intent"])
    operator.write_handoff(path, handoff)
    with pytest.raises(operator.OperatorError, match="publication window"):
        operator.validate_run_intent(handoff, paths=paths)


def test_window_creation_receipt_rejects_coordinated_handoff_and_database_drop(completed_window):
    paths, path, handoff = completed_window
    handoff.pop("publication_window")
    handoff["intent"].pop("publication_window")
    handoff["intent_hash"] = operator.json_hash(handoff["intent"])
    operator.write_handoff(path, handoff)
    conn = engage.connect_database(Path(handoff["database"]), sync_master=False)
    try:
        conn.execute("UPDATE engage_tiktok_runs SET publication_window_json='{}' WHERE run_id=?", (handoff["run_id"],))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(operator.OperatorError, match="immutable run creation"):
        operator.validate_run_intent(handoff, paths=paths)


def test_local_raw_evidence_publication_date_is_independently_rechecked(completed_window):
    paths, _path, handoff = completed_window
    conn = engage.connect_database(Path(handoff["database"]), sync_master=False)
    try:
        packet = json.loads(conn.execute("SELECT evidence_json FROM engage_tiktok_posts WHERE run_id=? AND post_id='904'", (handoff["run_id"],)).fetchone()[0])
        packet["published_at"] = "2020-01-01T00:00:00Z"
        conn.execute("UPDATE engage_tiktok_posts SET evidence_json=?,evidence_hash=? WHERE run_id=? AND post_id='904'", (operator.canonical_json(packet), operator.json_hash(packet), handoff["run_id"]))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(operator.OperatorError, match="local post 904 is outside the publication window"):
        operator.validate_completed_artifacts(handoff, paths=paths)


@pytest.mark.parametrize("published", ["", START.replace("24", "23"), END])
def test_export_projection_must_have_eligible_actual_publication_time(completed_window, published):
    paths, _path, handoff = completed_window
    run = operator.validate_run_intent(handoff, paths=paths)
    row = operator._read_export(Path(handoff["export_file"]))[0]
    row["evidence_packet"]["published_at"] = published
    row["evidence_packet"].pop("create_time", None)
    row["evidence_packet"].pop("createTime", None)
    row["evidence_projection_hash"] = operator.json_hash(row["evidence_packet"])
    with pytest.raises(operator.OperatorError, match="outside the publication window"):
        operator._validate_export_row(row, run=run, engage_module=engage)


def test_validator_rejects_oldest_first_export(completed_window):
    paths, _path, handoff = completed_window
    target = Path(handoff["export_file"])
    rows = operator._read_export(target)
    target.write_text("".join(json.dumps(row) + "\n" for row in reversed(rows)), encoding="utf-8")
    with pytest.raises(operator.OperatorError, match="newest first"):
        operator.validate_completed_artifacts(handoff, paths=paths)


def test_poll_reports_frozen_window_and_exclusions_without_writes(completed_window, monkeypatch, capsys):
    paths, path, handoff = completed_window
    before = {str(p): p.read_bytes() for p in (path, Path(handoff["database"]), Path(handoff["ledger_file"]))}
    monkeypatch.setattr(operator, "execution_liveness", lambda *_args, **_kwargs: {"operator_alive": False, "collector_alive": False, "inspection_uncertain": False, "lock_held": False})
    operator.poll_command(SimpleNamespace(handoff=path), paths)
    result = json.loads(capsys.readouterr().out)
    assert result["publication_window"] == handoff["publication_window"]
    assert result["publication_window_exclusions"]["published_before_window"] == 1
    assert before == {p: Path(p).read_bytes() for p in before}


def test_poll_does_not_claim_complete_after_publication_window_drift(completed_window, monkeypatch, capsys):
    paths, path, handoff = completed_window
    handoff["state"] = "complete"
    operator.write_handoff(path, handoff)
    conn = engage.connect_database(Path(handoff["database"]), sync_master=False)
    try:
        conn.execute("UPDATE engage_tiktok_runs SET publication_window_json='{}' WHERE run_id=?", (handoff["run_id"],))
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(operator, "execution_liveness", lambda *_args, **_kwargs: {"operator_alive": False, "collector_alive": False, "inspection_uncertain": False, "lock_held": False})
    operator.poll_command(SimpleNamespace(handoff=path), paths)
    result = json.loads(capsys.readouterr().out)
    assert result["operator_status"] == "BLOCKED"
    assert result["next_action"] == "inspect_publication_window_binding"
    assert result["safe_same_handoff_action"] is None
