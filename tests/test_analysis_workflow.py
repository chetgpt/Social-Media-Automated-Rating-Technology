import asyncio
import datetime as dt
import hashlib
import json
import sqlite3

import pytest

from campaign_spec import normalize_analysis_config
from comment_publication_capture import (
    PublicationCapture,
    looks_like_comment_write,
    redact_headers,
    redact_url,
    request_body_template,
)
from incremental_project import ingest_videos, init_db
from tiktok_publication_adapter import (
    is_tiktok_comment_publish_url,
    load_approved_publication,
    response_success,
)
from tiktok_scraper.analysis_workflow import (
    apply_analysis_result,
    authorize_publication,
    export_analysis_artifacts,
    load_analysis_lookup,
    queue_publication_drafts,
    record_publication_ai_review,
    sync_analysis_queue,
)


def memory_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def test_hybrid_analysis_defaults_are_publication_safe():
    config = normalize_analysis_config({"enabled": True})
    publication = config["publication"]
    assert config["analysis_version"] == "native-text-v4"
    assert config["scoring"]["formula_version"] == "social-review-v1"
    assert sum(item["weight"] for item in config["scoring"]["dimensions"]) == 100
    assert sum(
        item["weight"] for item in config["scoring"]["conversation_dimensions"]
    ) == 100
    assert config["scoring"]["post_weight"] == 80
    assert config["scoring"]["conversation_weight"] == 20
    assert config["scoring"]["max_conversation_adjustment"] == 10
    assert config["scoring"]["require_core_content_for_publication"] is True
    assert config["scoring"]["video_requires_transcript_or_subtitle"] is True
    assert publication["positive_only"] is True
    assert publication["min_public_score"] == 70
    assert publication["recommendation_only"] is True
    assert publication["require_strength_and_recommendation"] is True
    assert publication["require_score"] is True
    assert publication["require_publication_decision"] is True
    assert publication["require_novel_value"] is True
    assert publication["require_evidence_refs"] is True
    assert publication["require_fresh_evidence"] is True
    assert publication["min_confidence"] == 75
    assert publication["min_data_completeness"] == 70
    assert publication["allow_public_scores"] is True
    assert publication["public_rating"]["enabled"] is True
    assert publication["public_rating"]["required"] is True
    assert publication["public_rating"]["scale"] == 10
    assert publication["public_rating"]["increment"] == 0.1
    assert publication["language"]["mode"] == "auto"
    assert publication["language"]["default"] == "id"
    assert publication["allow_internal_diagnostics"] is False


def analysis_config():
    return normalize_analysis_config({
        "enabled": True,
        "mode": "shadow",
        "provider": "manual",
        "analysis_version": "native-text-v4",
        "max_comments_per_post": 50,
        "scoring": {
            "formula_version": "test-v1",
            "dimensions": [
                {"key": "factual_quality", "weight": 3},
                {"key": "usefulness", "weight": 1},
            ],
        },
        "publication": {
            "mode": "shadow",
            "require_approval": True,
            "require_score": True,
            "require_fresh_evidence": False,
            "min_confidence": 70,
            "min_data_completeness": 50,
        },
    })


def seed_video(conn):
    return ingest_videos(
        conn,
        project="analysis_project",
        keyword="public health",
        platform="youtube",
        run_id="run-1",
        scraped_at="2026-07-22T09:00:00+07:00",
        videos=[{
            "video_id": "video-1",
            "url": "https://www.youtube.com/watch?v=video-1",
            "content_creator": "Example Channel",
            "caption": "A public health explainer",
            "description": "An explanation with references.",
            "published_at": "2026-07-21T12:00:00+07:00",
            "view_count": 100,
            "like_count": 10,
            "reported_comment_count": 1,
            "transcript": "This is the platform-provided transcript.",
            "transcript_available": True,
            "transcript_language": "en",
            "transcript_source": "youtube-transcript-api",
            "comments": [{
                "id": "comment-1",
                "author": "Viewer",
                "text": "What evidence supports this claim?",
                "likes": 2,
            }],
        }],
    )


def social_review_scores(value=80):
    return {
        "integrity_and_evidence": value,
        "substance_and_audience_value": value,
        "clarity_and_craft": value,
        "context_and_completeness": value,
        "originality_and_perspective": value,
        "audience_fit": value,
        "responsibility_and_safety": value,
    }


def conversation_value_scores(value=80):
    return {
        "relevant_contribution": value,
        "specific_useful_information": value,
        "meaningful_perspective_diversity": value,
        "question_resolution": value,
        "creator_responsiveness": value,
        "civility_and_responsibility": value,
    }


def test_native_text_queue_analysis_completion_and_stale_detection(tmp_path):
    conn = memory_db()
    seed_video(conn)
    config = analysis_config()

    first = sync_analysis_queue(conn, "analysis_project", config)
    assert first["queued"] == 1
    pending = conn.execute("SELECT * FROM analysis_runs").fetchone()
    packet = json.loads(pending["evidence_json"])
    assert packet["native_text_only"] is True
    assert packet["media_processing_used"] is False
    assert packet["data_completeness"] == 100
    assert {item["type"] for item in packet["native_text_surfaces"]} >= {
        "creator_caption",
        "platform_transcript",
    }
    assert packet["detected_questions"][0]["comment_id"] == "comment-1"

    second = sync_analysis_queue(conn, "analysis_project", config)
    assert second["unchanged"] == 1
    assert conn.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 1

    result = apply_analysis_result(
        conn,
        pending["analysis_id"],
        {
            "internal_analysis": {
                "summary": "The post explains a public-health claim.",
                "language": "en",
                "claims": [{
                    "claim_id": "claim-1",
                    "claim": "A factual assertion",
                    "verdict": "supported",
                    "confidence": 80,
                    "reason": "The post says so, but no external source was supplied.",
                    "sources": [],
                }],
                "comment_questions": [{
                    "comment_id": "comment-1",
                    "question": "What evidence supports this claim?",
                    "answer": "External evidence is still required.",
                    "status": "unknown",
                    "sources": [],
                }],
                "missing_information": ["An external primary source"],
                "dimension_scores": {"factual_quality": 80, "usefulness": 60},
                "ai_recommended_score": 75,
                "confidence": 82,
                "score_rationale": "Weighted rubric result.",
            },
            "publication_decision": {
                "publish": True,
                "assessment": "positive",
                "reason": "The comment identifies an unresolved evidence gap.",
                "value_type": "useful_gap",
                "novel_value": "It distinguishes the post's assertion from external verification.",
                "strength": "The post gives a clear introduction to the topic.",
                "recommendation": "Add the primary evidence behind the central claim.",
                "evidence_refs": ["claim-1"],
                "risk_flags": [],
            },
            "public_comment": (
                "AI review: {PUBLIC_RATING}. This post raises a useful evidence gap; "
                "external verification is still needed."
            ),
        },
        config,
        provider="manual",
        model="test-model",
    )
    assert result["score"] == 75
    assert result["fact_check"]["claims"][0]["verdict"] == "needs_verification"
    assert result["publication_decision"]["status"] == "publish"
    assert result["publication_decision"]["positive_eligible"] is True
    assert result["publication_decision"]["public_rating"]["formatted"] == "7.5/10"
    assert result["publication_decision"]["comment_language"] == "en"
    assert result["publication_decision"]["language_mode"] == "auto"
    assert result["public_comment"].startswith("AI review: 7.5/10.")
    lookup = load_analysis_lookup(conn, "analysis_project")
    state = next(iter(lookup.values()))
    assert state["analyzed"] is True
    assert state["status"] == "complete"

    publication = queue_publication_drafts(conn, "analysis_project", config)
    assert publication == {
        "eligible": 1,
        "queued": 1,
        "skipped": 0,
        "abstained": 0,
        "stale": 0,
        "expired": 0,
    }
    assert queue_publication_drafts(conn, "analysis_project", config)["queued"] == 0
    queued = conn.execute("SELECT * FROM publication_queue").fetchone()
    assert queued["mode"] == "shadow"
    assert queued["approval_required"] == 1
    assert queued["status"] == "draft"
    assert queued["analysis_input_hash"] == pending["input_hash"]
    assert queued["valid_until"]

    exported = export_analysis_artifacts(
        conn,
        "analysis_project",
        tmp_path,
        config,
    )
    assert exported["summary"]["completed_analysis_records"] == 1
    assert exported["summary"]["publication_drafts"] == 1
    assert exported["summary"]["publication_decision_status_counts"] == {
        "publish": 1
    }
    assert (tmp_path / "analyses.jsonl").read_text(encoding="utf-8").count("\n") == 1

    ingest_videos(
        conn,
        project="analysis_project",
        keyword="public health",
        platform="youtube",
        run_id="run-2",
        scraped_at="2026-07-22T10:00:00+07:00",
        videos=[{
            "video_id": "video-1",
            "url": "https://www.youtube.com/watch?v=video-1",
            "comments": [{
                "id": "comment-2",
                "author": "Another Viewer",
                "text": "How recent is the source?",
            }],
        }],
    )
    changed = sync_analysis_queue(conn, "analysis_project", config)
    assert changed["stale"] == 1
    lookup = load_analysis_lookup(conn, "analysis_project")
    state = next(iter(lookup.values()))
    assert state["status"] == "stale"
    assert state["stale"] is True
    assert state["analyzed"] is False
    assert state["stale_reason"] == "native_text_comments_or_metrics_changed"


def test_unconfigured_formula_keeps_ai_recommendation_separate():
    conn = memory_db()
    seed_video(conn)
    config = normalize_analysis_config({
        "enabled": True,
        "provider": "manual",
        "scoring": {"formula_version": "later", "dimensions": []},
    })
    sync_analysis_queue(conn, "analysis_project", config)
    analysis_id = conn.execute("SELECT analysis_id FROM analysis_runs").fetchone()[0]
    result = apply_analysis_result(
        conn,
        analysis_id,
        {
            "summary": "Summary",
            "claims": [],
            "comment_questions": [],
            "dimension_scores": {},
            "ai_recommended_score": 88,
            "confidence": 90,
            "draft_comment": "Draft",
        },
        config,
        provider="manual",
    )
    assert result["score"] is None
    assert result["scoring"]["status"] == "awaiting_formula"
    assert result["scoring"]["ai_recommended_score"] == 88
    assert queue_publication_drafts(conn, "analysis_project", config)["queued"] == 0


def test_unavailable_transcript_marker_is_not_evidence():
    conn = memory_db()
    ingest_videos(
        conn,
        project="sentinel_project",
        keyword="coffee",
        platform="tiktok",
        run_id="run-sentinel",
        scraped_at="2026-07-22T10:00:00+07:00",
        videos=[{
            "video_id": "video-sentinel",
            "url": "https://www.tiktok.com/@creator/video/video-sentinel",
            "content_creator": "creator",
            "caption": "A measured coffee recipe",
            "published_at": "2026-07-22T09:00:00+07:00",
            "view_count": 10,
            "field_availability": {"transcript": "not_collected"},
            "comments": [],
        }],
    )
    config = normalize_analysis_config({"enabled": True, "provider": "manual"})
    sync_analysis_queue(conn, "sentinel_project", config)
    packet = json.loads(conn.execute("SELECT evidence_json FROM analysis_runs").fetchone()[0])
    assert packet["data_availability"]["transcript_or_subtitle"] is False
    assert packet["data_availability"]["comments"] is False
    assert packet["data_completeness"] == 66.67
    assert all(
        surface["text"] != "not_collected"
        for surface in packet["native_text_surfaces"]
    )


def test_ai_can_abstain_without_creating_a_publication_draft():
    conn = memory_db()
    seed_video(conn)
    config = normalize_analysis_config({
        "enabled": True,
        "provider": "manual",
        "publication": {
            "mode": "shadow",
            "require_fresh_evidence": False,
            "allow_public_scores": False,
        },
    })
    sync_analysis_queue(conn, "analysis_project", config)
    analysis_id = conn.execute("SELECT analysis_id FROM analysis_runs").fetchone()[0]
    result = apply_analysis_result(
        conn,
        analysis_id,
        {
            "internal_analysis": {
                "summary": "The available evidence only repeats the post.",
                "confidence": 90,
                "claims": [],
                "comment_questions": [],
                "missing_information": [],
            },
            "publication_decision": {
                "publish": False,
                "reason": "No novel public value.",
                "value_type": "none",
                "novel_value": "",
                "evidence_refs": [],
                "risk_flags": [],
            },
            "public_comment": "",
        },
        config,
        provider="manual",
    )
    assert result["publication_decision"]["status"] == "abstain"
    queued = queue_publication_drafts(conn, "analysis_project", config)
    assert queued["abstained"] == 1
    assert queued["queued"] == 0


def test_positive_only_policy_blocks_a_requested_low_score_comment():
    conn = memory_db()
    seed_video(conn)
    config = normalize_analysis_config({
        "enabled": True,
        "provider": "manual",
        "publication": {"mode": "shadow", "require_fresh_evidence": False},
    })
    sync_analysis_queue(conn, "analysis_project", config)
    analysis_id = conn.execute("SELECT analysis_id FROM analysis_runs").fetchone()[0]
    result = apply_analysis_result(
        conn,
        analysis_id,
        {
            "internal_analysis": {
                "summary": "The post has several important weaknesses.",
                "confidence": 90,
                "claims": [],
                "comment_questions": [],
                "dimension_scores": social_review_scores(60),
                "conversation_dimension_scores": conversation_value_scores(60),
            },
            "publication_decision": {
                "publish": True,
                "assessment": "positive",
                "reason": "The model requested publication despite the low score.",
                "value_type": "useful_gap",
                "novel_value": "Adds one recommendation.",
                "strength": "The topic is relevant.",
                "recommendation": "Add evidence and missing context.",
                "evidence_refs": ["content_items.caption"],
                "risk_flags": [],
            },
            "public_comment": "AI review {PUBLIC_RATING}: the topic is relevant; adding evidence would make it more useful.",
        },
        config,
        provider="manual",
    )
    assert result["score"] == 60
    assert result["publication_decision"]["status"] == "blocked_by_policy"
    assert "positive_score_threshold_not_met" in result["publication_decision"]["policy_violations"]
    queued = queue_publication_drafts(conn, "analysis_project", config)
    assert queued["queued"] == 0
    assert queued["skipped"] == 1


def test_conversation_score_cannot_rescue_a_below_threshold_post():
    conn = memory_db()
    seed_video(conn)
    config = normalize_analysis_config({
        "enabled": True,
        "provider": "manual",
        "publication": {"mode": "shadow", "require_fresh_evidence": False},
    })
    sync_analysis_queue(conn, "analysis_project", config)
    analysis_id = conn.execute("SELECT analysis_id FROM analysis_runs").fetchone()[0]
    result = apply_analysis_result(
        conn,
        analysis_id,
        {
            "internal_analysis": {
                "summary": "The discussion is stronger than the original post.",
                "language": "en",
                "confidence": 90,
                "claims": [],
                "comment_questions": [],
                "dimension_scores": social_review_scores(68),
                "conversation_dimension_scores": conversation_value_scores(100),
            },
            "publication_decision": {
                "publish": True,
                "assessment": "positive",
                "reason": "The discussion contains useful context.",
                "value_type": "useful_gap",
                "novel_value": "Summarizes the useful discussion.",
                "strength": "The post starts a useful discussion.",
                "recommendation": "Move the strongest clarification into the post.",
                "evidence_refs": ["content_items.caption"],
                "risk_flags": [],
            },
            "public_comment": "AI review {PUBLIC_RATING}: move the strongest clarification into the post.",
        },
        config,
        provider="manual",
    )
    assert result["scoring"]["post_score"] == 68
    assert result["scoring"]["conversation_score"] == 100
    assert result["score"] == 74.4
    assert result["scoring"]["conversation_adjustment"] == 6.4
    assert result["publication_decision"]["public_rating"]["formatted"] == "7.4/10"
    assert result["publication_decision"]["status"] == "blocked_by_policy"
    assert "post_score_threshold_not_met" in result["publication_decision"]["policy_violations"]
    assert queue_publication_drafts(conn, "analysis_project", config)["queued"] == 0


def test_video_without_transcript_keeps_score_internal_and_blocks_publication():
    conn = memory_db()
    ingest_videos(
        conn,
        project="caption_only_project",
        keyword="photography",
        platform="tiktok",
        run_id="run-caption-only",
        scraped_at="2026-07-22T09:00:00+07:00",
        videos=[{
            "video_id": "caption-only-video",
            "url": "https://www.tiktok.com/@creator/video/caption-only-video",
            "content_creator": "creator",
            "caption": "A short photography tutorial",
            "published_at": "2026-07-21T12:00:00+07:00",
            "view_count": 100,
            "comments": [{"id": "comment-1", "author": "viewer", "text": "Useful tip"}],
        }],
    )
    config = normalize_analysis_config({
        "enabled": True,
        "provider": "manual",
        "publication": {"mode": "shadow", "require_fresh_evidence": False},
    })
    sync_analysis_queue(conn, "caption_only_project", config)
    analysis_id = conn.execute("SELECT analysis_id FROM analysis_runs").fetchone()[0]
    result = apply_analysis_result(
        conn,
        analysis_id,
        {
            "internal_analysis": {
                "summary": "The available caption and discussion appear positive.",
                "language": "en",
                "confidence": 90,
                "claims": [],
                "comment_questions": [],
                "dimension_scores": social_review_scores(80),
                "conversation_dimension_scores": conversation_value_scores(80),
            },
            "publication_decision": {
                "publish": True,
                "assessment": "positive",
                "reason": "The post appears useful from the available evidence.",
                "value_type": "useful_gap",
                "novel_value": "Adds one concrete recommendation.",
                "strength": "The caption makes the topic approachable.",
                "recommendation": "Add the complete spoken instructions as subtitles.",
                "evidence_refs": ["content_items.caption"],
                "risk_flags": [],
            },
            "public_comment": "AI review {PUBLIC_RATING}: add the complete instructions as subtitles.",
        },
        config,
        provider="manual",
    )
    assert result["score"] == 80
    assert result["scoring"]["core_content"]["available"] is False
    assert result["publication_decision"]["core_content_eligible"] is False
    assert result["publication_decision"]["status"] == "blocked_by_policy"
    assert "core_content_incomplete" in result["publication_decision"]["policy_violations"]
    assert queue_publication_drafts(conn, "caption_only_project", config)["queued"] == 0


def test_negative_assessment_is_stored_but_quietly_abstains():
    conn = memory_db()
    seed_video(conn)
    config = normalize_analysis_config({
        "enabled": True,
        "provider": "manual",
        "publication": {"mode": "shadow", "require_fresh_evidence": False},
    })
    sync_analysis_queue(conn, "analysis_project", config)
    analysis_id = conn.execute("SELECT analysis_id FROM analysis_runs").fetchone()[0]
    result = apply_analysis_result(
        conn,
        analysis_id,
        {
            "internal_analysis": {
                "summary": "The post is not suitable for a public recommendation.",
                "confidence": 90,
                "claims": [],
                "comment_questions": [],
                "dimension_scores": social_review_scores(35),
                "conversation_dimension_scores": conversation_value_scores(35),
            },
            "publication_decision": {
                "publish": False,
                "assessment": "negative",
                "reason": "Positive-only mode skips negative assessments.",
                "value_type": "none",
                "novel_value": "",
                "strength": "",
                "recommendation": "",
                "evidence_refs": [],
                "risk_flags": [],
            },
            "public_comment": "",
        },
        config,
        provider="manual",
    )
    assert result["score"] == 35
    assert result["publication_decision"]["status"] == "abstain"
    queued = queue_publication_drafts(conn, "analysis_project", config)
    assert queued["abstained"] == 1
    assert queued["queued"] == 0


def test_internal_diagnostics_and_public_scores_are_blocked():
    conn = memory_db()
    seed_video(conn)
    config = normalize_analysis_config({
        "enabled": True,
        "provider": "manual",
        "publication": {
            "mode": "shadow",
            "require_fresh_evidence": False,
            "allow_public_scores": False,
        },
    })
    sync_analysis_queue(conn, "analysis_project", config)
    analysis_id = conn.execute("SELECT analysis_id FROM analysis_runs").fetchone()[0]
    result = apply_analysis_result(
        conn,
        analysis_id,
        {
            "internal_analysis": {
                "summary": "Summary",
                "confidence": 90,
                "claims": [{"claim_id": "claim-1", "verdict": "opinion"}],
                "comment_questions": [],
                "dimension_scores": social_review_scores(83),
                "conversation_dimension_scores": conversation_value_scores(83),
            },
            "publication_decision": {
                "publish": True,
                "assessment": "positive",
                "reason": "Requested for testing.",
                "value_type": "verified_context",
                "novel_value": "Adds context.",
                "strength": "The post communicates its central idea clearly.",
                "recommendation": "Add one supporting source for readers.",
                "evidence_refs": ["claim-1"],
                "risk_flags": [],
            },
            "public_comment": "Automated analysis: data completeness is 83/100.",
        },
        config,
        provider="manual",
    )
    assert result["publication_decision"]["status"] == "blocked_by_policy"
    assert set(result["publication_decision"]["policy_violations"]) >= {
        "internal_diagnostics_not_public",
        "public_score_not_allowed",
    }
    assert queue_publication_drafts(conn, "analysis_project", config)["queued"] == 0


def test_expired_evidence_cannot_enter_publication_queue():
    conn = memory_db()
    observed_at = (
        dt.datetime.now().astimezone() - dt.timedelta(hours=2)
    ).replace(microsecond=0).isoformat()
    ingest_videos(
        conn,
        project="freshness_project",
        keyword="example",
        platform="tiktok",
        run_id="run-old",
        scraped_at=observed_at,
        videos=[{
            "video_id": "old-video",
            "url": "https://www.tiktok.com/@creator/video/old-video",
            "content_creator": "creator",
            "caption": "A claim without a cited source",
            "transcript": "The complete platform transcript for the claim.",
            "transcript_available": True,
            "published_at": observed_at,
            "view_count": 10,
        }],
    )
    config = normalize_analysis_config({
        "enabled": True,
        "provider": "manual",
        "publication": {
            "mode": "shadow",
            "max_evidence_age_minutes": 30,
            "max_draft_age_minutes": 60,
        },
    })
    sync_analysis_queue(conn, "freshness_project", config)
    analysis_id = conn.execute("SELECT analysis_id FROM analysis_runs").fetchone()[0]
    apply_analysis_result(
        conn,
        analysis_id,
        {
            "internal_analysis": {
                "summary": "The caption contains an unsupported claim.",
                "confidence": 90,
                "claims": [],
                "comment_questions": [],
                "dimension_scores": social_review_scores(80),
            },
            "publication_decision": {
                "publish": True,
                "assessment": "positive",
                "reason": "A source request could add value.",
                "value_type": "clarifying_question",
                "novel_value": "Requests the missing primary source.",
                "strength": "The post presents a concise claim.",
                "recommendation": "Link the primary source behind the claim.",
                "evidence_refs": ["content_items.caption"],
                "risk_flags": [],
            },
            "public_comment": "AI review {PUBLIC_RATING}: is there a primary source for this claim?",
        },
        config,
        provider="manual",
    )
    queued = queue_publication_drafts(conn, "freshness_project", config)
    assert queued["expired"] == 1
    assert queued["queued"] == 0


def test_changed_evidence_invalidates_comment_before_queueing():
    conn = memory_db()
    seed_video(conn)
    config = normalize_analysis_config({
        "enabled": True,
        "provider": "manual",
        "publication": {"mode": "shadow", "require_fresh_evidence": False},
    })
    sync_analysis_queue(conn, "analysis_project", config)
    analysis_id = conn.execute("SELECT analysis_id FROM analysis_runs").fetchone()[0]
    apply_analysis_result(
        conn,
        analysis_id,
        {
            "internal_analysis": {
                "summary": "A supported analysis.",
                "confidence": 90,
                "claims": [],
                "comment_questions": [],
                "dimension_scores": social_review_scores(80),
                "conversation_dimension_scores": conversation_value_scores(80),
            },
            "publication_decision": {
                "publish": True,
                "assessment": "positive",
                "reason": "The source gap is useful to surface.",
                "value_type": "useful_gap",
                "novel_value": "Identifies an unresolved source gap.",
                "strength": "The explanation is concise and relevant.",
                "recommendation": "Add the external source used for the explanation.",
                "evidence_refs": ["content_items.caption"],
                "risk_flags": [],
            },
            "public_comment": "AI review {PUBLIC_RATING}: an external source would strengthen this explanation.",
        },
        config,
        provider="manual",
    )
    ingest_videos(
        conn,
        project="analysis_project",
        keyword="public health",
        platform="youtube",
        run_id="run-changed",
        scraped_at=dt.datetime.now().astimezone().replace(microsecond=0).isoformat(),
        videos=[{
            "video_id": "video-1",
            "url": "https://www.youtube.com/watch?v=video-1",
            "comments": [{
                "id": "comment-new",
                "author": "Viewer",
                "text": "The requested source is available here.",
            }],
        }],
    )
    queued = queue_publication_drafts(conn, "analysis_project", config)
    assert queued["stale"] == 1
    assert queued["queued"] == 0
    state = conn.execute("SELECT status, stale_reason FROM content_analysis_state").fetchone()
    assert state["status"] == "stale"
    assert state["stale_reason"] == "evidence_changed_before_publication"


def test_live_adapter_rejects_non_engage_legacy_publication(tmp_path):
    database = tmp_path / "state.sqlite"
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    now = dt.datetime.now().astimezone().replace(microsecond=0).isoformat()
    ingest_videos(
        conn,
        project="adapter_project",
        keyword="coffee",
        platform="tiktok",
        run_id="run-adapter",
        scraped_at=now,
        videos=[{
            "video_id": "adapter-video",
            "url": "https://www.tiktok.com/@creator/video/adapter-video",
            "content_creator": "creator",
            "caption": "A coffee recipe without bean details",
            "transcript": "The complete recipe is spoken in this platform transcript.",
            "transcript_available": True,
            "published_at": now,
            "view_count": 10,
        }],
    )
    config = normalize_analysis_config({
        "enabled": True,
        "provider": "manual",
        "publication": {"mode": "live"},
    })
    sync_analysis_queue(conn, "adapter_project", config)
    analysis_id = conn.execute("SELECT analysis_id FROM analysis_runs").fetchone()[0]
    apply_analysis_result(
        conn,
        analysis_id,
        {
            "internal_analysis": {
                "summary": "The bean details are absent.",
                "confidence": 90,
                "claims": [],
                "comment_questions": [],
                "dimension_scores": social_review_scores(80),
            },
            "publication_decision": {
                "publish": True,
                "assessment": "positive",
                "reason": "A specific clarification could help replication.",
                "value_type": "clarifying_question",
                "novel_value": "Requests the missing bean details.",
                "strength": "The recipe gives readers a useful starting point.",
                "recommendation": "Specify the beans and roast level for easier replication.",
                "evidence_refs": ["content_items.caption"],
                "risk_flags": [],
            },
            "public_comment": "AI review {PUBLIC_RATING}: which beans and roast level were used here?",
        },
        config,
        provider="manual",
    )
    assert queue_publication_drafts(conn, "adapter_project", config)["queued"] == 1
    publication = conn.execute(
        """
        SELECT publication_id, analysis_id, analysis_input_hash, draft_hash,
               target_url, content_key, expected_account, decision_json
        FROM publication_queue
        """
    ).fetchone()
    publication_id = publication["publication_id"]
    completed_analysis = conn.execute(
        """
        SELECT publication_decision_json, result_json
        FROM analysis_runs WHERE analysis_id=?
        """,
        (publication["analysis_id"],),
    ).fetchone()
    decision_hash = hashlib.sha256(
        json.dumps(
            json.loads(publication["decision_json"]),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    analysis_result_hash = hashlib.sha256(
        json.dumps(
            json.loads(completed_analysis["result_json"]),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    reviewed = record_publication_ai_review(
        conn,
        publication_id,
        {
            "approved": True,
            "independent_review": True,
            "analysis_id": publication["analysis_id"],
            "analysis_input_hash": publication["analysis_input_hash"],
            "reviewed_text_hash": publication["draft_hash"],
            "target_url": publication["target_url"],
            "content_key": publication["content_key"],
            "expected_account": publication["expected_account"],
            "decision_hash": decision_hash,
            "analysis_result_hash": analysis_result_hash,
            "grounding": "passed",
            "rating_consistency": "passed",
        },
        reviewer="codex-independent-critic",
        expected_draft_hash=publication["draft_hash"],
    )
    authorize_publication(
        conn,
        publication_id,
        authorized_by="tester",
        expected_draft_hash=publication["draft_hash"],
        expected_review_hash=reviewed["review_hash"],
    )
    conn.close()

    with pytest.raises(RuntimeError, match="handed off by TikTok ENGAGE"):
        load_approved_publication(database, publication_id)


def test_publication_capture_filters_and_redacts_replayable_values():
    assert looks_like_comment_write(
        "POST",
        "https://example.test/api/comment/create",
        '{"text":"Useful analysis"}',
    )
    assert not looks_like_comment_write(
        "POST",
        "https://example.test/analytics/log_event",
        '{"event":"comment_impression"}',
    )
    assert not looks_like_comment_write("GET", "https://example.test/api/comments")

    safe_url, fields = redact_url(
        "https://example.test/comment?csrf_token=secret&post_id=123"
    )
    assert "secret" not in safe_url
    assert "post_id=123" in safe_url
    assert fields == ["csrf_token"]

    headers, header_fields = redact_headers({
        "Authorization": "Bearer secret",
        "Cookie": "session=secret",
        "Content-Type": "application/json",
    })
    assert headers["authorization"] == "<redacted>"
    assert headers["cookie"] == "<redacted>"
    assert set(header_fields) == {"authorization", "cookie"}

    body, body_fields = request_body_template(
        '{"text":"Useful analysis","lsd":"secret","sessionid":"private"}',
        "application/json",
    )
    assert body["text"] == "Useful analysis"
    assert body["lsd"] == "<redacted>"
    assert body["sessionid"] == "<redacted>"
    assert set(body_fields) == {"lsd", "sessionid"}


def test_tiktok_publication_response_detection_and_capture_race():
    assert is_tiktok_comment_publish_url(
        "https://www.tiktok.com/api/comment/publish/?aid=1988"
    )
    assert not is_tiktok_comment_publish_url(
        "https://www.tiktok.com/api/comment/list/?aweme_id=123"
    )
    assert response_success({"response_status": 200, "response": {"status_code": 0}})
    assert not response_success({"response_status": 200, "response": {"status_code": 8}})

    class FakeImplementation:
        _guid = "request-guid"

    class FakeRequest:
        _impl_obj = FakeImplementation()
        method = "POST"
        url = "https://www.tiktok.com/api/comment/publish/?aid=1988"
        post_data = "text=Automated+analysis"

        async def all_headers(self):
            return {"content-type": "application/x-www-form-urlencoded"}

    class FakeResponse:
        request = FakeRequest()
        status = 200
        headers = {"content-type": "application/json"}

        async def json(self):
            return {"status_code": 0, "comment": {"cid": "comment-1"}}

    recorder = PublicationCapture("tiktok", "https://www.tiktok.com/video/1", "test")
    asyncio.run(recorder.on_response(FakeResponse()))
    assert len(recorder.records) == 1
    assert recorder.records[0]["response_status"] == 200
    assert recorder.records[0]["response"]["comment"]["cid"] == "comment-1"
