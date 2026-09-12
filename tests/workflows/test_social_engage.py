"""Synthetic-only regression tests for the new platform boundary and live gates."""
import copy
import json
import sqlite3
from types import SimpleNamespace

import pytest

import engage_social as cli
from social_engage.adapters import AdapterError, ThreadsAdapter, LinkedInAdapter, unsupported_blocks
from social_engage.state import State, StateError, canonical, digest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def fail(*a, **kw):
        raise AssertionError("network not allowed in workflow tests")
    monkeypatch.setattr("requests.sessions.Session.request", fail)


def scope(platform="threads", mode="live", workflow="engage", count=1):
    return {"platform": platform, "mode": mode, "workflow": workflow, "source": "own" if platform == "threads" else "organization",
            "target": "operator" if platform == "threads" else "urn:li:organization:123", "account": "operator" if platform == "threads" else "urn:li:organization:123",
            "posts": count, "comments": 20, "candidate_limit": 100, "since": None, "until": None, "api_version": "202608"}


def evidence(platform="threads", post_id="123"):
    return {"platform": platform, "id": post_id, "author": "creator", "url": "https://www.threads.com/@creator/post/ABC",
            "text": "The pot dried for two days before firing.", "comments": [{"id": "456", "author": "audience", "text": "How dry was the base?"}],
            "comments_complete": True, "comments_status": "available", "metrics": {"likes": 8}, "metrics_status": "available",
            "media_type": "TEXT_POST", "published_at": "2026-09-11T10:00:00+00:00", "observed_at": "2026-09-12T10:00:00+00:00", **unsupported_blocks()}


class FakeAdapter:
    platform = "threads"
    def __init__(self, clock=None):
        self.calls = []
        self.clock = clock
        self.ids = ["123"]
        self.fails = False

    def identity(self, expected):
        self.calls.append("identity")
        return {"id": "88", "username": expected}

    def discover(self, scope):
        self.calls.append("discover")
        return self.ids

    def collect(self, post_id, scope, actor):
        self.calls.append("collect:" + post_id)
        if self.fails:
            raise AdapterError("permission_denied")
        return evidence(scope["platform"], post_id)

    def preflight(self, e, s, a):
        self.calls.append("preflight")

    def publish(self, e, a, text, guard):
        guard()
        self.calls.append("create")
        if self.clock:
            self.clock[0] += 2000
        guard()
        self.calls.append("publish")
        return {"id": "999", "url": "https://www.threads.com/@operator/post/NEW", "verified": True}


def collected(tmp_path, **kwargs):
    clock = [1000000.0]
    s = State(tmp_path / "state.sqlite3", kwargs.get("platform", "threads"), clock=lambda: clock[0])
    run = s.create(scope(**kwargs))
    adapter = FakeAdapter()
    cli.collect(s, run, adapter)
    return s, run, clock


def analyzed(s, run):
    packet = s.packet(run, "123")
    s.analysis(run, "123", {"evidence_hash": packet["evidence_hash"], "agent_id": "writer", "summary": "Drying time is stated; the base condition is unknown.",
        "limitations": "Text only; no video or audio inspection.", "response_type": "clarifying_question", "score": 7.5, "evidence_refs": ["text", "comment:456"]})


def drafted(s, run):
    analyzed(s, run)
    p = s.packet(run, "123")
    s.draft(run, "123", {"evidence_hash": p["evidence_hash"], "analysis_hash": p["analysis_hash"], "agent_id": "writer",
                        "text": "AI-assisted question: after two days, was the base fully dry before firing?", "evidence_refs": ["text"]})


def review_input(s, run):
    p = s.packet(run, "123")
    return {"evidence_hash": p["evidence_hash"], "draft_hash": p["draft_hash"], "agent_id": "critic",
            "checks": {k: True for k in ("grounded", "specific", "useful", "not_repetitive", "tone", "disclosure", "response_type", "rating")},
            "notes": "Grounded in the caption; asks about an unknown detail and discloses AI assistance."}


def reviewed(s, run):
    drafted(s, run)
    s.review(run, "123", review_input(s, run))


def authorized(s, run):
    reviewed(s, run)
    shown = s.present(run, "123")
    s.authorize(run, "123", shown["presentation_hash"], shown["approval_token"])
    return shown


@pytest.mark.parametrize("platform", ["threads", "linkedin"])
def test_full_engage_pipeline_is_guarded_and_receipt_bound(tmp_path, platform):
    s, run, _ = collected(tmp_path, platform=platform)
    authorized(s, run)
    adapter = FakeAdapter()
    assert cli.publish(s, run, "123", adapter)["stages"] == {"123": "published"}
    assert adapter.calls == ["preflight", "create", "publish"]
    with pytest.raises(StateError):
        cli.publish(s, run, "123", adapter)
    s.close()


def test_exact_count_incomplete_blocks_ai_and_resume_keeps_inventory(tmp_path):
    s = State(tmp_path / "s.db", "threads")
    run = s.create(scope(count=2)); a = FakeAdapter(); a.fails = True
    result = cli.collect(s, run, a)
    assert result["status"] == "collection_incomplete"
    with pytest.raises(StateError, match="exact_collection"):
        s.packet(run, "123")
    a.fails = False; a.ids = ["999", "123", "789"]; a.calls.clear()
    result = cli.collect(s, run, a)
    assert result["evidence_ready"] == 1 and "discover" not in a.calls
    assert s.load(run)["inventory"] == ["123"]
    s.close()


def test_complete_resume_is_offline_and_new_only_is_global(tmp_path):
    s, run, _ = collected(tmp_path)
    a = FakeAdapter()
    cli.collect(s, run, a)
    assert a.calls == []
    other = s.create(scope()); cli.collect(s, other, a)
    assert s.status(other)["evidence_ready"] == 0
    assert s.load(other)["inventory"] == []
    s.close()


@pytest.mark.parametrize("workflow", ["listen", "music-audit"])
def test_collection_only_cannot_run_ai(tmp_path, workflow):
    s, run, _ = collected(tmp_path, workflow=workflow)
    with pytest.raises(StateError, match="collection_only"):
        s.packet(run, "123")
    with pytest.raises(StateError):
        analyzed(s, run)
    s.close()


def test_audit_cannot_draft_or_publish(tmp_path):
    s, run, _ = collected(tmp_path, workflow="audit", mode="shadow")
    with pytest.raises(StateError, match="draft_stage"):
        drafted(s, run)
    s.close()


def test_shadow_cannot_authorize_even_with_valid_token(tmp_path):
    s, run, _ = collected(tmp_path, mode="shadow")
    reviewed(s, run); shown = s.present(run, "123")
    with pytest.raises(StateError, match="shadow"):
        s.authorize(run, "123", shown["presentation_hash"], shown["approval_token"])
    s.close()


def test_review_cannot_be_self_approved_or_rebind_old_draft(tmp_path):
    s, run, _ = collected(tmp_path); drafted(s, run)
    data = review_input(s, run); data["agent_id"] = "writer"
    with pytest.raises(StateError, match="independent"):
        s.review(run, "123", data)
    data["agent_id"] = "critic"; data["draft_hash"] = "0" * 64
    with pytest.raises(StateError, match="stale_review"):
        s.review(run, "123", data)
    s.close()


def test_rejected_review_requires_redraft(tmp_path):
    s, run, _ = collected(tmp_path); drafted(s, run)
    data = review_input(s, run); data["checks"]["grounded"] = False
    s.review(run, "123", data)
    with pytest.raises(StateError):
        s.present(run, "123")
    assert s.status(run)["stages"]["123"] == "review_rejected"
    s.close()


def test_show_invalidates_old_token_and_binds_operator(tmp_path):
    s, run, _ = collected(tmp_path); reviewed(s, run)
    old = s.present(run, "123"); new = s.present(run, "123")
    with pytest.raises(StateError, match="approval_binding"):
        s.authorize(run, "123", old["presentation_hash"], old["approval_token"])
    with pytest.raises(StateError, match="approval_binding"):
        s.authorize(run, "123", new["presentation_hash"], new["approval_token"], "someone_else")
    s.authorize(run, "123", new["presentation_hash"], new["approval_token"])
    with pytest.raises(StateError):
        s.authorize(run, "123", new["presentation_hash"], new["approval_token"])
    s.close()


def test_expiration_after_container_prevents_final_submit(tmp_path):
    s, run, clock = collected(tmp_path); authorized(s, run)
    adapter = FakeAdapter(clock)
    with pytest.raises(StateError, match="uncertain"):
        cli.publish(s, run, "123", adapter)
    assert adapter.calls == ["preflight", "create"]
    assert s.status(run)["stages"]["123"] == "uncertain"
    with pytest.raises(StateError):
        s.ready(run, "123")
    s.close()


def test_expired_approval_blocks_before_preflight(tmp_path):
    s, run, clock = collected(tmp_path); authorized(s, run); clock[0] += 1801
    adapter = FakeAdapter()
    with pytest.raises(StateError, match="expired"):
        cli.publish(s, run, "123", adapter)
    assert adapter.calls == []
    s.close()


def test_preflight_failure_keeps_authorization_without_submit(tmp_path):
    s, run, _ = collected(tmp_path); authorized(s, run)
    class Blocked(FakeAdapter):
        def preflight(self, *args):
            raise AdapterError("duplicate_check_incomplete")
    with pytest.raises(AdapterError):
        cli.publish(s, run, "123", Blocked())
    assert s.status(run)["stages"]["123"] == "authorized"
    s.close()


def test_crash_after_reservation_blocks_retry_and_concurrent_publisher(tmp_path):
    s, run, _ = collected(tmp_path); authorized(s, run); s.reserve(run, "123")
    other = State(s.path, "threads", clock=s.clock)
    with pytest.raises(StateError):
        other.reserve(run, "123")
    with pytest.raises(StateError):
        other.ready(run, "123")
    other.close(); s.close()


def test_tampered_database_fails_hash_check(tmp_path):
    s, run, _ = collected(tmp_path)
    doc = s.load(run); doc["scope"]["posts"] = 2
    s.db.execute("UPDATE runs SET document=? WHERE id=?", (canonical(doc), run))
    with pytest.raises(StateError, match="integrity"):
        s.load(run)
    s.close()


def test_linkedin_purges_evidence_ai_drafts_hashes_and_preserves_known_ids(tmp_path):
    s, run, clock = collected(tmp_path, platform="linkedin"); authorized(s, run)
    cli.publish(s, run, "123", FakeAdapter()); clock[0] += 48 * 3600 + 1
    doc = s.load(run)
    assert doc["posts"]["123"] == {"stage": "purged", "purged_at": clock[0]}
    assert "123" in s.known()
    assert s.db.execute("SELECT receipt FROM attempts").fetchone()[0] == "{}"
    assert "fully dry" not in s.path.read_bytes().decode("latin1")
    s.close()


def test_wrong_platform_and_existing_foreign_database_are_refused(tmp_path):
    s, _, _ = collected(tmp_path)
    with pytest.raises(StateError, match="platform"):
        State(s.path, "linkedin")
    foreign = tmp_path / "foreign.db"
    db = sqlite3.connect(foreign); db.execute("CREATE TABLE keep_me(id)"); db.close()
    before = foreign.read_bytes()
    with pytest.raises(StateError, match="refusing"):
        State(foreign, "threads")
    assert foreign.read_bytes() == before
    s.close()


@pytest.mark.parametrize("argv", [
    ["--platform", "linkedin", "collect", "--source", "topic", "--target", "music", "--account", "urn:li:organization:1", "--posts", "1"],
    ["collect", "--source", "post", "--target", "https://threads.com/@a/post/a", "--account", "operator", "--posts", "1"],
    ["collect", "--source", "post", "--target", "123", "--account", "operator", "--posts", "2"],
    ["collect", "--source", "own", "--account", "operator", "--posts", "1", "--workflow", "listen", "--mode", "live"],
])
def test_invalid_scope_fails_before_network_or_runtime_state(argv):
    with pytest.raises((StateError, AdapterError)):
        cli.scope_from_args(cli.parser().parse_args(argv))


def test_capabilities_needs_no_database_or_token(monkeypatch):
    monkeypatch.setattr(cli, "State", lambda *a: pytest.fail("database accessed"))
    assert cli.execute(cli.parser().parse_args(["capabilities"]))["platform"] == "threads"


def test_reviewed_shadow_can_request_live_without_changing_scope_or_auto_approval(tmp_path):
    s, run, _ = collected(tmp_path, mode="shadow"); reviewed(s, run)
    scope_hash = s.load(run)["scope_hash"]
    old = s.present(run, "123")
    s.request_live(run, "123")
    assert s.load(run)["scope_hash"] == scope_hash
    assert s.status(run)["stages"]["123"] == "reviewed"
    with pytest.raises(StateError):
        s.authorize(run, "123", old["presentation_hash"], old["approval_token"])
    shown = s.present(run, "123")
    s.authorize(run, "123", shown["presentation_hash"], shown["approval_token"])
    assert cli.publish(s, run, "123", FakeAdapter())["stages"]["123"] == "published"
    s.close()


def test_refresh_appends_old_revision_resets_approval_and_keeps_scope(tmp_path):
    s, run, _ = collected(tmp_path); authorized(s, run)
    before = s.load(run); updated = evidence(); updated["text"] = "The pot dried for three days."
    s.refresh(run, updated)
    after = s.load(run)
    assert after["scope_hash"] == before["scope_hash"] and after["inventory"] == before["inventory"]
    p = after["posts"]["123"]
    assert p["stage"] == "collected" and "authorization" not in p and "analysis" not in p
    assert p["revisions"][0] == before["posts"]["123"]
    assert p["evidence_hash"] != before["posts"]["123"]["evidence_hash"]
    assert s.known() == {"123"}
    with pytest.raises(StateError):
        s.ready(run, "123")
    s.close()


def test_expired_evidence_reports_expiry_and_requires_explicit_refresh(tmp_path):
    s, run, clock = collected(tmp_path, platform="linkedin"); clock[0] += 48 * 3600 + 1
    assert s.status(run)["status"] == "evidence_expired"
    adapter = FakeAdapter()
    cli.collect(s, run, adapter)
    assert adapter.calls == []
    s.refresh(run, evidence("linkedin"))
    assert s.status(run)["status"] == "collection_complete"
    s.close()


def test_refresh_history_expires_independently(tmp_path):
    s, run, clock = collected(tmp_path, platform="linkedin"); analyzed(s, run)
    clock[0] += 24 * 3600
    s.refresh(run, evidence("linkedin"))
    clock[0] += 24 * 3600 + 1
    p = s.load(run)["posts"]["123"]
    assert "evidence" in p and p["revisions"] == []
    s.close()


def test_deleted_or_uncertain_post_cannot_refresh(tmp_path):
    s, run, _ = collected(tmp_path); authorized(s, run); s.reserve(run, "123")
    with pytest.raises(StateError, match="uncertain"):
        s.refresh(run, evidence())
    s.purge_expired("123")
    with pytest.raises(StateError, match="deleted"):
        s.refresh(run, evidence())
    s.close()


def test_operator_lock_rejects_second_owner(tmp_path):
    from social_engage.state import operator_lock
    with operator_lock(tmp_path / "s.db"):
        with pytest.raises(StateError, match="already_running"):
            with operator_lock(tmp_path / "s.db"):
                pytest.fail("second operator acquired lock")


def test_explicit_delete_after_expiry_still_blocks_refresh(tmp_path):
    s, run, clock = collected(tmp_path, platform="linkedin")
    clock[0] += 48 * 3600 + 1
    assert s.load(run)["posts"]["123"]["stage"] == "purged"
    assert s.purge_expired("123")["purged_posts"] == 1
    with pytest.raises(StateError, match="deleted"):
        s.refresh(run, evidence("linkedin"))
    s.close()


@pytest.mark.parametrize("sibling_outcome", ["published", "deleted"])
def test_refreshed_post_can_continue_after_terminal_sibling_expires(tmp_path, sibling_outcome):
    clock = [1000000.0]
    s = State(tmp_path / "s.db", "linkedin", clock=lambda: clock[0])
    run = s.create(scope(platform="linkedin", count=2)); adapter = FakeAdapter(); adapter.ids = ["123", "124"]
    cli.collect(s, run, adapter)
    if sibling_outcome == "published":
        # The separate sibling's terminal attempt survives payload expiry.
        s.db.execute("INSERT INTO attempts VALUES ('88','124',?,'published','{}')", (run,))
    else:
        s.purge_expired("124")
    clock[0] += 48 * 3600 + 1
    s.load(run)
    s.refresh(run, evidence("linkedin", "123"))
    assert s.status(run)["collection_verified"] is True
    assert s.status(run)["evidence_ready"] == 1
    # Original exact collection remains proven; only this target must be fresh.
    authorized(s, run)
    assert cli.publish(s, run, "123", FakeAdapter())["stages"]["123"] == "published"
    with pytest.raises(StateError):
        s.refresh(run, evidence("linkedin", "124"))
    s.close()


@pytest.mark.parametrize("bad_text", ["A question without disclosure", "AI question with 8/10 rating"])
def test_draft_policy_rejects_missing_disclosure_and_constructive_rating(tmp_path, bad_text):
    s, run, _ = collected(tmp_path); analyzed(s, run); p = s.packet(run, "123")
    with pytest.raises(StateError):
        s.draft(run, "123", {"text": bad_text, "agent_id": "writer", "evidence_refs": ["text"],
                            "analysis_hash": p["analysis_hash"], "evidence_hash": p["evidence_hash"]})
    s.close()
