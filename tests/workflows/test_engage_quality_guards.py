"""Offline regressions for observed ENGAGE rating and repeated-draft failures."""

import runpy
from pathlib import Path

import pytest

import engage_tiktok as engage


HELPERS = runpy.run_path(str(Path(__file__).with_name("test_engage_stage_gates.py")))
BODY = "Showing the chord changes slowly gives learners time to follow each voicing."


@pytest.fixture
def corpus(tmp_path):
    conn = engage.connect_database(tmp_path / "quality.sqlite")
    run_id = HELPERS["collected_run"](conn, 3)
    HELPERS["analyze"](conn, run_id, tmp_path, [1, 2, 3])
    try:
        yield conn, run_id, tmp_path
    finally:
        conn.close()


def snapshot(conn, run_id):
    return {
        table: [tuple(row) for row in conn.execute(
            f"SELECT * FROM {table} WHERE run_id=? ORDER BY rowid", (run_id,)
        )]
        for table in ("engage_tiktok_runs", "engage_tiktok_posts", "engage_tiktok_events")
    }


def draft_records(corpus, post_ids=(1, 2, 3)):
    conn, run_id, _ = corpus
    return [HELPERS["draft_record"](conn, run_id, post_id) for post_id in post_ids]


def import_drafts(corpus, records):
    conn, run_id, root = corpus
    path = HELPERS["write_records"](root / "drafts.jsonl", records)
    return engage.import_draft_results(conn, run_id, path, actor="codex-drafter")


def import_reviews(corpus, records):
    conn, run_id, root = corpus
    path = HELPERS["write_records"](root / "reviews.jsonl", records)
    return engage.import_review_results(conn, run_id, path, actor="codex-reviewer")


@pytest.mark.parametrize("token", [
    "8.4/10/10", "8.4/10 / 10", "8.4/10 out of ten", "8.4/10 stars",
    "8.4 / 10", "8,4/10", "8.4/10.0", "08.4/10", "8.4/10.5",
    "1/8.4/10", "8.4/10 and 8.4/10", "8.4/10; 8 out of ten",
    "8.4/10; score: 8", "8.4/10; /10",
])
def test_positive_response_rejects_noncanonical_or_repeated_rating(token):
    rating = engage.response_rating_metadata("positive_support", 84)
    with pytest.raises(engage.StageGateError, match="canonical public rating"):
        engage.verify_rendered_response(f"AI-assisted perspective: {token}. {BODY}", rating)


@pytest.mark.parametrize("draft", [
    "{PUBLIC_RATING}/10", "{PUBLIC_RATING} / 10", "{PUBLIC_RATING} out of ten",
    "{PUBLIC_RATING}; 8.4/10", "{PUBLIC_RATING}; rating: 8",
])
def test_renderer_rejects_extra_rating_scale_before_storage(draft):
    with pytest.raises(engage.StageGateError, match="canonical public rating"):
        engage.render_public_rating(draft, 84)


@pytest.mark.parametrize("score,token", [(84, "8.4/10"), (80, "8/10"), (100, "10/10")])
def test_positive_response_accepts_exact_rendered_rating(score, token):
    rendered, rating = engage.render_public_rating(
        "AI-assisted perspective: {PUBLIC_RATING}. " + BODY, score
    )
    assert token in rendered
    engage.verify_rendered_response(rendered, rating)


@pytest.mark.parametrize("fault", ["rating", "hash", "duplicate"])
def test_draft_batch_failure_rolls_back_first_valid_record(corpus, fault):
    conn, run_id, _ = corpus
    records = draft_records(corpus)
    if fault == "rating":
        records[1]["draft_text"] = records[1]["draft_text"].replace(
            "{PUBLIC_RATING}", "{PUBLIC_RATING}/10"
        )
    elif fault == "hash":
        records[1]["analysis_hash"] = "stale"
    else:
        records[1]["draft_text"] = records[0]["draft_text"]
    before = snapshot(conn, run_id)
    with pytest.raises(engage.StageGateError):
        import_drafts(corpus, records)
    conn.commit()  # A caller must not accidentally commit a partially accepted batch.
    assert snapshot(conn, run_id) == before


@pytest.mark.parametrize("existing_status", ["drafted", "reviewed", "authorized", "handed_off"])
def test_duplicate_checks_previously_imported_active_drafts(corpus, existing_status):
    conn, run_id, _ = corpus
    records = draft_records(corpus, (1, 2))
    assert import_drafts(corpus, records[:1]) == {"applied": 1}
    conn.execute("UPDATE engage_tiktok_posts SET status=? WHERE run_id=? AND post_id='1'",
                 (existing_status, run_id))
    conn.commit()
    records[1]["draft_text"] = records[0]["draft_text"]
    before = snapshot(conn, run_id)
    with pytest.raises(engage.StageGateError, match="duplicate substantive draft"):
        import_drafts(corpus, records[1:])
    assert snapshot(conn, run_id) == before


def test_duplicate_normalizes_rating_disclosure_spacing_and_match_suffix(corpus, monkeypatch):
    conn, run_id, _ = corpus
    contexts = {
        str(index): {"matches": [{"creator_handle": f"match_{index}",
                                  "reason": f"Specific connection reason number {index}."}]}
        for index in (1, 2, 3)
    }
    monkeypatch.setattr(engage, "_creator_match_contexts", lambda *_args: contexts)
    records = draft_records(corpus, (1, 2))
    records[0]["draft_text"] = "AI-assisted perspective: {PUBLIC_RATING}. " + BODY
    records[1]["draft_text"] = "[AI] {PUBLIC_RATING} - " + BODY.upper().replace(" ", "  ")
    # Scores can differ without making the substantive comment unique.
    conn.execute("UPDATE engage_tiktok_posts SET analysis_score=90 WHERE run_id=? AND post_id='2'", (run_id,))
    conn.commit()
    with pytest.raises(engage.StageGateError, match="duplicate substantive draft"):
        import_drafts(corpus, records)
    assert engage.run_status(conn, run_id)["drafted"] == 0


def test_same_scores_and_different_substance_are_accepted(corpus):
    assert import_drafts(corpus, draft_records(corpus)) == {"applied": 3}


def test_same_post_reimport_is_idempotent_and_does_not_clear_review(corpus):
    conn, run_id, _ = corpus
    records = draft_records(corpus)
    assert import_drafts(corpus, records) == {"applied": 3}
    review = HELPERS["review_record"](conn, run_id, 1)
    assert import_reviews(corpus, [review]) == {"applied": 1, "rejected": 0}
    before = snapshot(conn, run_id)
    assert import_drafts(corpus, records) == {"applied": 0}
    assert snapshot(conn, run_id) == before
    records[0]["draft_text"] += " Changed wording."
    with pytest.raises(engage.StageGateError, match="not awaiting a draft"):
        import_drafts(corpus, records[:1])
    assert snapshot(conn, run_id) == before


def test_review_batch_hash_failure_is_atomic(corpus):
    conn, run_id, _ = corpus
    import_drafts(corpus, draft_records(corpus))
    records = [HELPERS["review_record"](conn, run_id, post_id) for post_id in (1, 2)]
    records[1]["draft_hash"] = "stale"
    before = snapshot(conn, run_id)
    with pytest.raises(engage.StageGateError, match="draft_hash mismatch"):
        import_reviews(corpus, records)
    conn.commit()
    assert snapshot(conn, run_id) == before


@pytest.mark.parametrize("legacy_fault", ["rating", "duplicate"])
def test_legacy_bad_draft_cannot_be_approved_but_can_be_rejected(corpus, legacy_fault):
    conn, run_id, _ = corpus
    import_drafts(corpus, draft_records(corpus))
    first = HELPERS["post_row"](conn, run_id, 1)
    bad_text = first["draft_text"]
    if legacy_fault == "rating":
        bad_text = bad_text.replace("8.4/10", "8.4/10/10")
    conn.execute("UPDATE engage_tiktok_posts SET draft_text=?,draft_hash=? WHERE run_id=? AND post_id='2'",
                 (bad_text, engage.text_hash(bad_text), run_id))
    conn.commit()
    before = snapshot(conn, run_id)
    # Approval flags cannot override these deterministic content failures.
    with pytest.raises(engage.StageGateError):
        import_reviews(corpus, [HELPERS["review_record"](conn, run_id, 2)])
    assert snapshot(conn, run_id) == before
    rejected = HELPERS["review_record"](conn, run_id, 2, approved=False)
    assert import_reviews(corpus, [rejected]) == {"applied": 1, "rejected": 1}
    assert HELPERS["post_row"](conn, run_id, 2)["status"] == "review_rejected"
    assert import_drafts(corpus, draft_records(corpus, (2,))) == {"applied": 1}
