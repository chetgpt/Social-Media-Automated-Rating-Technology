from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from publish_comment_showcase import SQLiteShowcaseBackend
import tiktok_comment_showcase as showcase
import tiktok_master_database as master_state
from tiktok_scraper.analysis_workflow import publication_ai_review_hash


PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
)


JPEG_BYTES = (
    b"\xff\xd8"
    + b"\xff\xc0"
    + (17).to_bytes(2, "big")
    + b"\x08"
    + (1920).to_bytes(2, "big")
    + (1080).to_bytes(2, "big")
    + b"\x03"
    + b"\x01\x11\x00"
    + b"\x02\x11\x00"
    + b"\x03\x11\x00"
    + b"\xff\xd9"
)


def create_source(
    tmp_path: Path,
    *,
    suffix: str = "1",
    queue_status: str = "published",
    receipt_status: str = "published",
):
    database = tmp_path / f"engage-{suffix}.sqlite"
    conn = showcase.connect_database(database)
    conn.executescript(
        """
        CREATE TABLE publication_queue (
            publication_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            target_url TEXT NOT NULL,
            content_key TEXT NOT NULL,
            draft_text TEXT NOT NULL,
            draft_hash TEXT NOT NULL,
            master_attempt_id TEXT NOT NULL,
            expected_account TEXT NOT NULL,
            analysis_input_hash TEXT NOT NULL DEFAULT '',
            engage_run_id TEXT NOT NULL DEFAULT '',
            engage_post_id TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE publication_receipts (
            receipt_id TEXT PRIMARY KEY,
            publication_id TEXT NOT NULL,
            status TEXT NOT NULL,
            master_attempt_id TEXT NOT NULL,
            remote_comment_id TEXT NOT NULL,
            response_json TEXT NOT NULL
        );
        CREATE TABLE engage_tiktok_posts (
            run_id TEXT NOT NULL,
            post_id TEXT NOT NULL,
            publication_id TEXT NOT NULL,
            url TEXT NOT NULL,
            status TEXT NOT NULL,
            evidence_ready INTEGER NOT NULL,
            evidence_json TEXT NOT NULL,
            evidence_hash TEXT NOT NULL,
            analysis_json TEXT NOT NULL,
            analysis_hash TEXT NOT NULL,
            analysis_actor TEXT NOT NULL,
            analyzed_at TEXT NOT NULL,
            draft_text TEXT NOT NULL,
            draft_hash TEXT NOT NULL,
            draft_actor TEXT NOT NULL,
            drafted_at TEXT NOT NULL,
            review_json TEXT NOT NULL,
            review_hash TEXT NOT NULL,
            review_actor TEXT NOT NULL,
            reviewed_at TEXT NOT NULL,
            PRIMARY KEY (run_id, post_id)
        );
        """
    )
    publication_id = f"publication-{suffix}"
    receipt_id = f"receipt-{suffix}"
    master_attempt_id = f"master-attempt-{suffix}"
    post_id = f"700000000000000000{suffix}"
    comment_id = f"800000000000000000{suffix}"
    account = f"publisher{suffix}"
    target_url = f"https://www.tiktok.com/@creator{suffix}/video/{post_id}"
    comment_text = f"Specific, reviewed comment {suffix}. AI-assisted analysis."
    comment_hash = showcase.text_hash(comment_text)
    run_id = f"run-{suffix}"
    observed_at = "2026-07-29T11:30:00+07:00"
    analyzed_at = "2026-07-29T11:35:00+07:00"
    drafted_at = "2026-07-29T11:40:00+07:00"
    reviewed_at = "2026-07-29T11:45:00+07:00"
    evidence = {
        "schema_version": "tiktok-engage-evidence-v1",
        "platform": "tiktok",
        "topic": "fixture topic",
        "post_id": post_id,
        "url": target_url,
        "creator": f"creator{suffix}",
        "caption": f"Evidence-grounded source caption {suffix}.",
        "caption_status": "available",
        "metrics": {
            "views": 1200,
            "likes": 140,
            "reported_comments": 18,
            "shares": 12,
            "saves": 7,
            "followers": None,
        },
        "metric_availability": {
            "views": "available",
            "likes": "available",
            "reported_comments": "available",
            "shares": "available",
            "saves": "available",
            "followers": "missing_from_public_response",
        },
        "transcript": f"Stored source transcript {suffix}.",
        "transcript_status": "ok",
        "transcript_segments": [],
        "comments": [
            {
                "comment_id": f"source-thread-{suffix}",
                "text": "A useful stored audience reaction.",
                "replies": [],
            }
        ],
        "comments_status": {
            "ok": True,
            "complete": True,
            "exhausted": True,
            "limit_reached": False,
            "has_more": False,
            "error": "",
            "source": "authenticated_social_browser",
        },
        "observed_at": observed_at,
        "data_availability": {
            "caption_or_description": True,
            "transcript_or_subtitle": True,
            "comments": True,
            "creator": True,
            "publication_time": True,
            "engagement_metric": True,
        },
        "data_completeness": 100.0,
        "missing_data": [],
        "provenance": {"collector": "TikTokAPIIntegration"},
        "evidence_ready": True,
        "readiness_issues": [],
    }
    evidence_hash = showcase.json_hash(evidence)
    analysis = {
        "ai_execution_mode": "interactive_builtin_no_external_llm_api",
        "summary": f"Stored source analysis summary {suffix}.",
        "confidence": 92.0,
        "post_quality_score": 84.0,
        "conversation_value_score": 80.0,
        "analysis_score": 84.0,
        "response_type": "positive_support",
        "requested_response_type": "positive_support",
        "comment_eligible": True,
        "rating_required": True,
        "positive_eligible": True,
        "response_rationale": "The stored evidence supports this response.",
        "decision_reason": "A grounded public response adds value.",
        "evidence_refs": ["caption", "comments[0]"],
        "blocking_risk_flags": [],
        "data_completeness": 100.0,
    }
    analysis_hash = showcase.json_hash(
        {"evidence_hash": evidence_hash, "analysis": analysis}
    )
    analysis_id = showcase.stable_id(run_id, post_id, analysis_hash)
    decision_hash = showcase.json_hash(
        {"response_type": "positive_support", "publish": True}
    )
    analysis_result_hash = showcase.json_hash(
        {"analysis": analysis, "public_comment": comment_text}
    )
    review_actor = "antigravity-critic"
    review = {
        "ai_execution_mode": "interactive_builtin_no_external_llm_api",
        "approved": True,
        "independent_review": True,
        "grounding": "pass",
        "usefulness": "pass",
        "tone": "pass",
        "language": "pass",
        "ai_disclosure": "pass",
        "rating_consistency": "pass",
        "positive_only": "pass",
        "response_type": "positive_support",
        "rating_required": True,
        "analysis_id": analysis_id,
        "analysis_input_hash": evidence_hash,
        "reviewed_text_hash": comment_hash,
        "target_url": target_url,
        "content_key": post_id,
        "expected_account": account,
        "decision_hash": decision_hash,
        "analysis_result_hash": analysis_result_hash,
    }
    review_hash = publication_ai_review_hash(
        publication_id=publication_id,
        analysis_id=analysis_id,
        analysis_input_hash=evidence_hash,
        draft_hash=comment_hash,
        reviewer=review_actor,
        reviewed_at=reviewed_at,
        review_payload=review,
        target_url=target_url,
        content_key=post_id,
        expected_account=account,
        decision_hash=decision_hash,
        analysis_result_hash=analysis_result_hash,
    )
    media_path = tmp_path / f"comment-{suffix}.png"
    media_path.write_bytes(PNG_BYTES + suffix.encode("ascii"))
    media_bytes = media_path.read_bytes()
    media_hash = hashlib.sha256(media_bytes).hexdigest()
    proof = {
        "schema_version": showcase.SCREENSHOT_PROOF_SCHEMA,
        "capture_kind": "exact_comment_element",
        "unique_match": True,
        "source_post_id": post_id,
        "canonical_url": target_url,
        "remote_comment_id": comment_id,
        "comment_text_hash": comment_hash,
        "observed_account": account,
        "media_sha256": media_hash,
        "locator_strategy": "remote_comment_id",
        "captured_at": "2026-07-29T12:00:00+07:00",
    }
    response = {
        "target_url": target_url,
        "submitted_text": comment_text,
        "final_text_hash": comment_hash,
        "observed_account": account,
        "master_attempt_id": master_attempt_id,
        "remote_comment_id": comment_id,
        "exact_comment_screenshot": {
            "path": str(media_path),
            "sha256": media_hash,
            "size_bytes": len(media_bytes),
            "proof": proof,
        },
    }
    conn.execute(
        """
        INSERT INTO publication_queue (
            publication_id, status, target_url, content_key, draft_text,
            draft_hash, master_attempt_id, expected_account,
            analysis_input_hash, engage_run_id, engage_post_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            publication_id,
            queue_status,
            target_url,
            post_id,
            comment_text,
            comment_hash,
            master_attempt_id,
            account,
            evidence_hash,
            run_id,
            post_id,
        ),
    )
    conn.execute(
        """
        INSERT INTO publication_receipts (
            receipt_id, publication_id, status, master_attempt_id,
            remote_comment_id, response_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            receipt_id,
            publication_id,
            receipt_status,
            master_attempt_id,
            comment_id,
            json.dumps(response),
        ),
    )
    conn.execute(
        """
        INSERT INTO engage_tiktok_posts (
            run_id, post_id, publication_id, url, status, evidence_ready,
            evidence_json, evidence_hash, analysis_json, analysis_hash,
            analysis_actor, analyzed_at, draft_text, draft_hash,
            draft_actor, drafted_at, review_json, review_hash,
            review_actor, reviewed_at
        ) VALUES (?, ?, ?, ?, 'published', 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            post_id,
            publication_id,
            target_url,
            showcase.canonical_json(evidence),
            evidence_hash,
            showcase.canonical_json(analysis),
            analysis_hash,
            "codex-analysis",
            analyzed_at,
            comment_text,
            comment_hash,
            "codex-drafter",
            drafted_at,
            showcase.canonical_json(review),
            review_hash,
            review_actor,
            reviewed_at,
        ),
    )
    return {
        "conn": conn,
        "database": database,
        "publication_id": publication_id,
        "receipt_id": receipt_id,
        "master_attempt_id": master_attempt_id,
        "post_id": post_id,
        "comment_id": comment_id,
        "account": account,
        "target_url": target_url,
        "comment_text": comment_text,
        "comment_hash": comment_hash,
        "evidence_hash": evidence_hash,
        "media_path": media_path,
        "media_hash": media_hash,
    }


def enqueue(source):
    return showcase.enqueue_confirmed_comment(
        source["conn"],
        publication_id=source["publication_id"],
        receipt_id=source["receipt_id"],
    )


def bind_publish_media(source, job):
    media_path = source["media_path"].with_suffix(".jpg")
    media_path.write_bytes(JPEG_BYTES)
    media_bytes = media_path.read_bytes()
    media_hash = hashlib.sha256(media_bytes).hexdigest()
    source["publish_media_path"] = media_path
    source["publish_media_hash"] = media_hash
    return showcase.bind_publish_media(
        source["conn"],
        showcase_id=job["showcase_id"],
        media_path=media_path,
        media_sha256=media_hash,
        media_size_bytes=len(media_bytes),
        media_mime_type="image/jpeg",
        public_media_url=(
            "https://media.example.test/tiktok/showcases/"
            f"{job['showcase_id']}-{media_hash}.jpg"
        ),
    )


@pytest.mark.parametrize(
    "public_url",
    (
        "https://127.0.0.1/tiktok/showcases/comment.jpg",
        "https://localhost/tiktok/showcases/comment.jpg",
        "https://media.example.test:8443/tiktok/showcases/comment.jpg",
    ),
)
def test_publish_media_binding_rejects_non_public_or_nonstandard_urls(
    tmp_path,
    public_url,
):
    source = create_source(tmp_path)
    job = enqueue(source)
    media_path = source["media_path"].with_suffix(".jpg")
    media_path.write_bytes(JPEG_BYTES)
    media_hash = hashlib.sha256(JPEG_BYTES).hexdigest()

    with pytest.raises(showcase.StageGateError, match="public|standard"):
        showcase.bind_publish_media(
            source["conn"],
            showcase_id=job["showcase_id"],
            media_path=media_path,
            media_sha256=media_hash,
            media_size_bytes=len(JPEG_BYTES),
            media_mime_type="image/jpeg",
            public_media_url=public_url,
        )


def approved_review():
    return {
        "approved": True,
        "grounded": True,
        "media_verified": True,
        "privacy_safe": True,
        "caption_exact": True,
        "link_exact": True,
        "ai_disclosure": True,
        "safe": True,
        "notes": "Exact screenshot and grounded caption verified.",
    }


@pytest.mark.parametrize("command", ("claim", "submit-intent", "outcome"))
def test_unsafe_raw_publication_transitions_are_not_cli_commands(command):
    with pytest.raises(SystemExit):
        showcase.build_parser().parse_args(
            ["--database", "workflow.sqlite", command]
        )


def draft_and_review(source):
    job = enqueue(source)
    job = bind_publish_media(source, job)
    caption = (
        f"A practical takeaway from @{job['source_creator']}'s post and "
        f"our published response. Original: {job['canonical_url']}\n"
        "AI-assisted caption."
    )
    drafted = showcase.store_caption_draft(
        source["conn"],
        showcase_id=job["showcase_id"],
        caption=caption,
        drafted_by="codex-drafter",
        draft_context_id=f"draft-context-{job['showcase_id']}",
    )
    reviewed = showcase.review_caption(
        source["conn"],
        showcase_id=job["showcase_id"],
        reviewer="antigravity-critic",
        review_context_id=f"review-context-{job['showcase_id']}",
        decision=approved_review(),
    )
    return reviewed


def authorize(source, *, human: str = "Agson"):
    reviewed = draft_and_review(source)
    presentation = showcase.present_showcase(
        source["conn"],
        showcase_id=reviewed["showcase_id"],
        presented_to=human,
    )
    authorization = showcase.authorize_showcase(
        source["conn"],
        showcase_id=reviewed["showcase_id"],
        authorized_by=human,
        expected_presentation_hash=presentation["presentation_hash"],
        approval_token=presentation["approval_token"],
        expected_media_sha256=reviewed["publish_media_sha256"],
        expected_caption_hash=reviewed["caption_hash"],
        expected_review_hash=reviewed["review_hash"],
    )
    return showcase.get_showcase(source["conn"], reviewed["showcase_id"]), authorization


def test_enqueue_is_deterministic_idempotent_and_additive(tmp_path):
    source = create_source(tmp_path)
    first = enqueue(source)
    second = enqueue(source)

    assert first["created"] is True
    assert second["created"] is False
    assert first["showcase_id"] == showcase.stable_id(
        "tiktok-comment-showcase-v1",
        source["master_attempt_id"],
    )
    assert second["showcase_id"] == first["showcase_id"]
    assert second["status"] == "captured"
    assert second["media_sha256"] == source["media_hash"]
    assert (
        source["conn"]
        .execute("SELECT COUNT(*) FROM tiktok_comment_showcase_jobs")
        .fetchone()[0]
        == 1
    )
    assert (
        source["conn"]
        .execute("SELECT COUNT(*) FROM tiktok_comment_showcase_attempts")
        .fetchone()[0]
        == 0
    )


@pytest.mark.parametrize(
    ("queue_status", "receipt_status"),
    (("uncertain", "published"), ("published", "uncertain")),
)
def test_enqueue_rejects_any_unconfirmed_source(
    tmp_path,
    queue_status,
    receipt_status,
):
    source = create_source(
        tmp_path,
        queue_status=queue_status,
        receipt_status=receipt_status,
    )

    with pytest.raises(showcase.StageGateError, match="not confirmed published"):
        enqueue(source)

    assert (
        source["conn"]
        .execute("SELECT COUNT(*) FROM tiktok_comment_showcase_jobs")
        .fetchone()[0]
        == 0
    )


def test_enqueue_rejects_confirmed_comment_without_bound_engage_evidence(tmp_path):
    source = create_source(tmp_path)
    source["conn"].execute(
        "DELETE FROM engage_tiktok_posts WHERE run_id=? AND post_id=?",
        (f"run-1", source["post_id"]),
    )

    with pytest.raises(showcase.StageGateError, match="source context row is missing"):
        enqueue(source)

    assert (
        source["conn"]
        .execute("SELECT COUNT(*) FROM tiktok_comment_showcase_jobs")
        .fetchone()[0]
        == 0
    )


def test_enqueue_rejects_mismatched_master_attempt_or_capture_proof(tmp_path):
    source = create_source(tmp_path)
    source["conn"].execute(
        "UPDATE publication_receipts SET master_attempt_id='other'"
    )
    with pytest.raises(showcase.StageGateError, match="do not match"):
        enqueue(source)

    source["conn"].execute(
        "UPDATE publication_receipts SET master_attempt_id=?",
        (source["master_attempt_id"],),
    )
    response = json.loads(
        source["conn"]
        .execute("SELECT response_json FROM publication_receipts")
        .fetchone()[0]
    )
    response["exact_comment_screenshot"]["proof"]["unique_match"] = False
    source["conn"].execute(
        "UPDATE publication_receipts SET response_json=?",
        (json.dumps(response),),
    )
    with pytest.raises(showcase.StageGateError, match="not uniquely verified"):
        enqueue(source)


def test_media_tampering_blocks_every_later_gate(tmp_path):
    source = create_source(tmp_path)
    job = enqueue(source)
    job = bind_publish_media(source, job)
    source["media_path"].write_bytes(PNG_BYTES + b"tampered")
    caption = (
        f"Grounded context: {job['canonical_url']}\n"
        "AI-assisted caption."
    )

    with pytest.raises(showcase.StageGateError, match="hash changed"):
        showcase.caption_input(source["conn"], job["showcase_id"])
    with pytest.raises(showcase.StageGateError, match="hash changed"):
        showcase.store_caption_draft(
            source["conn"],
            showcase_id=job["showcase_id"],
            caption=caption,
            drafted_by="codex-drafter",
            draft_context_id="draft-context",
        )


def test_caption_review_presentation_and_authorization_are_exactly_bound(tmp_path):
    source = create_source(tmp_path)
    job = enqueue(source)
    job = bind_publish_media(source, job)
    caption = (
        f"A grounded takeaway from the reference post. {job['canonical_url']}\n"
        "AI-assisted caption."
    )
    drafted = showcase.store_caption_draft(
        source["conn"],
        showcase_id=job["showcase_id"],
        caption=caption,
        drafted_by="codex-drafter",
        draft_context_id="draft-context",
    )
    with pytest.raises(showcase.StageGateError, match="independent"):
        showcase.review_caption(
            source["conn"],
            showcase_id=job["showcase_id"],
            reviewer="antigravity-critic",
            review_context_id="draft-context",
            decision=approved_review(),
        )
    reviewed = showcase.review_caption(
        source["conn"],
        showcase_id=job["showcase_id"],
        reviewer="antigravity-critic",
        review_context_id="critic-context",
        decision=approved_review(),
    )
    assert reviewed["status"] == "reviewed"
    assert (
        json.loads(reviewed["review_json"])["source_context_hash"]
        == reviewed["source_context_hash"]
    )

    first = showcase.present_showcase(
        source["conn"],
        showcase_id=job["showcase_id"],
        presented_to="Agson",
    )
    second = showcase.present_showcase(
        source["conn"],
        showcase_id=job["showcase_id"],
        presented_to="Agson",
    )
    assert second["post_settings"] == {
        "privacy_level": "PUBLIC_TO_EVERYONE",
        "allow_comments": True,
        "auto_add_music": False,
        "brand_content": False,
        "brand_organic": False,
    }
    assert second["consent_declaration"] == (
        "By posting, you agree to TikTok's Music Usage Confirmation."
    )
    with pytest.raises(showcase.StageGateError, match="token is invalid"):
        showcase.authorize_showcase(
            source["conn"],
            showcase_id=job["showcase_id"],
            authorized_by="Agson",
            expected_presentation_hash=second["presentation_hash"],
            approval_token=first["approval_token"],
            expected_media_sha256=reviewed["publish_media_sha256"],
            expected_caption_hash=reviewed["caption_hash"],
            expected_review_hash=reviewed["review_hash"],
        )
    authorization = showcase.authorize_showcase(
        source["conn"],
        showcase_id=job["showcase_id"],
        authorized_by="Agson",
        expected_presentation_hash=second["presentation_hash"],
        approval_token=second["approval_token"],
        expected_media_sha256=reviewed["publish_media_sha256"],
        expected_caption_hash=reviewed["caption_hash"],
        expected_review_hash=reviewed["review_hash"],
    )
    stored = showcase.get_showcase(source["conn"], job["showcase_id"])
    assert stored["status"] == "authorized"
    assert stored["presentation_token_hash"] == ""
    assert authorization["media_sha256"] == source["publish_media_hash"]
    assert authorization["caption_hash"] == showcase.text_hash(caption)
    assert authorization["review_hash"] == reviewed["review_hash"]
    assert authorization["canonical_url"] == source["target_url"]
    assert authorization["posting_account"] == source["account"]
    assert authorization["post_settings"] == second["post_settings"]
    assert (
        authorization["consent_declaration"]
        == second["consent_declaration"]
    )


def test_direct_caption_change_invalidates_presented_authorization(tmp_path):
    source = create_source(tmp_path)
    reviewed = draft_and_review(source)
    shown = showcase.present_showcase(
        source["conn"],
        showcase_id=reviewed["showcase_id"],
        presented_to="Agson",
    )
    source["conn"].execute(
        """
        UPDATE tiktok_comment_showcase_jobs
        SET caption_text=caption_text || ' changed'
        WHERE showcase_id=?
        """,
        (reviewed["showcase_id"],),
    )

    with pytest.raises(showcase.StageGateError, match="caption text"):
        showcase.authorize_showcase(
            source["conn"],
            showcase_id=reviewed["showcase_id"],
            authorized_by="Agson",
            expected_presentation_hash=shown["presentation_hash"],
            approval_token=shown["approval_token"],
            expected_media_sha256=reviewed["publish_media_sha256"],
            expected_caption_hash=reviewed["caption_hash"],
            expected_review_hash=reviewed["review_hash"],
        )


def test_caption_policy_requires_disclosure_exact_url_and_utf16_limit(tmp_path):
    source = create_source(tmp_path)
    job = enqueue(source)
    job = bind_publish_media(source, job)
    with pytest.raises(showcase.StageGateError, match="AI disclosure"):
        showcase.store_caption_draft(
            source["conn"],
            showcase_id=job["showcase_id"],
            caption=f"Context: {source['target_url']}",
            drafted_by="codex-drafter",
            draft_context_id="draft-context",
        )
    with pytest.raises(showcase.StageGateError, match="exact canonical"):
        showcase.store_caption_draft(
            source["conn"],
            showcase_id=job["showcase_id"],
            caption="AI-assisted caption without its source.",
            drafted_by="codex-drafter",
            draft_context_id="draft-context",
        )
    oversized = (
        "😀" * 2000
        + f" {source['target_url']} "
        + "AI-assisted caption."
    )
    assert showcase.utf16_units(oversized) > 4000
    with pytest.raises(showcase.StageGateError, match="UTF-16"):
        showcase.store_caption_draft(
            source["conn"],
            showcase_id=job["showcase_id"],
            caption=oversized,
            drafted_by="codex-drafter",
            draft_context_id="draft-context",
        )


def test_pre_intent_failure_is_retryable_but_post_intent_failure_is_uncertain(
    tmp_path,
):
    source = create_source(tmp_path)
    job, authorization = authorize(source)
    first = showcase.claim_showcase(
        source["conn"],
        showcase_id=job["showcase_id"],
        expected_media_sha256=job["publish_media_sha256"],
        expected_caption_hash=job["caption_hash"],
        expected_authorization_hash=authorization["authorization_hash"],
    )
    retryable = showcase.record_outcome(
        source["conn"],
        showcase_id=job["showcase_id"],
        attempt_id=first["attempt_id"],
        outcome="failed",
        error="composer unavailable before click",
    )
    assert retryable["status"] == "retryable"
    assert (
        source["conn"]
        .execute(
            """
            SELECT state FROM tiktok_comment_showcase_attempts
            WHERE attempt_id=?
            """,
            (first["attempt_id"],),
        )
        .fetchone()[0]
        == "retryable"
    )

    second = showcase.claim_showcase(
        source["conn"],
        showcase_id=job["showcase_id"],
        expected_media_sha256=job["publish_media_sha256"],
        expected_caption_hash=job["caption_hash"],
        expected_authorization_hash=authorization["authorization_hash"],
    )
    showcase.mark_submit_intent(
        source["conn"],
        showcase_id=job["showcase_id"],
        attempt_id=second["attempt_id"],
    )
    uncertain = showcase.record_outcome(
        source["conn"],
        showcase_id=job["showcase_id"],
        attempt_id=second["attempt_id"],
        outcome="failed",
        error="browser closed after click",
    )
    assert uncertain["status"] == "uncertain"
    with pytest.raises(showcase.StageGateError, match="cannot be claimed"):
        showcase.claim_showcase(
            source["conn"],
            showcase_id=job["showcase_id"],
            expected_media_sha256=job["publish_media_sha256"],
            expected_caption_hash=job["caption_hash"],
            expected_authorization_hash=authorization["authorization_hash"],
        )

    remote_id = "9000000000000000001"
    confirmed = showcase.record_outcome(
        source["conn"],
        showcase_id=job["showcase_id"],
        attempt_id=second["attempt_id"],
        outcome="confirmed",
        remote_post_id=remote_id,
        remote_post_url=(
            f"https://www.tiktok.com/@{source['account']}/video/{remote_id}"
        ),
        visible=True,
        persisted=True,
        response={"reconciled": True},
    )
    assert confirmed["status"] == "published"
    stored = showcase.get_showcase(source["conn"], job["showcase_id"])
    assert stored["status"] == "published"
    assert stored["remote_post_id"] == remote_id
    assert stored["attempt_count"] == 2


def test_published_outcome_requires_durable_intent_and_exact_remote_account(
    tmp_path,
):
    source = create_source(tmp_path)
    job, authorization = authorize(source)
    claim = showcase.claim_showcase(
        source["conn"],
        showcase_id=job["showcase_id"],
        expected_media_sha256=job["publish_media_sha256"],
        expected_caption_hash=job["caption_hash"],
        expected_authorization_hash=authorization["authorization_hash"],
    )
    remote_id = "9000000000000000002"
    with pytest.raises(showcase.StageGateError, match="submit intent"):
        showcase.record_outcome(
            source["conn"],
            showcase_id=job["showcase_id"],
            attempt_id=claim["attempt_id"],
            outcome="published",
            remote_post_id=remote_id,
            remote_post_url=(
                f"https://www.tiktok.com/@{source['account']}/video/{remote_id}"
            ),
            visible=True,
            persisted=True,
        )
    showcase.mark_submit_intent(
        source["conn"],
        showcase_id=job["showcase_id"],
        attempt_id=claim["attempt_id"],
    )
    with pytest.raises(showcase.StageGateError, match="authorized account"):
        showcase.record_outcome(
            source["conn"],
            showcase_id=job["showcase_id"],
            attempt_id=claim["attempt_id"],
            outcome="published",
            remote_post_id=remote_id,
            remote_post_url=(
                f"https://www.tiktok.com/@another-account/video/{remote_id}"
            ),
            visible=True,
            persisted=True,
        )


def test_real_sqlite_backend_updates_local_and_master_hooks_atomically(tmp_path):
    source = create_source(tmp_path)
    master_database = tmp_path / "master.sqlite"
    schema = master_state.attach_master_database(
        source["conn"],
        master_database,
    )
    source["conn"].execute("BEGIN IMMEDIATE")
    comment_attempt_id = master_state.register_publication_claim(
        source["conn"],
        schema,
        account=source["account"],
        post_id=source["post_id"],
        publication_id=source["publication_id"],
        source_path=source["database"],
        target_url=source["target_url"],
        text_hash=source["comment_hash"],
        local_attempt_number=1,
    )
    master_state.mark_submit_intent(
        source["conn"],
        schema,
        comment_attempt_id,
    )
    master_state.register_publication_outcome(
        source["conn"],
        schema,
        attempt_id=comment_attempt_id,
        outcome="confirmed",
        receipt_id=source["receipt_id"],
        remote_comment_id=source["comment_id"],
        visible=True,
        persisted=True,
        text_hash=source["comment_hash"],
    )
    receipt = source["conn"].execute(
        "SELECT response_json FROM publication_receipts WHERE receipt_id=?",
        (source["receipt_id"],),
    ).fetchone()
    response = json.loads(receipt[0])
    response["master_attempt_id"] = comment_attempt_id
    source["conn"].execute(
        "UPDATE publication_queue SET master_attempt_id=? "
        "WHERE publication_id=?",
        (comment_attempt_id, source["publication_id"]),
    )
    source["conn"].execute(
        "UPDATE publication_receipts SET master_attempt_id=?, response_json=? "
        "WHERE receipt_id=?",
        (
            comment_attempt_id,
            showcase.canonical_json(response),
            source["receipt_id"],
        ),
    )
    source["conn"].commit()
    job, _authorization = authorize(source)
    source["conn"].close()

    backend = SQLiteShowcaseBackend(
        database=source["database"],
        master_database=master_database,
    )
    try:
        first_prepared = backend.prepare(job["showcase_id"])
        first_claim = backend.claim(first_prepared)
        released = backend.record_outcome(
            first_claim,
            outcome="pre_submit_failed",
            error="simulated crash before submit intent",
        )
        assert released["status"] == "retryable"

        second_prepared = backend.prepare(job["showcase_id"])
        second_claim = backend.claim(second_prepared)
        intended = backend.mark_submit_intent(second_claim)
        assert intended["status"] == "submit_intent"
        checkpoint = backend.checkpoint_publish_id(
            second_claim,
            "v_pub_url~v2.123456789",
        )
        assert checkpoint["publish_id"] == "v_pub_url~v2.123456789"
        local_checkpoint = backend.conn.execute(
            """
            SELECT publish_id, publish_id_checkpoint_at
            FROM tiktok_comment_showcase_attempts
            WHERE attempt_id=?
            """,
            (second_claim.attempt_id,),
        ).fetchone()
        master_checkpoint = backend.conn.execute(
            f"""
            SELECT publish_id, publish_id_checkpoint_at
            FROM "{backend.master_schema}".tiktok_master_showcase_attempts
            WHERE attempt_id=?
            """,
            (second_claim.master_attempt_id,),
        ).fetchone()
        assert local_checkpoint[0] == "v_pub_url~v2.123456789"
        assert master_checkpoint[0] == "v_pub_url~v2.123456789"
        assert local_checkpoint[1]
        assert master_checkpoint[1] == local_checkpoint[1]
        uncertain = backend.record_outcome(
            second_claim,
            outcome="uncertain",
            error="simulated interruption after submit intent",
        )
        assert uncertain["status"] == "uncertain"
        interrupted = backend.active_attempt(job["showcase_id"])
        assert interrupted["publish_id"] == "v_pub_url~v2.123456789"
        assert interrupted["response"] == {}
        master_guard = master_state.showcase_target_guard(
            backend.conn,
            backend.master_schema,
            source_comment_attempt_id=comment_attempt_id,
        )
        assert master_guard["blocked"] is True
        assert master_guard["state"] == "uncertain"
    finally:
        backend.close()
