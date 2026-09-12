import copy
import json
from pathlib import Path
import socket
import sqlite3

import pytest

import music_discovery as discovery
import music_discovery_search as search


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "w"
    root.mkdir()
    monkeypatch.setattr(search, "WORKSPACE", root)
    return root


def plan(workspace, *, queries=("penyanyi baru Indonesia",), posts=2, artists="ALL"):
    return search.plan_search(queries=queries, posts_per_query=posts, artists=artists,
                             focus="Indonesian artists", label="test", output_root=workspace / "r")


def manifest(result):
    return json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))


def make_handoff(job, *, state="complete"):
    operator = search._operator_module()
    paths = operator.OperatorPaths(workspace=search.WORKSPACE.resolve(), python=search.REQUIRED_PYTHON.resolve(),
        engage_script=(search.WORKSPACE / "engage_tiktok.py").resolve(),
        master_database=(search.WORKSPACE / "comments_data/tiktok_master/state/tiktok_master.sqlite").resolve())
    args = operator.build_parser().parse_args(["start", "--project", job["project"], "--topic", job["query"], "--posts", str(job["posts"])])
    mode, target, count, all_posts = operator.validate_start_scope(args)
    operator.freeze_start_options(args, source_mode=mode, source_target=target, requested_count=count)
    naming = operator.explicit_project_naming(job["project"], timestamp="20260831_010000_000001")
    path, handoff = operator.new_handoff(project=job["project"], source_mode=mode, source_target=target,
        requested_count=count, all_posts=all_posts, args=args, paths=paths, naming=naming)
    database = Path(handoff["database"])
    database.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(database).close()
    handoff["run_id"] = "engage_" + search._hash(job["query_job_id"])[:16]
    handoff["frozen_selection"] = {}
    handoff["frozen_selection_hash"] = operator.json_hash({})
    handoff["state"] = state
    operator.write_handoff(path, handoff)
    return path, handoff


def poll_result(job, path, handoff, *, state="COMPLETE"):
    result = {"operator_status": state, "project": job["project"], "requested": job["posts"],
              "run_id": handoff["run_id"], "next_action": "validate_or_stop", "safe_same_handoff_action": None,
              "operator_process_alive": state == "RUNNING", "collector_process_alive": state == "RUNNING",
              "evidence_ready": job["posts"] if state == "COMPLETE" else 0,
              "replacement_project_allowed": False}
    if state in {"RESUME_READY", "INTERRUPTED"}:
        result["next_action"] = "run_guarded_resume_same_handoff_without_preflight_or_after_restart"
        result["safe_same_handoff_action"] = {
            "operation": "guarded_resume",
            "argv": [str(search.REQUIRED_PYTHON.resolve()), str(search.OPERATOR.resolve()), "resume", "--handoff", str(path.resolve())],
            "requires_explicit_user_direction": state == "RESUME_READY",
        }
    if state == "COLLECTION_COMPLETE_NEEDS_FINALIZE":
        result["next_action"] = "offline_finalize"
    return result


def review_result(job, handoff):
    return {"schema_version": "google-music-audit-review-v1", "task_outcome": "COMPLETE", "offline_validation": True,
            "workflow": "listen", "source_mode": "topic", "source_target": job["query"], "project": job["project"],
            "run_id": handoff["run_id"], "collection_policy": "new_only", "database": handoff["database"],
            "intent_hash": handoff["intent_hash"], "intent": handoff["intent"],
            "status": {"status": "collection_complete", "requested": job["posts"], "evidence_ready": job["posts"]},
            "artifacts": {"evidence_export": {"path": handoff["export_file"], "schema_version": "tiktok-listen-evidence-export-v1", "records": job["posts"], "sha256": "a" * 64}},
            "ai_actions": [], "outbound_actions": []}


class FakeOperator:
    def __init__(self, result, *, state="COMPLETE", validate_change=None, start_code=0):
        self.result = result
        self.state = state
        self.validate_change = validate_change
        self.start_code = start_code
        self.calls = []
        self.handoffs = {}

    def __call__(self, args, *, foreground=False):
        self.calls.append((list(args), foreground))
        document = manifest(self.result)
        if args[0] == "start":
            job = next(job for job in document["jobs"] if job["project"] == args[2])
            assert job["stage"] == "launch_requested"
            assert job["launch_requested_at"] is not None
            assert args == ["start", "--project", job["project"], "--topic", job["query"], "--posts", str(job["posts"])]
            assert foreground is True
            path, handoff = make_handoff(job)
            self.handoffs[str(path.resolve())] = (job, handoff)
            return self.start_code, None
        assert foreground is False
        path = Path(args[2])
        if str(path.resolve()) in self.handoffs:
            job, handoff = self.handoffs[str(path.resolve())]
        else:
            handoff = json.loads(path.read_text(encoding="utf-8"))
            job = next(job for job in document["jobs"] if job["project"] == handoff["project"])
        if args[0] == "poll":
            return 0, poll_result(job, path, handoff, state=self.state)
        assert args[0] == "validate"
        result = review_result(job, handoff)
        if self.validate_change:
            self.validate_change(result)
        return 0, result


def no_external(*args, **kwargs):
    raise AssertionError("no browser/network/process permitted in this test")


def test_plan_and_status_are_offline_and_preserve_brief_v1(workspace, monkeypatch):
    monkeypatch.setattr(socket, "socket", no_external)
    monkeypatch.setattr(search.subprocess, "Popen", no_external)
    result = plan(workspace, queries=("penyanyi baru Indonesia", "band indie Bandung"), posts=70)
    assert result["planned_batches"] == result["pending_batches"] == 2
    assert result["planned_posts"] == 140
    assert result["requested_artists"] == "all"
    assert not result["all_artists_found"] and not result["query_exhaustion_verified"]
    assert not (workspace / "comments_data").exists()
    assert all(job["project"].startswith("music_audit_discovery_") for job in result["queries"])
    brief = json.loads((Path(result["run_dir"]) / "brief.json").read_text(encoding="utf-8"))
    assert brief["input_mode"] == "live_search" and brief["live_collectors_enabled"] is True
    assert search.live_search_status(result["run_dir"])["live_process_status"] == "not_checked"
    old = discovery.init_run(artists=1, output_root=workspace / "r")
    old_brief = Path(old["run_dir"]) / "brief.json"
    before = old_brief.read_bytes()
    assert search.live_search_status(old["run_dir"]) is None
    assert old_brief.read_bytes() == before
    assert json.loads(before)["schema"] == "music-discovery-brief-v1"


@pytest.mark.parametrize("posts", [0, -1, True, "ALL", "2.5"])
def test_explicit_positive_batch_size_required_before_run_creation(workspace, posts):
    with pytest.raises(ValueError, match="positive"):
        plan(workspace, posts=posts)
    assert not (workspace / "r").exists()


@pytest.mark.parametrize("queries", [(), ("",), ("same", "SAME"), ("--creator x",), ("hello\nworld",)])
def test_invalid_queries_create_nothing(workspace, queries):
    with pytest.raises(ValueError):
        plan(workspace, queries=queries)
    assert not (workspace / "r").exists()


def test_collect_starts_exactly_one_job_then_validates_and_imports(workspace, monkeypatch):
    result = plan(workspace, queries=("penyanyi baru", "band indie"))
    fake = FakeOperator(result)
    imported = []
    monkeypatch.setattr(search, "_run_operator", fake)
    monkeypatch.setattr(search, "_append_source", lambda *args: imported.append(args))
    outcome = search.collect_next(result["run_dir"])
    assert [args[0] for args, _ in fake.calls] == ["start", "poll", "validate"]
    assert len(imported) == 1
    assert outcome["started_new_child"] and outcome["imported_child"]
    assert outcome["imported_batches"] == 1 and outcome["pending_batches"] == 1
    assert manifest(result)["jobs"][1]["stage"] == "planned"
    first_binding = copy.deepcopy(manifest(result)["jobs"][0])
    second = search.collect_next(result["run_dir"])
    assert second["all_imported"] and len(imported) == 2
    assert manifest(result)["jobs"][0] == first_binding
    calls = len(fake.calls)
    again = search.collect_next(result["run_dir"])
    assert again["all_imported"] and len(fake.calls) == calls


@pytest.mark.parametrize("state", ["RUNNING", "INTERRUPTED", "RESUME_READY", "BLOCKED", "RESTART_PENDING", "COLLECTION_COMPLETE_NEEDS_FINALIZE"])
def test_existing_handoff_is_polled_never_replaced_or_resumed(workspace, monkeypatch, state):
    result = plan(workspace)
    job = manifest(result)["jobs"][0]
    path, handoff = make_handoff(job, state="collecting")
    original = path.read_bytes()
    fake = FakeOperator(result, state=state)
    monkeypatch.setattr(search, "_run_operator", fake)
    monkeypatch.setattr(search, "_append_source", no_external)
    outcome = search.collect_next(result["run_dir"])
    assert [args[0] for args, _ in fake.calls] == ["poll"]
    assert outcome["operator_status"] == state
    assert outcome["operator"] == poll_result(job, path, handoff, state=state)
    assert outcome["safe_same_handoff_action"] == outcome["operator"]["safe_same_handoff_action"]
    assert path.read_bytes() == original
    assert not outcome["started_new_child"]
    assert search.live_search_status(result["run_dir"])["live_process_status"] == "not_checked"


def test_existing_complete_handoff_can_be_imported_without_start(workspace, monkeypatch):
    result = plan(workspace)
    make_handoff(manifest(result)["jobs"][0])
    fake = FakeOperator(result)
    monkeypatch.setattr(search, "_run_operator", fake)
    monkeypatch.setattr(search, "_append_source", lambda *args: None)
    outcome = search.collect_next(result["run_dir"])
    assert [args[0] for args, _ in fake.calls] == ["poll", "validate"]
    assert outcome["all_imported"]


def test_missing_handoff_after_launch_never_relaunches(workspace, monkeypatch):
    result = plan(workspace)
    calls = []
    monkeypatch.setattr(search, "_run_operator", lambda *args, **kwargs: calls.append(args) or (1, None))
    with pytest.raises(ValueError, match="without a handoff"):
        search.collect_next(result["run_dir"])
    assert manifest(result)["jobs"][0]["stage"] == "launch_requested"
    with pytest.raises(ValueError, match="already requested"):
        search.collect_next(result["run_dir"])
    assert len(calls) == 1


def test_interruption_after_handoff_preserves_it_for_next_poll(workspace, monkeypatch):
    result = plan(workspace)
    def interrupted(args, *, foreground=False):
        make_handoff(manifest(result)["jobs"][0], state="collecting")
        raise KeyboardInterrupt()
    monkeypatch.setattr(search, "_run_operator", interrupted)
    with pytest.raises(KeyboardInterrupt):
        search.collect_next(result["run_dir"])
    fake = FakeOperator(result, state="INTERRUPTED")
    monkeypatch.setattr(search, "_run_operator", fake)
    outcome = search.collect_next(result["run_dir"])
    assert [args[0] for args, _ in fake.calls] == ["poll"]
    assert outcome["operator_status"] == "INTERRUPTED"


@pytest.mark.parametrize("alter", [
    lambda review: review["status"].update(evidence_ready=1),
    lambda review: review.update(offline_validation=False),
    lambda review: review.update(source_target="different query"),
    lambda review: review.update(run_id="engage_" + "0" * 16),
    lambda review: review.update(ai_actions=["analysis"]),
    lambda review: review["artifacts"]["evidence_export"].update(records=1),
])
def test_exit_zero_is_not_a_validation_or_import_gate(workspace, monkeypatch, alter):
    result = plan(workspace)
    fake = FakeOperator(result, validate_change=alter)
    monkeypatch.setattr(search, "_run_operator", fake)
    monkeypatch.setattr(search, "_append_source", no_external)
    with pytest.raises(ValueError, match="exact planned batch"):
        search.collect_next(result["run_dir"])
    assert manifest(result)["jobs"][0]["stage"] == "awaiting_operator"


def test_nonzero_start_exit_can_only_import_after_real_poll_and_validate(workspace, monkeypatch):
    result = plan(workspace)
    fake = FakeOperator(result, start_code=1)
    monkeypatch.setattr(search, "_run_operator", fake)
    monkeypatch.setattr(search, "_append_source", lambda *args: None)
    assert search.collect_next(result["run_dir"])["all_imported"]
    assert [args[0] for args, _ in fake.calls] == ["start", "poll", "validate"]


def test_handoff_hash_and_rehashed_wrong_scope_fail_before_poll(workspace, monkeypatch):
    result = plan(workspace)
    path, handoff = make_handoff(manifest(result)["jobs"][0])
    monkeypatch.setattr(search, "_run_operator", no_external)
    handoff["source_target"] = "different"
    path.write_text(json.dumps(handoff), encoding="utf-8")
    with pytest.raises(ValueError, match="hash/path/intent"):
        search.collect_next(result["run_dir"])
    handoff["intent"]["source_target"] = "different"
    handoff["intent_hash"] = search._hash(handoff["intent"])
    search._operator_module().write_handoff(path, handoff)
    with pytest.raises(ValueError, match="planned query"):
        search.collect_next(result["run_dir"])


def test_manifest_cannot_accept_arbitrary_argv_even_if_rehashed(workspace, monkeypatch):
    result = plan(workspace)
    document = manifest(result)
    document["jobs"][0]["argv"] = ["dangerous", "--anything"]
    search._save_manifest(Path(result["run_dir"]), document)
    monkeypatch.setattr(search, "_run_operator", no_external)
    with pytest.raises(ValueError, match="unexpected live-search job fields"):
        search.collect_next(result["run_dir"])


def test_import_failure_retries_same_completed_child_without_recollection(workspace, monkeypatch):
    result = plan(workspace)
    fake = FakeOperator(result)
    monkeypatch.setattr(search, "_run_operator", fake)
    calls = []
    def append(*args):
        calls.append(args)
        if len(calls) == 1:
            raise ValueError("simulated transaction rollback")
    monkeypatch.setattr(search, "_append_source", append)
    with pytest.raises(ValueError, match="rollback"):
        search.collect_next(result["run_dir"])
    outcome = search.collect_next(result["run_dir"])
    assert outcome["all_imported"] and len(calls) == 2
    assert [args[0] for args, _ in fake.calls].count("start") == 1
    assert calls[0][1]["query_job_id"] == calls[1][1]["query_job_id"]


def test_append_api_binds_exact_source_run_query_and_origin(workspace, monkeypatch):
    result = plan(workspace)
    job = manifest(result)["jobs"][0]
    path, handoff = make_handoff(job)
    import music_discovery_corpus as corpus
    calls = []
    monkeypatch.setattr(corpus, "append_source_batch", lambda *args, **kwargs: calls.append((args, kwargs)))
    search._append_source(Path(result["run_dir"]), job, handoff, path)
    args, kwargs = calls[0]
    assert args == (Path(result["run_dir"]), [Path(handoff["database"])])
    assert kwargs == {"run_ids": [handoff["run_id"]], "origin": {
        "kind": "live_search", "query": job["query"], "project": job["project"],
        "run_id": handoff["run_id"], "handoff": str(path.resolve()), "query_job_id": job["query_job_id"]}}


def test_add_query_extends_same_manifest_without_replacing_unfinished_scope(workspace):
    result = plan(workspace)
    original = copy.deepcopy(manifest(result)["jobs"][0])
    with pytest.raises(ValueError, match="unfinished batch"):
        search.add_query(result["run_dir"], query=original["query"], posts=3)
    extended = search.add_query(result["run_dir"], query="grup vokal Indonesia", posts=80)
    assert extended["planned_batches"] == 2
    assert manifest(result)["jobs"][0] == original
    assert extended["queries"][1]["project"].endswith("_q002")


def test_explicit_add_query_after_success_is_new_delta_not_total_cap(workspace, monkeypatch):
    result = plan(workspace)
    monkeypatch.setattr(search, "_run_operator", FakeOperator(result))
    monkeypatch.setattr(search, "_append_source", lambda *args: None)
    search.collect_next(result["run_dir"])
    extended = search.add_query(result["run_dir"], query=result["queries"][0]["query"], posts=100)
    assert extended["imported_batches"] == 1 and extended["pending_batches"] == 1
    assert extended["queries"][0]["query_job_id"] != extended["queries"][1]["query_job_id"]


def test_foreground_subprocess_inherits_heartbeats_no_shell_timeout_or_kill(workspace, monkeypatch):
    calls = []
    class Child:
        def __init__(self, args, **kwargs):
            calls.append((args, kwargs))
        def wait(self):
            return 7
    monkeypatch.setattr(search.subprocess, "Popen", Child)
    code, payload = search._run_operator(["start", "--topic", "artist; harmless literal", "--posts", "1"], foreground=True)
    assert code == 7 and payload is None
    args, kwargs = calls[0]
    assert args[:2] == [str(search.REQUIRED_PYTHON.resolve()), str(search.OPERATOR.resolve())]
    assert kwargs == {"cwd": str(workspace), "shell": False}
    assert "--run-label" not in args


def test_foreground_keyboard_interrupt_does_not_kill_child(workspace, monkeypatch):
    class Child:
        def __init__(self, *args, **kwargs):
            pass
        def wait(self):
            raise KeyboardInterrupt()
        def kill(self):
            raise AssertionError("must not kill guarded child")
        def terminate(self):
            raise AssertionError("must not terminate guarded child")
    monkeypatch.setattr(search.subprocess, "Popen", Child)
    with pytest.raises(KeyboardInterrupt):
        search._run_operator(["start", "--topic", "music", "--posts", "1"], foreground=True)


def test_returned_resume_argv_is_validated_but_never_executed(workspace, monkeypatch):
    result = plan(workspace)
    job = manifest(result)["jobs"][0]
    path, handoff = make_handoff(job)
    outcome = poll_result(job, path, handoff, state="RESUME_READY")
    outcome["safe_same_handoff_action"]["argv"] = ["unsafe command"]
    monkeypatch.setattr(search, "_run_operator", lambda *args, **kwargs: (0, outcome))
    with pytest.raises(ValueError, match="unexpected same-handoff action"):
        search.collect_next(result["run_dir"])


def test_existing_project_without_handoff_is_never_replaced(workspace, monkeypatch):
    result = plan(workspace)
    search._project_root(result["queries"][0]["project"]).mkdir(parents=True)
    monkeypatch.setattr(search, "_run_operator", no_external)
    with pytest.raises(ValueError, match="identifiable handoff"):
        search.collect_next(result["run_dir"])


def test_coordinator_lock_prevents_duplicate_starts(workspace, monkeypatch):
    result = plan(workspace)
    monkeypatch.setattr(search, "_run_operator", no_external)
    with search._search_lock(Path(result["run_dir"])):
        with pytest.raises(ValueError, match="already active"):
            search.collect_next(result["run_dir"])


def test_cli_requires_batch_count_and_has_no_raw_argv_or_status_setter():
    parser = search.build_parser()
    for args in (["plan", "--query", "music", "--artists", "ALL"],
                 ["collect-next", "--run-dir", "x", "--argv", "anything"],
                 ["status", "--run-dir", "x", "--status", "COMPLETE"]):
        with pytest.raises(SystemExit):
            parser.parse_args(args)


def test_cli_status_does_not_touch_operator_or_source(workspace, monkeypatch, capsys):
    result = plan(workspace)
    before = Path(result["manifest"]).read_bytes()
    monkeypatch.setattr(search, "_run_operator", no_external)
    assert search.main(["status", "--run-dir", result["run_dir"]]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["pending_batches"] == 1
    assert Path(result["manifest"]).read_bytes() == before
