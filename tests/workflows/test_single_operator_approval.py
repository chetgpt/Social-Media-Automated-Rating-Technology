"""A personal name is optional; exact human approval and publication gates are not."""

import datetime as dt
import json

import pytest

import engage_tiktok as engage
from test_engage_tiktok import advance_to_reviewed
from tiktok_publication_adapter import load_approved_publication


@pytest.fixture
def reviewed(tmp_path):
    database = tmp_path / "state.sqlite"
    conn = engage.connect_database(database)
    try:
        run_id = advance_to_reviewed(conn, tmp_path, mode="live")
        post = conn.execute(
            "SELECT * FROM engage_tiktok_posts WHERE run_id=?", (run_id,)
        ).fetchone()
        yield conn, run_id, post, database
    finally:
        conn.close()


def approval_arguments(post, shown):
    return {
        "expected_draft_hash": post["draft_hash"],
        "expected_review_hash": post["review_hash"],
        "expected_presentation_hash": shown["presentation_hash"],
        "approval_token": shown["approval_token"],
    }


@pytest.mark.parametrize("command,field", [
    ("show-response", "presented_to"),
    ("authorize", "authorized_by"),
])
@pytest.mark.parametrize("explicit_name", [None, "Existing Human"])
def test_cli_names_are_optional_and_existing_explicit_names_are_preserved(
    command, field, explicit_name
):
    argv = [command, "--run-id", "run", "--post-id", "1"]
    if command == "authorize":
        for option in ("draft-hash", "review-hash", "presentation-hash", "approval-token"):
            argv.extend([f"--{option}", f"test-{option}"])
    if explicit_name is not None:
        argv.extend([f"--{field.replace('_', '-')}", explicit_name])
    args = engage.parse_args(argv)
    assert getattr(args, field) == (explicit_name or "workspace-operator")


def test_default_operator_reaches_publisher_only_after_explicit_authorization(reviewed):
    conn, run_id, post, database = reviewed
    shown = engage.present_response(conn, run_id, post["post_id"])
    assert shown["presented_to"] == "workspace-operator"
    assert shown["final_response"] == post["draft_text"]
    stored = engage._post_row(conn, run_id, post["post_id"])
    assert stored["status"] == "reviewed"
    assert stored["authorization_by"] == ""
    assert json.loads(stored["presentation_json"])["presented_to"] == "workspace-operator"
    with pytest.raises(engage.StageGateError, match="live authorization"):
        engage.handoff_publication(conn, run_id, post["post_id"])

    # This explicit call represents the human's approval of the shown text.
    authorized = engage.authorize_response(
        conn, run_id, post["post_id"], **approval_arguments(post, shown)
    )
    assert authorized["authorized_by"] == "workspace-operator"
    handoff = engage.handoff_publication(conn, run_id, post["post_id"])
    adapter_record = load_approved_publication(database, handoff["publication_id"])
    assert adapter_record["approved_by"] == "workspace-operator"
    assert adapter_record["final_text"] == shown["final_response"]
    assert adapter_record["authorization_presentation_hash"] == shown["presentation_hash"]
    with pytest.raises(engage.StageGateError, match="independent AI review approval"):
        engage.authorize_response(
            conn, run_id, post["post_id"], **approval_arguments(post, shown)
        )


def test_legacy_named_presentation_requires_its_original_identity(reviewed):
    conn, run_id, post, database = reviewed
    shown = engage.present_response(conn, run_id, post["post_id"], presented_to="Taylor")
    with pytest.raises(engage.StageGateError, match="approval token"):
        engage.authorize_response(
            conn, run_id, post["post_id"], **approval_arguments(post, shown)
        )
    engage.authorize_response(
        conn, run_id, post["post_id"], authorized_by="Taylor",
        **approval_arguments(post, shown)
    )
    handoff = engage.handoff_publication(conn, run_id, post["post_id"])
    assert load_approved_publication(database, handoff["publication_id"])["approved_by"] == "Taylor"


def test_default_presentation_rejects_a_different_explicit_human_identity(reviewed):
    conn, run_id, post, _ = reviewed
    shown = engage.present_response(conn, run_id, post["post_id"])
    with pytest.raises(engage.StageGateError, match="approval token"):
        engage.authorize_response(
            conn, run_id, post["post_id"], authorized_by="Someone Else",
            **approval_arguments(post, shown)
        )


@pytest.mark.parametrize("identity", ["", "  ", "codex", "antigravity-reviewer", "AI"])
def test_explicit_blank_or_automation_identity_is_not_replaced_by_default(reviewed, identity):
    conn, run_id, post, _ = reviewed
    with pytest.raises(engage.StageGateError, match="non-AI user identity"):
        engage.present_response(conn, run_id, post["post_id"], presented_to=identity)
    shown = engage.present_response(conn, run_id, post["post_id"])
    with pytest.raises(engage.StageGateError, match="non-AI user authorization"):
        engage.authorize_response(
            conn, run_id, post["post_id"], authorized_by=identity,
            **approval_arguments(post, shown)
        )


@pytest.mark.parametrize("field,reason", [
    ("expected_draft_hash", "exact response text"),
    ("expected_review_hash", "AI review"),
    ("expected_presentation_hash", "shown response presentation"),
    ("approval_token", "approval token"),
])
def test_default_operator_cannot_bypass_exact_approval_bindings(reviewed, field, reason):
    conn, run_id, post, _ = reviewed
    shown = engage.present_response(conn, run_id, post["post_id"])
    arguments = approval_arguments(post, shown)
    arguments[field] = "invalid"
    with pytest.raises(engage.StageGateError, match=reason):
        engage.authorize_response(conn, run_id, post["post_id"], **arguments)
    assert engage._post_row(conn, run_id, post["post_id"])["status"] == "reviewed"


def test_replaced_presentation_token_cannot_be_reused(reviewed):
    conn, run_id, post, _ = reviewed
    old = engage.present_response(conn, run_id, post["post_id"])
    latest = engage.present_response(conn, run_id, post["post_id"])
    arguments = approval_arguments(post, latest)
    arguments["approval_token"] = old["approval_token"]
    with pytest.raises(engage.StageGateError, match="approval token"):
        engage.authorize_response(conn, run_id, post["post_id"], **arguments)


def test_presentation_before_review_is_rejected_without_a_name(reviewed, monkeypatch):
    conn, run_id, post, _ = reviewed
    before_review = engage.parse_iso(post["reviewed_at"]) - dt.timedelta(seconds=1)
    monkeypatch.setattr(engage, "now_iso", lambda: before_review.isoformat())
    shown = engage.present_response(conn, run_id, post["post_id"])
    with pytest.raises(engage.StageGateError, match="predates its AI review"):
        engage.authorize_response(
            conn, run_id, post["post_id"], **approval_arguments(post, shown)
        )


def test_unreviewed_response_cannot_be_presented_without_a_name(reviewed):
    conn, run_id, post, _ = reviewed
    conn.execute("UPDATE engage_tiktok_posts SET status='drafted' WHERE run_id=?", (run_id,))
    conn.commit()
    with pytest.raises(engage.StageGateError, match="independently reviewed"):
        engage.present_response(conn, run_id, post["post_id"])


def test_default_operator_does_not_authorize_shadow(tmp_path):
    conn = engage.connect_database(tmp_path / "shadow.sqlite")
    try:
        run_id = advance_to_reviewed(conn, tmp_path, mode="shadow")
        post = engage._post_row(conn, run_id, "1")
        shown = engage.present_response(conn, run_id, post["post_id"])
        with pytest.raises(engage.StageGateError, match="SHADOW"):
            engage.authorize_response(
                conn, run_id, post["post_id"], **approval_arguments(post, shown)
            )
    finally:
        conn.close()


def test_default_operator_does_not_bypass_handoff_freshness(reviewed):
    conn, run_id, post, _ = reviewed
    shown = engage.present_response(conn, run_id, post["post_id"])
    engage.authorize_response(
        conn, run_id, post["post_id"], **approval_arguments(post, shown)
    )
    stale_analysis = (dt.datetime.now().astimezone() - dt.timedelta(hours=2)).isoformat()
    conn.execute(
        "UPDATE engage_tiktok_posts SET analyzed_at=? WHERE run_id=?", (stale_analysis, run_id)
    )
    conn.commit()
    with pytest.raises(engage.StageGateError, match="stale"):
        engage.handoff_publication(conn, run_id, post["post_id"])
