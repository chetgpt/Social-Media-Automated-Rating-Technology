"""Versioned native-text analysis and publication workflow.

The collection database remains canonical. This module builds reproducible
analysis inputs from text already exposed by each platform, records AI results,
and prepares idempotent publication drafts. It never publishes a comment.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable

import requests


ANALYSIS_SCHEMA_VERSION = "1.3"
DEFAULT_ANALYSIS_VERSION = "native-text-v4"
DEFAULT_FORMULA_VERSION = "social-review-v1"
UNAVAILABLE_TEXT_MARKERS = {
    "false",
    "failed",
    "missing",
    "n/a",
    "na",
    "no captions",
    "no subtitles",
    "no transcript",
    "no_transcript",
    "none",
    "not attempted",
    "not available",
    "not collected",
    "not provided",
    "not supported",
    "not_attempted",
    "not_available",
    "not_collected",
    "not_provided",
    "null",
    "true",
    "unavailable",
    "unknown",
    "unsupported",
}
PUBLICATION_VALUE_TYPES = {
    "answer",
    "fact_correction",
    "verified_context",
    "useful_gap",
    "clarifying_question",
}
AUTOMATION_DISCLOSURE_MARKERS = (
    "ai analysis",
    "ai-assisted",
    "analisis ai",
    "analisis otomatis",
    "automated analysis",
    "dibantu ai",
)
INTERNAL_DIAGNOSTIC_MARKERS = (
    "analysis confidence",
    "confidence score",
    "data completeness",
    "evidence completeness",
    "kelengkapan bukti",
    "skor kepercayaan",
    "rubric not configured",
    "rubrik belum",
)
NEGATIVE_PUBLIC_JUDGMENT_MARKERS = (
    "bad post",
    "poor post",
    "konten buruk",
    "konten jelek",
    "low score",
    "negative score",
    "nilai rendah",
    "postingan buruk",
    "postingan jelek",
    "skor rendah",
)
QUESTION_PREFIXES = (
    "apa ",
    "apakah ",
    "bagaimana ",
    "berapa ",
    "bisa ",
    "dimana ",
    "di mana ",
    "gimana ",
    "kapan ",
    "kenapa ",
    "kok ",
    "mengapa ",
    "siapa ",
    "what ",
    "when ",
    "where ",
    "which ",
    "who ",
    "why ",
    "how ",
    "can ",
    "could ",
    "does ",
    "do ",
    "is ",
    "are ",
)
NATIVE_TEXT_KEYS = {
    "auto_caption",
    "auto_captions",
    "caption_tracks",
    "captions",
    "closed_captions",
    "subtitle",
    "subtitles",
    "transcript",
    "transcript_text",
    "video_subtitle",
}
SKIPPED_RAW_BRANCHES = {
    "comments",
    "comment_list",
    "commentlist",
    "replies",
    "reply_list",
    "users",
}


def _now() -> str:
    return dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _clamp(value: Any, low: float = 0.0, high: float = 100.0) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(low, min(high, number))


def _json_loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _usable_native_text(value: Any) -> bool:
    text = re.sub(r"[\s-]+", " ", _text(value)).strip().casefold()
    return bool(text) and text not in UNAVAILABLE_TEXT_MARKERS


def _parse_datetime(value: Any) -> dt.datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def _freshness_deadline(
    *,
    completed_at: Any,
    observed_at: Any,
    max_draft_age_minutes: int,
    max_evidence_age_minutes: int,
) -> dt.datetime | None:
    deadlines = []
    completed = _parse_datetime(completed_at)
    observed = _parse_datetime(observed_at)
    if completed:
        deadlines.append(completed + dt.timedelta(minutes=max(1, max_draft_age_minutes)))
    if observed:
        deadlines.append(observed + dt.timedelta(minutes=max(1, max_evidence_age_minutes)))
    return min(deadlines) if deadlines else None


def _stable_id(*parts: Any, length: int = 32) -> str:
    value = "\x1f".join(_text(part) for part in parts)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _migrate_publication_queue_history(conn: sqlite3.Connection) -> None:
    """Retain superseded ENGAGE rows without relaxing the active queue fence."""
    schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='publication_queue'"
    ).fetchone()[0]
    replacement, changed = re.subn(
        r",\s*UNIQUE\s*\(\s*platform\s*,\s*target_url\s*,\s*draft_hash\s*\)",
        "", schema, flags=re.IGNORECASE,
    )
    conn.execute("SAVEPOINT publication_queue_history_migration")
    try:
        if changed:
            # SQLite cannot drop a table-level UNIQUE constraint. Copy the exact
            # current columns and user-defined indexes/triggers transactionally.
            objects = conn.execute(
                "SELECT sql FROM sqlite_master WHERE tbl_name='publication_queue' "
                "AND type IN ('index','trigger') AND sql IS NOT NULL"
            ).fetchall()
            replacement = re.sub(
                r"^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"`\[]?publication_queue[\"`\]]?",
                "CREATE TABLE publication_queue_history_migration", replacement,
                count=1, flags=re.IGNORECASE,
            )
            conn.execute(replacement)
            columns = ", ".join(
                '"' + row[1].replace('"', '""') + '"'
                for row in conn.execute("PRAGMA table_info(publication_queue)")
            )
            conn.execute(
                f"INSERT INTO publication_queue_history_migration ({columns}) "
                f"SELECT {columns} FROM publication_queue"
            )
            conn.execute("DROP TABLE publication_queue")
            conn.execute(
                "ALTER TABLE publication_queue_history_migration RENAME TO publication_queue"
            )
            for obj in objects:
                conn.execute(obj[0])
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_publication_queue_active_text "
            "ON publication_queue(platform, target_url, draft_hash) "
            "WHERE status <> 'superseded'"
        )
        conn.execute("RELEASE publication_queue_history_migration")
    except BaseException:
        conn.execute("ROLLBACK TO publication_queue_history_migration")
        conn.execute("RELEASE publication_queue_history_migration")
        raise


def ensure_analysis_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS content_analysis_state (
            project TEXT NOT NULL,
            platform TEXT NOT NULL,
            content_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            current_input_hash TEXT NOT NULL DEFAULT '',
            latest_analysis_id TEXT NOT NULL DEFAULT '',
            analysis_version TEXT NOT NULL DEFAULT '',
            formula_version TEXT NOT NULL DEFAULT '',
            analyzed_at TEXT NOT NULL DEFAULT '',
            last_queued_at TEXT NOT NULL DEFAULT '',
            stale_reason TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (project, platform, content_key)
        );
        CREATE INDEX IF NOT EXISTS idx_analysis_state_project_status
            ON content_analysis_state(project, status, platform);

        CREATE TABLE IF NOT EXISTS analysis_runs (
            analysis_id TEXT PRIMARY KEY,
            project TEXT NOT NULL,
            platform TEXT NOT NULL,
            content_key TEXT NOT NULL,
            input_hash TEXT NOT NULL,
            analysis_version TEXT NOT NULL,
            formula_version TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT 'manual',
            model TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT '',
            completed_at TEXT NOT NULL DEFAULT '',
            score REAL,
            confidence REAL,
            data_completeness REAL,
            evidence_json TEXT NOT NULL,
            fact_check_json TEXT NOT NULL DEFAULT '{}',
            question_gaps_json TEXT NOT NULL DEFAULT '{}',
            scoring_json TEXT NOT NULL DEFAULT '{}',
            publication_decision_json TEXT NOT NULL DEFAULT '{}',
            draft_comment TEXT NOT NULL DEFAULT '',
            result_json TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '',
            UNIQUE (
                project, platform, content_key, input_hash,
                analysis_version, formula_version
            )
        );
        CREATE INDEX IF NOT EXISTS idx_analysis_runs_project_status
            ON analysis_runs(project, status, platform, created_at);
        CREATE INDEX IF NOT EXISTS idx_analysis_runs_content
            ON analysis_runs(project, platform, content_key, created_at);

        CREATE TABLE IF NOT EXISTS fact_check_sources (
            source_id TEXT PRIMARY KEY,
            analysis_id TEXT NOT NULL,
            claim_id TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            publisher TEXT NOT NULL DEFAULT '',
            source_type TEXT NOT NULL DEFAULT '',
            stance TEXT NOT NULL DEFAULT '',
            retrieved_at TEXT NOT NULL DEFAULT '',
            evidence_text TEXT NOT NULL DEFAULT '',
            raw_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE (analysis_id, claim_id, url)
        );
        CREATE INDEX IF NOT EXISTS idx_fact_sources_analysis
            ON fact_check_sources(analysis_id, claim_id);

        CREATE TABLE IF NOT EXISTS publication_queue (
            publication_id TEXT PRIMARY KEY,
            project TEXT NOT NULL,
            platform TEXT NOT NULL,
            content_key TEXT NOT NULL,
            analysis_id TEXT NOT NULL,
            target_url TEXT NOT NULL DEFAULT '',
            mode TEXT NOT NULL DEFAULT 'shadow',
            status TEXT NOT NULL DEFAULT 'draft',
            draft_text TEXT NOT NULL,
            draft_hash TEXT NOT NULL,
            decision_json TEXT NOT NULL DEFAULT '{}',
            analysis_input_hash TEXT NOT NULL DEFAULT '',
            evidence_observed_at TEXT NOT NULL DEFAULT '',
            valid_until TEXT NOT NULL DEFAULT '',
            max_comments_per_post INTEGER NOT NULL DEFAULT 200,
            revalidation_status TEXT NOT NULL DEFAULT 'pending',
            revalidated_at TEXT NOT NULL DEFAULT '',
            approval_required INTEGER NOT NULL DEFAULT 1,
            expected_account TEXT NOT NULL DEFAULT '',
            ai_review_status TEXT NOT NULL DEFAULT '',
            ai_reviewed_at TEXT NOT NULL DEFAULT '',
            ai_reviewer TEXT NOT NULL DEFAULT '',
            ai_review_text_hash TEXT NOT NULL DEFAULT '',
            ai_review_evidence_hash TEXT NOT NULL DEFAULT '',
            ai_review_target_url TEXT NOT NULL DEFAULT '',
            ai_review_content_key TEXT NOT NULL DEFAULT '',
            ai_review_decision_hash TEXT NOT NULL DEFAULT '',
            ai_review_analysis_hash TEXT NOT NULL DEFAULT '',
            ai_review_json TEXT NOT NULL DEFAULT '{}',
            ai_review_hash TEXT NOT NULL DEFAULT '',
            approved_at TEXT NOT NULL DEFAULT '',
            approved_by TEXT NOT NULL DEFAULT '',
            authorization_presentation_hash TEXT NOT NULL DEFAULT '',
            authorization_text_hash TEXT NOT NULL DEFAULT '',
            authorization_review_hash TEXT NOT NULL DEFAULT '',
            authorization_target_url TEXT NOT NULL DEFAULT '',
            authorization_content_key TEXT NOT NULL DEFAULT '',
            authorization_decision_hash TEXT NOT NULL DEFAULT '',
            authorization_analysis_hash TEXT NOT NULL DEFAULT '',
            authorization_expected_account TEXT NOT NULL DEFAULT '',
            engage_run_id TEXT NOT NULL DEFAULT '',
            engage_post_id TEXT NOT NULL DEFAULT '',
            engage_handoff_hash TEXT NOT NULL DEFAULT '',
            supersedes_publication_id TEXT NOT NULL DEFAULT '',
            superseded_by_publication_id TEXT NOT NULL DEFAULT '',
            superseded_status TEXT NOT NULL DEFAULT '',
            superseded_at TEXT NOT NULL DEFAULT '',
            not_before TEXT NOT NULL DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TEXT NOT NULL DEFAULT '',
            remote_comment_id TEXT NOT NULL DEFAULT '',
            remote_comment_url TEXT NOT NULL DEFAULT '',
            master_attempt_id TEXT NOT NULL DEFAULT '',
            published_at TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (project, platform, content_key, analysis_id)
        );
        CREATE INDEX IF NOT EXISTS idx_publication_queue_project_status
            ON publication_queue(project, status, platform, created_at);

        CREATE TABLE IF NOT EXISTS publication_receipts (
            receipt_id TEXT PRIMARY KEY,
            publication_id TEXT NOT NULL,
            attempted_at TEXT NOT NULL,
            mode TEXT NOT NULL,
            transport TEXT NOT NULL DEFAULT '',
            request_fingerprint TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            http_status INTEGER,
            remote_comment_id TEXT NOT NULL DEFAULT '',
            remote_comment_url TEXT NOT NULL DEFAULT '',
            master_attempt_id TEXT NOT NULL DEFAULT '',
            response_json TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_publication_receipts_publication
            ON publication_receipts(publication_id, attempted_at);

        CREATE TABLE IF NOT EXISTS publication_captures (
            capture_id TEXT PRIMARY KEY,
            project TEXT NOT NULL DEFAULT '',
            platform TEXT NOT NULL,
            target_url TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            request_url TEXT NOT NULL,
            method TEXT NOT NULL,
            request_headers_json TEXT NOT NULL DEFAULT '{}',
            request_body_template TEXT NOT NULL DEFAULT '',
            response_status INTEGER,
            response_json TEXT NOT NULL DEFAULT '{}',
            signature_fields_json TEXT NOT NULL DEFAULT '[]'
        );
        """
    )
    analysis_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(analysis_runs)").fetchall()
    }
    analysis_additions = {
        "attempt_count": "INTEGER NOT NULL DEFAULT 0",
        "publication_decision_json": "TEXT NOT NULL DEFAULT '{}'",
    }
    for column, definition in analysis_additions.items():
        if column not in analysis_columns:
            conn.execute(f"ALTER TABLE analysis_runs ADD COLUMN {column} {definition}")
    publication_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(publication_queue)").fetchall()
    }
    publication_additions = {
        "decision_json": "TEXT NOT NULL DEFAULT '{}'",
        "analysis_input_hash": "TEXT NOT NULL DEFAULT ''",
        "evidence_observed_at": "TEXT NOT NULL DEFAULT ''",
        "valid_until": "TEXT NOT NULL DEFAULT ''",
        "max_comments_per_post": "INTEGER NOT NULL DEFAULT 200",
        "revalidation_status": "TEXT NOT NULL DEFAULT 'pending'",
        "revalidated_at": "TEXT NOT NULL DEFAULT ''",
        "expected_account": "TEXT NOT NULL DEFAULT ''",
        "ai_review_status": "TEXT NOT NULL DEFAULT ''",
        "ai_reviewed_at": "TEXT NOT NULL DEFAULT ''",
        "ai_reviewer": "TEXT NOT NULL DEFAULT ''",
        "ai_review_text_hash": "TEXT NOT NULL DEFAULT ''",
        "ai_review_evidence_hash": "TEXT NOT NULL DEFAULT ''",
        "ai_review_target_url": "TEXT NOT NULL DEFAULT ''",
        "ai_review_content_key": "TEXT NOT NULL DEFAULT ''",
        "ai_review_decision_hash": "TEXT NOT NULL DEFAULT ''",
        "ai_review_analysis_hash": "TEXT NOT NULL DEFAULT ''",
        "ai_review_json": "TEXT NOT NULL DEFAULT '{}'",
        "ai_review_hash": "TEXT NOT NULL DEFAULT ''",
        "authorization_presentation_hash": "TEXT NOT NULL DEFAULT ''",
        "authorization_text_hash": "TEXT NOT NULL DEFAULT ''",
        "authorization_review_hash": "TEXT NOT NULL DEFAULT ''",
        "authorization_target_url": "TEXT NOT NULL DEFAULT ''",
        "authorization_content_key": "TEXT NOT NULL DEFAULT ''",
        "authorization_decision_hash": "TEXT NOT NULL DEFAULT ''",
        "authorization_analysis_hash": "TEXT NOT NULL DEFAULT ''",
        "authorization_expected_account": "TEXT NOT NULL DEFAULT ''",
        "engage_run_id": "TEXT NOT NULL DEFAULT ''",
        "engage_post_id": "TEXT NOT NULL DEFAULT ''",
        "engage_handoff_hash": "TEXT NOT NULL DEFAULT ''",
        "master_attempt_id": "TEXT NOT NULL DEFAULT ''",
        "supersedes_publication_id": "TEXT NOT NULL DEFAULT ''",
        "superseded_by_publication_id": "TEXT NOT NULL DEFAULT ''",
        "superseded_status": "TEXT NOT NULL DEFAULT ''",
        "superseded_at": "TEXT NOT NULL DEFAULT ''",
    }
    for column, definition in publication_additions.items():
        if column not in publication_columns:
            conn.execute(f"ALTER TABLE publication_queue ADD COLUMN {column} {definition}")
    _migrate_publication_queue_history(conn)
    receipt_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(publication_receipts)").fetchall()
    }
    if "master_attempt_id" not in receipt_columns:
        conn.execute(
            "ALTER TABLE publication_receipts "
            "ADD COLUMN master_attempt_id TEXT NOT NULL DEFAULT ''"
        )
    conn.commit()


def _strings_from_container(value: Any, *, limit: int = 50) -> list[str]:
    output: list[str] = []

    def visit(item: Any) -> None:
        if len(output) >= limit:
            return
        if isinstance(item, str):
            text = item.strip()
            if _usable_native_text(text):
                output.append(text)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, dict):
            preferred = ("text", "body", "content", "caption", "subtitle", "transcript")
            matched = False
            for key in preferred:
                if key in item:
                    matched = True
                    visit(item[key])
            if not matched:
                for child in item.values():
                    visit(child)

    visit(value)
    return output


def native_text_surfaces(content: sqlite3.Row, raw_content: dict[str, Any]) -> list[dict[str, Any]]:
    surfaces: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(kind: str, source_path: str, value: Any, **metadata: Any) -> None:
        text = _text(value)
        if kind in {"platform_transcript", "platform_subtitle"} and not _usable_native_text(text):
            return
        normalized = re.sub(r"\s+", " ", text).casefold()
        if not text or normalized in seen:
            return
        seen.add(normalized)
        surfaces.append({
            "type": kind,
            "source_path": source_path,
            "text": text,
            **metadata,
        })

    add("creator_caption", "content_items.caption", content["caption"])
    add("creator_description", "content_items.description", content["description"])
    if _text(content["transcript"]):
        add(
            "platform_transcript",
            "content_items.transcript",
            content["transcript"],
            language=_text(content["transcript_language"]),
            language_name=_text(content["transcript_language_name"]),
            auto_generated=bool(content["transcript_is_auto_generated"]),
            transcript_source=_text(content["transcript_source"]),
        )

    def walk(value: Any, path: str = "raw", depth: int = 0) -> None:
        if depth > 5 or len(surfaces) >= 80:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                key_text = str(key).casefold()
                if key_text in SKIPPED_RAW_BRANCHES:
                    continue
                child_path = f"{path}.{key}"
                if key_text in NATIVE_TEXT_KEYS:
                    kind = "platform_transcript" if "transcript" in key_text else "platform_subtitle"
                    for text in _strings_from_container(child):
                        add(kind, child_path, text)
                elif depth == 0 and key_text in {"title", "text", "desc", "description"}:
                    add("post_text", child_path, child)
                elif isinstance(child, (dict, list)):
                    walk(child, child_path, depth + 1)
        elif isinstance(value, list):
            for index, child in enumerate(value[:100]):
                walk(child, f"{path}[{index}]", depth + 1)

    walk(raw_content)
    return surfaces


def extract_questions(comments: Iterable[dict[str, Any]], limit: int = 50) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for comment in comments:
        text = _text(comment.get("text"))
        normalized = re.sub(r"\s+", " ", text).strip()
        lowered = normalized.casefold()
        is_question = "?" in normalized or lowered.startswith(QUESTION_PREFIXES)
        key = lowered.rstrip("?.! ")
        if not is_question or not key or key in seen:
            continue
        seen.add(key)
        output.append({
            "comment_id": _text(comment.get("comment_id")),
            "author": _text(comment.get("author")),
            "question": normalized,
            "status": "unanswered",
        })
        if len(output) >= max(1, limit):
            break
    return output


def _comment_rows(conn: sqlite3.Connection, project: str, platform: str, content_key: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT comment_key, parent_comment_id, author, author_id, text, likes,
               comment_time, reply_count, is_reply, first_seen_at, last_seen_at
        FROM comments
        WHERE project = ? AND platform = ? AND content_key = ?
        ORDER BY first_seen_at, comment_key
        """,
        (project, platform, content_key),
    ).fetchall()
    return [
        {
            "comment_id": row["comment_key"],
            "parent_comment_id": row["parent_comment_id"],
            "author": row["author"],
            "author_id": row["author_id"],
            "text": row["text"],
            "likes": row["likes"],
            "published_at": row["comment_time"],
            "reply_count": row["reply_count"],
            "is_reply": bool(row["is_reply"]),
            "first_seen_at": row["first_seen_at"],
            "last_seen_at": row["last_seen_at"],
        }
        for row in rows
    ]


def _select_comments(comments: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if limit <= 0 or len(comments) <= limit:
        return comments
    questions = [comment for comment in comments if extract_questions([comment], limit=1)]
    question_ids = {comment["comment_id"] for comment in questions}
    remainder = [comment for comment in comments if comment["comment_id"] not in question_ids]
    remainder.sort(
        key=lambda item: (
            int(item.get("likes") or 0),
            _text(item.get("last_seen_at")),
        ),
        reverse=True,
    )
    return [*questions, *remainder][:limit]


def build_evidence_packet(
    conn: sqlite3.Connection,
    content: sqlite3.Row,
    *,
    max_comments: int = 200,
) -> tuple[dict[str, Any], str]:
    raw_content = _json_loads(content["raw_json"], {})
    if not isinstance(raw_content, dict):
        raw_content = {}
    comments = _comment_rows(
        conn,
        content["project"],
        content["platform"],
        content["content_key"],
    )
    selected_comments = _select_comments(comments, max(0, int(max_comments)))
    surfaces = native_text_surfaces(content, raw_content)
    surface_types = {surface["type"] for surface in surfaces}
    metric_values = {
        "views": content["view_count"],
        "likes": content["like_count"],
        "reported_comments": content["reported_comment_count"],
        "shares": content["share_count"],
        "saves": content["save_count"],
        "followers": content["follower_count"],
    }
    checks = {
        "caption_or_description": bool(
            {"creator_caption", "creator_description", "post_text"}.intersection(surface_types)
        ),
        "transcript_or_subtitle": bool(
            {"platform_transcript", "platform_subtitle"}.intersection(surface_types)
        ),
        "comments": bool(comments),
        "creator": bool(_text(content["creator"])),
        "publication_time": bool(_text(content["published_at"])),
        "engagement_metric": any(value not in (None, "", 0) for value in metric_values.values()),
    }
    missing = [name for name, available in checks.items() if not available]
    completeness = round(100 * sum(checks.values()) / max(1, len(checks)), 2)
    packet = {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "project": content["project"],
        "platform": content["platform"],
        "content_key": content["content_key"],
        "video_id": content["video_id"],
        "media_id": content["media_id"],
        "url": content["url"],
        "creator": content["creator"],
        "published_at": content["published_at"],
        "content_type": content["content_type"],
        "native_text_surfaces": surfaces,
        "native_text_only": True,
        "media_processing_used": False,
        "comments": selected_comments,
        "comment_count_total": len(comments),
        "comment_count_in_packet": len(selected_comments),
        "detected_questions": extract_questions(selected_comments),
        "metrics": metric_values,
        "data_availability": checks,
        "missing_data": missing,
        "data_completeness": completeness,
        "observed_at": content["last_scraped_at"],
    }
    hash_payload = {
        "packet": packet,
        "all_comments": [
            {
                "id": comment["comment_id"],
                "parent": comment["parent_comment_id"],
                "text": comment["text"],
                "likes": comment["likes"],
                "last_seen_at": comment["last_seen_at"],
            }
            for comment in comments
        ],
    }
    return packet, hashlib.sha256(_canonical_json(hash_payload).encode("utf-8")).hexdigest()


def build_analysis_prompt(packet: dict[str, Any], config: dict[str, Any]) -> str:
    scoring = config.get("scoring") if isinstance(config.get("scoring"), dict) else {}
    dimensions = scoring.get("dimensions") if isinstance(scoring.get("dimensions"), list) else []
    conversation_dimensions = (
        scoring.get("conversation_dimensions")
        if isinstance(scoring.get("conversation_dimensions"), list)
        else []
    )
    fact_check = config.get("fact_check") if isinstance(config.get("fact_check"), dict) else {}
    publication = config.get("publication") if isinstance(config.get("publication"), dict) else {}
    language_policy = (
        publication.get("language")
        if isinstance(publication.get("language"), dict)
        else {}
    )
    public_rating = (
        publication.get("public_rating")
        if isinstance(publication.get("public_rating"), dict)
        else {}
    )
    instructions = {
        "task": (
            "Analyze one social post, then independently decide whether a public comment "
            "would add meaningful, evidence-grounded value."
        ),
        "requirements": [
            "Identify factual claims, audience questions, missing context, and contradictions.",
            "Evaluate audience questions by weighing their frequency and importance (likes). Check if a question has already been answered in the thread, and verify if that existing answer is factually correct. Focus your drafted answer on the main questions that are either unanswered or answered incorrectly.",
            "Do not invent missing facts or claim verification without a source URL.",
            "Answer comment questions only when evidence or a reliable cited source supports the answer.",
            "Use needs_verification or unknown when evidence is insufficient.",
            "Classify the post's intent or genre before applying the rubric; factual, opinion, promotional, personal, entertainment, and creative posts must be judged according to their actual intent.",
            "Score every configured post rubric dimension from 0 to 100 and explain the result with concise evidence references.",
            "When conversation scoring is enabled and comments exist, separately score every configured conversation dimension. Viewer comments measure reception and thread value; they must not retroactively make an inaccurate original post accurate.",
            "Summarize two to four concrete helpful details from comments when available. Distinguish creator replies, audience questions, firsthand experience, and unverified viewer claims.",
            "Judge opinions, entertainment, and personal expression according to their stated intent; do not penalize them merely for lacking citations.",
            "Keep completeness, confidence, raw 0-100 scores, and rubric diagnostics out of the public comment.",
            "Set publish=false when the comment would only repeat the post, offer generic praise, or expose internal diagnostics.",
            "When publication_policy.positive_only is true, set publish=true only for a positive post that is likely to meet the minimum public score.",
            "For mixed, negative, harmful, not-scorable, or core-content-incomplete posts, set publish=false and leave public_comment empty.",
            "A publish=true decision must name its novel value and cite evidence references from the packet or analyzed claims.",
            "When publishing, identify one specific strength and one actionable recommendation.",
            "Choose the public language and tone under the language policy. IMPORTANT: If the language policy mode is 'auto', you MUST write the public_comment and choose the comment_language in the primary language used in the original post (video, caption, or comments). Do not default to English unless the post is in English. Adapt naturally without impersonating the creator, copying distinctive errors, or reproducing abusive language; fall back to neutral language when uncertain.",
            "Draft one concise, respectful, post-specific recommendation and include the literal token AI as disclosure; never publish a negative verdict.",
            "When public rating is enabled, include its configured placeholder exactly once. Code replaces it with the deterministic one-decimal rating; never calculate or write the public number yourself.",
            "An empty public_comment is required when publish=false.",
        ],
        "fact_check": fact_check,
        "scoring_policy": {
            "formula_version": scoring.get("formula_version", DEFAULT_FORMULA_VERSION),
            "genre_mode": scoring.get("genre_mode", "auto"),
            "fixed_genre": scoring.get("genre", ""),
            "post_dimensions": dimensions,
            "conversation_enabled": scoring.get("conversation_enabled", False),
            "conversation_dimensions": conversation_dimensions,
            "post_weight": scoring.get("post_weight", 80),
            "conversation_weight": scoring.get("conversation_weight", 20),
            "maximum_conversation_adjustment": scoring.get("max_conversation_adjustment", 10),
            "require_core_content_for_publication": scoring.get(
                "require_core_content_for_publication", True
            ),
            "video_requires_transcript_or_subtitle": scoring.get(
                "video_requires_transcript_or_subtitle", True
            ),
        },
        "publication_policy": {
            "allowed_value_types": publication.get("allowed_value_types") or sorted(PUBLICATION_VALUE_TYPES),
            "require_novel_value": publication.get("require_novel_value", True),
            "require_evidence_refs": publication.get("require_evidence_refs", True),
            "disclose_automation": publication.get("disclose_automation", True),
            "allow_public_scores": publication.get("allow_public_scores", False),
            "public_rating": public_rating,
            "language": language_policy,
            "allow_internal_diagnostics": publication.get("allow_internal_diagnostics", False),
            "positive_only": publication.get("positive_only", True),
            "minimum_public_score": publication.get("min_public_score", 70),
            "recommendation_only": publication.get("recommendation_only", True),
            "require_strength_and_recommendation": publication.get(
                "require_strength_and_recommendation", True
            ),
            "minimum_characters": publication.get("min_public_comment_characters", 20),
            "maximum_characters": publication.get("max_public_comment_characters", 1000),
        },
        "output_schema": {
            "internal_analysis": {
                "summary": "string",
                "language": "string",
                "content_genre": "factual|educational|opinion|promotional|personal|entertainment|creative|news|other",
                "claims": [{
                    "claim_id": "string",
                    "claim": "string",
                    "verdict": "supported|contradicted|misleading|needs_verification|opinion|unknown",
                    "confidence": "0-100",
                    "reason": "string",
                    "sources": [{"url": "string", "title": "string", "evidence": "string"}],
                }],
                "comment_questions": [{
                    "comment_id": "string",
                    "question": "string",
                    "is_main_question": "boolean",
                    "existing_answer_status": "unanswered|answered_correctly|answered_incorrectly|in_video",
                    "our_proposed_answer": "string",
                    "status": "answered_with_sources|reasonable_inference|unknown",
                    "sources": [{"url": "string", "title": "string"}],
                }],
                "missing_information": ["string"],
                "dimension_scores": {"dimension_key": "0-100"},
                "conversation_dimension_scores": {"conversation_dimension_key": "0-100"},
                "ai_recommended_score": "0-100 or null",
                "confidence": "0-100",
                "score_rationale": "string",
                "conversation_score_rationale": "string",
                "helpful_comment_summary": [{
                    "summary": "specific contribution from the discussion",
                    "source_comment_ids": ["comment_id"],
                    "source_type": "creator_reply|audience_question|viewer_context|viewer_experience",
                    "verification": "supported|reasonable_context|anecdotal|needs_verification",
                }],
            },
            "publication_decision": {
                "publish": "boolean",
                "assessment": "positive|mixed|negative|not_scorable",
                "reason": "string",
                "value_type": "answer|fact_correction|verified_context|useful_gap|clarifying_question|none",
                "novel_value": "string",
                "strength": "specific strength in the post",
                "recommendation": "one constructive, actionable improvement",
                "comment_language": "BCP-47 language tag or concise language name",
                "evidence_refs": ["claim_id, comment_id, or native_text source_path"],
                "risk_flags": ["string"],
            },
            "public_comment": (
                "string containing the configured public-rating placeholder when publish=true; "
                "empty when publish=false"
            ),
        },
    }
    return (
        "Return one JSON object and no markdown.\n\n"
        + _canonical_json(instructions)
        + "\n\nEVIDENCE_PACKET\n"
        + _canonical_json(packet)
    )


def sync_analysis_queue(
    conn: sqlite3.Connection,
    project: str,
    config: dict[str, Any],
) -> dict[str, int]:
    ensure_analysis_schema(conn)
    analysis_version = _text(config.get("analysis_version")) or DEFAULT_ANALYSIS_VERSION
    scoring = config.get("scoring") if isinstance(config.get("scoring"), dict) else {}
    formula_version = _text(scoring.get("formula_version")) or DEFAULT_FORMULA_VERSION
    provider = _text(config.get("provider")) or "manual"
    model = _text(config.get("model"))
    max_comments = int(config.get("max_comments_per_post") or 200)
    now = _now()
    stats = {"content": 0, "queued": 0, "stale": 0, "unchanged": 0, "complete": 0}
    rows = conn.execute(
        "SELECT * FROM content_items WHERE project = ? ORDER BY platform, content_key",
        (project,),
    ).fetchall()
    for content in rows:
        stats["content"] += 1
        packet, input_hash = build_evidence_packet(conn, content, max_comments=max_comments)
        analysis_id = _stable_id(
            project,
            content["platform"],
            content["content_key"],
            input_hash,
            analysis_version,
            formula_version,
        )
        existing_state = conn.execute(
            """
            SELECT * FROM content_analysis_state
            WHERE project = ? AND platform = ? AND content_key = ?
            """,
            (project, content["platform"], content["content_key"]),
        ).fetchone()
        existing_run = conn.execute(
            "SELECT status FROM analysis_runs WHERE analysis_id = ?",
            (analysis_id,),
        ).fetchone()
        if existing_run:
            status = existing_run["status"]
            if status == "complete":
                stats["complete"] += 1
            else:
                stats["unchanged"] += 1
        else:
            was_analyzed = bool(existing_state and existing_state["analyzed_at"])
            status = "stale" if was_analyzed else "pending"
            conn.execute(
                """
                INSERT INTO analysis_runs (
                    analysis_id, project, platform, content_key, input_hash,
                    analysis_version, formula_version, provider, model, status,
                    created_at, data_completeness, evidence_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    analysis_id,
                    project,
                    content["platform"],
                    content["content_key"],
                    input_hash,
                    analysis_version,
                    formula_version,
                    provider,
                    model,
                    status,
                    now,
                    packet["data_completeness"],
                    _canonical_json(packet),
                ),
            )
            if status == "stale":
                stats["stale"] += 1
            else:
                stats["queued"] += 1
        stale_reason = ""
        if existing_state and existing_state["current_input_hash"] != input_hash:
            stale_reason = "native_text_comments_or_metrics_changed"
        elif existing_state and existing_state["analysis_version"] != analysis_version:
            stale_reason = "analysis_version_changed"
        elif existing_state and existing_state["formula_version"] != formula_version:
            stale_reason = "formula_version_changed"
        conn.execute(
            """
            INSERT INTO content_analysis_state (
                project, platform, content_key, status, current_input_hash,
                latest_analysis_id, analysis_version, formula_version,
                analyzed_at, last_queued_at, stale_reason, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, '')
            ON CONFLICT(project, platform, content_key) DO UPDATE SET
                status = excluded.status,
                current_input_hash = excluded.current_input_hash,
                latest_analysis_id = excluded.latest_analysis_id,
                analysis_version = excluded.analysis_version,
                formula_version = excluded.formula_version,
                last_queued_at = excluded.last_queued_at,
                stale_reason = excluded.stale_reason,
                error = ''
            """,
            (
                project,
                content["platform"],
                content["content_key"],
                status,
                input_hash,
                analysis_id,
                analysis_version,
                formula_version,
                now,
                stale_reason,
            ),
        )
    conn.commit()
    return stats


def _dimension_set_score(
    raw_scores: Any,
    dimensions: Any,
) -> tuple[float | None, dict[str, float], list[str]]:
    raw_scores = raw_scores if isinstance(raw_scores, dict) else {}
    dimensions = dimensions if isinstance(dimensions, list) else []
    weighted_total = 0.0
    total_weight = 0.0
    accepted: dict[str, float] = {}
    missing: list[str] = []
    for dimension in dimensions:
        if not isinstance(dimension, dict):
            continue
        key = _text(dimension.get("key") or dimension.get("name"))
        weight = max(0.0, float(dimension.get("weight") or 0.0))
        value = _clamp(raw_scores.get(key))
        if not key or not weight:
            continue
        if value is None:
            missing.append(key)
            continue
        accepted[key] = value
        weighted_total += value * weight
        total_weight += weight
    score = round(weighted_total / total_weight, 2) if total_weight and not missing else None
    return score, accepted, missing


def _core_content_assessment(
    packet: dict[str, Any],
    scoring: dict[str, Any],
) -> dict[str, Any]:
    availability = (
        packet.get("data_availability")
        if isinstance(packet.get("data_availability"), dict)
        else {}
    )
    content_type = _text(packet.get("content_type")).lower()
    likely_video = bool(_text(packet.get("video_id"))) or content_type in {
        "video",
        "short",
        "reel",
        "stream",
        "live",
    }
    transcript_available = bool(availability.get("transcript_or_subtitle"))
    caption_available = bool(availability.get("caption_or_description"))
    video_requires_transcript = bool(
        scoring.get("video_requires_transcript_or_subtitle", True)
    )
    if likely_video and video_requires_transcript:
        available = transcript_available
        required_surfaces = ["transcript_or_subtitle"]
    else:
        available = caption_available or transcript_available
        required_surfaces = ["caption_or_description_or_transcript"]
    return {
        "required_for_publication": bool(
            scoring.get("require_core_content_for_publication", True)
        ),
        "available": available,
        "content_type": content_type or ("video" if likely_video else "post"),
        "required_surfaces": required_surfaces,
        "caption_or_description_available": caption_available,
        "transcript_or_subtitle_available": transcript_available,
    }


def _dimension_score(
    result: dict[str, Any],
    scoring: dict[str, Any],
    packet: dict[str, Any] | None = None,
) -> tuple[float | None, dict[str, Any]]:
    raw_scores = result.get("dimension_scores")
    if not isinstance(raw_scores, dict):
        raw_scores = result.get("post_dimension_scores")
    dimensions = scoring.get("dimensions")
    core_content = _core_content_assessment(packet or {}, scoring)
    if not isinstance(dimensions, list) or not dimensions:
        return None, {
            "status": "awaiting_formula",
            "formula_version": _text(scoring.get("formula_version")) or DEFAULT_FORMULA_VERSION,
            "dimension_scores": raw_scores if isinstance(raw_scores, dict) else {},
            "post_score": None,
            "conversation_score": None,
            "overall_score": None,
            "core_content": core_content,
            "ai_recommended_score": _clamp(result.get("ai_recommended_score")),
            "rationale": _text(result.get("score_rationale")),
        }
    post_score, accepted, missing = _dimension_set_score(raw_scores, dimensions)
    base = {
        "status": "calculated" if post_score is not None else "incomplete_dimensions",
        "formula_version": _text(scoring.get("formula_version")) or DEFAULT_FORMULA_VERSION,
        "dimension_scores": accepted,
        "missing_dimensions": missing,
        "post_score": post_score,
        "core_content": core_content,
        "ai_recommended_score": _clamp(result.get("ai_recommended_score")),
        "rationale": _text(result.get("score_rationale")),
    }
    conversation_dimensions = scoring.get("conversation_dimensions")
    conversation_enabled = bool(scoring.get("conversation_enabled")) and bool(
        isinstance(conversation_dimensions, list) and conversation_dimensions
    )
    if not conversation_enabled:
        return post_score, {
            **base,
            "conversation_status": "disabled",
            "conversation_dimension_scores": {},
            "conversation_missing_dimensions": [],
            "conversation_score": None,
            "conversation_adjustment": 0.0 if post_score is not None else None,
            "overall_score": post_score,
        }

    comment_count = int((packet or {}).get("comment_count_in_packet") or 0)
    if comment_count <= 0:
        return post_score, {
            **base,
            "conversation_status": "not_applicable",
            "conversation_dimension_scores": {},
            "conversation_missing_dimensions": [],
            "conversation_score": None,
            "conversation_adjustment": 0.0 if post_score is not None else None,
            "overall_score": post_score,
        }

    conversation_raw = result.get("conversation_dimension_scores")
    conversation_score, conversation_accepted, conversation_missing = _dimension_set_score(
        conversation_raw,
        conversation_dimensions,
    )
    base.update({
        "conversation_status": (
            "calculated" if conversation_score is not None else "incomplete_dimensions"
        ),
        "conversation_dimension_scores": conversation_accepted,
        "conversation_missing_dimensions": conversation_missing,
        "conversation_score": conversation_score,
        "conversation_rationale": _text(result.get("conversation_score_rationale")),
    })
    if post_score is None or conversation_score is None:
        return None, {
            **base,
            "status": "incomplete_dimensions",
            "conversation_adjustment": None,
            "overall_score": None,
        }

    post_weight = max(0.0, float(scoring.get("post_weight") or 0.0))
    conversation_weight = max(0.0, float(scoring.get("conversation_weight") or 0.0))
    total_weight = post_weight + conversation_weight
    if total_weight <= 0:
        return None, {
            **base,
            "status": "invalid_component_weights",
            "conversation_adjustment": None,
            "overall_score": None,
        }
    weighted_score = (
        post_score * post_weight + conversation_score * conversation_weight
    ) / total_weight
    maximum_adjustment = max(
        0.0,
        min(100.0, float(scoring.get("max_conversation_adjustment") or 0.0)),
    )
    adjustment = max(
        -maximum_adjustment,
        min(maximum_adjustment, weighted_score - post_score),
    )
    overall_score = round(post_score + adjustment, 2)
    return overall_score, {
        **base,
        "status": "calculated",
        "component_weights": {
            "post": post_weight,
            "conversation": conversation_weight,
        },
        "maximum_conversation_adjustment": maximum_adjustment,
        "conversation_adjustment": round(adjustment, 2),
        "overall_score": overall_score,
    }


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    output = []
    seen = set()
    for item in value:
        text = _text(item)
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            output.append(text)
    return output


def _internal_analysis_payload(result: dict[str, Any]) -> dict[str, Any]:
    internal = result.get("internal_analysis")
    return internal if isinstance(internal, dict) else result


def _evidence_reference_set(packet: dict[str, Any], internal: dict[str, Any]) -> set[str]:
    references = {
        _text(surface.get("source_path"))
        for surface in packet.get("native_text_surfaces") or []
        if isinstance(surface, dict)
    }
    references.update(
        _text(claim.get("claim_id"))
        for claim in internal.get("claims") or []
        if isinstance(claim, dict)
    )
    references.update(
        _text(question.get("comment_id"))
        for question in internal.get("comment_questions") or []
        if isinstance(question, dict)
    )
    for claim in internal.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        references.update(
            _text(source.get("url"))
            for source in claim.get("sources") or []
            if isinstance(source, dict)
        )
    return {reference for reference in references if reference}


def _public_rating(score: float | None, config: dict[str, Any]) -> dict[str, Any]:
    publication = config.get("publication") if isinstance(config.get("publication"), dict) else {}
    rating = (
        publication.get("public_rating")
        if isinstance(publication.get("public_rating"), dict)
        else {}
    )
    enabled = bool(publication.get("allow_public_scores", False)) and bool(
        rating.get("enabled", False)
    )
    scale = max(1.0, min(100.0, float(rating.get("scale") or 10.0)))
    increment = max(0.1, min(scale, float(rating.get("increment") or 0.1)))
    payload = {
        "enabled": enabled,
        "required": enabled and bool(rating.get("required", True)),
        "scale": scale,
        "increment": increment,
        "value": None,
        "formatted": "",
        "band": "not_scorable",
    }
    if not enabled or score is None:
        return payload
    raw_value = max(0.0, min(scale, float(score) * scale / 100.0))
    decimal_increment = Decimal(str(increment))
    rounded_units = (
        Decimal(str(raw_value)) / decimal_increment
    ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    rounded_value = float(rounded_units * decimal_increment)
    rounded_value = max(0.0, min(scale, rounded_value))
    value_text = f"{rounded_value:.4f}".rstrip("0").rstrip(".")
    scale_text = f"{scale:.4f}".rstrip("0").rstrip(".")
    band = "unclassified"
    bands = rating.get("bands") if isinstance(rating.get("bands"), list) else []
    for candidate in bands:
        if not isinstance(candidate, dict):
            continue
        if rounded_value >= float(candidate.get("minimum") or 0.0):
            band = _text(candidate.get("key")) or band
            break
    return {
        **payload,
        "value": rounded_value,
        "formatted": f"{value_text}/{scale_text}",
        "band": band,
    }


def _render_public_rating(
    public_comment: str,
    public_rating: dict[str, Any],
    config: dict[str, Any],
    requested: bool,
) -> str:
    if not requested or not public_rating.get("enabled") or not public_rating.get("formatted"):
        return public_comment
    publication = config.get("publication") if isinstance(config.get("publication"), dict) else {}
    rating = (
        publication.get("public_rating")
        if isinstance(publication.get("public_rating"), dict)
        else {}
    )
    placeholder = _text(rating.get("placeholder") or "{PUBLIC_RATING}")
    rendered = public_comment
    if placeholder.startswith("{") and placeholder.endswith("}"):
        rendered = rendered.replace("{" + placeholder + "}", public_rating["formatted"])
    rendered = rendered.replace(placeholder, public_rating["formatted"])
    return rendered


def _rating_mentions(public_comment: str) -> list[tuple[float, float]]:
    output = []
    for match in re.finditer(
        r"(?<!\d)(\d+(?:[.,]\d+)?)\s*/\s*(\d+(?:[.,]\d+)?)(?!\d)",
        public_comment,
    ):
        try:
            output.append((
                float(match.group(1).replace(",", ".")),
                float(match.group(2).replace(",", ".")),
            ))
        except ValueError:
            continue
    return output


def _normalize_publication_decision(
    result: dict[str, Any],
    internal: dict[str, Any],
    packet: dict[str, Any],
    config: dict[str, Any],
    public_comment: str,
    score: float | None,
    scoring_result: dict[str, Any],
    public_rating: dict[str, Any],
) -> dict[str, Any]:
    publication = config.get("publication") if isinstance(config.get("publication"), dict) else {}
    raw = result.get("publication_decision")
    explicit = isinstance(raw, dict)
    raw = raw if explicit else {}
    requested = bool(raw.get("publish")) if explicit else bool(public_comment)
    value_type = _text(raw.get("value_type") or ("legacy" if requested else "none")).lower()
    reason = _text(raw.get("reason"))
    novel_value = _text(raw.get("novel_value"))
    assessment = _text(raw.get("assessment")).lower()
    strength = _text(raw.get("strength"))
    recommendation = _text(raw.get("recommendation"))
    language_policy = (
        publication.get("language")
        if isinstance(publication.get("language"), dict)
        else {}
    )
    comment_language = _text(
        raw.get("comment_language")
        or internal.get("language")
        or language_policy.get("default")
        or "id"
    )
    evidence_refs = _string_list(raw.get("evidence_refs"))
    risk_flags = _string_list(raw.get("risk_flags"))
    positive_only = bool(publication.get("positive_only", True))
    min_public_score = max(
        0.0,
        min(100.0, float(publication.get("min_public_score", 70.0))),
    )
    post_score = scoring_result.get("post_score")
    if post_score is None and not scoring_result.get("conversation_status"):
        post_score = score
    core_content = (
        scoring_result.get("core_content")
        if isinstance(scoring_result.get("core_content"), dict)
        else {}
    )
    core_content_eligible = not core_content.get("required_for_publication") or bool(
        core_content.get("available")
    )
    positive_eligible = (
        score is not None
        and score >= min_public_score
        and post_score is not None
        and float(post_score) >= min_public_score
        and core_content_eligible
    )
    violations = []
    if publication.get("require_publication_decision", True) and not explicit:
        violations.append("explicit_publication_decision_required")
    if explicit and not reason:
        violations.append("publication_reason_required")
    if explicit and not requested and public_comment:
        violations.append("abstention_comment_must_be_empty")
    if requested:
        if positive_only and not positive_eligible:
            violations.append("positive_score_threshold_not_met")
        if (
            post_score is not None
            and float(post_score) < min_public_score
            and score is not None
            and score >= min_public_score
        ):
            violations.append("post_score_threshold_not_met")
        if not core_content_eligible:
            violations.append("core_content_incomplete")
        if positive_only and assessment != "positive":
            violations.append("positive_assessment_required")
        if publication.get("require_strength_and_recommendation", True):
            if not strength:
                violations.append("public_strength_required")
            if not recommendation:
                violations.append("public_recommendation_required")
        allowed_types = {
            _text(value).lower()
            for value in publication.get("allowed_value_types") or PUBLICATION_VALUE_TYPES
        }
        if value_type not in allowed_types:
            violations.append("unsupported_publication_value_type")
        if not public_comment:
            violations.append("public_comment_required")
        minimum = max(1, int(publication.get("min_public_comment_characters") or 20))
        maximum = max(minimum, int(publication.get("max_public_comment_characters") or 1000))
        if public_comment and len(public_comment) < minimum:
            violations.append("public_comment_too_short")
        if len(public_comment) > maximum:
            violations.append("public_comment_too_long")
        if publication.get("require_novel_value", True) and not novel_value:
            violations.append("novel_value_required")
        valid_references = _evidence_reference_set(packet, internal)
        resolved_references = [reference for reference in evidence_refs if reference in valid_references]
        if publication.get("require_evidence_refs", True) and not resolved_references:
            violations.append("valid_evidence_reference_required")
        if publication.get("disclose_automation", True):
            lowered = public_comment.casefold()
            if not any(marker in lowered for marker in AUTOMATION_DISCLOSURE_MARKERS) and not re.search(
                r"\bai\b", lowered
            ):
                violations.append("automation_disclosure_required")
        if not publication.get("allow_internal_diagnostics", False):
            lowered = public_comment.casefold()
            if any(marker in lowered for marker in INTERNAL_DIAGNOSTIC_MARKERS):
                violations.append("internal_diagnostics_not_public")
        if publication.get("recommendation_only", True):
            lowered = public_comment.casefold()
            if any(marker in lowered for marker in NEGATIVE_PUBLIC_JUDGMENT_MARKERS):
                violations.append("negative_public_judgment_not_allowed")
        rating_mentions = _rating_mentions(public_comment)
        if not publication.get("allow_public_scores", False) and rating_mentions:
            violations.append("public_score_not_allowed")
        elif public_rating.get("enabled"):
            expected_value = public_rating.get("value")
            expected_scale = float(public_rating.get("scale") or 10.0)
            matching = [
                mention
                for mention in rating_mentions
                if expected_value is not None
                and abs(mention[0] - float(expected_value)) < 0.001
                and abs(mention[1] - expected_scale) < 0.001
            ]
            if public_rating.get("required") and expected_value is None:
                violations.append("public_rating_unavailable")
            elif public_rating.get("required") and not rating_mentions:
                violations.append("public_rating_required")
            elif rating_mentions and not matching:
                violations.append("public_rating_mismatch")
            elif public_rating.get("required") and len(matching) != 1:
                violations.append("public_rating_must_appear_once")
            if any(abs(denominator - expected_scale) >= 0.001 for _, denominator in rating_mentions):
                violations.append("internal_or_unsupported_score_not_public")
        if "PUBLIC_RATING" in public_comment:
            violations.append("public_rating_placeholder_unresolved")
    status = "abstain"
    if requested and violations:
        status = "blocked_by_policy"
    elif requested:
        status = "publish"
    return {
        "explicit": explicit,
        "publish_requested": requested,
        "publish": requested and not violations,
        "status": status,
        "reason": reason,
        "assessment": assessment,
        "positive_eligible": positive_eligible,
        "score": score,
        "post_score": post_score,
        "minimum_public_score": min_public_score,
        "core_content_eligible": core_content_eligible,
        "public_rating": public_rating,
        "comment_language": comment_language,
        "language_mode": _text(language_policy.get("mode") or "auto"),
        "value_type": value_type,
        "novel_value": novel_value,
        "strength": strength,
        "recommendation": recommendation,
        "evidence_refs": evidence_refs,
        "risk_flags": risk_flags,
        "policy_violations": violations,
    }


def _normalize_sources(result: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for claim in result.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        claim_id = _text(claim.get("claim_id"))
        valid_urls = []
        for source in claim.get("sources") or []:
            if not isinstance(source, dict):
                continue
            url = _text(source.get("url"))
            if not url.startswith(("https://", "http://")):
                continue
            valid_urls.append(url)
            key = (claim_id, url)
            if key in seen:
                continue
            seen.add(key)
            sources.append({
                "claim_id": claim_id,
                "url": url,
                "title": _text(source.get("title")),
                "publisher": _text(source.get("publisher")),
                "source_type": _text(source.get("source_type")),
                "stance": _text(source.get("stance")),
                "retrieved_at": _text(source.get("retrieved_at")) or _now(),
                "evidence_text": _text(source.get("evidence")),
                "raw": source,
            })
        verdict = _text(claim.get("verdict")).lower()
        if verdict in {"supported", "contradicted", "misleading"} and not valid_urls:
            claim["verdict"] = "needs_verification"
            claim["verification_warning"] = "No valid source URL was supplied."
    for source in result.get("grounding_sources") or []:
        if not isinstance(source, dict):
            continue
        url = _text(source.get("url"))
        key = ("", url)
        if not url.startswith(("https://", "http://")) or key in seen:
            continue
        seen.add(key)
        sources.append({
            "claim_id": "",
            "url": url,
            "title": _text(source.get("title")),
            "publisher": _text(source.get("publisher")),
            "source_type": "model_grounding",
            "stance": "context",
            "retrieved_at": _now(),
            "evidence_text": "",
            "raw": source,
        })
    return sources


def apply_analysis_result(
    conn: sqlite3.Connection,
    analysis_id: str,
    result: dict[str, Any],
    config: dict[str, Any],
    *,
    provider: str,
    model: str = "",
) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise ValueError("analysis result must be a JSON object")
    ensure_analysis_schema(conn)
    row = conn.execute(
        "SELECT * FROM analysis_runs WHERE analysis_id = ?",
        (analysis_id,),
    ).fetchone()
    if not row:
        raise KeyError(f"unknown analysis_id: {analysis_id}")
    packet = _json_loads(row["evidence_json"], {})
    internal = _internal_analysis_payload(result)
    scoring_config = config.get("scoring") if isinstance(config.get("scoring"), dict) else {}
    score, scoring = _dimension_score(internal, scoring_config, packet)
    confidence = _clamp(internal.get("confidence"))
    claims = internal.get("claims") if isinstance(internal.get("claims"), list) else []
    questions = internal.get("comment_questions") if isinstance(internal.get("comment_questions"), list) else []
    source_payload = dict(internal)
    if isinstance(result.get("grounding_sources"), list):
        source_payload["grounding_sources"] = result["grounding_sources"]
    sources = _normalize_sources(source_payload)
    fact_check = {
        "status": "complete" if claims and all(
            _text(claim.get("verdict")) not in {"", "needs_verification", "unknown"}
            for claim in claims
            if isinstance(claim, dict)
        ) else "partial",
        "claims": claims,
        "source_count": len(sources),
    }
    question_gaps = {
        "questions": questions,
        "detected_question_count": len(packet.get("detected_questions") or []),
        "missing_information": internal.get("missing_information") or [],
    }
    completed_at = _now()
    draft_comment = _text(result.get("public_comment") or result.get("draft_comment"))
    raw_publication_decision = result.get("publication_decision")
    publish_requested = bool(
        raw_publication_decision.get("publish")
        if isinstance(raw_publication_decision, dict)
        else draft_comment
    )
    public_rating = _public_rating(score, config)
    draft_comment = _render_public_rating(
        draft_comment,
        public_rating,
        config,
        publish_requested,
    )
    publication_decision = _normalize_publication_decision(
        result,
        internal,
        packet,
        config,
        draft_comment,
        score,
        scoring,
        public_rating,
    )
    normalized_internal = {
        "summary": _text(internal.get("summary")),
        "language": _text(internal.get("language")),
        "content_genre": _text(internal.get("content_genre")),
        "helpful_comment_summary": (
            internal.get("helpful_comment_summary")
            if isinstance(internal.get("helpful_comment_summary"), list)
            else []
        ),
        "fact_check": fact_check,
        "question_gaps": question_gaps,
        "scoring": scoring,
        "score": score,
        "confidence": confidence,
        "data_completeness": packet.get("data_completeness"),
    }
    normalized_result = {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "internal_analysis": normalized_internal,
        "publication_decision": publication_decision,
        "public_comment": draft_comment,
        # Compatibility fields for existing report consumers.
        **normalized_internal,
        "draft_comment": draft_comment,
    }
    conn.execute(
        """
        UPDATE analysis_runs SET
            provider = ?, model = ?, status = 'complete', completed_at = ?,
            score = ?, confidence = ?, data_completeness = ?,
            fact_check_json = ?, question_gaps_json = ?, scoring_json = ?,
            publication_decision_json = ?, draft_comment = ?, result_json = ?, error = ''
        WHERE analysis_id = ?
        """,
        (
            provider,
            model,
            completed_at,
            score,
            confidence,
            packet.get("data_completeness"),
            _canonical_json(fact_check),
            _canonical_json(question_gaps),
            _canonical_json(scoring),
            _canonical_json(publication_decision),
            draft_comment,
            _canonical_json(normalized_result),
            analysis_id,
        ),
    )
    for source in sources:
        source_id = _stable_id(analysis_id, source["claim_id"], source["url"])
        conn.execute(
            """
            INSERT OR REPLACE INTO fact_check_sources (
                source_id, analysis_id, claim_id, url, title, publisher,
                source_type, stance, retrieved_at, evidence_text, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                analysis_id,
                source["claim_id"],
                source["url"],
                source["title"],
                source["publisher"],
                source["source_type"],
                source["stance"],
                source["retrieved_at"],
                source["evidence_text"],
                _canonical_json(source["raw"]),
            ),
        )
    conn.execute(
        """
        UPDATE content_analysis_state SET
            status = 'complete', analyzed_at = ?, stale_reason = '', error = ''
        WHERE project = ? AND platform = ? AND content_key = ?
          AND latest_analysis_id = ?
        """,
        (completed_at, row["project"], row["platform"], row["content_key"], analysis_id),
    )
    conn.commit()
    return normalized_result


def _parse_model_json(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value)
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise ValueError("model response was not a JSON object")
    return payload


def analyze_with_gemini(packet: dict[str, Any], config: dict[str, Any]) -> tuple[dict[str, Any], str]:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is required for provider=gemini")
    model = _text(config.get("model")) or "gemini-2.5-flash"
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": build_analysis_prompt(packet, config)}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }
    fact_check = config.get("fact_check") if isinstance(config.get("fact_check"), dict) else {}
    if fact_check.get("google_search_grounding", True):
        body["tools"] = [{"google_search": {}}]
    response = requests.post(
        endpoint,
        params={"key": api_key},
        json=body,
        timeout=max(30, int(config.get("timeout_seconds") or 120)),
    )
    response.raise_for_status()
    payload = response.json()
    candidates = payload.get("candidates") or []
    if not candidates:
        raise RuntimeError("Gemini returned no candidates")
    parts = candidates[0].get("content", {}).get("parts") or []
    text = "".join(_text(part.get("text")) for part in parts if isinstance(part, dict))
    result = _parse_model_json(text)
    grounding = candidates[0].get("groundingMetadata") or {}
    grounded_sources = []
    for chunk in grounding.get("groundingChunks") or []:
        web = chunk.get("web") if isinstance(chunk, dict) else None
        if isinstance(web, dict) and _text(web.get("uri")):
            grounded_sources.append({"url": web["uri"], "title": _text(web.get("title"))})
    if grounded_sources:
        result["grounding_sources"] = grounded_sources
    return result, model


def run_pending_analyses(
    conn: sqlite3.Connection,
    project: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    provider = _text(config.get("provider") or "manual").lower()
    limit = max(0, int(config.get("max_posts_per_run") or 20))
    max_attempts = max(1, int(config.get("max_attempts") or 3))
    stats: dict[str, Any] = {
        "provider": provider,
        "attempted": 0,
        "completed": 0,
        "failed": 0,
        "pending_manual": 0,
        "errors": [],
    }
    pending = conn.execute(
        """
        SELECT * FROM analysis_runs
        WHERE project = ? AND status IN ('pending', 'stale', 'failed')
          AND attempt_count < ?
        ORDER BY created_at, platform, content_key
        LIMIT ?
        """,
        (project, max_attempts, limit if limit > 0 else 2147483647),
    ).fetchall()
    if provider in {"manual", "none", "codex"}:
        stats["pending_manual"] = len(pending)
        return stats
    if provider != "gemini":
        stats["errors"].append(f"Unsupported analysis provider: {provider}")
        return stats
    for row in pending:
        stats["attempted"] += 1
        started_at = _now()
        conn.execute(
            """
            UPDATE analysis_runs SET status = 'processing', started_at = ?,
                attempt_count = attempt_count + 1, error = ''
            WHERE analysis_id = ?
            """,
            (started_at, row["analysis_id"]),
        )
        conn.commit()
        try:
            packet = _json_loads(row["evidence_json"], {})
            result, model = analyze_with_gemini(packet, config)
            apply_analysis_result(
                conn,
                row["analysis_id"],
                result,
                config,
                provider=provider,
                model=model,
            )
            stats["completed"] += 1
        except Exception as exc:
            message = str(exc)
            conn.execute(
                "UPDATE analysis_runs SET status = 'failed', error = ? WHERE analysis_id = ?",
                (message, row["analysis_id"]),
            )
            conn.execute(
                """
                UPDATE content_analysis_state SET status = 'failed', error = ?
                WHERE project = ? AND platform = ? AND content_key = ?
                  AND latest_analysis_id = ?
                """,
                (message, project, row["platform"], row["content_key"], row["analysis_id"]),
            )
            conn.commit()
            stats["failed"] += 1
            stats["errors"].append({"analysis_id": row["analysis_id"], "error": message})
    return stats


def queue_publication_drafts(
    conn: sqlite3.Connection,
    project: str,
    config: dict[str, Any],
) -> dict[str, int]:
    stats = {
        "eligible": 0,
        "queued": 0,
        "skipped": 0,
        "abstained": 0,
        "stale": 0,
        "expired": 0,
    }
    publication = config.get("publication") if isinstance(config.get("publication"), dict) else {}
    mode = _text(publication.get("mode") or "shadow").lower()
    if mode == "disabled":
        return stats
    allowed_platforms = {
        _text(value).lower()
        for value in publication.get("allowed_platforms") or ["youtube", "tiktok", "instagram", "facebook", "x"]
    }
    allowed_value_types = {
        _text(value).lower()
        for value in publication.get("allowed_value_types") or PUBLICATION_VALUE_TYPES
    }
    min_confidence = float(publication.get("min_confidence") or 70.0)
    min_completeness = float(publication.get("min_data_completeness") or 50.0)
    require_score = bool(publication.get("require_score", False))
    positive_only = bool(publication.get("positive_only", True))
    min_public_score = max(
        0.0,
        min(100.0, float(publication.get("min_public_score", 70.0))),
    )
    require_decision = bool(publication.get("require_publication_decision", True))
    require_fresh = bool(publication.get("require_fresh_evidence", True))
    max_comments = max(0, int(config.get("max_comments_per_post") or 200))
    max_evidence_age = max(1, int(publication.get("max_evidence_age_minutes") or 60))
    max_draft_age = max(1, int(publication.get("max_draft_age_minutes") or 60))
    min_length = max(1, int(publication.get("min_public_comment_characters") or 20))
    max_length = max(min_length, int(publication.get("max_public_comment_characters") or 1000))
    approval_required = 1 if publication.get("require_approval", True) else 0
    rows = conn.execute(
        """
        SELECT ar.*
        FROM analysis_runs ar
        WHERE ar.project = ? AND ar.status = 'complete'
        ORDER BY ar.completed_at, ar.analysis_id
        """,
        (project,),
    ).fetchall()
    now = _now()
    now_datetime = _parse_datetime(now) or dt.datetime.now().astimezone()
    for row in rows:
        decision = _json_loads(row["publication_decision_json"], {})
        if not isinstance(decision, dict):
            decision = {}
        requested = bool(decision.get("publish_requested"))
        if not requested:
            stats["abstained"] += 1
            continue
        if not decision.get("publish") or decision.get("policy_violations"):
            stats["skipped"] += 1
            continue
        if require_decision and not decision.get("explicit"):
            stats["skipped"] += 1
            continue
        if _text(decision.get("value_type")).lower() not in allowed_value_types:
            stats["skipped"] += 1
            continue
        if row["platform"] not in allowed_platforms:
            stats["skipped"] += 1
            continue
        if row["confidence"] is None or float(row["confidence"]) < min_confidence:
            stats["skipped"] += 1
            continue
        if row["data_completeness"] is None or float(row["data_completeness"]) < min_completeness:
            stats["skipped"] += 1
            continue
        if require_score and row["score"] is None:
            stats["skipped"] += 1
            continue
        if positive_only and (
            row["score"] is None or float(row["score"]) < min_public_score
        ):
            stats["skipped"] += 1
            continue
        draft_text = _text(row["draft_comment"])
        if not min_length <= len(draft_text) <= max_length:
            stats["skipped"] += 1
            continue
        content = conn.execute(
            """
            SELECT * FROM content_items
            WHERE project = ? AND platform = ? AND content_key = ?
            """,
            (project, row["platform"], row["content_key"]),
        ).fetchone()
        if not content:
            stats["skipped"] += 1
            continue
        packet, current_hash = build_evidence_packet(
            conn,
            content,
            max_comments=max_comments,
        )
        if current_hash != row["input_hash"]:
            conn.execute(
                """
                UPDATE content_analysis_state SET
                    status = 'stale', stale_reason = 'evidence_changed_before_publication'
                WHERE project = ? AND platform = ? AND content_key = ?
                  AND latest_analysis_id = ?
                """,
                (project, row["platform"], row["content_key"], row["analysis_id"]),
            )
            conn.execute(
                """
                UPDATE publication_queue SET status = 'stale',
                    error = 'Evidence changed before publication', updated_at = ?
                WHERE analysis_id = ? AND status IN ('draft', 'reviewed', 'approved')
                """,
                (now, row["analysis_id"]),
            )
            stats["stale"] += 1
            continue
        completed = _parse_datetime(row["completed_at"])
        observed = _parse_datetime(packet.get("observed_at"))
        draft_deadline = completed + dt.timedelta(minutes=max_draft_age) if completed else None
        evidence_deadline = observed + dt.timedelta(minutes=max_evidence_age) if observed else None
        deadlines = [deadline for deadline in (draft_deadline, evidence_deadline if require_fresh else None) if deadline]
        valid_until = min(deadlines) if deadlines else None
        expired = draft_deadline is None or draft_deadline <= now_datetime
        if require_fresh:
            expired = expired or evidence_deadline is None or evidence_deadline <= now_datetime
        if expired:
            conn.execute(
                """
                UPDATE publication_queue SET status = 'expired',
                    error = 'Analysis or evidence freshness window expired', updated_at = ?
                WHERE analysis_id = ? AND status IN ('draft', 'reviewed', 'approved')
                """,
                (now, row["analysis_id"]),
            )
            stats["expired"] += 1
            continue
        stats["eligible"] += 1
        draft_hash = hashlib.sha256(draft_text.encode("utf-8")).hexdigest()
        publication_id = _stable_id(project, row["platform"], row["content_key"], row["analysis_id"])
        inserted = conn.execute(
            """
            INSERT OR IGNORE INTO publication_queue (
                publication_id, project, platform, content_key, analysis_id,
                target_url, mode, status, draft_text, draft_hash,
                decision_json, analysis_input_hash, evidence_observed_at,
                valid_until, max_comments_per_post, revalidation_status,
                revalidated_at, approval_required, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?,
                      'validated', ?, ?, ?, ?)
            """,
            (
                publication_id,
                project,
                row["platform"],
                row["content_key"],
                row["analysis_id"],
                content["url"],
                mode,
                draft_text,
                draft_hash,
                _canonical_json(decision),
                row["input_hash"],
                _text(packet.get("observed_at")),
                valid_until.isoformat() if valid_until else "",
                max_comments,
                now,
                approval_required,
                now,
                now,
            ),
        ).rowcount
        stats["queued"] += int(bool(inserted))
    conn.commit()
    return stats


def publication_ai_review_hash(
    *,
    publication_id: str,
    analysis_id: str,
    analysis_input_hash: str,
    draft_hash: str,
    reviewer: str,
    reviewed_at: str,
    review_payload: dict[str, Any],
    target_url: str = "",
    content_key: str = "",
    expected_account: str = "",
    decision_hash: str = "",
    analysis_result_hash: str = "",
) -> str:
    """Bind an independent AI review to one evidence version and final text."""
    value = {
        "publication_id": _text(publication_id),
        "analysis_id": _text(analysis_id),
        "analysis_input_hash": _text(analysis_input_hash),
        "draft_hash": _text(draft_hash),
        "target_url": _text(target_url),
        "content_key": _text(content_key),
        "expected_account": _text(expected_account),
        "decision_hash": _text(decision_hash),
        "analysis_result_hash": _text(analysis_result_hash),
        "reviewer": _text(reviewer),
        "reviewed_at": _text(reviewed_at),
        "review": review_payload,
    }
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def record_publication_ai_review(
    conn: sqlite3.Connection,
    publication_id: str,
    review_payload: dict[str, Any],
    *,
    reviewer: str,
    expected_draft_hash: str,
) -> dict[str, Any]:
    """Store a separate, hash-bound AI critic decision before authorization."""
    ensure_analysis_schema(conn)
    if not isinstance(review_payload, dict):
        raise ValueError("AI review must be a JSON object")
    reviewer = _text(reviewer)
    if not reviewer:
        raise ValueError("AI reviewer identity is required")
    row = conn.execute(
        "SELECT * FROM publication_queue WHERE publication_id = ?",
        (publication_id,),
    ).fetchone()
    if not row:
        raise KeyError(f"unknown publication_id: {publication_id}")
    if row["status"] not in {"draft", "reviewed"}:
        raise ValueError("Only an unapproved draft can receive an AI review")
    analysis_row = conn.execute(
        """
        SELECT status, publication_decision_json, result_json
        FROM analysis_runs WHERE analysis_id = ?
        """,
        (row["analysis_id"],),
    ).fetchone()
    if not analysis_row or analysis_row["status"] != "complete":
        raise ValueError("A completed analysis is required before AI review")
    queue_decision = _json_loads(row["decision_json"], {})
    analysis_decision = _json_loads(
        analysis_row["publication_decision_json"],
        {},
    )
    if queue_decision != analysis_decision:
        raise ValueError("Publication decision does not match the completed analysis")
    decision_hash = hashlib.sha256(
        _canonical_json(queue_decision).encode("utf-8")
    ).hexdigest()
    analysis_result = _json_loads(analysis_row["result_json"], {})
    analysis_result_hash = hashlib.sha256(
        _canonical_json(analysis_result).encode("utf-8")
    ).hexdigest()
    if not expected_draft_hash or expected_draft_hash != row["draft_hash"]:
        raise ValueError("AI review text hash does not match the stored final text")
    if review_payload.get("approved") is not True:
        raise ValueError("AI review did not approve the response")
    if review_payload.get("independent_review") is not True:
        raise ValueError("AI review must be an explicitly independent critic pass")
    payload_text_hash = _text(review_payload.get("reviewed_text_hash"))
    if payload_text_hash != row["draft_hash"]:
        raise ValueError("AI review payload is not bound to the final text hash")
    payload_evidence_hash = _text(review_payload.get("analysis_input_hash"))
    if payload_evidence_hash != row["analysis_input_hash"]:
        raise ValueError("AI review payload is not bound to the analysis evidence hash")
    payload_analysis_id = _text(review_payload.get("analysis_id"))
    if payload_analysis_id != row["analysis_id"]:
        raise ValueError("AI review payload is not bound to the completed analysis")
    for field, expected in {
        "target_url": row["target_url"],
        "content_key": row["content_key"],
        "expected_account": row["expected_account"],
        "decision_hash": decision_hash,
        "analysis_result_hash": analysis_result_hash,
    }.items():
        if _text(review_payload.get(field)) != _text(expected):
            raise ValueError(f"AI review payload has a mismatched {field}")

    reviewed_at = _now()
    normalized_review = {
        **review_payload,
        "approved": True,
        "independent_review": True,
        "analysis_id": row["analysis_id"],
        "analysis_input_hash": row["analysis_input_hash"],
        "reviewed_text_hash": row["draft_hash"],
        "target_url": row["target_url"],
        "content_key": row["content_key"],
        "expected_account": row["expected_account"],
        "decision_hash": decision_hash,
        "analysis_result_hash": analysis_result_hash,
    }
    review_hash = publication_ai_review_hash(
        publication_id=row["publication_id"],
        analysis_id=row["analysis_id"],
        analysis_input_hash=row["analysis_input_hash"],
        draft_hash=row["draft_hash"],
        reviewer=reviewer,
        reviewed_at=reviewed_at,
        review_payload=normalized_review,
        target_url=row["target_url"],
        content_key=row["content_key"],
        expected_account=row["expected_account"],
        decision_hash=decision_hash,
        analysis_result_hash=analysis_result_hash,
    )
    conn.execute(
        """
        UPDATE publication_queue SET
            status = 'reviewed',
            ai_review_status = 'approved',
            ai_reviewed_at = ?,
            ai_reviewer = ?,
            ai_review_text_hash = ?,
            ai_review_evidence_hash = ?,
            ai_review_target_url = ?,
            ai_review_content_key = ?,
            ai_review_decision_hash = ?,
            ai_review_analysis_hash = ?,
            ai_review_json = ?,
            ai_review_hash = ?,
            approved_at = '',
            approved_by = '',
            authorization_presentation_hash = '',
            authorization_text_hash = '',
            authorization_review_hash = '',
            authorization_target_url = '',
            authorization_content_key = '',
            authorization_decision_hash = '',
            authorization_analysis_hash = '',
            authorization_expected_account = '',
            updated_at = ?
        WHERE publication_id = ?
        """,
        (
            reviewed_at,
            reviewer,
            row["draft_hash"],
            row["analysis_input_hash"],
            row["target_url"],
            row["content_key"],
            decision_hash,
            analysis_result_hash,
            _canonical_json(normalized_review),
            review_hash,
            reviewed_at,
            publication_id,
        ),
    )
    conn.commit()
    return {
        "publication_id": publication_id,
        "status": "reviewed",
        "review_hash": review_hash,
        "reviewed_text_hash": row["draft_hash"],
        "reviewed_at": reviewed_at,
        "reviewer": reviewer,
    }


def authorize_publication(
    conn: sqlite3.Connection,
    publication_id: str,
    *,
    authorized_by: str,
    expected_draft_hash: str,
    expected_review_hash: str,
) -> dict[str, Any]:
    """Record explicit user authorization for the exact reviewed response."""
    ensure_analysis_schema(conn)
    authorized_by = _text(authorized_by)
    if not authorized_by:
        raise ValueError("Authorizer identity is required")
    if authorized_by.casefold() in {"ai", "codex", "antigravity", "system", "automation"}:
        raise ValueError("AI or automation cannot grant user publication authorization")
    row = conn.execute(
        "SELECT * FROM publication_queue WHERE publication_id = ?",
        (publication_id,),
    ).fetchone()
    if not row:
        raise KeyError(f"unknown publication_id: {publication_id}")
    if row["mode"] != "live":
        raise ValueError("Only a live-mode reviewed response can be authorized")
    if row["status"] != "reviewed" or row["ai_review_status"] != "approved":
        raise ValueError("Independent AI review approval is required before authorization")
    if not expected_draft_hash or expected_draft_hash != row["draft_hash"]:
        raise ValueError("Authorization text hash does not match the reviewed response")
    if row["ai_review_text_hash"] != row["draft_hash"]:
        raise ValueError("Reviewed response hash no longer matches the stored response")
    if (
        row["ai_review_target_url"] != row["target_url"]
        or row["ai_review_content_key"] != row["content_key"]
        or row["ai_review_evidence_hash"] != row["analysis_input_hash"]
    ):
        raise ValueError("Reviewed target or evidence no longer matches")
    if not expected_review_hash or expected_review_hash != row["ai_review_hash"]:
        raise ValueError("Authorization review hash does not match the stored AI review")

    approved_at = _now()
    conn.execute(
        """
        UPDATE publication_queue SET
            status = 'approved',
            approved_at = ?,
            approved_by = ?,
            authorization_text_hash = ?,
            authorization_review_hash = ?,
            authorization_target_url = ?,
            authorization_content_key = ?,
            authorization_decision_hash = ?,
            authorization_analysis_hash = ?,
            authorization_expected_account = ?,
            updated_at = ?
        WHERE publication_id = ?
        """,
        (
            approved_at,
            authorized_by,
            row["draft_hash"],
            row["ai_review_hash"],
            row["target_url"],
            row["content_key"],
            row["ai_review_decision_hash"],
            row["ai_review_analysis_hash"],
            row["expected_account"],
            approved_at,
            publication_id,
        ),
    )
    conn.commit()
    return {
        "publication_id": publication_id,
        "status": "approved",
        "authorized_at": approved_at,
        "authorized_by": authorized_by,
        "authorized_text_hash": row["draft_hash"],
        "authorized_review_hash": row["ai_review_hash"],
    }


def run_analysis_workflow(
    conn: sqlite3.Connection,
    project: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    ensure_analysis_schema(conn)
    if not config.get("enabled", False):
        return {"status": "disabled"}
    queue = sync_analysis_queue(conn, project, config)
    analysis = run_pending_analyses(conn, project, config)
    publication = queue_publication_drafts(conn, project, config)
    return {
        "status": "complete",
        "mode": _text(config.get("mode") or "shadow"),
        "queue": queue,
        "analysis": analysis,
        "publication": publication,
    }


def load_analysis_lookup(
    conn: sqlite3.Connection,
    project: str,
    content_keys: Iterable[str] | None = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    selected = set(content_keys or [])
    rows = conn.execute(
        """
        SELECT cas.*, ar.score, ar.confidence, ar.data_completeness,
               ar.provider, ar.model, ar.completed_at, ar.fact_check_json,
               ar.question_gaps_json, ar.scoring_json,
               ar.publication_decision_json, ar.draft_comment
        FROM content_analysis_state cas
        LEFT JOIN analysis_runs ar ON ar.analysis_id = cas.latest_analysis_id
        WHERE cas.project = ?
        """,
        (project,),
    ).fetchall()
    lookup: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if selected and row["content_key"] not in selected:
            continue
        lookup[(row["platform"], row["content_key"])] = {
            "status": row["status"],
            "analyzed": row["status"] == "complete",
            "stale": row["status"] == "stale",
            "analysis_id": row["latest_analysis_id"],
            "analysis_version": row["analysis_version"],
            "formula_version": row["formula_version"],
            "input_hash": row["current_input_hash"],
            "analyzed_at": row["analyzed_at"],
            "stale_reason": row["stale_reason"],
            "score": row["score"],
            "confidence": row["confidence"],
            "data_completeness": row["data_completeness"],
            "provider": row["provider"],
            "model": row["model"],
            "fact_check": _json_loads(row["fact_check_json"], {}),
            "question_gaps": _json_loads(row["question_gaps_json"], {}),
            "scoring": _json_loads(row["scoring_json"], {}),
            "publication_decision": _json_loads(
                row["publication_decision_json"], {}
            ),
            "publication_draft_available": bool(_text(row["draft_comment"])),
            "error": row["error"],
        }
    return lookup


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _atomic_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    count = 0
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(_canonical_json(row))
            handle.write("\n")
            count += 1
    temporary.replace(path)
    return count


def export_analysis_artifacts(
    conn: sqlite3.Connection,
    project: str,
    output_dir: Path,
    config: dict[str, Any],
    *,
    content_keys: Iterable[str] | None = None,
) -> dict[str, Any]:
    ensure_analysis_schema(conn)
    selected = set(content_keys or [])
    rows = conn.execute(
        "SELECT * FROM analysis_runs WHERE project = ? ORDER BY created_at, analysis_id",
        (project,),
    ).fetchall()
    pending_rows = []
    completed_rows = []
    for row in rows:
        if selected and row["content_key"] not in selected:
            continue
        packet = _json_loads(row["evidence_json"], {})
        base = {
            "analysis_id": row["analysis_id"],
            "project": row["project"],
            "platform": row["platform"],
            "content_key": row["content_key"],
            "input_hash": row["input_hash"],
            "analysis_version": row["analysis_version"],
            "formula_version": row["formula_version"],
            "provider": row["provider"],
            "model": row["model"],
            "status": row["status"],
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
        }
        if row["status"] == "complete":
            completed_rows.append({
                **base,
                "result": _json_loads(row["result_json"], {}),
            })
        else:
            pending_rows.append({
                **base,
                "evidence_packet": packet,
                "prompt": build_analysis_prompt(packet, config),
                "error": row["error"],
            })
    publication_rows = conn.execute(
        "SELECT * FROM publication_queue WHERE project = ? ORDER BY created_at, publication_id",
        (project,),
    ).fetchall()
    publication_export = []
    for row in publication_rows:
        if selected and row["content_key"] not in selected:
            continue
        publication_export.append({key: row[key] for key in row.keys()})

    queue_path = output_dir / "analysis_queue.jsonl"
    analyses_path = output_dir / "analyses.jsonl"
    publication_path = output_dir / "publication_queue.jsonl"
    summary_path = output_dir / "analysis_summary.json"
    queue_count = _atomic_jsonl(queue_path, pending_rows)
    analyses_count = _atomic_jsonl(analyses_path, completed_rows)
    publication_count = _atomic_jsonl(publication_path, publication_export)
    status_counts: dict[str, int] = {}
    for row in [*pending_rows, *completed_rows]:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
    decision_counts: dict[str, int] = {}
    for row in completed_rows:
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        decision = result.get("publication_decision")
        decision = decision if isinstance(decision, dict) else {}
        decision_status = _text(decision.get("status") or "legacy_or_missing")
        decision_counts[decision_status] = decision_counts.get(decision_status, 0) + 1
    summary = {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "project": project,
        "generated_at": _now(),
        "native_text_only": True,
        "media_processing_used": False,
        "analysis_status_counts": status_counts,
        "publication_decision_status_counts": decision_counts,
        "pending_analysis_records": queue_count,
        "completed_analysis_records": analyses_count,
        "publication_drafts": publication_count,
        "publication_is_shadow_only": all(row.get("mode") != "live" for row in publication_export),
        "artifacts": {
            "analysis_queue": queue_path.name,
            "analyses": analyses_path.name,
            "publication_queue": publication_path.name,
        },
    }
    _atomic_json(summary_path, summary)
    return {
        "summary": summary,
        "summary_path": str(summary_path),
        "analysis_queue_path": str(queue_path),
        "analyses_path": str(analyses_path),
        "publication_queue_path": str(publication_path),
        "lookup": load_analysis_lookup(conn, project, selected),
    }


def import_analysis_results(
    conn: sqlite3.Connection,
    project: str,
    path: Path,
    config: dict[str, Any],
    *,
    provider: str = "manual",
    model: str = "",
) -> dict[str, int]:
    applied = 0
    skipped = 0
    with path.open("r", encoding="utf-8-sig") as handle:
        records = [json.loads(line) for line in handle if line.strip()] if path.suffix == ".jsonl" else json.load(handle)
    if isinstance(records, dict):
        records = records.get("results") or [records]
    for record in records:
        if not isinstance(record, dict):
            skipped += 1
            continue
        analysis_id = _text(record.get("analysis_id"))
        result = record.get("result") if isinstance(record.get("result"), dict) else record
        row = conn.execute(
            "SELECT project FROM analysis_runs WHERE analysis_id = ?",
            (analysis_id,),
        ).fetchone()
        if not row or row["project"] != project:
            skipped += 1
            continue
        apply_analysis_result(
            conn,
            analysis_id,
            result,
            config,
            provider=provider,
            model=model,
        )
        applied += 1
    return {"applied": applied, "skipped": skipped}
