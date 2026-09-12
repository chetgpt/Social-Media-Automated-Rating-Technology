import asyncio
import copy
import hashlib
import json

import pytest

from engage_tiktok import (
    StageGateError,
    collect_exact,
    connect_database,
    create_run,
    export_analysis_queue,
    export_draft_queue,
    export_reclassification_queue,
    export_review_queue,
    import_analysis_results,
    import_draft_results,
    import_reclassification_results,
    import_review_results,
    present_response,
    response_rating_metadata,
    run_status,
    verify_rendered_response,
)
from tiktok_publication_adapter import prepare_final_publication_text


CONSTRUCTIVE_TYPES = (
    "constructive_suggestion",
    "constructive_correction",
    "clarifying_question",
)


def write_jsonl(path, rows):
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def read_jsonl(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def evidence(post_id):
    return {
        "id": str(post_id),
        "url": f"https://www.tiktok.com/@creator/video/{post_id}",
        "username": "creator",
        "caption": f"Useful but incomplete post {post_id}",
        "view_count": 100,
        "like_count": 10,
        "comment_count": 1,
        "share_count": 2,
        "transcript": "",
        "transcript_status": "unavailable",
        "subtitle_no_caption_reason": "not_provided",
        "comments": [
            {
                "cid": f"comment-{post_id}",
                "text": "Could the creator clarify the source?",
            }
        ],
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


class ReadyPreflight:
    async def ensure_ready(self):
        return {
            "reachable": True,
            "tiktok_authenticated": True,
            "observed_account": "creator",
            "checked_at": "2026-07-28T10:00:00+07:00",
        }


class StaticCollector:
    def __init__(self, records):
        self.records = records

    async def collect(self, **_kwargs):
        return list(self.records)


def collected_run(conn, *, requested=1, mode="shadow", run_id="run"):
    create_run(
        conn,
        project="constructive_test",
        topic="AI tools",
        requested_count=requested,
        max_comments=20,
        max_pages=10,
        mode=mode,
        music_catalogs=(),
        run_id=run_id,
    )
    asyncio.run(
        collect_exact(
            conn,
            run_id=run_id,
            preflight=ReadyPreflight(),
            collector=StaticCollector(
                [evidence(index) for index in range(1, requested + 1)]
            ),
        )
    )
    return run_id


def constructive_analysis(response_type):
    correction_target = (
        "The post presents an unqualified claim."
        if response_type == "constructive_correction"
        else ""
    )
    return {
        "summary": "The post has a useful idea but needs one grounded addition.",
        "post_quality_score": 55,
        "conversation_value_score": 75,
        "confidence": 92,
        "positive_eligible": False,
        "response_type": response_type,
        "response_opportunity_score": 91,
        "grounding_confidence": 95,
        "response_objective": "Add a specific, respectful public-usefulness point.",
        "response_rationale": "The stored caption and comment identify a clear gap.",
        "correction_target": correction_target,
        "blocking_risk_flags": [],
        "decision_reason": "A constructive response adds value without endorsement.",
        "novel_value": "It adds a verification or implementation detail.",
        "strength": "The post introduces a practical topic.",
        "recommendation": "Offer one grounded improvement.",
        "evidence_refs": ["caption", "comment-1"],
        "skip_reason": "",
    }


def positive_analysis():
    return {
        "summary": "The post and discussion are useful.",
        "post_quality_score": 80,
        "conversation_value_score": 100,
        "confidence": 90,
        "positive_eligible": True,
        "response_type": "positive_support",
        "decision_reason": "The post passes the positive support gate.",
        "novel_value": "It adds a concrete supporting detail.",
        "strength": "The explanation is clear.",
        "recommendation": "Add one practical example.",
        "evidence_refs": ["caption", "comment-1"],
        "skip_reason": "",
    }


def skip_analysis():
    return {
        "summary": "The evidence does not support a safe public contribution.",
        "post_quality_score": 35,
        "conversation_value_score": 25,
        "confidence": 90,
        "positive_eligible": False,
        "response_type": "skip",
        "response_opportunity_score": 20,
        "grounding_confidence": 60,
        "response_objective": "",
        "response_rationale": "",
        "correction_target": "",
        "blocking_risk_flags": [],
        "decision_reason": "There is not enough grounded value to comment.",
        "novel_value": "No safe novel value is available.",
        "strength": "The topic is identifiable.",
        "recommendation": "Do not comment.",
        "evidence_refs": ["caption"],
        "skip_reason": "insufficient_grounded_response_value",
    }


def import_analyses(conn, run_id, tmp_path, analyses):
    queue_path = tmp_path / f"{run_id}-analysis-queue.jsonl"
    assert export_analysis_queue(conn, run_id, queue_path) == len(analyses)
    queue = read_jsonl(queue_path)
    result_path = tmp_path / f"{run_id}-analysis-results.jsonl"
    write_jsonl(
        result_path,
        [
            {
                "post_id": task["post_id"],
                "evidence_hash": task["evidence_hash"],
                "analysis": analysis,
            }
            for task, analysis in zip(queue, analyses, strict=True)
        ],
    )
    return import_analysis_results(
        conn,
        run_id,
        result_path,
        actor="codex-constructive-analysis",
    )


def review_result(task):
    return {
        "post_id": task["post_id"],
        "evidence_hash": task["evidence_hash"],
        "analysis_hash": task["analysis_hash"],
        "draft_hash": task["draft_hash"],
        "target_url": task["target_url"],
        "content_key": task["content_key"],
        "expected_account": task["expected_account"],
        "decision_hash": task["decision_hash"],
        "analysis_result_hash": task["analysis_result_hash"],
        "response_type": task["response_type"],
        "independent_review": True,
        "approved": True,
        "grounding": "pass",
        "usefulness": "pass",
        "tone": "pass",
        "language": "pass",
        "ai_disclosure": "pass",
        "rating_policy": "pass",
        "response_type_policy": "pass",
        "constructive_safety": "pass",
        "unsupported_claims": [],
        "issues": [],
    }


def test_constructive_suggestion_drafts_reviews_stores_without_rating(tmp_path):
    conn = connect_database(tmp_path / "state.sqlite")
    try:
        run_id = collected_run(conn, mode="live")
        assert import_analyses(
            conn,
            run_id,
            tmp_path,
            [constructive_analysis("constructive_suggestion")],
        ) == {"applied": 1}

        draft_queue_path = tmp_path / "draft-queue.jsonl"
        assert export_draft_queue(conn, run_id, draft_queue_path) == 1
        draft_task = read_jsonl(draft_queue_path)[0]
        assert draft_task["response_type"] == "constructive_suggestion"
        assert draft_task["rating_required"] is False
        assert "practical improvement" in draft_task["prompt"]

        draft_path = tmp_path / "draft-result.jsonl"
        draft_text = (
            "AI-assisted suggestion: naming the current usage and privacy limits "
            "would make this recommendation easier for viewers to evaluate."
        )
        write_jsonl(
            draft_path,
            [
                {
                    "post_id": draft_task["post_id"],
                    "evidence_hash": draft_task["evidence_hash"],
                    "analysis_hash": draft_task["analysis_hash"],
                    "response_type": "constructive_suggestion",
                    "draft_text": draft_text,
                }
            ],
        )
        assert import_draft_results(
            conn,
            run_id,
            draft_path,
            actor="codex-constructive-drafter",
        ) == {"applied": 1}

        review_queue_path = tmp_path / "review-queue.jsonl"
        assert export_review_queue(conn, run_id, review_queue_path) == 1
        review_task = read_jsonl(review_queue_path)[0]
        assert review_task["rating_required"] is False
        assert review_task["publication_decision"]["public_rating"] == {
            "enabled": False,
            "required": False,
            "scale": None,
            "increment": None,
            "value": None,
            "formatted": "",
        }
        review_path = tmp_path / "review-result.jsonl"
        write_jsonl(review_path, [review_result(review_task)])
        assert import_review_results(
            conn,
            run_id,
            review_path,
            actor="codex-independent-constructive-reviewer",
        ) == {"applied": 1, "rejected": 0}

        post = conn.execute(
            "SELECT * FROM engage_tiktok_posts WHERE run_id=?",
            (run_id,),
        ).fetchone()
        assert post["status"] == "reviewed"
        assert post["draft_text"] == draft_text
        assert "/10" not in post["draft_text"]
        assert run_status(conn, run_id)["status"] == "stored"
        empty_reclassification_path = tmp_path / "empty-reclassification.jsonl"
        assert (
            export_reclassification_queue(
                conn,
                run_id,
                empty_reclassification_path,
            )
            == 0
        )
        assert run_status(conn, run_id)["status"] == "stored"

        presentation = present_response(
            conn,
            run_id,
            post["post_id"],
            presented_to="human@example",
        )
        assert presentation["schema_version"] == "tiktok-engage-presentation-v2"
        assert presentation["response_type"] == "constructive_suggestion"
        assert presentation["rating_required"] is False
        assert presentation["public_rating"] is None
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("response_type", "prompt_phrase"),
    [
        ("constructive_suggestion", "practical improvement"),
        ("constructive_correction", "qualify uncertainty"),
        ("clarifying_question", "good-faith clarifying question"),
    ],
)
def test_each_constructive_type_exports_its_unrated_policy_prompt(
    tmp_path,
    response_type,
    prompt_phrase,
):
    conn = connect_database(tmp_path / "state.sqlite")
    try:
        run_id = collected_run(conn)
        import_analyses(
            conn,
            run_id,
            tmp_path,
            [constructive_analysis(response_type)],
        )
        queue_path = tmp_path / "drafts.jsonl"
        assert export_draft_queue(conn, run_id, queue_path) == 1
        task = read_jsonl(queue_path)[0]
        assert task["response_type"] == response_type
        assert task["rating_required"] is False
        assert prompt_phrase in task["prompt"]
        assert "Do not include any numeric rating" in task["prompt"]
    finally:
        conn.close()


@pytest.mark.parametrize(
    "draft_text",
    [
        (
            "AI-assisted suggestion: add the privacy limitation before "
            "{PUBLIC_RATING} so viewers can evaluate the tool."
        ),
        (
            "AI-assisted suggestion: add the privacy limitation; this would "
            "otherwise read as a 7/10 recommendation."
        ),
        (
            "AI-assisted suggestion: add the privacy limitation; I would rate "
            "the current explanation eight out of ten."
        ),
        (
            "AI-assisted suggestion: add the privacy limitation; the current "
            "explanation earns four stars."
        ),
    ],
)
def test_constructive_draft_rejects_placeholder_and_literal_rating(
    tmp_path,
    draft_text,
):
    conn = connect_database(tmp_path / "state.sqlite")
    try:
        run_id = collected_run(conn)
        import_analyses(
            conn,
            run_id,
            tmp_path,
            [constructive_analysis("constructive_suggestion")],
        )
        queue_path = tmp_path / "drafts.jsonl"
        export_draft_queue(conn, run_id, queue_path)
        task = read_jsonl(queue_path)[0]
        results = tmp_path / "draft-results.jsonl"
        write_jsonl(
            results,
            [
                {
                    "post_id": task["post_id"],
                    "evidence_hash": task["evidence_hash"],
                    "analysis_hash": task["analysis_hash"],
                    "response_type": task["response_type"],
                    "draft_text": draft_text,
                }
            ],
        )
        with pytest.raises(StageGateError, match="rating"):
            import_draft_results(
                conn,
                run_id,
                results,
                actor="codex-constructive-drafter",
            )
    finally:
        conn.close()


def test_positive_support_still_requires_rating_placeholder(tmp_path):
    conn = connect_database(tmp_path / "state.sqlite")
    try:
        run_id = collected_run(conn)
        import_analyses(conn, run_id, tmp_path, [positive_analysis()])
        queue_path = tmp_path / "drafts.jsonl"
        export_draft_queue(conn, run_id, queue_path)
        task = read_jsonl(queue_path)[0]
        assert task["rating_required"] is True
        results = tmp_path / "draft-results.jsonl"
        write_jsonl(
            results,
            [
                {
                    "post_id": task["post_id"],
                    "evidence_hash": task["evidence_hash"],
                    "analysis_hash": task["analysis_hash"],
                    "response_type": "positive_support",
                    "draft_text": (
                        "AI-assisted take: the practical example makes this useful."
                    ),
                }
            ],
        )
        with pytest.raises(StageGateError, match="PUBLIC_RATING.*exactly once"):
            import_draft_results(
                conn,
                run_id,
                results,
                actor="codex-positive-drafter",
            )
    finally:
        conn.close()


def test_topic_only_ai_mention_is_not_an_ai_disclosure():
    rating = response_rating_metadata("constructive_suggestion", 55)
    with pytest.raises(StageGateError, match="explicit AI disclosure"):
        verify_rendered_response(
            "AI tools need clearer privacy and retention limits for users.",
            rating,
        )


@pytest.mark.parametrize(
    "disclosure",
    [
        "[AI]",
        "[Ulasan dibuat oleh AI]",
        "Penilaian berbantuan AI:",
    ],
)
def test_explicit_legacy_ai_disclosure_markers_remain_valid(disclosure):
    rating = response_rating_metadata("constructive_suggestion", 55)
    verify_rendered_response(
        f"Add the current plan limits so viewers can verify the claim. {disclosure}",
        rating,
    )


def test_unrated_constructive_response_allows_a_nonrating_fraction():
    rating = response_rating_metadata("constructive_suggestion", 55)
    verify_rendered_response(
        "AI-assisted perspective: specify whether the test used a "
        "1/2-inch nozzle so viewers can reproduce it.",
        rating,
    )


def test_true_skip_is_absent_from_draft_queue(tmp_path):
    conn = connect_database(tmp_path / "state.sqlite")
    try:
        run_id = collected_run(conn, requested=2)
        import_analyses(
            conn,
            run_id,
            tmp_path,
            [
                constructive_analysis("constructive_suggestion"),
                skip_analysis(),
            ],
        )
        queue_path = tmp_path / "drafts.jsonl"
        assert export_draft_queue(conn, run_id, queue_path) == 1
        tasks = read_jsonl(queue_path)
        assert [task["post_id"] for task in tasks] == ["1"]
        assert run_status(conn, run_id)["skipped"] == 1
    finally:
        conn.close()


def test_positive_support_with_blocking_risk_is_skipped(tmp_path):
    conn = connect_database(tmp_path / "state.sqlite")
    try:
        run_id = collected_run(conn)
        analysis = positive_analysis()
        analysis["blocking_risk_flags"] = ["material_safety_risk"]
        import_analyses(conn, run_id, tmp_path, [analysis])
        post = conn.execute(
            "SELECT status, analysis_json FROM engage_tiktok_posts WHERE run_id=?",
            (run_id,),
        ).fetchone()
        normalized = json.loads(post["analysis_json"])
        assert post["status"] == "skipped"
        assert normalized["response_type"] == "skip"
        assert normalized["positive_eligible"] is False
        assert normalized["blocking_risk_flags"] == ["material_safety_risk"]
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("missing_field", "message"),
    [
        (
            "response_opportunity_score",
            "requires response_opportunity_score",
        ),
        ("grounding_confidence", "requires grounding_confidence"),
    ],
)
def test_constructive_analysis_requires_separate_scores(
    tmp_path,
    missing_field,
    message,
):
    conn = connect_database(tmp_path / "state.sqlite")
    try:
        run_id = collected_run(conn)
        analysis = constructive_analysis("constructive_suggestion")
        analysis.pop(missing_field)
        with pytest.raises(StageGateError, match=message):
            import_analyses(conn, run_id, tmp_path, [analysis])
        assert run_status(conn, run_id)["analyzed"] == 0
    finally:
        conn.close()


def initial_skipped_run(conn, tmp_path, *, requested=3, run_id="reclass-run"):
    collected_run(conn, requested=requested, run_id=run_id)
    import_analyses(
        conn,
        run_id,
        tmp_path,
        [skip_analysis() for _ in range(requested)],
    )
    return run_id


def reclassification_records(queue, response_types):
    return [
        {
            "post_id": task["post_id"],
            "evidence_hash": task["evidence_hash"],
            "prior_analysis_hash": task["prior_analysis_hash"],
            "analysis": (
                constructive_analysis(response_type)
                if response_type != "skip"
                else skip_analysis()
            ),
        }
        for task, response_type in zip(queue, response_types, strict=True)
    ]


def test_reclassification_requires_and_applies_the_exact_full_skipped_batch(
    tmp_path,
):
    conn = connect_database(tmp_path / "state.sqlite")
    try:
        run_id = initial_skipped_run(conn, tmp_path)
        queue_path = tmp_path / "reclassification-queue.jsonl"
        assert export_reclassification_queue(conn, run_id, queue_path) == 3
        queue = read_jsonl(queue_path)
        results = reclassification_records(queue, CONSTRUCTIVE_TYPES)
        result_path = tmp_path / "reclassification-results.jsonl"
        write_jsonl(result_path, results)

        assert import_reclassification_results(
            conn,
            run_id,
            result_path,
            actor="codex-constructive-reclassifier",
        ) == {
            "applied": 3,
            "response_eligible": 3,
            "remaining_skipped": 0,
        }
        status = run_status(conn, run_id)
        assert status["status"] == "analysis_complete"
        assert status["analyzed"] == 3
        assert status["skipped"] == 0
        draft_path = tmp_path / "drafts.jsonl"
        assert export_draft_queue(conn, run_id, draft_path) == 3
        assert {
            task["response_type"] for task in read_jsonl(draft_path)
        } == set(CONSTRUCTIVE_TYPES)
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("wrong_evidence", "input hash mismatch"),
        ("partial", "every currently skipped post exactly once"),
        ("duplicate", "every currently skipped post exactly once"),
        ("stale_prior_hash", "input hash mismatch"),
        ("malformed_later_score", "response_opportunity_score must be numeric"),
        ("malformed_risk_flags", "blocking_risk_flags must be a JSON array"),
        (
            "missing_explicit_objective",
            "explicit response objective and rationale",
        ),
    ],
)
def test_reclassification_rejects_wrong_partial_duplicate_or_stale_inputs(
    tmp_path,
    mutation,
    message,
):
    conn = connect_database(tmp_path / "state.sqlite")
    try:
        run_id = initial_skipped_run(conn, tmp_path, requested=2)
        queue_path = tmp_path / "reclassification-queue.jsonl"
        export_reclassification_queue(conn, run_id, queue_path)
        queue = read_jsonl(queue_path)
        records = reclassification_records(
            queue,
            ("constructive_suggestion", "clarifying_question"),
        )
        if mutation == "wrong_evidence":
            records[1]["evidence_hash"] = "wrong"
        elif mutation == "partial":
            records = records[:1]
        elif mutation == "duplicate":
            records[1]["post_id"] = records[0]["post_id"]
        elif mutation == "stale_prior_hash":
            records[1]["prior_analysis_hash"] = "stale"
        elif mutation == "malformed_later_score":
            records[1]["analysis"]["response_opportunity_score"] = "not-a-score"
        elif mutation == "malformed_risk_flags":
            records[1]["analysis"]["blocking_risk_flags"] = "unsafe"
        else:
            records[1]["analysis"].pop("response_objective")
        result_path = tmp_path / "reclassification-results.jsonl"
        write_jsonl(result_path, records)

        with pytest.raises(StageGateError, match=message):
            import_reclassification_results(
                conn,
                run_id,
                result_path,
                actor="codex-constructive-reclassifier",
            )
        assert run_status(conn, run_id)["skipped"] == 2
    finally:
        conn.close()


def test_zero_new_eligible_reclassification_returns_run_to_stored(tmp_path):
    conn = connect_database(tmp_path / "state.sqlite")
    try:
        run_id = initial_skipped_run(conn, tmp_path, requested=2)
        queue_path = tmp_path / "reclassification-queue.jsonl"
        export_reclassification_queue(conn, run_id, queue_path)
        queue = read_jsonl(queue_path)
        results_path = tmp_path / "reclassification-results.jsonl"
        write_jsonl(
            results_path,
            reclassification_records(queue, ("skip", "skip")),
        )
        assert import_reclassification_results(
            conn,
            run_id,
            results_path,
            actor="codex-constructive-reclassifier",
        ) == {
            "applied": 2,
            "response_eligible": 0,
            "remaining_skipped": 2,
        }
        assert run_status(conn, run_id)["status"] == "stored"
        draft_path = tmp_path / "drafts.jsonl"
        assert export_draft_queue(conn, run_id, draft_path) == 0
        assert run_status(conn, run_id)["status"] == "stored"
    finally:
        conn.close()


def constructive_publication(
    *,
    text=(
        "AI-assisted suggestion: state the privacy limitation so viewers can "
        "evaluate the recommendation safely."
    ),
):
    decision = {
        "publish": True,
        "score": 55,
        "assessment": "constructive",
        "response_type": "constructive_suggestion",
        "response_eligible": True,
        "positive_eligible": False,
        "constructive_eligible": True,
        "rating_required": False,
        "policy_violations": [],
        "public_rating": {
            "enabled": False,
            "required": False,
            "scale": None,
            "increment": None,
            "value": None,
            "formatted": "",
        },
    }
    return {
        "analysis_score": 55,
        "draft_text": text,
        "draft_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "decision_json": json.dumps(decision),
    }


def test_adapter_accepts_valid_rating_free_constructive_text():
    publication = constructive_publication()
    final_text, final_hash, rating = prepare_final_publication_text(publication)
    assert final_text == publication["draft_text"]
    assert final_hash == publication["draft_hash"]
    assert rating == ""


def test_adapter_allows_nonrating_fraction_in_constructive_text():
    publication = constructive_publication(
        text=(
            "AI-assisted perspective: specify whether the test used a "
            "1/2-inch nozzle so viewers can reproduce it."
        )
    )
    final_text, _, rating = prepare_final_publication_text(publication)
    assert "1/2-inch" in final_text
    assert rating == ""


def test_adapter_rejects_topic_only_ai_mention_as_disclosure():
    publication = constructive_publication(
        text="AI tools need clearer privacy and retention limits for users."
    )
    with pytest.raises(RuntimeError, match="explicit AI disclosure"):
        prepare_final_publication_text(publication)


@pytest.mark.parametrize(
    "mutation",
    [
        "rating_metadata",
        "rating_text",
        "rating_out_of_ten",
        "rating_stars",
    ],
)
def test_adapter_rejects_constructive_rating_metadata_or_text(mutation):
    publication = constructive_publication()
    if mutation == "rating_metadata":
        decision = json.loads(publication["decision_json"])
        decision["public_rating"] = {
            "enabled": True,
            "required": True,
            "scale": 10,
            "increment": 0.1,
            "value": 5.5,
            "formatted": "5.5/10",
        }
        publication["decision_json"] = json.dumps(decision)
    elif mutation == "rating_text":
        publication["draft_text"] += " Rating: 5.5/10."
        publication["draft_hash"] = hashlib.sha256(
            publication["draft_text"].encode("utf-8")
        ).hexdigest()
    elif mutation == "rating_out_of_ten":
        publication["draft_text"] += " I rate it eight out of ten."
        publication["draft_hash"] = hashlib.sha256(
            publication["draft_text"].encode("utf-8")
        ).hexdigest()
    else:
        publication["draft_text"] += " It earns four stars."
        publication["draft_hash"] = hashlib.sha256(
            publication["draft_text"].encode("utf-8")
        ).hexdigest()

    with pytest.raises(RuntimeError, match="Constructive publication"):
        prepare_final_publication_text(publication)
