#!/usr/bin/env python
"""Submit one approved TikTok publication draft through the visible web UI."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from playwright.async_api import (
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from comment_publication_capture import (
    PublicationCapture,
    store_captures,
    write_capture_files,
)
from tiktok_scraper.analysis_workflow import (
    build_evidence_packet,
    ensure_analysis_schema,
    publication_ai_review_hash,
)
from tiktok_master_database import (
    DEFAULT_MASTER_DATABASE,
    attach_master_database,
    bind_confirmed_comment_remote_id as bind_master_confirmed_comment_remote_id,
    comment_target_guard,
    mark_submit_intent as mark_master_submit_intent,
    register_publication_claim,
    register_publication_outcome,
    register_run_from_local,
)
from engage_tiktok import (
    DEFAULT_BROWSER_STATE,
    PROFILE7_STARTUP_TIMEOUT_SECONDS,
    SocialBrowserPreflight,
    active_tiktok_account,
    is_automation_identity,
)


INPUT_SELECTORS = (
    '[data-e2e="comment-input"] [contenteditable="true"]',
    '[data-e2e="comment-input"][contenteditable="true"]',
    'div.public-DraftEditor-content[contenteditable="true"][role="textbox"]',
    '[contenteditable="true"][role="textbox"][aria-describedby^="placeholder-"]',
    'div[contenteditable="true"][aria-label*="comment" i]',
    'textarea[placeholder*="comment" i]',
    'textarea[placeholder*="komentar" i]',
)
COMMENTS_TAB_SELECTORS = (
    'button[data-testid="tux-web-tab-bar"]:has-text("Comments")',
    'button[data-testid="tux-web-tab-bar"]:has-text("Komentar")',
    'button:has([data-e2e="comment-icon"])',
    'div[data-e2e="comment-icon"]',
    'span[data-e2e="comment-icon"]',
    '[data-e2e="comment-icon"]',
    '[data-e2e="video-comment-icon"]',
    'button[aria-label*="comment" i]',
    'button[aria-label*="komentar" i]',
    'button[role="tab"]:has-text("Comments")',
    '#tabs-0-tab-search-comment'
)
SUBMIT_SELECTORS = (
    '[data-e2e="comment-post"]',
    'button:has-text("Post")',
    '[role="button"]:has-text("Post")',
    'button:has-text("Kirim")',
    '[role="button"]:has-text("Kirim")',
)
RATING_PATTERN = re.compile(
    r"(?<!\d)(\d+(?:[.,]\d+)?)\s*/\s*(10(?:[.,]0+)?)(?!\d)"
)
UNRATED_TEXT_RATING_PATTERN = re.compile(
    r"""
    (?:
        \b(?:\d+(?:[.,]\d+)?|zero|one|two|three|four|five|six|seven|eight|nine|ten)
        \s+out\s+of\s+(?:10|ten)\b
      |
        \b(?:\d+(?:[.,]\d+)?|zero|one|two|three|four|five)
        \s+stars?\b
      |
        \b(?:rating|score)\s*(?::|=|is|of|at)\s*
        (?:\d+(?:[.,]\d+)?|zero|one|two|three|four|five|six|seven|eight|nine|ten)\b
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
AI_DISCLOSURE_PATTERN = re.compile(
    r"""
    (?:
        \bAI[\s-]+(?:assisted|generated|drafted|written)\b
      |
        \b(?:assisted|generated|drafted|written)\s+by\s+AI\b
      |
        \b(?:berbantuan|dibantu|dibuat\s+oleh|dihasilkan\s+oleh)\s+AI\b
      |
        (?:^|[.!?]\s+)\s*AI\s+
        (?:review|perspective|take|comment|assessment)\b
      |
        \[\s*AI\s*\]
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
TIKTOK_TARGET_ID_KEYS = {"aweme_id", "item_id", "video_id"}
POSITIVE_RESPONSE_TYPE = "positive_support"
UNRATED_RESPONSE_TYPES = frozenset(
    {
        "constructive_suggestion",
        "constructive_correction",
        "clarifying_question",
    }
)
PUBLISHABLE_RESPONSE_TYPES = frozenset(
    {POSITIVE_RESPONSE_TYPE, *UNRATED_RESPONSE_TYPES}
)
DEFAULT_DAILY_LIMIT = 0
PUBLICATION_CAPTURE_SETTLE_TIMEOUT_SECONDS = 3.0
PUBLICATION_CAPTURE_CANCEL_TIMEOUT_SECONDS = 0.5
ENGAGE_FAILED_POST_STATUSES = frozenset(
    {
        "failed",
        "publication_failed",
        "publication_uncertain",
        "stale",
        "expired",
    }
)
ENGAGE_TERMINAL_POST_STATUSES = frozenset(
    {"published", "skipped", *ENGAGE_FAILED_POST_STATUSES}
)


def now_iso() -> str:
    return dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def parse_iso(value: Any) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone() if parsed.tzinfo else parsed.astimezone()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def json_object(value: Any, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} contains invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return parsed


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        is not None
    )


def resolved_master_database(
    database: Path,
    master_database: str | Path | None,
) -> Path:
    """Resolve every publisher path to the workspace registry by default."""
    del database
    if master_database in (None, ""):
        return Path(DEFAULT_MASTER_DATABASE).resolve()
    return Path(master_database).resolve()


def bind_engage_master_database(
    database: Path,
    publication: dict[str, Any],
    master_database: str | Path | None,
) -> Path:
    """Validate or atomically initialize an ENGAGE run's master binding."""
    database = Path(database).resolve()
    selected = resolved_master_database(database, master_database)
    engage_run_id = str(publication.get("engage_run_id") or "").strip()
    if not engage_run_id:
        raise RuntimeError(
            "Publication is not linked to an ENGAGE run for master binding"
        )

    conn = sqlite3.connect(database, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        if not table_exists(conn, "engage_tiktok_runs"):
            raise RuntimeError(
                "ENGAGE run table is missing for publisher master binding"
            )
        conn.execute("BEGIN IMMEDIATE")
        run_columns = {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(engage_tiktok_runs)"
            ).fetchall()
        }
        if "master_database" not in run_columns:
            conn.execute(
                "ALTER TABLE engage_tiktok_runs "
                "ADD COLUMN master_database TEXT NOT NULL DEFAULT ''"
            )
            run_columns.add("master_database")
        publication_id = str(
            publication.get("publication_id") or ""
        ).strip()
        if publication_id and table_exists(conn, "publication_queue"):
            queue_columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(publication_queue)"
                ).fetchall()
            }
            if "engage_run_id" not in queue_columns:
                raise RuntimeError(
                    "Publication queue has not been migrated for ENGAGE "
                    "master binding"
                )
            queue_row = conn.execute(
                """
                SELECT engage_run_id
                FROM publication_queue
                WHERE publication_id=?
                """,
                (publication_id,),
            ).fetchone()
            if not queue_row:
                raise RuntimeError(
                    "Publication is missing for ENGAGE master binding"
                )
            queue_run_id = str(queue_row[0] or "").strip()
            if not queue_run_id or queue_run_id != engage_run_id:
                raise RuntimeError(
                    "Publication ENGAGE run binding changed before master "
                    "database validation"
                )

        row = conn.execute(
            """
            SELECT master_database
            FROM engage_tiktok_runs
            WHERE run_id=?
            """,
            (engage_run_id,),
        ).fetchone()
        if not row:
            raise RuntimeError(
                "ENGAGE run is missing for publisher master binding"
            )

        stored = str(row["master_database"] or "").strip()
        if stored:
            bound = Path(stored).resolve()
            if bound != selected:
                raise RuntimeError(
                    "Publisher master database does not match the ENGAGE "
                    f"run binding: stored={bound}, selected={selected}"
                )
        else:
            timestamp = now_iso()
            if "updated_at" in run_columns:
                changed = conn.execute(
                    """
                    UPDATE engage_tiktok_runs
                    SET master_database=?, updated_at=?
                    WHERE run_id=? AND TRIM(master_database)=''
                    """,
                    (str(selected), timestamp, engage_run_id),
                ).rowcount
            else:
                changed = conn.execute(
                    """
                    UPDATE engage_tiktok_runs
                    SET master_database=?
                    WHERE run_id=? AND TRIM(master_database)=''
                    """,
                    (str(selected), engage_run_id),
                ).rowcount
            if changed != 1:
                raise RuntimeError(
                    "ENGAGE run master database could not be atomically bound"
                )
            bound = selected

        conn.commit()
        publication["_engage_master_database"] = str(bound)
        return bound
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def tiktok_video_id_from_url(url: str) -> str:
    parsed = urlsplit(str(url or ""))
    if (parsed.hostname or "").casefold() not in {"tiktok.com", "www.tiktok.com"}:
        return ""
    match = re.search(r"/(?:video|photo)/([^/?#]+)", parsed.path)
    return match.group(1) if match else ""


def _target_ids_from_value(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in TIKTOK_TARGET_ID_KEYS and child not in (None, ""):
                found.add(str(child))
            found.update(_target_ids_from_value(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_target_ids_from_value(child))
    return found


def capture_targets_content(record: dict[str, Any], content_key: str) -> bool:
    ids = _target_ids_from_value(record.get("request_body_template"))
    for key, value in parse_qsl(
        urlsplit(str(record.get("request_url") or "")).query,
        keep_blank_values=True,
    ):
        if key.casefold() in TIKTOK_TARGET_ID_KEYS and value:
            ids.add(value)
    return str(content_key) in ids


def capture_confirms_submission(
    record: dict[str, Any],
    *,
    content_key: str,
    final_text: str,
    observed_account: str,
) -> bool:
    """Validate a response captured from this attempt's own submit request.

    The caller must separately fence capture to requests begun after submit
    intent. A matching target alone does not bind a response to the reviewed
    text, and a generic HTTP 200 does not prove that a comment was created.
    """
    if (
        record.get("method") != "POST"
        or not is_tiktok_comment_publish_url(str(record.get("request_url") or ""))
        or not response_success(record)
        or not capture_targets_content(record, content_key)
        or not str(observed_account or "").strip()
    ):
        return False
    request_values: dict[str, set[str]] = {}
    body = record.get("request_body_template")
    if isinstance(body, dict):
        for key, value in body.items():
            if isinstance(value, (str, int)):
                request_values.setdefault(str(key).casefold(), set()).add(str(value))
    for key, value in parse_qsl(
        urlsplit(str(record.get("request_url") or "")).query,
        keep_blank_values=True,
    ):
        request_values.setdefault(key.casefold(), set()).add(value)
    texts = set().union(
        *(request_values.get(key, set()) for key in ("text", "comment_text"))
    )
    targets = set().union(
        *(request_values.get(key, set()) for key in TIKTOK_TARGET_ID_KEYS)
    )
    if texts != {final_text} or targets != {str(content_key)}:
        return False
    payload = record.get("response")
    comment = payload.get("comment") if isinstance(payload, dict) else None
    if not isinstance(comment, dict):
        return False
    raw_comment_ids = [
        comment[key]
        for key in ("cid", "comment_id", "commentId")
        if comment.get(key) not in (None, "")
    ]
    if any(
        isinstance(value, bool) or not isinstance(value, (str, int))
        for value in raw_comment_ids
    ):
        return False
    comment_ids = {str(value) for value in raw_comment_ids}
    if len(comment_ids) != 1 or any(
        re.search(r"[\s\x00-\x1f\x7f-\x9f]", value) for value in comment_ids
    ):
        return False
    if "text" in comment and comment["text"] != final_text:
        return False
    response_targets = _target_ids_from_value(comment)
    if response_targets and response_targets != {str(content_key)}:
        return False
    for author_key in ("user", "author"):
        author = comment.get(author_key)
        if not isinstance(author, dict):
            continue
        account = str(observed_account).lstrip("@").casefold()
        handles = {
            str(author[key]).lstrip("@").casefold()
            for key in ("unique_id", "uniqueId")
            if author.get(key)
        }
        if handles and handles != {account}:
            return False
    return True


def deterministic_public_rating(
    score: Any,
    *,
    scale: Any = 10.0,
    increment: Any = 0.1,
) -> tuple[float, str]:
    try:
        numeric_score = float(score)
        numeric_scale = float(scale)
        numeric_increment = float(increment)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Publication score/rating metadata is not numeric") from exc
    if not all(
        math.isfinite(value)
        for value in (numeric_score, numeric_scale, numeric_increment)
    ):
        raise RuntimeError("Publication score/rating metadata must be finite")
    if not 0.0 <= numeric_score <= 100.0:
        raise RuntimeError("Analysis score must be between 0 and 100")
    if not 1.0 <= numeric_scale <= 100.0:
        raise RuntimeError("Public rating scale must be between 1 and 100")
    if not 0.0 < numeric_increment <= numeric_scale:
        raise RuntimeError("Public rating increment is invalid")

    decimal_scale = Decimal(str(numeric_scale))
    decimal_increment = Decimal(str(numeric_increment))
    raw_value = Decimal(str(numeric_score)) * decimal_scale / Decimal("100")
    rounded_units = (raw_value / decimal_increment).quantize(
        Decimal("1"),
        rounding=ROUND_HALF_UP,
    )
    rounded_value = float(rounded_units * decimal_increment)
    rounded_value = max(0.0, min(numeric_scale, rounded_value))
    value_text = f"{rounded_value:.4f}".rstrip("0").rstrip(".")
    scale_text = f"{numeric_scale:.4f}".rstrip("0").rstrip(".")
    return rounded_value, f"{value_text}/{scale_text}"


def decision_response_type(decision: dict[str, Any]) -> str:
    raw = str(decision.get("response_type") or "").strip().casefold()
    aliases = {
        "positive": POSITIVE_RESPONSE_TYPE,
        "positive_support": POSITIVE_RESPONSE_TYPE,
        "constructive_suggestion": "constructive_suggestion",
        "constructive_correction": "constructive_correction",
        "clarifying_question": "clarifying_question",
    }
    if raw:
        response_type = aliases.get(raw)
        if not response_type:
            raise RuntimeError("Approved publication has an invalid response_type")
        return response_type
    if (
        decision.get("assessment") == "positive"
        and decision.get("positive_eligible") is True
    ):
        return POSITIVE_RESPONSE_TYPE
    legacy_rating = decision.get("public_rating")
    if (
        isinstance(legacy_rating, dict)
        and legacy_rating.get("enabled") is True
        and legacy_rating.get("required") is True
    ):
        return POSITIVE_RESPONSE_TYPE
    raise RuntimeError("Approved publication has no publishable response_type")


def prepare_final_publication_text(
    publication: dict[str, Any],
) -> tuple[str, str, str]:
    try:
        decision = json.loads(publication.get("decision_json") or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("Approved publication has invalid decision_json") from exc
    if not isinstance(decision, dict):
        raise RuntimeError("Approved publication decision must be a JSON object")

    analysis_score = publication.get("analysis_score")
    decision_score = decision.get("score")
    try:
        if analysis_score is None or decision_score is None:
            raise RuntimeError(
                "Approved publication is missing its completed analysis score"
            )
        numeric_analysis_score = float(analysis_score)
        numeric_decision_score = float(decision_score)
        if not math.isfinite(numeric_decision_score):
            raise RuntimeError("Publication decision score must be finite")
        if abs(numeric_analysis_score - numeric_decision_score) >= 0.001:
            raise RuntimeError(
                "Publication decision score does not match the completed analysis"
            )
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Approved publication has an invalid analysis score") from exc

    response_type = decision_response_type(decision)
    final_text = str(publication.get("draft_text") or "")
    if not final_text.strip():
        raise RuntimeError("Approved publication has no final text")
    if not AI_DISCLOSURE_PATTERN.search(final_text):
        raise RuntimeError("Approved final text is missing the explicit AI disclosure")
    if "PUBLIC_RATING" in final_text.upper():
        raise RuntimeError(
            "Approved publication still contains an unresolved rating placeholder"
        )
    rating = decision.get("public_rating")
    rating_mentions = [
        (
            float(match.group(1).replace(",", ".")),
            float(match.group(2).replace(",", ".")),
        )
        for match in RATING_PATTERN.finditer(final_text)
    ]
    if response_type == POSITIVE_RESPONSE_TYPE:
        if (
            not isinstance(rating, dict)
            or rating.get("enabled") is not True
            or rating.get("required") is not True
        ):
            raise RuntimeError(
                "Approved positive publication is missing deterministic "
                "public-rating metadata"
            )
        expected_value, expected_rating = deterministic_public_rating(
            analysis_score,
            scale=rating.get("scale"),
            increment=rating.get("increment"),
        )
        try:
            stored_value = float(rating.get("value"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Stored public rating value is invalid") from exc
        if (
            not math.isfinite(stored_value)
            or abs(stored_value - expected_value) >= 0.001
        ):
            raise RuntimeError(
                "Stored public rating value does not match the deterministic rating"
            )
        if str(rating.get("formatted") or "").strip() != expected_rating:
            raise RuntimeError(
                "Stored public rating text does not match the deterministic rating"
            )
        expected_scale = float(rating["scale"])
        matching_mentions = [
            mention
            for mention in rating_mentions
            if abs(mention[0] - expected_value) < 0.001
            and abs(mention[1] - expected_scale) < 0.001
        ]
        if (
            len(rating_mentions) != 1
            or len(matching_mentions) != 1
            or final_text.count(expected_rating) != 1
        ):
            raise RuntimeError(
                "Final positive publication text must contain exactly one "
                "deterministic public rating"
            )
    else:
        if (
            response_type not in UNRATED_RESPONSE_TYPES
            or not isinstance(rating, dict)
            or rating.get("enabled") is not False
            or rating.get("required") is not False
            or rating.get("value") is not None
            or str(rating.get("formatted") or "")
        ):
            raise RuntimeError(
                "Constructive publication must have disabled public-rating metadata"
            )
        if rating_mentions or UNRATED_TEXT_RATING_PATTERN.search(final_text):
            raise RuntimeError(
                "Constructive publication text must not contain a public rating"
            )
        expected_rating = ""

    final_hash = hashlib.sha256(final_text.encode("utf-8")).hexdigest()
    if publication.get("draft_hash") != final_hash:
        raise RuntimeError(
            "Approved final-text hash does not match the text that would be published"
        )
    return final_text, final_hash, expected_rating


def validate_engage_source(
    conn: sqlite3.Connection,
    record: dict[str, Any],
    *,
    decision: dict[str, Any],
    analysis_result: dict[str, Any],
    decision_hash: str,
    analysis_result_hash: str,
) -> bool:
    """Revalidate the immutable ENGAGE packet without replacing its hash."""
    run_id = str(record.get("engage_run_id") or "").strip()
    post_id = str(record.get("engage_post_id") or "").strip()
    if not run_id and not post_id:
        raise RuntimeError(
            "TikTok publication is not sourced from the ENGAGE state machine"
        )
    if not run_id or not post_id:
        raise RuntimeError("ENGAGE publication source identifiers are incomplete")
    if not all(
        table_exists(conn, table)
        for table in ("engage_tiktok_runs", "engage_tiktok_posts")
    ):
        raise RuntimeError("ENGAGE publication source tables are missing")

    run_row = conn.execute(
        "SELECT * FROM engage_tiktok_runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    post_row = conn.execute(
        """
        SELECT * FROM engage_tiktok_posts
        WHERE run_id=? AND post_id=?
        """,
        (run_id, post_id),
    ).fetchone()
    if not run_row or not post_row:
        raise RuntimeError("ENGAGE publication source record is missing")
    run = {key: run_row[key] for key in run_row.keys()}
    post = {key: post_row[key] for key in post_row.keys()}

    requested = int(run.get("requested_count") or 0)
    ready_rows = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM engage_tiktok_posts
            WHERE run_id=? AND evidence_ready=1
            """,
            (run_id,),
        ).fetchone()[0]
    )
    unique_rows = int(
        conn.execute(
            "SELECT COUNT(*) FROM engage_tiktok_posts WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
    )
    if (
        requested <= 0
        or str(run.get("mode") or "").casefold() != "live"
        or run.get("project") != record.get("project")
        or int(run.get("requested") or 0) != requested
        or int(run.get("unique_collected") or 0) != unique_rows
        or unique_rows < requested
        or int(run.get("evidence_ready") or 0) != requested
        or ready_rows != requested
        or run.get("status") not in {"review_pending", "stored"}
    ):
        raise RuntimeError(
            "ENGAGE live-run provenance or exact-count gate no longer passes"
        )
    if (
        post.get("status") != "handed_off"
        or int(post.get("evidence_ready") or 0) != 1
        or post.get("publication_id") != record.get("publication_id")
        or post.get("url") != record.get("target_url")
        or post.get("post_id") != record.get("content_key")
    ):
        raise RuntimeError("ENGAGE post handoff state no longer matches the queue")

    expected_account = str(run.get("expected_account") or "").lstrip("@").casefold()
    observed_account = str(run.get("observed_account") or "").lstrip("@").casefold()
    queued_account = str(record.get("expected_account") or "").lstrip("@").casefold()
    if queued_account != expected_account:
        raise RuntimeError("ENGAGE expected account changed after authorization")
    if expected_account and observed_account != expected_account:
        raise RuntimeError("ENGAGE collection used a different TikTok account")

    browser_check = json_object(
        run.get("browser_preflight_json"),
        "ENGAGE browser preflight",
    )
    if (
        browser_check.get("reachable") is not True
        or browser_check.get("tiktok_authenticated") is not True
    ):
        raise RuntimeError("ENGAGE browser/login preflight is not valid")

    evidence = json_object(post.get("evidence_json"), "ENGAGE evidence packet")
    if (
        canonical_hash(evidence) != post.get("evidence_hash")
        or post.get("evidence_hash") != record.get("analysis_input_hash")
        or evidence.get("evidence_ready") is not True
        or str(evidence.get("post_id") or "") != post_id
        or evidence.get("url") != record.get("target_url")
        or evidence.get("observed_at") != record.get("evidence_observed_at")
    ):
        raise RuntimeError("ENGAGE evidence packet or hash changed after review")
    if (
        record.get("analysis_run_input_hash") != post.get("evidence_hash")
        or json_object(
            record.get("analysis_evidence_json"),
            "completed analysis evidence",
        )
        != evidence
    ):
        raise RuntimeError("Completed analysis no longer references ENGAGE evidence")

    analysis = json_object(post.get("analysis_json"), "ENGAGE analysis")
    expected_analysis_hash = canonical_hash(
        {
            "evidence_hash": post.get("evidence_hash"),
            "analysis": analysis,
        }
    )
    if expected_analysis_hash != post.get("analysis_hash"):
        raise RuntimeError("ENGAGE analysis hash changed after review")
    analysis_decision = json_object(
        record.get("analysis_decision_json"),
        "completed ENGAGE analysis decision",
    )
    try:
        score_matches = (
            math.isfinite(float(record.get("analysis_score")))
            and abs(
                float(record.get("analysis_score"))
                - float(decision.get("score"))
            )
            < 0.001
        )
    except (TypeError, ValueError):
        score_matches = False
    if (
        record.get("analysis_status") != "complete"
        or not score_matches
        or analysis_decision != decision
        or analysis_result.get("internal_analysis") != analysis
        or analysis_result.get("publication_decision") != decision
        or analysis_result.get("public_comment") != record.get("draft_text")
    ):
        raise RuntimeError("Completed analysis result no longer matches ENGAGE state")

    required_text = ("reason", "novel_value", "strength", "recommendation")
    response_type = decision_response_type(decision)
    if (
        any(not str(decision.get(field) or "").strip() for field in required_text)
        or not isinstance(decision.get("evidence_refs"), list)
        or not decision.get("evidence_refs")
    ):
        raise RuntimeError("ENGAGE publication decision is missing policy evidence")
    if response_type == POSITIVE_RESPONSE_TYPE:
        if (
            decision.get("assessment") != "positive"
            or decision.get("positive_eligible") is not True
            or decision.get("blocking_risk_flags") not in (None, [])
        ):
            raise RuntimeError(
                "ENGAGE positive response no longer passes eligibility"
            )
        threshold_pairs = (
            ("score", "minimum_public_score"),
            ("post_score", "minimum_public_score"),
            ("conversation_score", "minimum_conversation_score"),
            ("confidence", "minimum_confidence"),
            ("data_completeness", "minimum_data_completeness"),
        )
    else:
        if (
            response_type not in UNRATED_RESPONSE_TYPES
            or decision.get("assessment") != "constructive"
            or decision.get("response_eligible") is not True
            or decision.get("constructive_eligible") is not True
            or decision.get("rating_required") is not False
            or not str(decision.get("response_objective") or "").strip()
            or not str(decision.get("response_rationale") or "").strip()
            or decision.get("blocking_risk_flags") not in (None, [])
            or (
                response_type == "constructive_correction"
                and not str(
                    decision.get("correction_target") or ""
                ).strip()
            )
        ):
            raise RuntimeError(
                "ENGAGE constructive response no longer passes eligibility"
            )
        threshold_pairs = (
            (
                "response_opportunity_score",
                "minimum_response_opportunity_score",
            ),
            (
                "grounding_confidence",
                "minimum_grounding_confidence",
            ),
            ("data_completeness", "minimum_data_completeness"),
        )
    try:
        for value_field, minimum_field in threshold_pairs:
            if float(decision[value_field]) < float(decision[minimum_field]):
                raise RuntimeError(
                    f"ENGAGE publication no longer passes {minimum_field}"
                )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("ENGAGE publication threshold metadata is invalid") from exc

    if (
        post.get("draft_text") != record.get("draft_text")
        or post.get("draft_hash") != record.get("draft_hash")
        or hashlib.sha256(
            str(post.get("draft_text") or "").encode("utf-8")
        ).hexdigest()
        != post.get("draft_hash")
        or post.get("review_hash") != record.get("ai_review_hash")
        or post.get("review_json") != record.get("ai_review_json")
        or post.get("review_actor") != record.get("ai_reviewer")
        or post.get("reviewed_at") != record.get("ai_reviewed_at")
    ):
        raise RuntimeError("ENGAGE reviewed response changed after handoff")
    if (
        post.get("authorization_by") != record.get("approved_by")
        or post.get("authorized_at") != record.get("approved_at")
        or post.get("authorization_presentation_hash")
        != record.get("authorization_presentation_hash")
        or post.get("authorization_text_hash") != record.get("draft_hash")
        or post.get("authorization_review_hash") != record.get("ai_review_hash")
        or post.get("authorization_target_url") != record.get("target_url")
        or post.get("authorization_content_key") != record.get("content_key")
        or post.get("authorization_decision_hash") != decision_hash
        or post.get("authorization_analysis_hash") != analysis_result_hash
        or str(post.get("authorization_expected_account") or "")
        != str(record.get("expected_account") or "")
    ):
        raise RuntimeError("ENGAGE user authorization changed after handoff")

    presentation = json_object(
        post.get("presentation_json"),
        "ENGAGE response presentation",
    )
    presentation_hash = str(post.get("presentation_hash") or "")
    if "response_type" not in analysis:
        expected_presentation = {
            "schema_version": "tiktok-engage-presentation-v1",
            "run_id": run_id,
            "post_id": post_id,
            "publication_id": record.get("publication_id"),
            "target_url": record.get("target_url"),
            "expected_account": record.get("expected_account") or "",
            "final_response": record.get("draft_text"),
            "draft_hash": record.get("draft_hash"),
            "review_hash": record.get("ai_review_hash"),
            "public_rating": (
                decision.get("public_rating", {}).get("formatted")
                if isinstance(decision.get("public_rating"), dict)
                else None
            ),
            "presented_to": record.get("approved_by"),
            "presented_at": post.get("presented_at"),
        }
    else:
        rating = decision.get("public_rating")
        expected_presentation = {
            "schema_version": "tiktok-engage-presentation-v2",
            "run_id": run_id,
            "post_id": post_id,
            "publication_id": record.get("publication_id"),
            "target_url": record.get("target_url"),
            "expected_account": record.get("expected_account") or "",
            "response_type": response_type,
            "rating_required": response_type == POSITIVE_RESPONSE_TYPE,
            "final_response": record.get("draft_text"),
            "draft_hash": record.get("draft_hash"),
            "review_hash": record.get("ai_review_hash"),
            "public_rating": (
                rating.get("formatted")
                if isinstance(rating, dict)
                and rating.get("enabled") is True
                else None
            ),
            "presented_to": record.get("approved_by"),
            "presented_at": post.get("presented_at"),
        }
    presented_at = parse_iso(post.get("presented_at"))
    reviewed_at = parse_iso(post.get("reviewed_at"))
    authorized_at = parse_iso(post.get("authorized_at"))
    if (
        not presentation_hash
        or presentation != expected_presentation
        or canonical_hash(presentation) != presentation_hash
        or post.get("authorization_presentation_hash") != presentation_hash
        or record.get("authorization_presentation_hash") != presentation_hash
        or post.get("presented_to") != record.get("approved_by")
        or not re.fullmatch(
            r"[0-9a-f]{64}",
            str(post.get("presentation_token_hash") or ""),
        )
        or presented_at is None
        or reviewed_at is None
        or authorized_at is None
        or presented_at < reviewed_at
        or authorized_at < presented_at
    ):
        raise RuntimeError(
            "ENGAGE user authorization is not bound to the exact shown response"
        )

    handoff = json_object(post.get("handoff_json"), "ENGAGE handoff")
    expected_handoff = {
        "publication_id": record.get("publication_id"),
        "run_id": run_id,
        "post_id": post_id,
        "target_url": record.get("target_url"),
        "expected_account": record.get("expected_account") or "",
        "analysis_id": record.get("analysis_id"),
        "evidence_hash": record.get("analysis_input_hash"),
        "analysis_hash": post.get("analysis_hash"),
        "analysis_result_hash": analysis_result_hash,
        "decision_hash": decision_hash,
        "draft_hash": record.get("draft_hash"),
        "review_hash": record.get("ai_review_hash"),
        "presentation_hash": presentation_hash,
        "authorized_by": record.get("approved_by"),
        "valid_until": record.get("valid_until"),
    }
    handoff_hash = canonical_hash(expected_handoff)
    if (
        handoff != expected_handoff
        or post.get("handoff_hash") != handoff_hash
        or record.get("engage_handoff_hash") != handoff_hash
    ):
        raise RuntimeError("ENGAGE handoff attestation is invalid")
    return True


def sync_engage_blocked_state(
    conn: sqlite3.Connection,
    publication: dict[str, Any],
    *,
    post_status: str,
    event: str,
    reason: str,
    changed_at: str,
) -> None:
    """Mirror a terminal publication guard failure into ENGAGE state."""
    run_id = str(publication.get("engage_run_id") or "").strip()
    post_id = str(publication.get("engage_post_id") or "").strip()
    if not run_id and not post_id:
        return
    if not run_id or not post_id:
        raise RuntimeError("ENGAGE blocked-state identifiers are incomplete")
    if not all(
        table_exists(conn, table)
        for table in (
            "engage_tiktok_runs",
            "engage_tiktok_posts",
            "engage_tiktok_events",
        )
    ):
        raise RuntimeError("ENGAGE blocked-state tables are missing")
    current = conn.execute(
        """
        SELECT status FROM engage_tiktok_posts
        WHERE run_id=? AND post_id=? AND publication_id=?
        """,
        (run_id, post_id, publication["publication_id"]),
    ).fetchone()
    if not current:
        raise RuntimeError("ENGAGE blocked publication record is missing")
    already_terminal = current["status"] == post_status
    if not already_terminal:
        changed = conn.execute(
            """
            UPDATE engage_tiktok_posts SET status=?, updated_at=?
            WHERE run_id=? AND post_id=? AND publication_id=?
              AND status IN ('authorized', 'handed_off')
            """,
            (
                post_status,
                changed_at,
                run_id,
                post_id,
                publication["publication_id"],
            ),
        ).rowcount
        if changed != 1:
            raise RuntimeError(
                "ENGAGE blocked publication state could not be synchronized"
            )
        conn.execute(
            """
            INSERT INTO engage_tiktok_events (
                run_id, post_id, stage, event, payload_json, created_at
            ) VALUES (?, ?, 'publication_guard', ?, ?, ?)
            """,
            (
                run_id,
                post_id,
                event,
                canonical_json(
                    {
                        "publication_id": publication["publication_id"],
                        "reason": reason,
                    }
                ),
                changed_at,
            ),
        )
    sync_engage_run_outcome(conn, run_id, changed_at=changed_at)


def sync_engage_run_outcome(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    changed_at: str,
) -> str:
    """Refresh publication counters and set a terminal run outcome when complete."""
    run = conn.execute(
        """
        SELECT status, requested_count
        FROM engage_tiktok_runs
        WHERE run_id=?
        """,
        (run_id,),
    ).fetchone()
    if not run:
        raise RuntimeError("ENGAGE run outcome record is missing")

    rows = conn.execute(
        """
        SELECT status, COUNT(*) AS amount
        FROM engage_tiktok_posts
        WHERE run_id=?
        GROUP BY status
        """,
        (run_id,),
    ).fetchall()
    status_counts = {
        str(row["status"]): int(row["amount"])
        for row in rows
    }
    total = sum(status_counts.values())
    requested = int(run["requested_count"] or 0)
    published = status_counts.get("published", 0)
    failed = sum(
        status_counts.get(status, 0)
        for status in ENGAGE_FAILED_POST_STATUSES
    )
    terminal = sum(
        status_counts.get(status, 0)
        for status in ENGAGE_TERMINAL_POST_STATUSES
    )

    next_status = str(run["status"])
    if requested > 0 and total == requested and terminal == requested:
        if failed:
            next_status = "completed_with_failures"
        elif published:
            next_status = "published"
        else:
            next_status = "completed"

    updated = conn.execute(
        """
        UPDATE engage_tiktok_runs SET
            status=?,
            published=?,
            failed=?,
            updated_at=?
        WHERE run_id=?
        """,
        (next_status, published, failed, changed_at, run_id),
    ).rowcount
    if updated != 1:
        raise RuntimeError("ENGAGE run outcome could not be synchronized")
    return next_status


def load_approved_publication(database: Path, publication_id: str) -> dict[str, Any]:
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    ensure_analysis_schema(conn)
    try:
        row = conn.execute(
            """
            SELECT pq.*, ar.status AS analysis_status,
                   ar.score AS analysis_score,
                   ar.publication_decision_json AS analysis_decision_json,
                   ar.result_json AS analysis_result_json,
                   ar.input_hash AS analysis_run_input_hash,
                   ar.evidence_json AS analysis_evidence_json
            FROM publication_queue pq
            LEFT JOIN analysis_runs ar ON ar.analysis_id = pq.analysis_id
            WHERE pq.publication_id = ?
            """,
            (publication_id,),
        ).fetchone()
        if not row:
            raise RuntimeError(f"Publication draft not found: {publication_id}")
        record = {key: row[key] for key in row.keys()}
        if record["platform"] != "tiktok":
            raise RuntimeError("This adapter only accepts TikTok publication drafts")
        if not str(record.get("engage_run_id") or "").strip() or not str(
            record.get("engage_post_id") or ""
        ).strip():
            raise RuntimeError(
                "Only publications handed off by TikTok ENGAGE are accepted"
            )
        if record["mode"] != "live":
            raise RuntimeError("Publication mode must be live for this adapter")
        if record["status"] != "approved":
            raise RuntimeError("Publication status must be approved")
        if not record["approved_at"] or not record["approved_by"]:
            raise RuntimeError("Approval timestamp and approver are required")
        if is_automation_identity(record["approved_by"]):
            raise RuntimeError("AI or automation cannot grant user authorization")
        if record.get("analysis_status") != "complete":
            raise RuntimeError("Publication analysis is not complete")
        if not record["target_url"].startswith("https://www.tiktok.com/"):
            raise RuntimeError("Target URL is not a canonical TikTok URL")
        final_text, final_hash, public_rating = prepare_final_publication_text(record)
        record["final_text"] = final_text
        record["final_text_hash"] = final_hash
        record["public_rating"] = public_rating
        decision = json_object(record.get("decision_json"), "Approved publication decision")
        if not decision.get("publish") or decision.get("policy_violations"):
            raise RuntimeError("Approved draft no longer has a valid AI publication decision")
        analysis_decision = json_object(
            record.get("analysis_decision_json"),
            "Completed analysis decision",
        )
        analysis_result = json_object(
            record.get("analysis_result_json"),
            "Completed analysis result",
        )
        if decision != analysis_decision:
            raise RuntimeError(
                "Publication decision no longer matches the completed analysis"
            )
        decision_hash = canonical_hash(decision)
        analysis_result_hash = canonical_hash(analysis_result)
        response_type = decision_response_type(decision)
        record["response_type"] = response_type
        if response_type == POSITIVE_RESPONSE_TYPE:
            if (
                decision.get("assessment") != "positive"
                or decision.get("positive_eligible") is not True
                or decision.get("blocking_risk_flags") not in (None, [])
            ):
                raise RuntimeError(
                    "Approved draft is not a positive eligible publication"
                )
            threshold_pairs = (
                ("score", "minimum_public_score"),
                ("post_score", "minimum_public_score"),
                ("conversation_score", "minimum_conversation_score"),
                ("confidence", "minimum_confidence"),
                ("data_completeness", "minimum_data_completeness"),
            )
        else:
            if (
                response_type not in UNRATED_RESPONSE_TYPES
                or decision.get("assessment") != "constructive"
                or decision.get("response_eligible") is not True
                or decision.get("constructive_eligible") is not True
                or decision.get("rating_required") is not False
                or decision.get("blocking_risk_flags") not in (None, [])
                or (
                    response_type == "constructive_correction"
                    and not str(
                        decision.get("correction_target") or ""
                    ).strip()
                )
            ):
                raise RuntimeError(
                    "Approved draft is not an eligible constructive publication"
                )
            threshold_pairs = (
                (
                    "response_opportunity_score",
                    "minimum_response_opportunity_score",
                ),
                (
                    "grounding_confidence",
                    "minimum_grounding_confidence",
                ),
                ("data_completeness", "minimum_data_completeness"),
            )
        try:
            for value_field, minimum_field in threshold_pairs:
                if float(decision[value_field]) < float(
                    decision[minimum_field]
                ):
                    raise RuntimeError(
                        "Approved draft no longer passes "
                        f"{minimum_field}"
                    )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "Approved draft has invalid response-threshold metadata"
            ) from exc

        review_payload = json_object(
            record.get("ai_review_json"),
            "Approved publication AI review",
        )
        if (
            record.get("ai_review_status") != "approved"
            or review_payload.get("approved") is not True
            or review_payload.get("independent_review") is not True
        ):
            raise RuntimeError("Independent AI review approval is required")
        if (
            record.get("ai_review_text_hash") != record.get("draft_hash")
            or record.get("ai_review_evidence_hash") != record.get("analysis_input_hash")
            or record.get("ai_review_target_url") != record.get("target_url")
            or record.get("ai_review_content_key") != record.get("content_key")
            or record.get("ai_review_decision_hash") != decision_hash
            or record.get("ai_review_analysis_hash") != analysis_result_hash
        ):
            raise RuntimeError("AI review hashes do not match the approved response")
        expected_review_hash = publication_ai_review_hash(
            publication_id=record["publication_id"],
            analysis_id=record["analysis_id"],
            analysis_input_hash=record["analysis_input_hash"],
            draft_hash=record["draft_hash"],
            reviewer=record.get("ai_reviewer") or "",
            reviewed_at=record.get("ai_reviewed_at") or "",
            review_payload=review_payload,
            target_url=record.get("target_url") or "",
            content_key=record.get("content_key") or "",
            expected_account=record.get("expected_account") or "",
            decision_hash=decision_hash,
            analysis_result_hash=analysis_result_hash,
        )
        if not record.get("ai_review_hash") or record["ai_review_hash"] != expected_review_hash:
            raise RuntimeError("AI review attestation hash is invalid")
        if (
            record.get("authorization_text_hash") != record.get("draft_hash")
            or record.get("authorization_review_hash") != record.get("ai_review_hash")
            or record.get("authorization_target_url") != record.get("target_url")
            or record.get("authorization_content_key") != record.get("content_key")
            or record.get("authorization_decision_hash") != decision_hash
            or record.get("authorization_analysis_hash") != analysis_result_hash
            or record.get("authorization_expected_account")
            != record.get("expected_account")
        ):
            raise RuntimeError("User authorization is not bound to the reviewed response")

        duplicate = conn.execute(
            """
            SELECT publication_id FROM publication_queue
            WHERE project = ? AND platform = ? AND content_key = ?
              AND status = 'published' AND publication_id <> ?
            LIMIT 1
            """,
            (
                record["project"],
                record["platform"],
                record["content_key"],
                record["publication_id"],
            ),
        ).fetchone()
        if duplicate:
            raise RuntimeError("This TikTok post already has a published response")
        not_before = parse_iso(record.get("not_before"))
        if not_before is not None and not_before > dt.datetime.now().astimezone():
            raise RuntimeError("Approved publication is not yet inside its publication window")
        deadline = parse_iso(record.get("valid_until"))
        if deadline is None or deadline <= dt.datetime.now().astimezone():
            changed_at = now_iso()
            reason = "Publication freshness deadline expired"
            conn.execute(
                """
                UPDATE publication_queue SET status='expired',
                    error=?, updated_at=?
                WHERE publication_id=? AND status='approved'
                """,
                (reason, changed_at, publication_id),
            )
            sync_engage_blocked_state(
                conn,
                record,
                post_status="expired",
                event="expired",
                reason=reason,
                changed_at=changed_at,
            )
            conn.commit()
            raise RuntimeError("Approved publication has expired and must be reanalyzed")
        if not record.get("analysis_input_hash"):
            raise RuntimeError("Approved publication has no evidence hash")
        try:
            engage_source = validate_engage_source(
                conn,
                record,
                decision=decision,
                analysis_result=analysis_result,
                decision_hash=decision_hash,
                analysis_result_hash=analysis_result_hash,
            )
        except RuntimeError as exc:
            changed_at = now_iso()
            reason = f"ENGAGE publication revalidation failed: {exc}"
            conn.execute(
                """
                UPDATE publication_queue SET status='stale', error=?, updated_at=?
                WHERE publication_id=? AND status='approved'
                """,
                (reason, changed_at, publication_id),
            )
            sync_engage_blocked_state(
                conn,
                record,
                post_status="stale",
                event="stale",
                reason=reason,
                changed_at=changed_at,
            )
            conn.commit()
            raise RuntimeError(
                "Approved ENGAGE publication is stale and must be reanalyzed"
            ) from exc
        if not engage_source:
            if not table_exists(conn, "content_items"):
                raise RuntimeError(
                    "Publication target is missing from the canonical content store"
                )
            content = conn.execute(
                """
                SELECT * FROM content_items
                WHERE project=? AND platform=? AND content_key=?
                """,
                (record["project"], record["platform"], record["content_key"]),
            ).fetchone()
            if not content:
                raise RuntimeError(
                    "Publication target is missing from the canonical content store"
                )
            if content["url"] != record["target_url"]:
                raise RuntimeError("Publication target URL changed after authorization")
            _, current_hash = build_evidence_packet(
                conn,
                content,
                max_comments=max(0, int(record.get("max_comments_per_post") or 200)),
            )
            state = conn.execute(
                """
                SELECT status, latest_analysis_id, current_input_hash
                FROM content_analysis_state
                WHERE project=? AND platform=? AND content_key=?
                """,
                (record["project"], record["platform"], record["content_key"]),
            ).fetchone()
            if (
                current_hash != record["analysis_input_hash"]
                or not state
                or state["status"] != "complete"
                or state["latest_analysis_id"] != record["analysis_id"]
                or state["current_input_hash"] != current_hash
            ):
                conn.execute(
                    """
                    UPDATE publication_queue SET status='stale',
                        error='Evidence changed after approval', updated_at=?
                    WHERE publication_id=? AND status='approved'
                    """,
                    (now_iso(), publication_id),
                )
                conn.commit()
                raise RuntimeError(
                    "Approved publication evidence changed and must be reanalyzed"
                )
        conn.execute(
            """
            UPDATE publication_queue SET revalidation_status='validated',
                revalidated_at=?, error=''
            WHERE publication_id=?
            """,
            (now_iso(), publication_id),
        )
        conn.commit()
        return record
    finally:
        conn.close()


def daily_publication_count(database: Path, project: str) -> int:
    conn = sqlite3.connect(database)
    start = (
        dt.datetime.now()
        .astimezone()
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .isoformat()
    )
    count = conn.execute(
        """
        SELECT COUNT(*) FROM publication_queue
        WHERE project = ? AND platform = 'tiktok'
          AND (
              (status IN ('published', 'deleted') AND published_at >= ?)
              OR (status = 'publishing' AND last_attempt_at >= ?)
          )
        """,
        (project, start, start),
    ).fetchone()[0]
    conn.close()
    return int(count)


def claim_publication(
    database: Path,
    publication: dict[str, Any],
    *,
    daily_limit: int,
    max_attempts: int,
    master_database: str | Path | None = None,
    observed_account: str = "",
) -> str:
    """Atomically reserve one approved row before any text is entered."""
    if daily_limit < 0:
        raise RuntimeError(
            "TikTok daily publication limit must be 0 (unlimited) or positive"
        )
    if max_attempts <= 0:
        raise RuntimeError("TikTok publication attempt limit must be positive")
    database = Path(database).resolve()
    master_path = bind_engage_master_database(
        database,
        publication,
        master_database,
    )
    account = str(
        observed_account
        or publication.get("observed_account")
        or publication.get("expected_account")
        or ""
    ).lstrip("@").casefold()
    if not account:
        raise RuntimeError(
            "A verified TikTok account is required for the master comment claim"
        )
    conn = sqlite3.connect(database, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    ensure_analysis_schema(conn)
    master_schema = attach_master_database(conn, master_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        claim_time = dt.datetime.now().astimezone()
        attempted_at = claim_time.replace(microsecond=0).isoformat()
        start = claim_time.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        ).isoformat()
        row = conn.execute(
            """
            SELECT pq.*, ar.status AS analysis_status,
                   ar.score AS analysis_score,
                   ar.publication_decision_json AS analysis_decision_json,
                   ar.result_json AS analysis_result_json,
                   ar.input_hash AS analysis_run_input_hash,
                   ar.evidence_json AS analysis_evidence_json
            FROM publication_queue pq
            LEFT JOIN analysis_runs ar ON ar.analysis_id = pq.analysis_id
            WHERE pq.publication_id=?
            """,
            (publication["publication_id"],),
        ).fetchone()
        if not row:
            raise RuntimeError("Publication is no longer available for an atomic claim")
        row = {key: row[key] for key in row.keys()}
        if row["status"] != "approved":
            raise RuntimeError("Publication is no longer available for an atomic claim")
        if int(row["attempts"] or 0) >= max_attempts:
            raise RuntimeError(
                f"TikTok publication attempt limit reached ({max_attempts})"
            )
        if row["revalidation_status"] != "validated":
            raise RuntimeError("Publication was not revalidated before its atomic claim")
        not_before = parse_iso(row["not_before"])
        if not_before is not None and not_before > claim_time:
            raise RuntimeError("Publication is not yet inside its publication window")
        deadline = parse_iso(row["valid_until"])
        if deadline is None or deadline <= claim_time:
            reason = "Publication freshness deadline expired before claim"
            conn.execute(
                """
                UPDATE publication_queue SET status='expired',
                    error=?, updated_at=?
                WHERE publication_id=? AND status='approved'
                """,
                (reason, attempted_at, publication["publication_id"]),
            )
            sync_engage_blocked_state(
                conn,
                publication,
                post_status="expired",
                event="expired_before_claim",
                reason=reason,
                changed_at=attempted_at,
            )
            conn.commit()
            raise RuntimeError(
                "Approved publication expired immediately before its atomic claim"
            )

        try:
            immutable_fields = (
                "project",
                "platform",
                "content_key",
                "analysis_id",
                "target_url",
                "mode",
                "draft_text",
                "draft_hash",
                "decision_json",
                "analysis_input_hash",
                "evidence_observed_at",
                "not_before",
                "valid_until",
                "max_comments_per_post",
                "approval_required",
                "expected_account",
                "ai_review_status",
                "ai_reviewed_at",
                "ai_reviewer",
                "ai_review_text_hash",
                "ai_review_evidence_hash",
                "ai_review_target_url",
                "ai_review_content_key",
                "ai_review_decision_hash",
                "ai_review_analysis_hash",
                "ai_review_json",
                "ai_review_hash",
                "approved_at",
                "approved_by",
                "authorization_presentation_hash",
                "authorization_text_hash",
                "authorization_review_hash",
                "authorization_target_url",
                "authorization_content_key",
                "authorization_decision_hash",
                "authorization_analysis_hash",
                "authorization_expected_account",
                "engage_run_id",
                "engage_post_id",
                "engage_handoff_hash",
                "analysis_status",
                "analysis_score",
                "analysis_decision_json",
                "analysis_result_json",
                "analysis_run_input_hash",
                "analysis_evidence_json",
            )
            for field in immutable_fields:
                if row.get(field) != publication.get(field):
                    raise RuntimeError(
                        f"publication binding changed: {field}"
                    )

            final_text, final_hash, public_rating = prepare_final_publication_text(row)
            if (
                final_text != publication.get("final_text")
                or final_hash != publication.get("final_text_hash")
                or public_rating != publication.get("public_rating")
            ):
                raise RuntimeError("final publication text changed")

            decision = json_object(
                row.get("decision_json"),
                "claim-time publication decision",
            )
            analysis_decision = json_object(
                row.get("analysis_decision_json"),
                "claim-time completed analysis decision",
            )
            analysis_result = json_object(
                row.get("analysis_result_json"),
                "claim-time analysis result",
            )
            if (
                row.get("analysis_status") != "complete"
                or decision != analysis_decision
            ):
                raise RuntimeError(
                    "completed analysis changed before the atomic claim"
                )
            decision_hash = canonical_hash(decision)
            analysis_result_hash = canonical_hash(analysis_result)
            if (
                row.get("authorization_decision_hash") != decision_hash
                or row.get("authorization_analysis_hash")
                != analysis_result_hash
            ):
                raise RuntimeError(
                    "authorization no longer matches the completed analysis"
                )

            if row.get("engage_run_id") or row.get("engage_post_id"):
                if not validate_engage_source(
                    conn,
                    row,
                    decision=decision,
                    analysis_result=analysis_result,
                    decision_hash=decision_hash,
                    analysis_result_hash=analysis_result_hash,
                ):
                    raise RuntimeError(
                        "ENGAGE source identifiers disappeared before claim"
                    )
        except RuntimeError as exc:
            reason = f"Claim-time publication revalidation failed: {exc}"
            changed = conn.execute(
                """
                UPDATE publication_queue SET status='stale',
                    revalidation_status='failed', error=?, updated_at=?
                WHERE publication_id=? AND status='approved'
                """,
                (
                    reason,
                    attempted_at,
                    publication["publication_id"],
                ),
            ).rowcount
            if changed != 1:
                raise RuntimeError(
                    "Invalid publication could not be blocked"
                ) from exc
            sync_engage_blocked_state(
                conn,
                publication,
                post_status="stale",
                event="stale_before_claim",
                reason=reason,
                changed_at=attempted_at,
            )
            conn.commit()
            raise RuntimeError(
                "Approved publication became stale before its atomic claim"
            ) from exc
        duplicate = conn.execute(
            """
            SELECT publication_id FROM publication_queue
            WHERE project=? AND platform='tiktok' AND content_key=?
              AND publication_id<>?
              AND status IN ('publishing', 'published', 'uncertain')
            LIMIT 1
            """,
            (
                row["project"],
                row["content_key"],
                publication["publication_id"],
            ),
        ).fetchone()
        if duplicate:
            raise RuntimeError(
                "This TikTok post already has an active or completed publication"
            )
        if daily_limit > 0:
            reserved = conn.execute(
                """
                SELECT COUNT(*) FROM publication_queue
                WHERE project=? AND platform='tiktok'
                  AND publication_id<>?
                  AND (
                      (status IN ('published', 'deleted') AND published_at >= ?)
                      OR (status='publishing' AND last_attempt_at >= ?)
                  )
                """,
                (row["project"], publication["publication_id"], start, start),
            ).fetchone()[0]
            if int(reserved) >= daily_limit:
                raise RuntimeError(
                    f"TikTok daily publication limit reached ({daily_limit})"
                )
        guard = comment_target_guard(
            conn,
            master_schema,
            account=account,
            post_id=str(row["content_key"]),
        )
        if guard.get("blocked"):
            blocker = str(
                guard.get("state")
                or guard.get("reason")
                or "existing comment history"
            )
            raise RuntimeError(
                "Master comment history blocks another TikTok response for "
                f"@{account} on post {row['content_key']}: {blocker}"
            )
        master_attempt_id = str(
            register_publication_claim(
                conn,
                master_schema,
                account=account,
                post_id=str(row["content_key"]),
                publication_id=str(row["publication_id"]),
                source_path=database,
                run_id=str(row.get("engage_run_id") or ""),
                target_url=str(row["target_url"]),
                text_hash=final_hash,
                local_attempt_number=int(row["attempts"] or 0) + 1,
                metadata={
                    "project": str(row["project"]),
                    "final_text": final_text,
                    "analysis_id": str(row["analysis_id"]),
                    "engage_post_id": str(row.get("engage_post_id") or ""),
                    "review_hash": str(row.get("ai_review_hash") or ""),
                    "authorization_presentation_hash": str(
                        row.get("authorization_presentation_hash") or ""
                    ),
                    "engage_handoff_hash": str(
                        row.get("engage_handoff_hash") or ""
                    ),
                },
            )
        )
        if not master_attempt_id:
            raise RuntimeError(
                "Master comment history did not issue an attempt identifier"
            )
        changed = conn.execute(
            """
            UPDATE publication_queue SET status='publishing',
                attempts=attempts+1, last_attempt_at=?, master_attempt_id=?,
                error='', updated_at=?
            WHERE publication_id=? AND status='approved' AND attempts < ?
            """,
            (
                attempted_at,
                master_attempt_id,
                attempted_at,
                publication["publication_id"],
                max_attempts,
            ),
        ).rowcount
        if changed != 1:
            raise RuntimeError("Publication could not be atomically claimed")
        conn.commit()
        publication["master_attempt_id"] = master_attempt_id
        publication["_master_database"] = str(master_path)
        return attempted_at
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def publication_status(database: Path, publication_id: str) -> str:
    conn = sqlite3.connect(database)
    try:
        row = conn.execute(
            "SELECT status FROM publication_queue WHERE publication_id=?",
            (publication_id,),
        ).fetchone()
        return str(row[0]) if row else ""
    finally:
        conn.close()


def mark_publication_submit_intent(
    database: Path,
    publication: dict[str, Any],
    *,
    master_database: str | Path | None = None,
) -> None:
    """Durably fence the account/post before the submit click can occur."""
    database = Path(database).resolve()
    master_path = resolved_master_database(
        database,
        master_database or publication.get("_master_database"),
    )
    attempt_id = str(publication.get("master_attempt_id") or "")
    if not attempt_id:
        raise RuntimeError(
            "Cannot mark submit intent without a master attempt identifier"
        )
    conn = sqlite3.connect(database, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    ensure_analysis_schema(conn)
    master_schema = attach_master_database(conn, master_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT status, master_attempt_id
            FROM publication_queue
            WHERE publication_id=?
            """,
            (publication["publication_id"],),
        ).fetchone()
        if (
            not row
            or row["status"] != "publishing"
            or str(row["master_attempt_id"] or "") != attempt_id
        ):
            raise RuntimeError(
                "Local publication claim changed before submit intent"
            )
        mark_master_submit_intent(
            conn,
            master_schema,
            attempt_id,
        )
        conn.commit()
        publication["_submit_intent"] = True
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


async def first_visible(page: Page, selectors: tuple[str, ...]) -> tuple[Locator | None, str]:
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()
        except Exception:
            # TikTok frequently replaces the page execution context while a
            # client-side video route is settling. A polling caller can safely
            # reacquire these read-only locators on its next iteration.
            continue
        for index in range(min(count, 5)):
            candidate = locator.nth(index)
            try:
                if await candidate.is_visible():
                    return candidate, selector
            except Exception:
                continue
    return None, ""


async def wait_for_first_visible(
    page: Page,
    selectors: tuple[str, ...],
    *,
    timeout_ms: int,
    poll_ms: int = 250,
) -> tuple[Locator | None, str]:
    """Poll for a usable control and return as soon as TikTok renders it."""
    deadline = (
        asyncio.get_running_loop().time()
        + max(0, int(timeout_ms)) / 1000.0
    )
    while True:
        locator, selector = await first_visible(page, selectors)
        if locator is not None:
            return locator, selector
        remaining_ms = int(
            max(0.0, deadline - asyncio.get_running_loop().time()) * 1000
        )
        if remaining_ms <= 0:
            return None, ""
        await page.wait_for_timeout(min(max(1, poll_ms), remaining_ms))


async def open_comments_panel(
    page: Page,
    *,
    timeout_ms: int = 12000,
    poll_ms: int = 250,
) -> dict[str, Any]:
    """Open TikTok comments while tolerating a transient route render."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0, int(timeout_ms)) / 1000.0
    opened = False
    opened_selector = ""
    while True:
        input_locator, input_selector = await first_visible(
            page,
            INPUT_SELECTORS,
        )
        if input_locator is not None:
            return {
                "opened": opened,
                "selector": opened_selector,
                "input_selector": input_selector,
            }

        tab, tab_selector = await first_visible(page, COMMENTS_TAB_SELECTORS)
        if tab is not None and not opened:
            try:
                await tab.click(timeout=1500)
                opened = True
                opened_selector = tab_selector
            except Exception:
                # The route may detach the discovered control. Reacquire it
                # inside this same bounded loop rather than treating the
                # transient DOM as an account/browser failure.
                pass

        remaining_ms = int(max(0.0, deadline - loop.time()) * 1000)
        if remaining_ms <= 0:
            return {
                "opened": opened,
                "selector": opened_selector,
                "input_selector": "",
            }
        await page.wait_for_timeout(min(max(1, int(poll_ms)), remaining_ms))


async def inspect_controls(page: Page) -> dict[str, Any]:
    inputs = []
    submits = []
    for selector in INPUT_SELECTORS:
        locator = page.locator(selector)
        inputs.append({
            "selector": selector,
            "count": await locator.count(),
            "visible": any(
                [await locator.nth(index).is_visible() for index in range(min(await locator.count(), 5))]
            ) if await locator.count() else False,
        })
    for selector in SUBMIT_SELECTORS:
        locator = page.locator(selector)
        submits.append({
            "selector": selector,
            "count": await locator.count(),
            "visible": any(
                [await locator.nth(index).is_visible() for index in range(min(await locator.count(), 5))]
            ) if await locator.count() else False,
        })
    return {"url": page.url, "inputs": inputs, "submits": submits}


def _walk_for_id(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("cid", "comment_id", "commentId"):
            candidate = value.get(key)
            if candidate not in (None, ""):
                return str(candidate)
        for child in value.values():
            candidate = _walk_for_id(child)
            if candidate:
                return candidate
    elif isinstance(value, list):
        for child in value:
            candidate = _walk_for_id(child)
            if candidate:
                return candidate
    return ""


def response_success(record: dict[str, Any]) -> bool:
    status = record.get("response_status")
    if not isinstance(status, int) or not 200 <= status < 300:
        return False
    payload = record.get("response")
    if isinstance(payload, dict):
        for key in ("status_code", "statusCode", "code"):
            if key in payload and payload[key] not in (0, "0", None, ""):
                return False
        if payload.get("success") is False:
            return False
    return True


def is_tiktok_comment_publish_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme.casefold() == "https"
            and (parsed.hostname or "").casefold() in {"tiktok.com", "www.tiktok.com"}
            and parsed.username is None
            and parsed.password is None
            and parsed.port is None
            and parsed.path.rstrip("/") == "/api/comment/publish"
        )
    except ValueError:
        return False


async def _comment_identity_html(comment: Locator) -> str:
    """Return bounded comment-card markup used only for remote-ID matching."""

    fragments: list[str] = []
    candidates = [
        comment,
        comment.locator(
            "xpath=ancestor::*[@data-e2e='comment-item' "
            "or @data-comment-id or @data-id][1]"
        ),
        comment.locator(
            "xpath=ancestor::div[contains(@class,'CommentItem')][1]"
        ),
    ]
    for candidate in candidates:
        try:
            if candidate is not comment and await candidate.count() != 1:
                continue
            markup = str(
                await candidate.evaluate("(node) => node.outerHTML")
                or ""
            )
        except Exception:
            continue
        if markup:
            fragments.append(markup)
    return "\n".join(fragments)


COMMENT_ID_MARKUP_PATTERNS = (
    re.compile(
        r'''\b(?:data-comment-id|data-id)=["']([^"']+)["']''',
        re.IGNORECASE,
    ),
    re.compile(
        r'''\bid=["'](?:comment[-_:])([^"']+)["']''',
        re.IGNORECASE,
    ),
    re.compile(
        r'''["'](?:cid|comment_id|commentId)["']\s*:\s*["']?'''
        r'''([A-Za-z0-9_-]+)''',
        re.IGNORECASE,
    ),
)


def observed_comment_ids(markup: str) -> set[str]:
    """Extract exact comment-identity values, never arbitrary substrings."""

    return {
        match.group(1)
        for pattern in COMMENT_ID_MARKUP_PATTERNS
        for match in pattern.finditer(str(markup or ""))
        if match.group(1)
    }


async def exact_published_comment_locator(
    page: Page,
    draft_text: str,
    *,
    remote_comment_id: str = "",
    timeout_ms: int = 0,
) -> Locator | None:
    """Resolve exactly one visible copy of the submitted comment."""

    timeout_ms = max(0, int(timeout_ms))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_ms / 1000.0
    while True:
        comments = page.locator('[data-e2e="comment-level-1"]')
        matches: list[Locator] = []
        for index in range(await comments.count()):
            comment = comments.nth(index)
            try:
                if await comment.is_visible() and (
                    await comment.inner_text()
                ).strip() == draft_text:
                    matches.append(comment)
            except Exception:
                continue
        if remote_comment_id and matches:
            id_matches: list[Locator] = []
            for comment in matches:
                outer_html = await _comment_identity_html(comment)
                if remote_comment_id in observed_comment_ids(outer_html):
                    id_matches.append(comment)
            if len(id_matches) == 1:
                return id_matches[0]
            if len(id_matches) > 1:
                return None
            # A confirmed remote ID is authoritative. Never fall back to a
            # sole text match when that element did not actually expose the
            # ID; another account can publish identical text.
        elif not remote_comment_id and len(matches) == 1:
            return matches[0]
        elif len(matches) > 1:
            return None
        remaining_ms = int(max(0.0, deadline - loop.time()) * 1000)
        if remaining_ms <= 0:
            return None
        await page.wait_for_timeout(min(250, remaining_ms))


async def published_comment_visible(page: Page, draft_text: str) -> bool:
    return (
        await exact_published_comment_locator(page, draft_text)
        is not None
    )


def png_dimensions(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    if (
        len(header) < 24
        or header[:8] != b"\x89PNG\r\n\x1a\n"
        or header[12:16] != b"IHDR"
    ):
        raise RuntimeError("published-comment screenshot is not a valid PNG")
    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    if width <= 0 or height <= 0:
        raise RuntimeError("published-comment screenshot dimensions are invalid")
    return width, height


async def capture_exact_published_comment(
    page: Page,
    draft_text: str,
    *,
    output_path: Path,
    remote_comment_id: str,
    observed_account: str,
    source_post_id: str,
    canonical_url: str,
    locator_timeout_ms: int = 0,
    capture_phase: str = "",
) -> dict[str, Any]:
    """Capture only the uniquely resolved published comment element/card."""

    if not remote_comment_id:
        raise RuntimeError(
            "a verified remote comment ID is required for showcase capture"
        )
    comment = await exact_published_comment_locator(
        page,
        draft_text,
        remote_comment_id=remote_comment_id,
        timeout_ms=locator_timeout_ms,
    )
    if comment is None:
        raise RuntimeError(
            "exact published comment could not be uniquely resolved for capture"
        )
    capture = comment
    resolution = "comment_text_element"
    for xpath, label in (
        (
            "xpath=ancestor::*[@data-e2e='comment-item' "
            "or @data-comment-id or @data-id][1]",
            "comment_identity_container",
        ),
        (
            "xpath=ancestor::div[@data-e2e='comment-item'][1]",
            "comment_item",
        ),
        (
            "xpath=ancestor::div[contains(@class,'CommentItem')][1]",
            "comment_item_class",
        ),
        ("xpath=../..", "comment_parent"),
    ):
        try:
            candidate = comment.locator(xpath)
            if await candidate.count() == 1 and await candidate.is_visible():
                capture = candidate
                resolution = label
                break
        except Exception:
            continue
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    await capture.scroll_into_view_if_needed()
    await capture.screenshot(path=str(output_path))
    width, height = png_dimensions(output_path)
    screenshot_hash = hashlib.sha256(output_path.read_bytes()).hexdigest()
    outer_html = await _comment_identity_html(comment)
    if remote_comment_id not in observed_comment_ids(outer_html):
        raise RuntimeError(
            "captured comment element does not expose the confirmed remote ID"
        )
    captured_at = now_iso()
    proof = {
        "schema_version": "tiktok-exact-comment-screenshot-v1",
        "capture_kind": "exact_comment_element",
        "unique_match": True,
        "source_post_id": str(source_post_id),
        "canonical_url": str(canonical_url),
        "remote_comment_id": str(remote_comment_id),
        "comment_text_hash": hashlib.sha256(
            draft_text.encode("utf-8")
        ).hexdigest(),
        "observed_account": str(observed_account),
        "media_sha256": screenshot_hash,
        "locator_strategy": "remote_comment_id",
        "capture_resolution": resolution,
        "captured_at": captured_at,
    }
    if capture_phase:
        proof["capture_phase"] = str(capture_phase)
    return {
        "path": str(output_path),
        "sha256": screenshot_hash,
        "size_bytes": output_path.stat().st_size,
        "mime_type": "image/png",
        "width": width,
        "height": height,
        "proof": proof,
    }


def promote_quarantined_comment_capture(
    screenshot: dict[str, Any],
    *,
    final_path: Path,
) -> dict[str, Any]:
    """Promote immutable pre-reload bytes after a receipt is confirmed."""

    source_path = Path(str(screenshot.get("path") or "")).resolve()
    final_path = Path(final_path).resolve()
    if not source_path.is_file():
        raise RuntimeError("quarantined exact-comment screenshot is missing")
    if source_path == final_path:
        raise RuntimeError("quarantined capture path must differ from final path")
    declared_hash = str(screenshot.get("sha256") or "").strip().casefold()
    declared_size = int(screenshot.get("size_bytes") or 0)
    actual_bytes = source_path.read_bytes()
    actual_hash = hashlib.sha256(actual_bytes).hexdigest()
    if actual_hash != declared_hash or len(actual_bytes) != declared_size:
        raise RuntimeError("quarantined exact-comment screenshot bytes changed")
    proof = screenshot.get("proof")
    if (
        not isinstance(proof, dict)
        or str(proof.get("media_sha256") or "").casefold() != actual_hash
        or str(proof.get("remote_comment_id") or "").strip() == ""
    ):
        raise RuntimeError("quarantined exact-comment proof is incomplete")
    png_dimensions(source_path)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    if final_path.exists():
        raise RuntimeError("final exact-comment screenshot path already exists")
    source_path.replace(final_path)
    promoted = dict(screenshot)
    promoted["path"] = str(final_path)
    if hashlib.sha256(final_path.read_bytes()).hexdigest() != actual_hash:
        raise RuntimeError("promoted exact-comment screenshot bytes changed")
    return promoted


async def wait_for_published_comment(
    page: Page,
    draft_text: str,
    *,
    timeout_ms: int,
    poll_ms: int = 500,
) -> bool:
    """Poll for the exact submitted text while bounding the old fixed wait."""
    deadline = (
        asyncio.get_running_loop().time()
        + max(0, int(timeout_ms)) / 1000.0
    )
    while True:
        if await published_comment_visible(page, draft_text):
            return True
        remaining_ms = int(
            max(0.0, deadline - asyncio.get_running_loop().time()) * 1000
        )
        if remaining_ms <= 0:
            return False
        await page.wait_for_timeout(min(max(1, poll_ms), remaining_ms))


async def verify_persisted_comment(
    page: Page,
    draft_text: str,
    content_key: str,
    *,
    remote_comment_id: str = "",
    observed_account: str = "",
) -> bool:
    # The remote ID must come from this attempt's correlated publish response.
    # Text alone can match a pre-existing comment from any account, so it is
    # never independent evidence that our submit succeeded.
    if not remote_comment_id or not observed_account:
        return False
    try:
        await page.reload(wait_until="domcontentloaded", timeout=60000)
        await wait_for_first_visible(
            page,
            INPUT_SELECTORS + COMMENTS_TAB_SELECTORS,
            timeout_ms=5000,
        )
        await open_comments_panel(page)
        active_account = await active_tiktok_account(page)
    except Exception:
        return False
    if (
        tiktok_video_id_from_url(page.url) != str(content_key)
        or str(active_account).lstrip("@").casefold()
        != str(observed_account).lstrip("@").casefold()
    ):
        return False
    return (
        await exact_published_comment_locator(
            page,
            draft_text,
            remote_comment_id=remote_comment_id,
            timeout_ms=3000,
        )
        is not None
    )


def store_receipt(
    database: Path,
    publication: dict[str, Any],
    *,
    success: bool,
    capture_records: list[dict[str, Any]],
    remote_comment_id: str,
    visible: bool,
    persisted: bool,
    verification_path: str,
    error: str = "",
    outcome: str | None = None,
    master_database: str | Path | None = None,
) -> str:
    outcome = outcome or ("published" if success else "failed")
    if outcome not in {"published", "failed", "uncertain"}:
        raise ValueError(f"Unsupported publication outcome: {outcome}")
    if outcome == "failed" and publication.get("_submit_intent"):
        raise ValueError(
            "A publication with durable submit intent must be recorded as "
            "uncertain unless it is confirmed"
        )
    success = outcome == "published"
    database = Path(database).resolve()
    master_path = resolved_master_database(
        database,
        master_database or publication.get("_master_database"),
    )
    conn = sqlite3.connect(database, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    ensure_analysis_schema(conn)
    master_schema = attach_master_database(conn, master_path)
    attempted_at = dt.datetime.now().astimezone().isoformat()
    receipt_id = hashlib.sha256(
        f"{publication['publication_id']}\x1f{attempted_at}".encode("utf-8")
    ).hexdigest()[:32]
    request_fingerprint = capture_records[0]["capture_id"] if capture_records else ""
    response_status = capture_records[0].get("response_status") if capture_records else None
    response_payload = {
        "capture_records": capture_records,
        "target_url": publication.get("target_url", ""),
        "submitted_text": publication.get("final_text", ""),
        "comment_visible_after_submit": visible,
        "comment_persisted_after_reload": persisted,
        "verification_path": verification_path,
        "final_text_hash": publication.get("final_text_hash", ""),
        "public_rating": publication.get("public_rating", ""),
        "expected_account": publication.get("expected_account", ""),
        "observed_account": publication.get("observed_account", ""),
        "master_attempt_id": publication.get("master_attempt_id", ""),
    }
    engage_run_id = str(publication.get("engage_run_id") or "").strip()
    engage_post_id = str(publication.get("engage_post_id") or "").strip()
    try:
        conn.execute("BEGIN IMMEDIATE")
        queue_row = conn.execute(
            """
            SELECT status, target_url, content_key, draft_hash, ai_review_hash,
                   engage_run_id, engage_post_id, master_attempt_id
            FROM publication_queue WHERE publication_id=?
            """,
            (publication["publication_id"],),
        ).fetchone()
        if not queue_row or queue_row["status"] != "publishing":
            raise RuntimeError(
                "Cannot store a receipt without an active atomic publication claim"
            )
        for field in ("target_url", "content_key", "draft_hash", "ai_review_hash"):
            if queue_row[field] != publication.get(field):
                raise RuntimeError(
                    f"Claimed publication changed before receipt storage: {field}"
                )
        if (
            str(queue_row["engage_run_id"] or "") != engage_run_id
            or str(queue_row["engage_post_id"] or "") != engage_post_id
        ):
            raise RuntimeError("ENGAGE receipt source identifiers changed")
        master_attempt_id = str(queue_row["master_attempt_id"] or "")
        if (
            not master_attempt_id
            or master_attempt_id
            != str(publication.get("master_attempt_id") or "")
        ):
            raise RuntimeError("Master publication attempt binding changed")
        if bool(engage_run_id) != bool(engage_post_id):
            raise RuntimeError("ENGAGE receipt source identifiers are incomplete")
        if engage_run_id:
            if not (
                table_exists(conn, "engage_tiktok_runs")
                and table_exists(conn, "engage_tiktok_posts")
            ):
                raise RuntimeError("ENGAGE receipt source tables are missing")
            post_row = conn.execute(
                """
                SELECT status, publication_id FROM engage_tiktok_posts
                WHERE run_id=? AND post_id=?
                """,
                (engage_run_id, engage_post_id),
            ).fetchone()
            if (
                not post_row
                or post_row["status"] != "handed_off"
                or post_row["publication_id"] != publication["publication_id"]
            ):
                raise RuntimeError(
                    "ENGAGE post is not in the claimed handoff state"
                )

        master_outcome = register_publication_outcome(
            conn,
            master_schema,
            attempt_id=master_attempt_id,
            outcome={
                "published": "confirmed",
                "uncertain": "uncertain",
                "failed": "pre_submit_failed",
            }[outcome],
            receipt_id=receipt_id,
            remote_comment_id=remote_comment_id,
            remote_comment_url=str(publication.get("remote_comment_url") or ""),
            visible=visible,
            persisted=persisted,
            text_hash=str(publication.get("final_text_hash") or ""),
            error=error,
        )
        if outcome == "failed" and master_outcome == "uncertain":
            # The durable master fence is authoritative. This covers the tiny
            # crash/error window after submit intent commits but before the
            # in-memory publication dictionary can be updated.
            outcome = "uncertain"
        expected_master_outcome = {
            "published": "confirmed",
            "uncertain": "uncertain",
            "failed": "retryable",
        }[outcome]
        if master_outcome != expected_master_outcome:
            raise RuntimeError(
                "Master/local publication outcomes could not be synchronized"
            )
        queue_status = "approved" if outcome == "failed" else outcome
        queue_changed = conn.execute(
            """
            UPDATE publication_queue SET
                status = ?, last_attempt_at = ?,
                remote_comment_id = ?, published_at = ?, error = ?, updated_at = ?
            WHERE publication_id = ? AND status = 'publishing'
            """,
            (
                queue_status,
                attempted_at,
                remote_comment_id,
                attempted_at if success else "",
                error,
                attempted_at,
                publication["publication_id"],
            ),
        ).rowcount
        if queue_changed != 1:
            raise RuntimeError("Atomic publication claim was lost before receipt storage")

        if engage_run_id:
            if outcome != "failed":
                post_status = {
                    "published": "published",
                    "uncertain": "publication_uncertain",
                }[outcome]
                post_changed = conn.execute(
                    """
                    UPDATE engage_tiktok_posts SET status=?, updated_at=?
                    WHERE run_id=? AND post_id=? AND publication_id=?
                      AND status='handed_off'
                    """,
                    (
                        post_status,
                        attempted_at,
                        engage_run_id,
                        engage_post_id,
                        publication["publication_id"],
                    ),
                ).rowcount
                if post_changed != 1:
                    raise RuntimeError(
                        "ENGAGE publication outcome could not be synchronized"
                    )
            sync_engage_run_outcome(
                conn,
                engage_run_id,
                changed_at=attempted_at,
            )
            register_run_from_local(
                conn,
                master_schema,
                engage_run_id,
                database,
            )
            conn.execute(
                """
                INSERT INTO engage_tiktok_events (
                    run_id, post_id, stage, event, payload_json, created_at
                ) VALUES (?, ?, 'publication', ?, ?, ?)
                """,
                (
                    engage_run_id,
                    engage_post_id,
                    (
                        "pre_submit_failed"
                        if outcome == "failed"
                        else outcome
                    ),
                    canonical_json(
                        {
                            "publication_id": publication["publication_id"],
                            "receipt_id": receipt_id,
                            "remote_comment_id": remote_comment_id,
                            "error": error,
                        }
                    ),
                    attempted_at,
                ),
            )

        conn.execute(
            """
            INSERT INTO publication_receipts (
                receipt_id, publication_id, attempted_at, mode, transport,
                request_fingerprint, status, http_status, remote_comment_id,
                master_attempt_id, response_json, error
            ) VALUES (
                ?, ?, ?, 'live', 'browser-ui-captured-request',
                ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                receipt_id,
                publication["publication_id"],
                attempted_at,
                request_fingerprint,
                outcome,
                response_status,
                remote_comment_id,
                master_attempt_id,
                json.dumps(response_payload, ensure_ascii=False),
                error,
            ),
        )
        conn.commit()
        return receipt_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def bind_recovered_remote_comment_id(
    database: Path,
    *,
    publication_id: str,
    receipt_id: str,
    remote_comment_id: str,
    source_post_id: str,
    posting_account: str,
    comment_text_hash: str,
    master_database: str | Path | None = None,
) -> str:
    """Atomically bind an ID recovered from one exact published comment.

    Browser persistence can confirm a comment before TikTok's publish response
    exposes its ID.  Recovery may later discover that ID from a unique exact
    text element authored by the confirmed account.  This transition updates
    the local queue, its exact receipt, and the workspace master attempt in one
    crash-atomic attached-database transaction.  It cannot repair or replace a
    conflicting binding.
    """

    publication_key = str(publication_id or "").strip()
    receipt_key = str(receipt_id or "").strip()
    comment_id = str(remote_comment_id or "").strip()
    post_id = str(source_post_id or "").strip()
    account = str(posting_account or "").strip().lstrip("@").casefold()
    text_hash = str(comment_text_hash or "").strip().casefold()
    if not all(
        (
            publication_key,
            receipt_key,
            comment_id,
            post_id,
            account,
            text_hash,
        )
    ):
        raise ValueError(
            "recovered comment ID binding requires publication, receipt, ID, "
            "post, account, and text hash"
        )

    database = Path(database).resolve()
    master_path = resolved_master_database(database, master_database)
    conn = sqlite3.connect(database, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        master_schema = attach_master_database(conn, master_path)
        conn.execute("BEGIN IMMEDIATE")
        queue = conn.execute(
            """
            SELECT platform, status, content_key, target_url, draft_text,
                   draft_hash, expected_account, remote_comment_id,
                   master_attempt_id
            FROM publication_queue WHERE publication_id=?
            """,
            (publication_key,),
        ).fetchone()
        receipt = conn.execute(
            """
            SELECT status, remote_comment_id, master_attempt_id, response_json
            FROM publication_receipts
            WHERE receipt_id=? AND publication_id=?
            """,
            (receipt_key, publication_key),
        ).fetchone()
        if queue is None or receipt is None:
            raise RuntimeError(
                "recovered comment ID requires the exact local publication receipt"
            )
        if (
            str(queue["platform"] or "").casefold() != "tiktok"
            or str(queue["status"] or "") != "published"
            or str(receipt["status"] or "") != "published"
        ):
            raise RuntimeError(
                "recovered comment ID requires a confirmed TikTok publication"
            )
        try:
            response = json.loads(str(receipt["response_json"] or "{}"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("confirmed receipt response JSON is invalid") from exc
        if not isinstance(response, dict):
            raise RuntimeError("confirmed receipt response must be an object")

        queue_post_id = str(queue["content_key"] or "").strip()
        queue_url = str(queue["target_url"] or "").strip()
        queue_text = str(queue["draft_text"] or "")
        queue_text_hash = str(queue["draft_hash"] or "").strip().casefold()
        expected_account = str(
            queue["expected_account"] or ""
        ).strip().lstrip("@").casefold()
        observed_account = str(
            response.get("observed_account") or ""
        ).strip().lstrip("@").casefold()
        if (
            queue_post_id != post_id
            or tiktok_video_id_from_url(queue_url) != post_id
            or str(response.get("target_url") or "") != queue_url
        ):
            raise RuntimeError("recovered comment ID source post drift")
        if (
            not queue_text
            or hashlib.sha256(queue_text.encode("utf-8")).hexdigest()
            != text_hash
            or queue_text_hash != text_hash
            or str(response.get("submitted_text") or "") != queue_text
            or str(response.get("final_text_hash") or "").casefold()
            != text_hash
        ):
            raise RuntimeError("recovered comment ID source text-hash drift")
        if (
            not observed_account
            or observed_account != account
            or (expected_account and expected_account != account)
        ):
            raise RuntimeError("recovered comment ID posting-account drift")

        queue_attempt_id = str(queue["master_attempt_id"] or "").strip()
        receipt_attempt_id = str(receipt["master_attempt_id"] or "").strip()
        response_attempt_id = str(
            response.get("master_attempt_id") or ""
        ).strip()
        if (
            not queue_attempt_id
            or queue_attempt_id != receipt_attempt_id
            or (response_attempt_id and response_attempt_id != queue_attempt_id)
        ):
            raise RuntimeError("recovered comment ID master-attempt drift")
        queue_comment_id = str(queue["remote_comment_id"] or "").strip()
        receipt_comment_id = str(receipt["remote_comment_id"] or "").strip()
        if queue_comment_id not in {"", comment_id} or receipt_comment_id not in {
            "",
            comment_id,
        }:
            raise RuntimeError("recovered remote comment ID conflicts locally")

        bound_id = bind_master_confirmed_comment_remote_id(
            conn,
            master_schema,
            attempt_id=queue_attempt_id,
            account=account,
            post_id=post_id,
            publication_id=publication_key,
            receipt_id=receipt_key,
            text_hash=text_hash,
            remote_comment_id=comment_id,
        )
        binding = {
            "schema_version": "tiktok-recovered-comment-id-binding-v1",
            "status": "bound",
            "remote_comment_id": bound_id,
            "source_post_id": post_id,
            "posting_account": account,
            "comment_text_hash": text_hash,
            "master_attempt_id": queue_attempt_id,
            "bound_at": now_iso(),
        }
        existing_binding = response.get("remote_comment_id_binding")
        if isinstance(existing_binding, dict):
            immutable_fields = (
                "remote_comment_id",
                "source_post_id",
                "posting_account",
                "comment_text_hash",
                "master_attempt_id",
            )
            if any(
                str(existing_binding.get(field) or "")
                != str(binding[field])
                for field in immutable_fields
            ):
                raise RuntimeError(
                    "confirmed receipt already has a conflicting ID binding"
                )
            binding = existing_binding
        response["remote_comment_id"] = bound_id
        response["remote_comment_id_binding"] = binding
        queue_changed = conn.execute(
            """
            UPDATE publication_queue SET remote_comment_id=?
            WHERE publication_id=? AND status='published'
              AND platform='tiktok' AND content_key=? AND draft_hash=?
              AND master_attempt_id=? AND remote_comment_id IN ('', ?)
            """,
            (
                bound_id,
                publication_key,
                post_id,
                text_hash,
                queue_attempt_id,
                bound_id,
            ),
        ).rowcount
        receipt_changed = conn.execute(
            """
            UPDATE publication_receipts
            SET remote_comment_id=?, response_json=?
            WHERE receipt_id=? AND publication_id=? AND status='published'
              AND master_attempt_id=? AND remote_comment_id IN ('', ?)
            """,
            (
                bound_id,
                json.dumps(
                    response,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                receipt_key,
                publication_key,
                queue_attempt_id,
                bound_id,
            ),
        ).rowcount
        if queue_changed != 1 or receipt_changed != 1:
            raise RuntimeError(
                "local publication changed during recovered comment ID binding"
            )
        conn.commit()
        return bound_id
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def attach_comment_showcase_capture(
    database: Path,
    *,
    publication_id: str,
    receipt_id: str,
    screenshot: dict[str, Any] | None = None,
    error: str = "",
) -> None:
    """Attach screenshot state without changing a confirmed comment outcome."""

    conn = sqlite3.connect(
        Path(database).resolve(),
        timeout=30,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT status, response_json
            FROM publication_receipts
            WHERE receipt_id=? AND publication_id=?
            """,
            (receipt_id, publication_id),
        ).fetchone()
        if not row or str(row["status"]) != "published":
            raise RuntimeError(
                "comment showcase capture requires a confirmed comment receipt"
            )
        try:
            response = json.loads(row["response_json"] or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError("comment receipt response JSON is invalid") from exc
        if not isinstance(response, dict):
            raise RuntimeError("comment receipt response JSON must be an object")
        existing = response.get("exact_comment_screenshot")
        if screenshot:
            if existing is not None and existing != screenshot:
                raise RuntimeError(
                    "confirmed comment receipt already has a different "
                    "exact-comment screenshot"
                )
            exact_screenshot = existing or screenshot
            response["exact_comment_screenshot"] = exact_screenshot
            response["comment_showcase_capture"] = {
                "status": "captured",
                "screenshot": exact_screenshot,
                "error": "",
                "updated_at": now_iso(),
            }
        elif isinstance(existing, dict):
            # A later auxiliary retry error cannot downgrade or replace an
            # already attached immutable capture.
            response["comment_showcase_capture"] = {
                "status": "captured",
                "screenshot": existing,
                "error": "",
                "updated_at": now_iso(),
            }
        else:
            response["comment_showcase_capture"] = {
                "status": "capture_retryable",
                "screenshot": {},
                "error": str(error),
                "updated_at": now_iso(),
            }
        changed = conn.execute(
            """
            UPDATE publication_receipts
            SET response_json=?
            WHERE receipt_id=? AND publication_id=? AND status='published'
            """,
            (
                json.dumps(
                    response,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                receipt_id,
                publication_id,
            ),
        ).rowcount
        if changed != 1:
            raise RuntimeError("confirmed comment receipt changed during capture")
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def auto_prepare_enqueued_comment_showcase(
    database: Path,
    *,
    showcase: dict[str, Any],
    config_path: str | Path | None,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Run only the optional local media step after a durable enqueue."""

    if not showcase:
        return showcase, {}, ""
    try:
        from tiktok_showcase_auto_prepare import auto_prepare_showcase

        preparation = auto_prepare_showcase(
            database,
            showcase_id=str(showcase["showcase_id"]),
            config_path=config_path,
        )
        prepared_showcase = preparation.pop("showcase", None)
        if isinstance(prepared_showcase, dict):
            showcase = prepared_showcase
        return showcase, preparation, str(preparation.get("error") or "")
    except Exception as exc:
        # This is after the comment receipt and showcase enqueue are durable.
        # An optional local-media bug cannot change that confirmed outcome.
        error = str(exc)
        return (
            showcase,
            {
                "attempted": True,
                "status": "preparation_failed",
                "showcase_id": str(showcase.get("showcase_id") or ""),
                "error": error,
                "error_persisted": False,
            },
            error,
        )


class _PublicationAttemptCapture:
    """Fence and serialize one attempt's request/response event capture."""

    def __init__(self, page: Page, recorder: PublicationCapture) -> None:
        self.page = page
        self.recorder = recorder
        self.active = False
        self.closed = False
        self.settlement_complete: bool | None = None
        self.initial_record_count = len(recorder.records)
        self.request_tasks: dict[str, asyncio.Task[Any]] = {}
        self.response_tasks: dict[str, asyncio.Task[Any]] = {}
        self.listeners = {
            "request": self.capture_request,
            "response": self.capture_response,
        }
        self.attached: set[str] = set()

    def attach(self) -> None:
        for event, listener in self.listeners.items():
            self.page.on(event, listener)
            self.attached.add(event)

    def capture_request(self, request: Any) -> None:
        if (
            not self.closed
            and self.active
            and request.method == "POST"
            and is_tiktok_comment_publish_url(request.url)
        ):
            # Fence identity synchronously at event delivery, before awaiting
            # payload access can cross the submit-intent boundary.
            key = PublicationCapture._request_key(request)
            if key not in self.request_tasks:
                self.request_tasks[key] = asyncio.create_task(
                    self.recorder.on_request(request)
                )

    def capture_response(self, response: Any) -> asyncio.Task[Any] | None:
        key = PublicationCapture._request_key(response.request)
        if self.closed or key not in self.request_tasks:
            return None
        # Playwright's event and explicit expect_response result can arrive
        # together. Both must join the same in-flight task, not parse/store the
        # response twice while PublicationCapture is still awaiting its body.
        if key not in self.response_tasks:
            self.response_tasks[key] = asyncio.create_task(
                self._capture_response_once(key, response)
            )
        return self.response_tasks[key]

    async def _capture_response_once(self, key: str, response: Any) -> None:
        await self.request_tasks[key]
        await self.recorder.on_response(response)

    async def finish(self) -> bool:
        self.active = False
        self.closed = True
        for event in tuple(self.attached):
            try:
                self.page.remove_listener(event, self.listeners[event])
            except Exception:
                # A page may already have closed on the error path. Never
                # remove another listener or mask the original failure.
                pass
            self.attached.discard(event)
        # Listener callbacks register tasks synchronously, and are detached
        # before this snapshot: no pending response can be missed by flush.
        if self.settlement_complete is not None:
            return self.settlement_complete
        tasks = {*self.request_tasks.values(), *self.response_tasks.values()}
        if not tasks:
            self.settlement_complete = True
            return True
        done, pending = await asyncio.wait(
            tasks, timeout=PUBLICATION_CAPTURE_SETTLE_TIMEOUT_SECONDS
        )
        self.settlement_complete = not pending
        if pending:
            for task in pending:
                task.cancel()
            cancelled, _ = await asyncio.wait(
                pending, timeout=PUBLICATION_CAPTURE_CANCEL_TIMEOUT_SECONDS
            )
            done.update(cancelled)
        for task in done:
            if task.cancelled() or task.exception() is not None:
                self.settlement_complete = False
        return self.settlement_complete


async def _run_on_page(
    page: Page,
    args: argparse.Namespace,
    database: Path,
    publication: dict[str, Any],
    recorder: PublicationCapture,
) -> dict[str, Any]:
    capture = _PublicationAttemptCapture(page, recorder)
    try:
        capture.attach()
        return await _run_on_page_attempt(
            page, args, database, publication, recorder, capture
        )
    finally:
        await capture.finish()


async def _run_on_page_attempt(
    page: Page,
    args: argparse.Namespace,
    database: Path,
    publication: dict[str, Any],
    recorder: PublicationCapture,
    capture: _PublicationAttemptCapture,
) -> dict[str, Any]:
    await page.goto(
        publication["target_url"],
        wait_until="domcontentloaded",
        timeout=60000,
    )
    current_video_id = tiktok_video_id_from_url(page.url)
    if current_video_id != str(publication.get("content_key") or ""):
        raise RuntimeError(
            "TikTok target redirected or no longer matches the approved post"
        )
    observed_account = await active_tiktok_account(page, timeout_ms=15000)
    expected_account = str(publication.get("expected_account") or "").lstrip("@").casefold()
    if args.execute and not observed_account:
        raise RuntimeError(
            "Could not resolve the active TikTok account before live publication"
        )
    if expected_account and not observed_account:
        raise RuntimeError(
            "Could not verify the active TikTok account before publication"
        )
    if expected_account and observed_account != expected_account:
        raise RuntimeError(
            f"Wrong TikTok account: expected @{expected_account}, "
            f"observed @{observed_account}"
        )
    publication["observed_account"] = observed_account
    comments_panel = await open_comments_panel(page)
    controls = await inspect_controls(page)
    controls["comments_panel"] = comments_panel
    if not args.execute:
        return {
            "status": "dry_run",
            "publication_id": publication["publication_id"],
            "target_url": publication["target_url"],
            "expected_account": expected_account,
            "observed_account": observed_account,
            "controls": controls,
        }

    # Browser loading can outlive a short publication window. Revalidate
    # the approved row and its evidence hash immediately before typing.
    revalidated = load_approved_publication(database, args.publication_id)
    publication.clear()
    publication.update(revalidated)
    publication["observed_account"] = observed_account
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    before_comment_path = output_dir / (
        f"tiktok_comment_before_{publication['publication_id']}_"
        f"{dt.datetime.now().astimezone().strftime('%Y%m%d_%H%M%S_%f')}.png"
    )
    await page.screenshot(path=str(before_comment_path), full_page=False)
    input_locator, input_selector = await first_visible(page, INPUT_SELECTORS)
    if input_locator is None:
        raise RuntimeError(f"No visible TikTok comment input was found: {controls}")
    if tiktok_video_id_from_url(page.url) != str(publication["content_key"]):
        raise RuntimeError("TikTok target changed immediately before publication")
    observed_account = await active_tiktok_account(page)
    expected_account = str(
        publication.get("expected_account") or ""
    ).lstrip("@").casefold()
    if not observed_account:
        raise RuntimeError(
            "Could not resolve the active TikTok account immediately before publication"
        )
    if expected_account and observed_account != expected_account:
        raise RuntimeError(
            f"Wrong TikTok account immediately before publication: "
            f"expected @{expected_account}, observed @{observed_account}"
        )
    publication["observed_account"] = observed_account
    claim_publication(
        database,
        publication,
        daily_limit=args.daily_limit,
        max_attempts=args.max_attempts,
        master_database=getattr(args, "master_database", None),
        observed_account=observed_account,
    )
    publication["_claimed"] = True
    # TikTok's DraftJS placeholder overlays the empty editor and can
    # intercept pointer clicks even though the editor itself is visible.
    await input_locator.focus()
    try:
        await input_locator.click()
    except Exception:
        pass

    final_text = publication["final_text"]

    try:
        await input_locator.fill(final_text)
    except Exception:
        await page.keyboard.insert_text(final_text)

    await page.wait_for_timeout(300)
    await input_locator.focus()
    await page.keyboard.type(" ")
    await page.keyboard.press("Backspace")
    await page.wait_for_timeout(400)

    submit_locator, submit_selector = await first_visible(page, SUBMIT_SELECTORS)

    try:
        async with page.expect_response(
            lambda response: response.request.method == "POST"
            and is_tiktok_comment_publish_url(response.url),
            timeout=30000,
        ) as response_info:
            mark_publication_submit_intent(
                database,
                publication,
                master_database=getattr(args, "master_database", None),
            )
            capture.active = True
            if submit_locator and not await submit_locator.is_disabled():
                await submit_locator.click()
            else:
                await input_locator.focus()
                await page.keyboard.press("Enter")
        publish_response = await response_info.value
        capture.capture_response(publish_response)
    except PlaywrightTimeoutError:
        # Any response captured for this attempt below can still confirm it.
        # An unrelated persisted text match cannot stand in for that proof.
        pass
    capture_settled = await capture.finish()
    recorder.flush_unanswered()
    publication_records = [
        record
        for record in recorder.records[capture.initial_record_count:]
        if is_tiktok_comment_publish_url(record.get("request_url", ""))
    ]
    successful_records = [
        record
        for record in publication_records
        if capture_settled and capture_confirms_submission(
            record,
            content_key=publication["content_key"],
            final_text=final_text,
            observed_account=observed_account,
        )
    ]
    remote_comment_id = ""
    for record in successful_records:
        remote_comment_id = _walk_for_id(record["response"]["comment"])
        if remote_comment_id:
            break
    capture_timestamp = dt.datetime.now().astimezone().strftime(
        "%Y%m%d_%H%M%S_%f"
    )
    comment_screenshot_path = output_dir / (
        "tiktok_published_comment_"
        f"{publication['publication_id']}_{capture_timestamp}.png"
    )
    quarantine_path = (
        output_dir
        / ".comment_capture_quarantine"
        / (
            "tiktok_published_comment_"
            f"{publication['publication_id']}_{capture_timestamp}.pending.png"
        )
    )
    quarantined_comment_screenshot: dict[str, Any] = {}
    pre_reload_capture_error = ""
    if successful_records and remote_comment_id:
        # TikTok may rank the thread differently after the persistence reload.
        # Capture exact ID-bound bytes now, while the newly submitted element
        # is still in the live DOM.  These bytes remain quarantined and cannot
        # be attached or enqueued until store_receipt commits a confirmation.
        try:
            quarantined_comment_screenshot = (
                await capture_exact_published_comment(
                    page,
                    final_text,
                    output_path=quarantine_path,
                    remote_comment_id=remote_comment_id,
                    observed_account=observed_account,
                    source_post_id=str(publication["content_key"]),
                    canonical_url=str(publication["target_url"]),
                    locator_timeout_ms=8000,
                    capture_phase="pre_reload_quarantine",
                )
            )
        except Exception as exc:
            pre_reload_capture_error = str(exc)
        visible = bool(quarantined_comment_screenshot)
        if not visible:
            visible = (
                await exact_published_comment_locator(
                    page,
                    final_text,
                    remote_comment_id=remote_comment_id,
                )
                is not None
            )
    else:
        visible = await wait_for_published_comment(
            page,
            final_text,
            timeout_ms=8000,
        )
    persisted = await verify_persisted_comment(
        page,
        final_text,
        publication["content_key"],
        remote_comment_id=remote_comment_id,
        observed_account=observed_account,
    )
    success = bool(successful_records) and bool(remote_comment_id)
    verification_path = output_dir / (
        "tiktok_comment_verification_"
        f"{dt.datetime.now().astimezone().strftime('%Y%m%d_%H%M%S_%f')}.png"
    )
    verification_path_text = str(verification_path)
    verification_error = ""
    try:
        await page.screenshot(path=str(verification_path), full_page=False)
    except Exception as exc:
        verification_error = str(exc)
        verification_path_text = ""
    capture_path = write_capture_files(
        publication_records,
        output_dir,
        "tiktok",
    )
    store_captures(database, publication_records)
    outcome = "published" if success else "uncertain"
    error = "" if success else (
        "TikTok submission was attempted but no current-attempt, exact-text "
        "comment creation could be verified; manual reconciliation is "
        "required before any retry"
    )
    receipt_id = store_receipt(
        database,
        publication,
        success=success,
        capture_records=publication_records,
        remote_comment_id=remote_comment_id,
        visible=visible,
        persisted=persisted,
        verification_path=verification_path_text,
        error=error,
        outcome=outcome,
        master_database=getattr(args, "master_database", None),
    )
    comment_screenshot: dict[str, Any] = {}
    showcase: dict[str, Any] = {}
    showcase_preparation: dict[str, Any] = {}
    showcase_capture_error = ""
    showcase_enqueue_error = ""
    showcase_preparation_error = ""
    if success:
        try:
            if quarantined_comment_screenshot:
                comment_screenshot = promote_quarantined_comment_capture(
                    quarantined_comment_screenshot,
                    final_path=comment_screenshot_path,
                )
            else:
                # Preserve the prior auxiliary recovery attempt for response
                # paths where no pre-reload ID-bound capture was available.
                comment_screenshot = await capture_exact_published_comment(
                    page,
                    final_text,
                    output_path=comment_screenshot_path,
                    remote_comment_id=remote_comment_id,
                    observed_account=observed_account,
                    source_post_id=str(publication["content_key"]),
                    canonical_url=str(publication["target_url"]),
                    capture_phase="post_receipt_recovery",
                )
            attach_comment_showcase_capture(
                database,
                publication_id=str(publication["publication_id"]),
                receipt_id=receipt_id,
                screenshot=comment_screenshot,
            )
        except Exception as exc:
            errors = [pre_reload_capture_error, str(exc)]
            showcase_capture_error = "; ".join(
                item for item in errors if item
            )
            try:
                attach_comment_showcase_capture(
                    database,
                    publication_id=str(publication["publication_id"]),
                    receipt_id=receipt_id,
                    error=showcase_capture_error,
                )
            except Exception as attach_exc:
                showcase_capture_error = (
                    f"{showcase_capture_error}; capture status could not be "
                    f"stored: {attach_exc}"
                )
        if comment_screenshot and not showcase_capture_error:
            from tiktok_comment_showcase import (
                connect_database as connect_showcase_database,
                enqueue_confirmed_comment,
            )

            try:
                showcase_conn = connect_showcase_database(database)
                try:
                    showcase = enqueue_confirmed_comment(
                        showcase_conn,
                        publication_id=str(publication["publication_id"]),
                        receipt_id=receipt_id,
                    )
                finally:
                    showcase_conn.close()
            except Exception as exc:
                showcase_enqueue_error = str(exc)
        if showcase and not showcase_enqueue_error:
            (
                showcase,
                showcase_preparation,
                showcase_preparation_error,
            ) = auto_prepare_enqueued_comment_showcase(
                database,
                showcase=showcase,
                config_path=getattr(
                    args,
                    "showcase_auto_prepare_config",
                    "",
                ),
            )
    return {
        "status": outcome,
        "capture_settlement_complete": capture_settled,
        "publication_id": publication["publication_id"],
        "receipt_id": receipt_id,
        "target_url": publication["target_url"],
        "input_selector": input_selector,
        "submit_selector": submit_selector,
        "capture_records": len(publication_records),
        "target_capture_verified": bool(successful_records),
        "capture_path": str(capture_path),
        "verification_path": verification_path_text,
        "verification_error": verification_error,
        "comment_screenshot": comment_screenshot,
        "comment_showcase": showcase,
        "showcase_preparation": showcase_preparation,
        "showcase_capture_error": showcase_capture_error,
        "showcase_enqueue_error": showcase_enqueue_error,
        "showcase_preparation_error": showcase_preparation_error,
        "comment_screenshot_captured_before_reload": bool(
            quarantined_comment_screenshot
        ),
        "pre_reload_capture_error": pre_reload_capture_error,
        "remote_comment_id": remote_comment_id,
        "visible_after_submit": visible,
        "persisted_after_reload": persisted,
        "final_text_hash": publication["final_text_hash"],
        "public_rating": publication["public_rating"],
        "expected_account": publication.get("expected_account", ""),
        "observed_account": observed_account,
        "error": error,
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    database = Path(args.database).resolve()
    publication = load_approved_publication(database, args.publication_id)
    daily_limit = int(getattr(args, "daily_limit", DEFAULT_DAILY_LIMIT))
    max_attempts = int(getattr(args, "max_attempts", 3))
    if daily_limit < 0:
        raise RuntimeError(
            "TikTok daily publication limit must be 0 (unlimited) or positive"
        )
    if max_attempts <= 0:
        raise RuntimeError("TikTok publication attempt limit must be positive")
    args.daily_limit = daily_limit
    args.max_attempts = max_attempts
    args.master_database = str(
        bind_engage_master_database(
            database,
            publication,
            getattr(args, "master_database", None),
        )
    )
    state_path = Path(
        getattr(args, "social_browser_state", DEFAULT_BROWSER_STATE)
    ).resolve()
    startup_timeout = float(
        getattr(
            args,
            "browser_startup_timeout",
            PROFILE7_STARTUP_TIMEOUT_SECONDS,
        )
    )
    await SocialBrowserPreflight(
        state_path=state_path,
        expected_account=str(publication.get("expected_account") or ""),
        startup_timeout=startup_timeout,
    ).ensure_ready()
    from social_browser import (
        load_engage_profile7_designation,
        load_verified_profile7_state,
        platform_authentication,
        verified_profile_context,
    )

    designation = load_engage_profile7_designation(state_path.parent)
    browser_state = load_verified_profile7_state(
        state_path.parent,
        designation,
    )
    cdp_url = str(browser_state.get("cdp_url") or "")
    if not cdp_url:
        raise RuntimeError(
            "Verified Edge Profile 7 state has no live-debugging endpoint"
        )
    execute = bool(getattr(args, "execute", False))
    if execute:
        if int(publication.get("attempts") or 0) >= max_attempts:
            raise RuntimeError(
                f"TikTok publication attempt limit reached ({max_attempts})"
            )
        if (
            daily_limit > 0
            and daily_publication_count(database, publication["project"])
            >= daily_limit
        ):
            raise RuntimeError(
                f"TikTok daily publication limit reached ({daily_limit})"
            )

    recorder = PublicationCapture(
        "tiktok",
        publication["target_url"],
        publication["project"],
    )
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise RuntimeError("Edge Profile 7 has no active browser context")
        context, _ = await verified_profile_context(browser, designation)
        authentication = await platform_authentication(context, "tiktok")
        if not authentication.get("authenticated"):
            raise RuntimeError(
                "The verified Edge Profile 7 context is not authenticated to TikTok"
            )

        page = await context.new_page()
        try:
            try:
                return await _run_on_page(
                    page,
                    args,
                    database,
                    publication,
                    recorder,
                )
            except Exception as exc:
                if (
                    execute
                    and publication.get("_claimed")
                    and publication_status(
                        database,
                        publication["publication_id"],
                    )
                    == "publishing"
                ):
                    recorder.flush_unanswered()
                    publication_records = [
                        record
                        for record in recorder.records
                        if is_tiktok_comment_publish_url(
                            record.get("request_url", "")
                        )
                    ]
                    try:
                        submit_possible = bool(
                            publication.get("_submit_intent")
                            or publication_records
                        )
                        store_receipt(
                            database,
                            publication,
                            success=False,
                            capture_records=publication_records,
                            remote_comment_id="",
                            visible=False,
                            persisted=False,
                            verification_path="",
                            error=(
                                (
                                    "Publication attempt ended after durable "
                                    "submit intent; manual reconciliation is "
                                    "required: "
                                )
                                if submit_possible
                                else (
                                    "Publication attempt ended before durable "
                                    "submit intent; a fresh gated run may retry: "
                                )
                            )
                            + str(exc),
                            outcome=(
                                "uncertain"
                                if submit_possible
                                else "failed"
                            ),
                            master_database=args.master_database,
                        )
                    except Exception as receipt_exc:
                        raise RuntimeError(
                            f"{exc}; additionally failed to store the "
                            f"publication receipt: {receipt_exc}"
                        ) from exc
                raise
        finally:
            if not page.is_closed():
                await page.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Submit exactly one approved TikTok publication draft."
    )
    parser.add_argument("--database", required=True)
    parser.add_argument("--publication-id", required=True)
    parser.add_argument(
        "--master-database",
        default=str(DEFAULT_MASTER_DATABASE),
        help=(
            "Workspace-wide TikTok comment history database used to prevent "
            "repeat comments across project runs."
        ),
    )
    parser.add_argument(
        "--social-browser-state",
        default=str(DEFAULT_BROWSER_STATE),
        help=(
            "Profile 7 runtime state path. ENGAGE starts/reuses and revalidates "
            "that exact profile automatically."
        ),
    )
    parser.add_argument(
        "--browser-startup-timeout",
        type=float,
        default=PROFILE7_STARTUP_TIMEOUT_SECONDS,
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--daily-limit",
        type=int,
        default=DEFAULT_DAILY_LIMIT,
        help=(
            "Optional per-project local-day cap; 0 disables the cap "
            "(default)."
        ),
    )
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument(
        "--output-dir",
        default=str(Path("comments_data") / "publication_captures"),
    )
    parser.add_argument(
        "--showcase-auto-prepare-config",
        default="",
        help=(
            "Optional path to an explicitly enabled, non-secret JSON config "
            "for local showcase JPEG preparation. This does not draft, "
            "authorize, or publish a showcase post."
        ),
    )
    return parser.parse_args()


def main() -> None:
    result = asyncio.run(run(parse_args()))
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    if result["status"] in {"failed", "uncertain"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
