"""Offline tests for the durable multi-topic MUSIC AUDIT coordinator."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

import music_audit_topics as coordinator


NOW = datetime(2026, 9, 2, 3, 4, 5, tzinfo=timezone.utc)


def forbidden(*args, **kwargs):
    raise AssertionError("This unit test must not launch a browser, process, or network request")


class FakeOperatorModule:
    class OperatorError(RuntimeError):
        pass

    class OperatorPaths:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    @staticmethod
    def load_handoff(path, paths):
        del paths
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise FakeOperatorModule.OperatorError("invalid fixture handoff")
        return value


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "engage_tiktok.py").write_text("# isolated test placeholder\n", encoding="utf-8")
    monkeypatch.setattr(coordinator, "WORKSPACE", root)
    monkeypatch.setattr(
        coordinator, "DEFAULT_OUTPUT_ROOT", root / "comments_data/music_audit_topic_runs"
    )
    monkeypatch.setattr(coordinator, "_now", lambda: NOW)
    monkeypatch.setattr(coordinator, "_operator_module", lambda: FakeOperatorModule)
    monkeypatch.setattr(coordinator.subprocess, "Popen", forbidden)
    return root


def read_manifest(result):
    return json.loads((Path(result["run_dir"]) / "manifest.json").read_text(encoding="utf-8"))


def write_manifest(result, manifest, *, resign=True):
    if resign:
        manifest["sha256"] = coordinator._hash(
            {key: value for key, value in manifest.items() if key != "sha256"}
        )
    (Path(result["run_dir"]) / "manifest.json").write_text(
        coordinator._json(manifest) + "\n", encoding="utf-8"
    )


def make_plan(**changes):
    options = {
        "count_mode": "total",
        "topics": ["mr diy", "home improvement"],
        "posts": 5,
    }
    options.update(changes)
    return coordinator.plan_topics(**options)


class FakeGuardedOperator:
    """Mock all guarded commands while persisting exact fake handoff artifacts."""

    def __init__(self, result, *, initial_states=None, mutate_review=None):
        self.result = result
        self.initial_states = dict(initial_states or {})
        self.mutate_review = mutate_review
        self.states = {}
        self.calls = []
        self.resume_argvs = []

    def _manifest(self):
        return read_manifest(self.result)

    def _topic_for_project(self, project):
        for topic in self._manifest()["topics"]:
            if topic["child_project"] == project:
                return topic
        raise AssertionError("unknown child project")

    def _create_handoff(self, topic):
        manifest = self._manifest()
        root = coordinator.WORKSPACE / "comments_data" / ("project_" + topic["child_project"])
        root.mkdir(parents=True, exist_ok=False)
        export = root / "fixture_evidence.jsonl"
        export.write_text(f"fixture evidence for {topic['topic']}\n", encoding="utf-8")
        database = root / "fixture.sqlite"
        database.write_bytes(b"fixture database")
        review_file = root / "fixture_review.json"
        review_file.write_text("{}\n", encoding="utf-8")
        handoff_path = root / "fixture_handoff.json"
        handoff = {
            "project": topic["child_project"],
            "source_mode": "topic",
            "source_target": topic["topic"],
            "requested_count": topic["quota"],
            "collection_policy": "new_only",
            "topic_query_policy": "exact",
            "all_posts": False,
            "expected_account": manifest["expected_account"],
            "run_id": "engage_" + coordinator._hash(topic["child_project"])[:16],
            "intent_hash": coordinator._hash(
                {"project": topic["child_project"], "topic": topic["topic"]}
            ),
            "database": str(database.resolve()),
            "export_file": str(export.resolve()),
            "review_json": str(review_file.resolve()),
        }
        handoff_path.write_text(coordinator._json(handoff) + "\n", encoding="utf-8")
        self.states[topic["child_project"]] = self.initial_states.get(
            topic["ordinal"], "COMPLETE"
        )
        return handoff_path, handoff

    def _poll(self, handoff_path, handoff):
        topic = self._topic_for_project(handoff["project"])
        state = self.states[handoff["project"]]
        action = None
        if state in {"INTERRUPTED", "RESUME_READY"}:
            action = {
                "operation": "guarded_resume",
                "argv": coordinator._expected_resume_argv(handoff_path),
                "first_browser_touching_operation": True,
                "after_restart": False,
                "replacement_project_allowed": False,
                "requires_explicit_user_direction": state == "RESUME_READY",
            }
        return {
            "operator_status": state,
            "project": handoff["project"],
            "run_id": handoff["run_id"],
            "requested": topic["quota"],
            "evidence_ready": topic["quota"] if state in {
                "COMPLETE", "COLLECTION_COMPLETE_NEEDS_FINALIZE"
            } else 0,
            "topic_query_policy": "exact",
            "next_action": {
                "COMPLETE": "validate_or_stop",
                "COLLECTION_COMPLETE_NEEDS_FINALIZE": "offline_finalize",
                "RUNNING": "keep_waiting_do_not_restart",
                "BLOCKED": "preserve_same_run",
                "RESTART_PENDING": "human_review_required",
            }.get(state, "resume_same_handoff"),
            "safe_same_handoff_action": action,
            "replacement_project_allowed": False,
        }

    def _review(self, handoff):
        topic = self._topic_for_project(handoff["project"])
        post_ids = [
            f"{topic['ordinal']}{index:018d}"
            for index in range(1, topic["quota"] + 1)
        ]
        review = {
            "schema_version": "google-music-audit-review-v1",
            "task_outcome": "COMPLETE",
            "offline_validation": True,
            "project": handoff["project"],
            "run_id": handoff["run_id"],
            "source_mode": "topic",
            "source_target": topic["topic"],
            "workflow": "listen",
            "collection_policy": "new_only",
            "intent_hash": handoff["intent_hash"],
            "database": handoff["database"],
            "runtime": {"operator": self._manifest()["operator_identity"]},
            "status": {
                "status": "collection_complete",
                "requested": topic["quota"],
                "evidence_ready": topic["quota"],
                "topic_query_policy": "exact",
            },
            "artifacts": {
                "evidence_export": {
                    "path": handoff["export_file"],
                    "records": topic["quota"],
                    "schema_version": "tiktok-listen-evidence-export-v1",
                    "sha256": coordinator._file_hash(Path(handoff["export_file"])),
                }
            },
            "posts": [{"post_id": post_id} for post_id in post_ids],
            "ai_actions": [],
            "outbound_actions": [],
        }
        if self.mutate_review:
            self.mutate_review(review)
        return review

    def __call__(self, arguments, *, foreground=False):
        arguments = list(arguments)
        self.calls.append((arguments, foreground))
        command = arguments[0]
        if command == "start":
            assert foreground is True
            project = arguments[arguments.index("--project") + 1]
            topic = self._topic_for_project(project)
            assert arguments == [
                "start", "--project", project, "--topic", topic["topic"],
                "--posts", str(topic["quota"]),
            ] + (
                ["--expected-account", self._manifest()["expected_account"]]
                if self._manifest()["expected_account"] else []
            )
            self._create_handoff(topic)
            return 0, None
        assert foreground is False
        handoff_path = Path(arguments[2])
        handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
        if command == "poll":
            assert arguments == ["poll", "--handoff", str(handoff_path.resolve())]
            return 0, self._poll(handoff_path, handoff)
        if command == "finalize":
            assert arguments == ["finalize", "--handoff", str(handoff_path.resolve())]
            self.states[handoff["project"]] = "COMPLETE"
            return 0, {"task_outcome": "COMPLETE"}
        assert command == "validate"
        assert arguments == ["validate", "--handoff", str(handoff_path.resolve())]
        return 0, self._review(handoff)

    def resume_popen(self, argv, *, cwd, shell):
        assert shell is False and Path(cwd) == coordinator.WORKSPACE
        handoff_path = Path(argv[-1])
        assert list(argv) == coordinator._expected_resume_argv(handoff_path)
        handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
        self.resume_argvs.append(list(argv))
        owner = self

        class Process:
            @staticmethod
            def wait():
                owner.states[handoff["project"]] = "COLLECTION_COMPLETE_NEEDS_FINALIZE"
                return 0

        return Process()


def test_total_each_and_custom_plans_freeze_ordered_positive_quotas(workspace):
    total = coordinator.plan_topics(
        count_mode="total",
        topics=["  mr   diy ", "Home Tools", "paint ideas"],
        posts=8,
        expected_account="@KitaScore",
    )
    total_manifest = read_manifest(total)
    assert [item["topic"] for item in total_manifest["topics"]] == [
        "mr diy", "Home Tools", "paint ideas"
    ]
    assert [item["quota"] for item in total_manifest["topics"]] == [3, 3, 2]
    assert total_manifest["requested_total"] == 8
    assert total_manifest["expected_account"] == "kitascore"
    assert total_manifest["collection_policy"] == "new_only"
    assert total_manifest["topic_query_policy"] == "exact"
    assert total_manifest["allocation_rule"] == "floor_remainder_input_order"
    assert total_manifest["operator_identity"] == coordinator._operator_identity()
    assert len({item["child_project"] for item in total_manifest["topics"]}) == 3
    assert total_manifest["spec_sha256"] == coordinator._hash(
        coordinator._spec(total_manifest)
    )

    each = coordinator.plan_topics(
        count_mode="each", topics=["mr diy", "ace hardware"], posts=4
    )
    assert [item["quota"] for item in read_manifest(each)["topics"]] == [4, 4]
    assert each["requested_total"] == 8

    custom = coordinator.plan_topics(topic_quotas=["mr diy=7", "ace hardware = 2"])
    custom_manifest = read_manifest(custom)
    assert custom_manifest["requested_posts"] is None
    assert [(item["topic"], item["quota"]) for item in custom_manifest["topics"]] == [
        ("mr diy", 7), ("ace hardware", 2)
    ]
    assert custom["requested_total"] == 9


@pytest.mark.parametrize(
    "options, reason",
    [
        ({"count_mode": "total", "topics": ["a", "b", "c"], "posts": 2}, "TOTAL"),
        ({"count_mode": "each", "topics": ["MR DIY", " mr   diy "], "posts": 1}, "unique"),
        ({"count_mode": "each", "topics": ["one"], "posts": 1}, "at least two"),
        ({"topic_quotas": ["a=1", "b=0"]}, "positive"),
        ({"topics": ["a", "b"], "posts": 2,
          "topic_quotas": ["a=1", "b=1"]}, "CUSTOM"),
        ({"count_mode": "total", "topics": ["a", "b"], "posts": 2,
          "topic_quotas": ["a=1", "b=1"]}, "TOTAL/EACH"),
    ],
)
def test_invalid_or_ambiguous_plans_create_nothing(workspace, options, reason):
    with pytest.raises(coordinator.MusicAuditTopicsError, match=reason):
        coordinator.plan_topics(**options)
    assert not coordinator.DEFAULT_OUTPUT_ROOT.exists()


def test_manifest_topic_tampering_is_detected_before_operator_access(workspace, monkeypatch):
    result = make_plan()
    manifest = read_manifest(result)
    manifest["topics"][0]["topic"] = "changed topic"
    write_manifest(result, manifest)
    monkeypatch.setattr(coordinator, "_operator_call", forbidden)
    with pytest.raises(coordinator.MusicAuditTopicsError) as error:
        coordinator.status_topics(result["run_dir"])
    assert error.value.code == "manifest_invalid"


def test_collect_runs_exact_children_sequentially_and_parent_validates(workspace, monkeypatch):
    result = coordinator.plan_topics(
        count_mode="total",
        topics=["mr diy", "ace hardware", "home tools"],
        posts=8,
        expected_account="@KitaScore",
    )
    fake = FakeGuardedOperator(result)
    monkeypatch.setattr(coordinator, "_operator_call", fake)
    outcome = coordinator.collect_topics(result["run_dir"])
    assert outcome["status"] == "collection_complete"
    assert outcome["evidence_ready"] == outcome["requested_total"] == 8
    assert outcome["topic_query_policy"] == "exact"
    assert outcome["quota_redistribution"] is False
    assert outcome["ai_actions"] == outcome["outbound_actions"] == []
    assert Path(outcome["parent_review"]["path"]).is_file()
    assert len(outcome["validation_sha256"]) == 64
    assert read_manifest(result)["parent_validation_sha256"] == (
        outcome["validation_sha256"]
    )
    assert all(topic["database"] for topic in outcome["topics"])
    assert all(topic["evidence_export_sha256"] for topic in outcome["topics"])
    assert [call[0][0] for call in fake.calls] == [
        "start", "poll", "validate",
        "start", "poll", "validate",
        "start", "poll", "validate",
        "validate", "validate", "validate",
    ]
    starts = [call[0] for call in fake.calls if call[0][0] == "start"]
    assert [start[start.index("--topic") + 1] for start in starts] == [
        "mr diy", "ace hardware", "home tools"
    ]
    assert [start[start.index("--posts") + 1] for start in starts] == ["3", "3", "2"]
    assert all("related" not in " ".join(start) for start in starts)
    validation = coordinator.validate_topics(result["run_dir"])
    assert validation["validated"] is True
    assert validation["validation_result"] == "pass"
    assert validation["validation_sha256"] == outcome["validation_sha256"]
    assert Path(validation["parent_validation"]["path"]).is_file()
    assert [child["state"] for child in read_manifest(result)["children"]] == [
        "complete", "complete", "complete"
    ]


def test_collect_never_resumes_or_replaces_an_existing_child(workspace, monkeypatch):
    result = make_plan()
    fake = FakeGuardedOperator(result, initial_states={1: "RESUME_READY"})
    monkeypatch.setattr(coordinator, "_operator_call", fake)
    first = coordinator.collect_topics(result["run_dir"])
    assert first["operator_status"] == "RESUME_READY"
    assert first["coordinator_state"] == "awaiting_continue"
    assert [call[0][0] for call in fake.calls] == ["start", "poll"]
    second = coordinator.collect_topics(result["run_dir"])
    assert second["operator_status"] == "RESUME_READY"
    assert [call[0][0] for call in fake.calls] == ["start", "poll", "poll"]
    manifest = read_manifest(result)
    assert manifest["children"][0]["state"] == "bound"
    assert manifest["children"][1]["state"] == "planned"
    assert fake.resume_argvs == []


def test_continue_uses_exact_resume_then_offline_finalize_and_next_child(workspace, monkeypatch):
    result = make_plan()
    fake = FakeGuardedOperator(result, initial_states={1: "RESUME_READY"})
    monkeypatch.setattr(coordinator, "_operator_call", fake)
    coordinator.collect_topics(result["run_dir"])
    monkeypatch.setattr(coordinator.subprocess, "Popen", fake.resume_popen)
    outcome = coordinator.continue_topics(result["run_dir"])
    assert outcome["status"] == "collection_complete"
    assert len(fake.resume_argvs) == 1
    first_handoff = Path(read_manifest(result)["children"][0]["handoff"])
    assert fake.resume_argvs[0] == coordinator._expected_resume_argv(first_handoff)
    commands = [call[0][0] for call in fake.calls]
    assert commands.count("start") == 2
    assert commands.count("finalize") == 1
    assert commands[-5:] == ["start", "poll", "validate", "validate", "validate"]


def test_status_poll_is_read_only_and_blocked_child_is_preserved(workspace, monkeypatch):
    result = make_plan()
    fake = FakeGuardedOperator(result, initial_states={1: "BLOCKED"})
    monkeypatch.setattr(coordinator, "_operator_call", fake)
    coordinator.collect_topics(result["run_dir"])
    manifest_path = Path(result["run_dir"]) / "manifest.json"
    before = manifest_path.read_bytes()
    status = coordinator.status_topics(result["run_dir"])
    assert status["operator_status"] == "BLOCKED"
    assert status["next_action"] == "preserve_same_run"
    assert manifest_path.read_bytes() == before
    assert [call[0][0] for call in fake.calls].count("start") == 1


def test_child_validation_requires_exact_query_policy(workspace, monkeypatch):
    result = make_plan()

    def mutate(review):
        review["status"]["topic_query_policy"] = "related_variants_v1"

    fake = FakeGuardedOperator(result, mutate_review=mutate)
    monkeypatch.setattr(coordinator, "_operator_call", fake)
    with pytest.raises(coordinator.MusicAuditTopicsError) as error:
        coordinator.collect_topics(result["run_dir"])
    assert error.value.code == "child_not_validated"
    manifest = read_manifest(result)
    assert manifest["state"] != "complete"
    assert manifest["children"][1]["state"] == "planned"


def test_cross_topic_duplicate_post_id_blocks_parent_completion(workspace, monkeypatch):
    result = make_plan()

    def overlap(review):
        review["posts"][0]["post_id"] = "9999999999999999999"

    fake = FakeGuardedOperator(result, mutate_review=overlap)
    monkeypatch.setattr(coordinator, "_operator_call", fake)
    with pytest.raises(coordinator.MusicAuditTopicsError) as error:
        coordinator.collect_topics(result["run_dir"])
    assert error.value.code == "cross_topic_overlap"
    manifest = read_manifest(result)
    assert manifest["children"][0]["state"] == "complete"
    assert manifest["children"][1]["state"] != "complete"


def test_copied_parent_directory_is_rejected(workspace):
    result = make_plan()
    source = Path(result["run_dir"])
    copied_root = workspace / "copied"
    copied_root.mkdir()
    copied = copied_root / source.name
    copied.mkdir()
    (copied / "manifest.json").write_bytes((source / "manifest.json").read_bytes())

    with pytest.raises(coordinator.MusicAuditTopicsError) as error:
        coordinator.status_topics(copied)
    assert error.value.code == "not_multi_topic_run"


def test_cli_custom_shape_and_status_are_available_offline(workspace, capsys, monkeypatch):
    code = coordinator.main(
        [
            "plan", "--topic-quota", "mr diy=2",
            "--topic-quota", "ace hardware=3",
        ]
    )
    assert code == 0
    planned = json.loads(capsys.readouterr().out)
    monkeypatch.setattr(coordinator, "_operator_call", forbidden)
    code = coordinator.main(["status", "--run-dir", planned["run_dir"]])
    assert code == 0
    status = json.loads(capsys.readouterr().out)
    assert status["requested_total"] == 5
    assert status["operator_status"] == "not_polled"
