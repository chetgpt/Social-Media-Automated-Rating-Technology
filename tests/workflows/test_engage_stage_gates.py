"""Offline regressions for run-wide ENGAGE AI stage prerequisites."""

import asyncio
import json

import pytest

from engage_tiktok import (
    StageGateError,
    collect_exact,
    connect_database,
    create_run,
    engage_analysis_result_payload,
    engage_decision_payload,
    export_draft_queue,
    export_review_queue,
    import_analysis_results,
    import_draft_results,
    import_review_results,
    json_hash,
    run_status,
)


class ReadyPreflight:
    async def ensure_ready(self):
        return {
            "reachable": True,
            "tiktok_authenticated": True,
            "observed_account": "creator",
            "checked_at": "2026-07-28T10:00:00+07:00",
        }


class StaticCollector:
    async def collect(self, *, requested_count, **_kwargs):
        return [
            {
                "id": str(index),
                "url": f"https://www.tiktok.com/@creator/video/{index}",
                "username": "creator",
                "caption": "A useful explanation with room for a source.",
                "view_count": 100,
                "like_count": 10,
                "comment_count": 1,
                "share_count": 2,
                "transcript": "",
                "transcript_status": "unavailable",
                "subtitle_no_caption_reason": "not_provided",
                "comments": [{"cid": "comment-1", "text": "What is the source?"}],
                "ok": True,
                "complete": True,
                "exhausted": True,
                "limit_reached": False,
                "has_more": False,
                "source": "test-double",
                "discovery_method": "test",
                "discovery_source": "test",
                "metadata_method": "test",
            }
            for index in range(1, requested_count + 1)
        ]


@pytest.fixture
def conn(tmp_path):
    database = connect_database(tmp_path / "state.sqlite")
    try:
        yield database
    finally:
        database.close()


def collected_run(conn, count):
    run_id = create_run(
        conn,
        project="stage_gate_test",
        topic="coffee",
        requested_count=count,
        max_comments=20,
        max_pages=10,
        mode="shadow",
        music_catalogs=(),
    )
    asyncio.run(
        collect_exact(
            conn,
            run_id=run_id,
            preflight=ReadyPreflight(),
            collector=StaticCollector(),
        )
    )
    return run_id


def write_records(path, records):
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    return path


def post_row(conn, run_id, post_id):
    return conn.execute(
        "SELECT * FROM engage_tiktok_posts WHERE run_id=? AND post_id=?",
        (run_id, str(post_id)),
    ).fetchone()


def analyze(conn, run_id, tmp_path, post_ids, *, skipped=()):
    records = []
    for post_id in post_ids:
        skip = post_id in skipped
        analysis = {
            "summary": "The post is useful but a supporting source would help.",
            "post_quality_score": 35 if skip else 80,
            "conversation_value_score": 25 if skip else 100,
            "confidence": 90,
            "positive_eligible": not skip,
            "response_type": "skip" if skip else "positive_support",
            "decision_reason": "No safe contribution." if skip else "Useful support.",
            "novel_value": "No safe novel value." if skip else "A source adds value.",
            "strength": "The explanation is clear.",
            "recommendation": "Do not comment." if skip else "Add a source.",
            "evidence_refs": ["caption", "comment-1"],
            "skip_reason": "insufficient_grounded_response_value" if skip else "",
        }
        records.append(
            {
                "post_id": str(post_id),
                "evidence_hash": post_row(conn, run_id, post_id)["evidence_hash"],
                "analysis": analysis,
            }
        )
    source = write_records(tmp_path / "analysis.jsonl", records)
    return import_analysis_results(conn, run_id, source, actor="codex-analysis")


def draft_record(conn, run_id, post_id):
    row = post_row(conn, run_id, post_id)
    observations = (
        "The explanation is useful; adding a concrete source would make it easier to verify.",
        "Linking the reference could help the viewer who asks where this explanation comes from.",
        "The discussion asks for a source; identifying the original reference would make a useful follow-up.",
        "A reference alongside this explanation would give readers a way to check its context.",
        "Connecting the explanation to its source would let interested learners explore the reasoning further.",
    )
    return {
        "post_id": row["post_id"],
        "evidence_hash": row["evidence_hash"],
        "analysis_hash": row["analysis_hash"],
        "draft_text": (
            "AI-assisted perspective: {PUBLIC_RATING}. "
            + observations[int(post_id) - 1]
        ),
    }


def draft(conn, run_id, tmp_path, post_ids):
    source = write_records(
        tmp_path / "drafts.jsonl",
        [draft_record(conn, run_id, post_id) for post_id in post_ids],
    )
    return import_draft_results(conn, run_id, source, actor="codex-drafter")


def review_record(conn, run_id, post_id, *, approved=True):
    row = post_row(conn, run_id, post_id)
    return {
        "post_id": row["post_id"],
        "evidence_hash": row["evidence_hash"],
        "analysis_hash": row["analysis_hash"],
        "draft_hash": row["draft_hash"],
        "target_url": row["url"],
        "content_key": row["post_id"],
        "expected_account": "creator",
        "decision_hash": json_hash(engage_decision_payload(row)),
        "analysis_result_hash": json_hash(engage_analysis_result_payload(row)),
        "independent_review": True,
        "approved": approved,
        "grounding": "pass",
        "usefulness": "pass",
        "tone": "pass",
        "language": "pass",
        "ai_disclosure": "pass",
        "rating_consistency": "pass",
        "positive_only": "pass",
        "issues": [] if approved else ["Revise the recommendation for clarity."],
    }


def review(conn, run_id, tmp_path, post_id, *, approved=True):
    source = write_records(
        tmp_path / "reviews.jsonl",
        [review_record(conn, run_id, post_id, approved=approved)],
    )
    return import_review_results(conn, run_id, source, actor="codex-reviewer")


def post_and_event_snapshot(conn, run_id):
    return {
        table: [tuple(row) for row in conn.execute(
            f"SELECT * FROM {table} WHERE run_id=? ORDER BY rowid", (run_id,)
        )]
        for table in ("engage_tiktok_posts", "engage_tiktok_events")
    }


@pytest.mark.parametrize("operation", ["export", "import"])
def test_drafting_waits_for_whole_run_analysis(conn, tmp_path, operation):
    run_id = collected_run(conn, 3)
    assert analyze(conn, run_id, tmp_path, [1, 2]) == {"applied": 2}
    source = write_records(
        tmp_path / "early-drafts.jsonl",
        [draft_record(conn, run_id, post_id) for post_id in (1, 2)],
    )
    output = tmp_path / "early-draft-queue.jsonl"
    before = post_and_event_snapshot(conn, run_id)

    with pytest.raises(StageGateError, match="every collected post.*analyzed"):
        if operation == "export":
            export_draft_queue(conn, run_id, output)
        else:
            import_draft_results(conn, run_id, source, actor="codex-drafter")

    assert post_and_event_snapshot(conn, run_id) == before
    assert not output.exists()
    assert run_status(conn, run_id)["drafted"] == 0


@pytest.mark.parametrize("operation", ["export", "import"])
def test_review_waits_for_whole_run_drafting(conn, tmp_path, operation):
    run_id = collected_run(conn, 3)
    assert analyze(conn, run_id, tmp_path, [1, 2, 3]) == {"applied": 3}
    assert draft(conn, run_id, tmp_path, [1, 2]) == {"applied": 2}
    source = write_records(
        tmp_path / "early-reviews.jsonl",
        [review_record(conn, run_id, post_id) for post_id in (1, 2)],
    )
    output = tmp_path / "early-review-queue.jsonl"
    before = post_and_event_snapshot(conn, run_id)

    with pytest.raises(StageGateError, match="every eligible post.*drafted"):
        if operation == "export":
            export_review_queue(conn, run_id, output)
        else:
            import_review_results(conn, run_id, source, actor="codex-reviewer")

    assert post_and_event_snapshot(conn, run_id) == before
    assert not output.exists()
    assert run_status(conn, run_id)["reviewed"] == 0


def test_incremental_imports_allow_skips_and_rejected_redrafts(conn, tmp_path):
    run_id = collected_run(conn, 4)
    assert analyze(conn, run_id, tmp_path, [1, 3], skipped=(3,)) == {"applied": 2}
    assert analyze(conn, run_id, tmp_path, [2, 4], skipped=(4,)) == {"applied": 2}
    assert export_draft_queue(conn, run_id, tmp_path / "draft-queue.jsonl") == 2
    assert draft(conn, run_id, tmp_path, [1]) == {"applied": 1}
    assert export_draft_queue(conn, run_id, tmp_path / "draft-queue.jsonl") == 1
    assert draft(conn, run_id, tmp_path, [2]) == {"applied": 1}
    assert export_review_queue(conn, run_id, tmp_path / "review-queue.jsonl") == 2

    assert review(conn, run_id, tmp_path, 1, approved=False) == {
        "applied": 1, "rejected": 1,
    }
    assert post_row(conn, run_id, 1)["status"] == "review_rejected"
    assert export_review_queue(conn, run_id, tmp_path / "review-queue.jsonl") == 1
    assert review(conn, run_id, tmp_path, 2) == {"applied": 1, "rejected": 0}
    assert export_draft_queue(conn, run_id, tmp_path / "draft-queue.jsonl") == 1
    assert draft(conn, run_id, tmp_path, [1]) == {"applied": 1}
    assert export_review_queue(conn, run_id, tmp_path / "review-queue.jsonl") == 1
    assert review(conn, run_id, tmp_path, 1) == {"applied": 1, "rejected": 0}

    status = run_status(conn, run_id)
    assert status["status"] == "stored"
    assert status["analyzed"] == 4
    assert status["drafted"] == status["reviewed"] == 2
    assert status["skipped"] == 2
    assert post_row(conn, run_id, 3)["draft_hash"] == ""
    assert post_row(conn, run_id, 4)["review_hash"] == ""


def test_all_skipped_run_needs_no_drafts_or_reviews(conn, tmp_path):
    run_id = collected_run(conn, 2)
    assert analyze(conn, run_id, tmp_path, [1, 2], skipped=(1, 2)) == {"applied": 2}
    assert export_draft_queue(conn, run_id, tmp_path / "draft-queue.jsonl") == 0
    assert draft(conn, run_id, tmp_path, []) == {"applied": 0}
    assert export_review_queue(conn, run_id, tmp_path / "review-queue.jsonl") == 0
    source = write_records(tmp_path / "reviews.jsonl", [])
    assert import_review_results(conn, run_id, source, actor="codex-reviewer") == {
        "applied": 0, "rejected": 0,
    }
    assert run_status(conn, run_id)["status"] == "stored"
