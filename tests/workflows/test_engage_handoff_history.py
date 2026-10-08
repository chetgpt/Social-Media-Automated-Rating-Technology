"""Fresh approved retries preserve prior publications and all duplicate fences."""

import sqlite3

import pytest

import engage_tiktok as engage
from test_engage_tiktok import advance_to_reviewed
from tiktok_master_database import (
    attach_master_database,
    mark_submit_intent,
    register_publication_claim,
    register_publication_outcome,
)
from tiktok_scraper.analysis_workflow import ensure_analysis_schema


def authorize(conn, run_id):
    row = conn.execute(
        "SELECT * FROM engage_tiktok_posts WHERE run_id=?", (run_id,)
    ).fetchone()
    shown = engage.present_response(conn, run_id, row["post_id"])
    engage.authorize_response(
        conn, run_id, row["post_id"],
        expected_draft_hash=row["draft_hash"],
        expected_review_hash=row["review_hash"],
        expected_presentation_hash=shown["presentation_hash"],
        approval_token=shown["approval_token"],
    )


@pytest.fixture
def pair(tmp_path):
    conn = engage.connect_database(tmp_path / "state.sqlite")
    for run_id in ("old", "fresh"):
        advance_to_reviewed(conn, tmp_path, run_id=run_id, project=run_id)
        authorize(conn, run_id)
    first = engage.handoff_publication(conn, "old", "1")
    attach_master_database(conn, tmp_path / "master.sqlite")
    conn.commit()
    yield conn, first
    conn.close()


def prior_attempt(conn, first, *, outcome="failed", submit=False):
    attempt_id = register_publication_claim(
        conn, "master", account="creator", post_id="1",
        publication_id=first["publication_id"], local_attempt_number=1,
        run_id="old", target_url=first["target_url"], text_hash=first["draft_hash"],
    )
    if submit:
        mark_submit_intent(conn, "master", attempt_id)
    if outcome:
        register_publication_outcome(
            conn, "master", attempt_id=attempt_id, outcome=outcome,
            remote_comment_id="remote" if outcome == "published" else "",
        )
    conn.execute(
        "UPDATE publication_queue SET attempts=1, master_attempt_id=?, "
        "error='editor focus failed' WHERE publication_id=?",
        (attempt_id, first["publication_id"]),
    )
    conn.execute(
        "INSERT INTO publication_receipts(receipt_id,publication_id,attempted_at,mode,status,error) "
        "VALUES ('receipt',?,'2026-09-08T00:00:00Z','live','failed','editor focus failed')",
        (first["publication_id"],),
    )
    conn.commit()
    return attempt_id


def supersede(conn, first):
    return engage.handoff_publication(
        conn, "fresh", "1", supersedes_publication_id=first["publication_id"]
    )


@pytest.mark.parametrize("status", ["approved", "publishing", "published", "uncertain"])
def test_exact_handoff_replay_does_not_reset_adapter_state(pair, status):
    conn, first = pair
    conn.execute(
        "UPDATE publication_queue SET status=?,attempts=4,error='retained', "
        "master_attempt_id='original' WHERE publication_id=?",
        (status, first["publication_id"]),
    )
    conn.commit()
    before = dict(conn.execute("SELECT * FROM publication_queue").fetchone())
    result = engage.handoff_publication(conn, "old", "1")
    assert result["idempotent"] is True
    assert result["publication_status"] == status
    assert result["handoff_hash"] == first["handoff_hash"]
    assert dict(conn.execute("SELECT * FROM publication_queue").fetchone()) == before


def test_tampered_exact_handoff_is_not_repaired_by_replacement(pair):
    conn, first = pair
    conn.execute("UPDATE publication_queue SET draft_text='different'")
    conn.commit()
    with pytest.raises(engage.StageGateError, match="immutable queue row"):
        engage.handoff_publication(conn, "old", "1")
    assert conn.execute("SELECT draft_text FROM publication_queue").fetchone()[0] == "different"


def test_same_text_fresh_handoff_requires_explicit_supersession(pair):
    conn, first = pair
    prior_attempt(conn, first)
    with pytest.raises(engage.StageGateError, match="--supersedes-publication-id"):
        engage.handoff_publication(conn, "fresh", "1")
    assert conn.execute("SELECT COUNT(*) FROM publication_queue").fetchone()[0] == 1


def test_pre_submit_retry_preserves_old_row_receipt_analysis_and_provenance(pair):
    conn, first = pair
    attempt_id = prior_attempt(conn, first)
    prior = dict(conn.execute("SELECT * FROM publication_queue").fetchone())
    receipt = dict(conn.execute("SELECT * FROM publication_receipts").fetchone())
    new = supersede(conn, first)
    rows = {
        row["publication_id"]: dict(row)
        for row in conn.execute("SELECT * FROM publication_queue")
    }
    assert len(rows) == 2
    old = rows[first["publication_id"]]
    assert old["status"] == "superseded"
    assert old["superseded_status"] == "approved"
    assert old["superseded_by_publication_id"] == new["publication_id"]
    assert old["master_attempt_id"] == attempt_id
    for field in ("draft_hash", "ai_review_hash", "authorization_presentation_hash", "attempts", "created_at", "error"):
        assert old[field] == prior[field]
    assert rows[new["publication_id"]]["supersedes_publication_id"] == first["publication_id"]
    assert rows[new["publication_id"]]["attempts"] == 0
    assert dict(conn.execute("SELECT * FROM publication_receipts").fetchone()) == receipt
    assert conn.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 2
    assert engage.handoff_publication(conn, "old", "1")["publication_status"] == "superseded"
    assert engage.handoff_publication(conn, "fresh", "1")["idempotent"] is True


def test_expired_never_attempted_row_can_be_explicitly_superseded(pair):
    conn, first = pair
    conn.execute("UPDATE publication_queue SET valid_until='2000-01-01T00:00:00Z'")
    conn.commit()
    assert supersede(conn, first)["supersedes_publication_id"] == first["publication_id"]


def test_active_unattempted_approval_cannot_be_superseded(pair):
    conn, first = pair
    with pytest.raises(engage.StageGateError, match="before it expires"):
        supersede(conn, first)


@pytest.mark.parametrize("outcome,submit", [("published", True), ("uncertain", True), (None, True), (None, False)])
def test_master_confirmed_uncertain_or_active_attempt_blocks_fresh_same_text(pair, outcome, submit):
    conn, first = pair
    prior_attempt(conn, first, outcome=outcome, submit=submit)
    with pytest.raises(engage.StageGateError, match="master publication guard"):
        supersede(conn, first)
    assert conn.execute("SELECT COUNT(*) FROM publication_queue").fetchone()[0] == 1


def test_expired_attempt_without_master_proof_cannot_be_superseded(pair):
    conn, first = pair
    conn.execute("UPDATE publication_queue SET attempts=1,valid_until='2000-01-01T00:00:00Z'")
    conn.commit()
    with pytest.raises(engage.StageGateError, match="verified retryable"):
        supersede(conn, first)


def test_prior_submit_intent_never_becomes_a_safe_fresh_retry(pair):
    conn, first = pair
    prior_attempt(conn, first)
    conn.execute("UPDATE master.tiktok_master_comment_attempts SET submit_intent_at='2026-09-08T00:00:00Z'")
    conn.commit()
    with pytest.raises(engage.StageGateError, match="no submit intent"):
        supersede(conn, first)


def test_legacy_non_engage_queue_entry_is_not_superseded(pair):
    conn, first = pair
    conn.execute("UPDATE publication_queue SET engage_run_id='',status='expired'")
    conn.commit()
    with pytest.raises(engage.StageGateError, match="not a supersedable ENGAGE"):
        supersede(conn, first)


def test_handoff_failure_rolls_back_supersession_and_keeps_prior_receipt(pair):
    conn, first = pair
    prior_attempt(conn, first)
    conn.execute(
        "CREATE TRIGGER fail_new_handoff BEFORE INSERT ON publication_queue "
        "BEGIN SELECT RAISE(ABORT,'test interrupted insert'); END"
    )
    conn.commit()
    with pytest.raises(engage.StageGateError, match="immutable history"):
        supersede(conn, first)
    assert conn.execute("SELECT status FROM publication_queue").fetchone()[0] == "approved"
    assert conn.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM publication_receipts").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM engage_tiktok_posts WHERE run_id='fresh'").fetchone()[0] == "authorized"


def test_legacy_schema_migration_retains_rows_receipts_indexes_and_duplicate_fence():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_analysis_schema(conn)
    schema = conn.execute("SELECT sql FROM sqlite_master WHERE name='publication_queue'").fetchone()[0]
    conn.execute("DROP TABLE publication_queue")
    legacy_schema = schema.rstrip()[:-1] + ", UNIQUE(platform,target_url,draft_hash))"
    conn.execute(legacy_schema)
    conn.execute("CREATE INDEX custom_publication_index ON publication_queue(error)")
    conn.execute("CREATE TABLE marker(value TEXT)")
    conn.execute("CREATE TRIGGER custom_publication_trigger AFTER UPDATE ON publication_queue BEGIN INSERT INTO marker VALUES (NEW.status); END")
    conn.execute(
        "INSERT INTO publication_queue(publication_id,project,platform,content_key,analysis_id,"
        "target_url,draft_text,draft_hash,created_at,updated_at) "
        "VALUES ('prior','p','tiktok','post','analysis','url','text','hash','then','then')"
    )
    conn.execute("INSERT INTO publication_receipts(receipt_id,publication_id,attempted_at,mode,status) VALUES ('r','prior','then','live','failed')")
    conn.commit()
    prior = dict(conn.execute("SELECT * FROM publication_queue").fetchone())
    ensure_analysis_schema(conn)
    ensure_analysis_schema(conn)
    assert dict(conn.execute("SELECT * FROM publication_queue").fetchone()) == prior
    assert conn.execute("SELECT publication_id FROM publication_receipts").fetchone()[0] == "prior"
    insert = (
        "INSERT INTO publication_queue(publication_id,project,platform,content_key,analysis_id,"
        "target_url,draft_text,draft_hash,created_at,updated_at) "
        "VALUES ('fresh','p','tiktok','post','fresh-analysis','url','text','hash','now','now')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert)
    conn.execute("UPDATE publication_queue SET status='superseded'")
    conn.execute(insert)
    assert conn.execute("SELECT COUNT(*) FROM publication_queue").fetchone()[0] == 2
    assert conn.execute("SELECT value FROM marker").fetchone()[0] == "superseded"
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='custom_publication_index'").fetchone()
    conn.close()
