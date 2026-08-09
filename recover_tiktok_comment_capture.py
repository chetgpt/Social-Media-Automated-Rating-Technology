#!/usr/bin/env python
"""Recover a failed exact-comment screenshot without republishing a comment.

This command is deliberately narrower than the TikTok publication adapter.  It
accepts one confirmed publication receipt, reopens only that receipt's
canonical post in the verified Edge Profile 7 session, captures the uniquely
resolved published comment, attaches the proof to the existing receipt, and
idempotently enqueues the comment-showcase job.  It never enters a comment,
clicks a submit control, or changes the confirmed publication outcome.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any

from playwright.async_api import Page, async_playwright

from engage_tiktok import (
    DEFAULT_BROWSER_STATE,
    PROFILE7_STARTUP_TIMEOUT_SECONDS,
    SocialBrowserPreflight,
    active_tiktok_account,
)
import tiktok_publication_adapter as publication_adapter
from tiktok_master_database import DEFAULT_MASTER_DATABASE


SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
REMOTE_COMMENT_ID_PATTERNS = (
    re.compile(r'''\bdata-comment-id=["'](\d{5,})["']''', re.IGNORECASE),
    re.compile(r'''\bdata-id=["'](\d{5,})["']''', re.IGNORECASE),
    re.compile(r'''\bid=["'](?:comment[-_:])?(\d{5,})["']''', re.IGNORECASE),
    re.compile(
        r'''["'](?:cid|comment_id|commentId)["']\s*:\s*["']?(\d{5,})''',
        re.IGNORECASE,
    ),
)
COMMENT_ITEM_SELECTOR = '[data-e2e="comment-level-1"]'
COMMENT_LIST_SELECTOR = '[data-e2e="comment-list"]'
COMMENT_LOAD_MORE_SELECTORS = (
    f'{COMMENT_LIST_SELECTOR} button:has-text("View more comments")',
    f'{COMMENT_LIST_SELECTOR} button:has-text("Load more comments")',
    f'{COMMENT_LIST_SELECTOR} button:has-text("Lihat komentar lainnya")',
    f'{COMMENT_LIST_SELECTOR} button:has-text("Muat komentar lainnya")',
)
COMMENT_PANEL_OPEN_WAIT_MS = 25_000
COMMENT_LOAD_MAX_WAIT_MS = 30_000
COMMENT_LOAD_MAX_ROUNDS = 30
COMMENT_LOAD_STABLE_ROUNDS = 5
COMMENT_LOAD_POLL_MS = 700

# Scroll only the closest scrollable ancestor of the rendered comment list.
# TikTok has used both a dedicated list scroller and document scrolling across
# layouts, so the document is a bounded fallback when no list ancestor can
# scroll.  Returning geometry lets Python stop when repeated attempts make no
# progress instead of scrolling indefinitely.
COMMENT_PANEL_ADVANCE_SCRIPT = r"""
({itemSelector, listSelector}) => {
  const visible = (node) => {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      rect.width > 0 && rect.height > 0;
  };
  const items = Array.from(document.querySelectorAll(itemSelector))
    .filter(visible);
  const list = document.querySelector(listSelector);
  const anchor = items.length ? items[items.length - 1] : list;
  let node = anchor ? anchor.parentElement : null;
  while (node && node !== document.documentElement) {
    const style = window.getComputedStyle(node);
    const overflowY = String(style.overflowY || '').toLowerCase();
    if ((overflowY === 'auto' || overflowY === 'scroll' ||
         overflowY === 'overlay') &&
        node.scrollHeight > node.clientHeight + 1) {
      const before = node.scrollTop;
      node.scrollTop = node.scrollHeight;
      node.dispatchEvent(new Event('scroll', {bubbles: true}));
      return {
        kind: 'comment_ancestor',
        before,
        after: node.scrollTop,
        scrollHeight: node.scrollHeight,
        clientHeight: node.clientHeight,
      };
    }
    node = node.parentElement;
  }
  const root = document.scrollingElement;
  if (!root) {
    return {
      kind: 'unavailable', before: 0, after: 0,
      scrollHeight: 0, clientHeight: 0,
    };
  }
  const before = root.scrollTop;
  root.scrollTop = root.scrollHeight;
  root.dispatchEvent(new Event('scroll', {bubbles: true}));
  return {
    kind: 'document',
    before,
    after: root.scrollTop,
    scrollHeight: root.scrollHeight,
    clientHeight: root.clientHeight,
  };
}
"""


class CommentCaptureRecoveryError(RuntimeError):
    """A fail-closed recovery gate rejected the requested capture."""


@dataclass(frozen=True)
class ConfirmedCommentSource:
    publication_id: str
    receipt_id: str
    canonical_url: str
    post_id: str
    comment_text: str
    comment_text_hash: str
    remote_comment_id: str
    posting_account: str
    master_attempt_id: str
    response: dict[str, Any]


def migrate_legacy_master_attempt_binding(
    database: Path,
    *,
    publication_id: str,
    receipt_id: str,
    master_database: Path = DEFAULT_MASTER_DATABASE,
) -> str:
    """Bind a pre-registry local receipt to one exact confirmed master row.

    Older confirmed publications predate the additive local
    ``master_attempt_id`` columns. This migration is allowed only when the
    workspace master registry already contains exactly one confirmed attempt
    whose post, account, optional comment ID, and text hash all match the local
    rows.  An empty ID remains empty until the exact-text browser recovery has
    discovered and atomically bound it.
    """

    database = Path(database).resolve()
    conn = sqlite3.connect(database, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        queue_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(publication_queue)")
        }
        receipt_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(publication_receipts)")
        }
        queue = conn.execute(
            """
            SELECT platform, status, content_key, target_url, draft_text,
                   draft_hash, expected_account, remote_comment_id
            FROM publication_queue WHERE publication_id=?
            """,
            (str(publication_id),),
        ).fetchone()
        receipt = conn.execute(
            """
            SELECT status, remote_comment_id, response_json
            FROM publication_receipts
            WHERE receipt_id=? AND publication_id=?
            """,
            (str(receipt_id), str(publication_id)),
        ).fetchone()
        if not queue or not receipt:
            raise CommentCaptureRecoveryError(
                "legacy master binding requires the exact local publication receipt"
            )
        existing_queue_attempt = ""
        existing_receipt_attempt = ""
        if "master_attempt_id" in queue_columns:
            existing_queue_attempt = str(
                conn.execute(
                    "SELECT master_attempt_id FROM publication_queue "
                    "WHERE publication_id=?",
                    (str(publication_id),),
                ).fetchone()[0]
                or ""
            )
        if "master_attempt_id" in receipt_columns:
            existing_receipt_attempt = str(
                conn.execute(
                    "SELECT master_attempt_id FROM publication_receipts "
                    "WHERE receipt_id=?",
                    (str(receipt_id),),
                ).fetchone()[0]
                or ""
            )
        if (
            existing_queue_attempt
            and existing_receipt_attempt
            and existing_queue_attempt == existing_receipt_attempt
        ):
            return existing_queue_attempt
        if str(queue["platform"]).casefold() != "tiktok" or str(
            queue["status"]
        ) != "published" or str(receipt["status"]) != "published":
            raise CommentCaptureRecoveryError(
                "legacy master binding requires a confirmed TikTok publication"
            )
        try:
            response = json.loads(str(receipt["response_json"] or "{}"))
        except json.JSONDecodeError as exc:
            raise CommentCaptureRecoveryError(
                "legacy confirmed receipt response JSON is invalid"
            ) from exc
        if not isinstance(response, dict):
            raise CommentCaptureRecoveryError(
                "legacy confirmed receipt response must be an object"
            )
        from tiktok_comment_showcase import canonical_tiktok_url

        canonical_url, _, url_post_id = canonical_tiktok_url(queue["target_url"])
        post_id = str(queue["content_key"] or "")
        comment_hash = str(queue["draft_hash"] or "").casefold()
        remote_comment_id = str(
            receipt["remote_comment_id"] or queue["remote_comment_id"] or ""
        )
        account = _account(
            queue["expected_account"] or response.get("observed_account")
        )
        if (
            post_id != url_post_id
            or str(response.get("target_url") or "") != canonical_url
            or not comment_hash
            or _text_hash(str(queue["draft_text"] or "")) != comment_hash
            or not account
        ):
            raise CommentCaptureRecoveryError(
                "legacy local publication bindings are incomplete"
            )
        master_path = Path(master_database).resolve()
        if not master_path.is_file():
            raise CommentCaptureRecoveryError(
                "workspace master database is missing for legacy binding"
            )
        master = sqlite3.connect(master_path)
        master.row_factory = sqlite3.Row
        try:
            candidates = master.execute(
                """
                SELECT attempt_id, account_key, post_id, publication_id,
                       text_hash, state, remote_comment_id
                FROM tiktok_master_comment_attempts
                WHERE publication_id=? AND state='confirmed'
                """,
                (str(publication_id),),
            ).fetchall()
        finally:
            master.close()
        exact = [
            row
            for row in candidates
            if str(row["post_id"] or "") == post_id
            and _account(row["account_key"]) == account
            and str(row["remote_comment_id"] or "") == remote_comment_id
            and str(row["text_hash"] or "").casefold() == comment_hash
        ]
        if len(exact) != 1:
            raise CommentCaptureRecoveryError(
                "legacy receipt does not have one exact confirmed master attempt"
            )
        attempt_id = str(exact[0]["attempt_id"] or "")
        if (
            (existing_queue_attempt and existing_queue_attempt != attempt_id)
            or (
                existing_receipt_attempt
                and existing_receipt_attempt != attempt_id
            )
        ):
            raise CommentCaptureRecoveryError(
                "legacy local master-attempt binding conflicts with the registry"
            )

        conn.execute("BEGIN IMMEDIATE")
        if "master_attempt_id" not in queue_columns:
            conn.execute(
                "ALTER TABLE publication_queue ADD COLUMN "
                "master_attempt_id TEXT NOT NULL DEFAULT ''"
            )
        if "master_attempt_id" not in receipt_columns:
            conn.execute(
                "ALTER TABLE publication_receipts ADD COLUMN "
                "master_attempt_id TEXT NOT NULL DEFAULT ''"
            )
        response["master_attempt_id"] = attempt_id
        queue_changed = conn.execute(
            """
            UPDATE publication_queue SET master_attempt_id=?
            WHERE publication_id=? AND status='published'
              AND COALESCE(master_attempt_id, '') IN ('', ?)
            """,
            (attempt_id, str(publication_id), attempt_id),
        ).rowcount
        receipt_changed = conn.execute(
            """
            UPDATE publication_receipts
            SET master_attempt_id=?, response_json=?
            WHERE receipt_id=? AND publication_id=? AND status='published'
              AND COALESCE(master_attempt_id, '') IN ('', ?)
            """,
            (
                attempt_id,
                json.dumps(
                    response,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                str(receipt_id),
                str(publication_id),
                attempt_id,
            ),
        ).rowcount
        if queue_changed != 1 or receipt_changed != 1:
            raise CommentCaptureRecoveryError(
                "legacy receipt changed during master-attempt migration"
            )
        conn.commit()
        return attempt_id
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def _account(value: Any) -> str:
    return str(value or "").strip().lstrip("@").casefold()


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _require_columns(
    conn: sqlite3.Connection,
    table: str,
    required: set[str],
) -> None:
    columns = {
        str(row[1])
        for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    }
    missing = sorted(required - columns)
    if missing:
        raise CommentCaptureRecoveryError(
            f"{table} is missing required recovery fields: "
            + ", ".join(missing)
        )


def load_confirmed_comment_source(
    database: Path,
    *,
    publication_id: str,
    receipt_id: str,
) -> ConfirmedCommentSource:
    """Load and cross-check the immutable source for a screenshot retry."""

    database = Path(database).resolve()
    if not database.is_file():
        raise CommentCaptureRecoveryError(f"database does not exist: {database}")
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        _require_columns(
            conn,
            "publication_queue",
            {
                "publication_id",
                "platform",
                "status",
                "content_key",
                "target_url",
                "draft_text",
                "draft_hash",
                "expected_account",
                "remote_comment_id",
                "master_attempt_id",
            },
        )
        _require_columns(
            conn,
            "publication_receipts",
            {
                "receipt_id",
                "publication_id",
                "status",
                "remote_comment_id",
                "master_attempt_id",
                "response_json",
            },
        )
        queue_row = conn.execute(
            """
            SELECT publication_id, platform, status, content_key, target_url,
                   draft_text, draft_hash, expected_account,
                   remote_comment_id, master_attempt_id
            FROM publication_queue
            WHERE publication_id=?
            """,
            (str(publication_id),),
        ).fetchone()
        receipt_row = conn.execute(
            """
            SELECT receipt_id, publication_id, status, remote_comment_id,
                   master_attempt_id, response_json
            FROM publication_receipts
            WHERE receipt_id=? AND publication_id=?
            """,
            (str(receipt_id), str(publication_id)),
        ).fetchone()
    finally:
        conn.close()

    if not queue_row:
        raise CommentCaptureRecoveryError(
            f"publication does not exist: {publication_id}"
        )
    if not receipt_row:
        raise CommentCaptureRecoveryError(
            "receipt is not bound to the requested publication"
        )
    queue = _row_dict(queue_row)
    receipt = _row_dict(receipt_row)
    if str(queue["platform"]).casefold() != "tiktok":
        raise CommentCaptureRecoveryError("capture recovery is TikTok-only")
    if str(queue["status"]) != "published":
        raise CommentCaptureRecoveryError(
            "capture recovery requires a published publication queue row"
        )
    if str(receipt["status"]) != "published":
        raise CommentCaptureRecoveryError(
            "capture recovery requires a confirmed published receipt"
        )

    try:
        response = json.loads(str(receipt["response_json"] or "{}"))
    except json.JSONDecodeError as exc:
        raise CommentCaptureRecoveryError(
            "confirmed receipt response JSON is invalid"
        ) from exc
    if not isinstance(response, dict):
        raise CommentCaptureRecoveryError(
            "confirmed receipt response JSON must be an object"
        )

    # Reuse the showcase URL validator so recovery and downstream enqueue use
    # one canonical definition rather than merely accepting a TikTok-looking
    # URL.
    from tiktok_comment_showcase import canonical_tiktok_url

    try:
        canonical_url, _, url_post_id = canonical_tiktok_url(queue["target_url"])
    except Exception as exc:
        raise CommentCaptureRecoveryError(str(exc)) from exc
    post_id = str(queue["content_key"] or "").strip()
    if not post_id or post_id != url_post_id:
        raise CommentCaptureRecoveryError(
            "publication post ID does not match its canonical TikTok URL"
        )
    if str(response.get("target_url") or "") != canonical_url:
        raise CommentCaptureRecoveryError(
            "confirmed receipt target URL does not match the publication"
        )

    comment_text = str(queue["draft_text"] or "")
    comment_hash = str(queue["draft_hash"] or "").casefold()
    receipt_text = str(response.get("submitted_text") or "")
    receipt_hash = str(response.get("final_text_hash") or "").casefold()
    if (
        not comment_text
        or receipt_text != comment_text
        or _text_hash(comment_text) != comment_hash
        or receipt_hash != comment_hash
    ):
        raise CommentCaptureRecoveryError(
            "confirmed receipt is not bound to the exact reviewed comment text"
        )

    expected_account = _account(queue["expected_account"])
    observed_account = _account(response.get("observed_account"))
    if not observed_account:
        raise CommentCaptureRecoveryError(
            "confirmed receipt has no observed TikTok posting account"
        )
    if expected_account and observed_account != expected_account:
        raise CommentCaptureRecoveryError(
            "confirmed receipt was produced by a different TikTok account"
        )
    posting_account = expected_account or observed_account

    queue_comment_id = str(queue["remote_comment_id"] or "").strip()
    receipt_comment_id = str(receipt["remote_comment_id"] or "").strip()
    if queue_comment_id and receipt_comment_id and queue_comment_id != receipt_comment_id:
        raise CommentCaptureRecoveryError(
            "publication and receipt remote comment IDs do not match"
        )
    remote_comment_id = receipt_comment_id or queue_comment_id

    queue_attempt_id = str(queue["master_attempt_id"] or "").strip()
    receipt_attempt_id = str(receipt["master_attempt_id"] or "").strip()
    if (
        not queue_attempt_id
        or not receipt_attempt_id
        or queue_attempt_id != receipt_attempt_id
    ):
        raise CommentCaptureRecoveryError(
            "publication and receipt master attempt bindings do not match"
        )

    return ConfirmedCommentSource(
        publication_id=str(publication_id),
        receipt_id=str(receipt_id),
        canonical_url=canonical_url,
        post_id=post_id,
        comment_text=comment_text,
        comment_text_hash=comment_hash,
        remote_comment_id=remote_comment_id,
        posting_account=posting_account,
        master_attempt_id=queue_attempt_id,
        response=response,
    )


def _existing_screenshot(source: ConfirmedCommentSource) -> dict[str, Any] | None:
    screenshot = source.response.get("exact_comment_screenshot")
    if screenshot is None:
        return None
    if not isinstance(screenshot, dict):
        raise CommentCaptureRecoveryError(
            "confirmed receipt has an invalid existing screenshot record"
        )
    return screenshot


def _validate_existing_screenshot_file(
    database: Path,
    source: ConfirmedCommentSource,
    screenshot: dict[str, Any],
) -> Path:
    """Verify an attached capture before taking the browser-free fast path."""

    _validate_capture_proof(screenshot, source)
    raw_path = str(screenshot.get("path") or "").strip()
    if not raw_path:
        raise CommentCaptureRecoveryError(
            "existing exact-comment screenshot has no file path"
        )
    path = Path(raw_path)
    if not path.is_absolute():
        path = Path(database).resolve().parent / path
    path = path.resolve()
    if not path.is_file():
        raise CommentCaptureRecoveryError(
            "existing exact-comment screenshot file is missing"
        )
    actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    declared_hash = str(screenshot.get("sha256") or "").casefold()
    try:
        declared_size = int(screenshot.get("size_bytes"))
    except (TypeError, ValueError) as exc:
        raise CommentCaptureRecoveryError(
            "existing exact-comment screenshot size is invalid"
        ) from exc
    if actual_hash != declared_hash or path.stat().st_size != declared_size:
        raise CommentCaptureRecoveryError(
            "existing exact-comment screenshot file hash or size changed"
        )
    if str(screenshot.get("mime_type") or "").casefold() != "image/png":
        raise CommentCaptureRecoveryError(
            "existing exact-comment screenshot is not a PNG"
        )
    try:
        publication_adapter.png_dimensions(path)
    except Exception as exc:
        raise CommentCaptureRecoveryError(
            "existing exact-comment screenshot PNG is invalid"
        ) from exc
    return path


def _invalidate_unbound_screenshot_for_recovery(
    database: Path,
    source: ConfirmedCommentSource,
    screenshot: dict[str, Any],
    *,
    reason: str,
) -> None:
    """Remove only an invalid attachment that no showcase job has consumed."""

    conn = sqlite3.connect(Path(database).resolve(), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='tiktok_comment_showcase_jobs'"
        ).fetchone()
        if table_exists:
            bound = conn.execute(
                """
                SELECT showcase_id FROM tiktok_comment_showcase_jobs
                WHERE source_publication_id=? OR source_receipt_id=?
                LIMIT 1
                """,
                (source.publication_id, source.receipt_id),
            ).fetchone()
            if bound:
                raise CommentCaptureRecoveryError(
                    "invalid screenshot is already bound to an immutable "
                    "showcase job and cannot be recaptured automatically"
                )
        row = conn.execute(
            """
            SELECT status, response_json FROM publication_receipts
            WHERE receipt_id=? AND publication_id=?
            """,
            (source.receipt_id, source.publication_id),
        ).fetchone()
        if not row or str(row["status"]) != "published":
            raise CommentCaptureRecoveryError(
                "confirmed receipt changed before screenshot invalidation"
            )
        response = json.loads(str(row["response_json"] or "{}"))
        if not isinstance(response, dict) or response.get(
            "exact_comment_screenshot"
        ) != screenshot:
            raise CommentCaptureRecoveryError(
                "exact-comment screenshot changed before recovery"
            )
        invalid_binding_hash = hashlib.sha256(
            json.dumps(
                screenshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        response.pop("exact_comment_screenshot", None)
        if response.get("comment_screenshot") == screenshot:
            response.pop("comment_screenshot", None)
        response["comment_showcase_capture"] = {
            "status": "capture_retryable",
            "screenshot": {},
            "error": _safe_error(reason),
            "invalidated_attachment_hash": invalid_binding_hash,
        }
        changed = conn.execute(
            """
            UPDATE publication_receipts SET response_json=?
            WHERE receipt_id=? AND publication_id=? AND status='published'
            """,
            (
                json.dumps(
                    response,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                source.receipt_id,
                source.publication_id,
            ),
        ).rowcount
        if changed != 1:
            raise CommentCaptureRecoveryError(
                "confirmed receipt changed during screenshot invalidation"
            )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def _safe_filename(value: str) -> str:
    result = SAFE_NAME.sub("-", value).strip(".-")
    return result[:120] or "capture"


def default_output_path(
    database: Path,
    source: ConfirmedCommentSource,
) -> Path:
    filename = (
        f"{_safe_filename(source.publication_id)}-"
        f"{_safe_filename(source.receipt_id)}.png"
    )
    return (
        Path(database).resolve().parent
        / "comments_data"
        / "comment_showcase_captures"
        / filename
    )


def _validate_capture_proof(
    screenshot: dict[str, Any],
    source: ConfirmedCommentSource,
) -> None:
    proof = screenshot.get("proof")
    if not isinstance(proof, dict):
        raise CommentCaptureRecoveryError("capture returned no proof object")
    expected = {
        "schema_version": "tiktok-exact-comment-screenshot-v1",
        "capture_kind": "exact_comment_element",
        "source_post_id": source.post_id,
        "canonical_url": source.canonical_url,
        "remote_comment_id": source.remote_comment_id,
        "comment_text_hash": source.comment_text_hash,
        "observed_account": source.posting_account,
    }
    for field, value in expected.items():
        if field == "remote_comment_id" and not value:
            if not str(proof.get(field) or "").strip():
                raise CommentCaptureRecoveryError(
                    "exact-text fallback did not resolve a stable remote comment ID"
                )
            continue
        actual = str(proof.get(field) or "")
        if field == "observed_account":
            actual = _account(actual)
        if actual != value:
            raise CommentCaptureRecoveryError(
                f"capture proof is not bound to the source {field}"
            )
    if proof.get("unique_match") is not True:
        raise CommentCaptureRecoveryError(
            "capture proof did not uniquely verify the exact comment"
        )
    if proof.get("locator_strategy") not in {
        "remote_comment_id",
        "unique_exact_text_with_confirmed_publish_id",
    }:
        raise CommentCaptureRecoveryError(
            "capture proof used an unsupported comment locator strategy"
        )


def _bind_discovered_comment_id(
    database: Path,
    source: ConfirmedCommentSource,
    screenshot: dict[str, Any],
    *,
    master_database: Path,
) -> ConfirmedCommentSource:
    """Bind a text-fallback ID before a screenshot can be attached/enqueued."""

    if source.remote_comment_id:
        return source
    proof = screenshot.get("proof")
    if not isinstance(proof, dict):
        raise CommentCaptureRecoveryError(
            "text-fallback capture returned no exact-comment proof"
        )
    discovered_id = str(proof.get("remote_comment_id") or "").strip()
    if not discovered_id:
        raise CommentCaptureRecoveryError(
            "text-fallback capture returned no stable remote comment ID"
        )
    try:
        publication_adapter.bind_recovered_remote_comment_id(
            Path(database).resolve(),
            publication_id=source.publication_id,
            receipt_id=source.receipt_id,
            remote_comment_id=discovered_id,
            source_post_id=source.post_id,
            posting_account=source.posting_account,
            comment_text_hash=source.comment_text_hash,
            master_database=Path(master_database).resolve(),
        )
    except Exception as exc:
        raise CommentCaptureRecoveryError(
            f"recovered remote comment ID could not be bound: {exc}"
        ) from exc
    rebound = load_confirmed_comment_source(
        Path(database).resolve(),
        publication_id=source.publication_id,
        receipt_id=source.receipt_id,
    )
    if rebound.remote_comment_id != discovered_id:
        raise CommentCaptureRecoveryError(
            "recovered remote comment ID was not stored consistently"
        )
    _validate_capture_proof(screenshot, rebound)
    return rebound


async def _comment_id_for_capture(
    page: Page,
    source: ConfirmedCommentSource,
) -> str:
    """Prefer the receipt ID; otherwise derive one from one exact-text match."""

    if source.remote_comment_id:
        return source.remote_comment_id
    comment = await publication_adapter.exact_published_comment_locator(
        page,
        source.comment_text,
    )
    if comment is None:
        raise CommentCaptureRecoveryError(
            "comment has no confirmed remote ID and its exact text is not unique"
        )
    markup = await publication_adapter._comment_identity_html(comment)
    account_pattern = re.compile(
        r'''href=["'](?:https://www\.tiktok\.com)?/@'''
        + re.escape(source.posting_account)
        + r'''(?:[/?#"'])''',
        re.IGNORECASE,
    )
    if account_pattern.search(markup) is None:
        raise CommentCaptureRecoveryError(
            "unique exact-text fallback is not visibly authored by the "
            "confirmed posting account"
        )
    discovered = {
        match.group(1)
        for pattern in REMOTE_COMMENT_ID_PATTERNS
        for match in pattern.finditer(markup)
    }
    if len(discovered) != 1:
        raise CommentCaptureRecoveryError(
            "unique exact-text comment has no single stable remote ID in its element"
        )
    return discovered.pop()


async def _visible_comment_load_more(page: Page) -> tuple[Any | None, str]:
    """Return one visible, comment-list-scoped load-more control."""

    for selector in COMMENT_LOAD_MORE_SELECTORS:
        try:
            controls = page.locator(selector)
            count = await controls.count()
        except Exception:
            # A TikTok client-side navigation can detach the current DOM while
            # locators are being inspected. The bounded caller revalidates the
            # post URL before retrying.
            continue
        for index in range(min(count, 3)):
            control = controls.nth(index)
            try:
                if await control.is_visible():
                    return control, selector
            except Exception:
                continue
    return None, ""


async def _advance_comment_panel(page: Page) -> dict[str, Any]:
    """Make one bounded comment-panel advance and report progress geometry."""

    clicked_selector = ""
    control, selector = await _visible_comment_load_more(page)
    if control is not None:
        try:
            await control.click(timeout=1500)
            clicked_selector = selector
        except Exception:
            # Scrolling is still a safe recovery path when a transient render
            # detaches a load-more control between discovery and click.
            clicked_selector = ""

    try:
        scroll = await page.evaluate(
            COMMENT_PANEL_ADVANCE_SCRIPT,
            {
                "itemSelector": COMMENT_ITEM_SELECTOR,
                "listSelector": COMMENT_LIST_SELECTOR,
            },
        )
    except Exception:
        scroll = {
            "kind": "unavailable",
            "before": 0,
            "after": 0,
            "scrollHeight": 0,
            "clientHeight": 0,
        }
    if not isinstance(scroll, dict):
        scroll = {
            "kind": "unavailable",
            "before": 0,
            "after": 0,
            "scrollHeight": 0,
            "clientHeight": 0,
        }
    try:
        comment_count = await page.locator(COMMENT_ITEM_SELECTOR).count()
    except Exception:
        comment_count = -1
    return {
        "comment_count": int(comment_count),
        "clicked_selector": clicked_selector,
        "scroll_kind": str(scroll.get("kind") or "unavailable"),
        "scroll_before": int(scroll.get("before") or 0),
        "scroll_after": int(scroll.get("after") or 0),
        "scroll_height": int(scroll.get("scrollHeight") or 0),
        "client_height": int(scroll.get("clientHeight") or 0),
    }


def _comment_progress_signature(progress: dict[str, Any]) -> tuple[Any, ...]:
    """Select stable, non-content progress fields for bounded loading."""

    return (
        int(progress.get("comment_count") or 0),
        str(progress.get("clicked_selector") or ""),
        str(progress.get("scroll_kind") or ""),
        int(progress.get("scroll_after") or 0),
        int(progress.get("scroll_height") or 0),
        int(progress.get("client_height") or 0),
    )


async def _load_exact_confirmed_comment(
    page: Page,
    source: ConfirmedCommentSource,
    *,
    remote_comment_id: str,
    max_wait_ms: int = COMMENT_LOAD_MAX_WAIT_MS,
    max_rounds: int = COMMENT_LOAD_MAX_ROUNDS,
    stable_rounds: int = COMMENT_LOAD_STABLE_ROUNDS,
    poll_ms: int = COMMENT_LOAD_POLL_MS,
) -> Any | None:
    """Load and resolve only the receipt-bound comment within hard bounds.

    The confirmed remote ID is supplied to every resolver call. It is never
    dropped in favor of an exact-text-only match, even when only one visible
    comment currently has the same text.
    """

    remote_comment_id = str(remote_comment_id or "").strip()
    if not remote_comment_id:
        raise CommentCaptureRecoveryError(
            "bounded comment loading requires a stable remote comment ID"
        )
    max_wait_ms = max(1, min(int(max_wait_ms), COMMENT_LOAD_MAX_WAIT_MS))
    max_rounds = max(1, min(int(max_rounds), COMMENT_LOAD_MAX_ROUNDS))
    stable_rounds = max(1, min(int(stable_rounds), max_rounds))
    poll_ms = max(1, min(int(poll_ms), 2_000))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait_ms / 1000.0
    previous_signature: tuple[Any, ...] | None = None
    unchanged_rounds = 0

    for round_index in range(max_rounds):
        remaining_seconds = deadline - loop.time()
        if remaining_seconds <= 0:
            break
        if publication_adapter.tiktok_video_id_from_url(page.url) != source.post_id:
            raise CommentCaptureRecoveryError(
                "TikTok navigated away from the confirmed source post while "
                "loading comments"
            )
        try:
            comment = await asyncio.wait_for(
                publication_adapter.exact_published_comment_locator(
                    page,
                    source.comment_text,
                    remote_comment_id=remote_comment_id,
                ),
                timeout=remaining_seconds,
            )
        except TimeoutError:
            break
        except Exception:
            # TikTok can replace the comments subtree while it appends a page.
            # Treat that as transient only inside this bounded loop.
            comment = None
        if comment is not None:
            return comment

        remaining_seconds = deadline - loop.time()
        if remaining_seconds <= 0:
            break
        try:
            progress = await asyncio.wait_for(
                _advance_comment_panel(page),
                timeout=remaining_seconds,
            )
        except TimeoutError:
            break
        if progress["comment_count"] <= 0 and round_index < 3:
            # The panel control itself can arrive after domcontentloaded. Retry
            # the existing open helper briefly, without clicking any submit UI.
            remaining_seconds = deadline - loop.time()
            if remaining_seconds <= 0:
                break
            try:
                await asyncio.wait_for(
                    publication_adapter.open_comments_panel(page),
                    timeout=remaining_seconds,
                )
            except Exception:
                pass
        signature = _comment_progress_signature(progress)
        if signature == previous_signature:
            unchanged_rounds += 1
        else:
            unchanged_rounds = 0
            previous_signature = signature
        if unchanged_rounds >= stable_rounds:
            break

        remaining_ms = int(max(0.0, deadline - loop.time()) * 1000)
        if remaining_ms <= 0:
            break
        await page.wait_for_timeout(min(poll_ms, remaining_ms))

    # One final authoritative lookup catches content appended by the last
    # scroll without extending the deadline. It still includes the remote ID.
    if publication_adapter.tiktok_video_id_from_url(page.url) != source.post_id:
        raise CommentCaptureRecoveryError(
            "TikTok navigated away from the confirmed source post while "
            "loading comments"
        )
    remaining_seconds = deadline - loop.time()
    if remaining_seconds <= 0:
        return None
    try:
        return await asyncio.wait_for(
            publication_adapter.exact_published_comment_locator(
                page,
                source.comment_text,
                remote_comment_id=remote_comment_id,
            ),
            timeout=remaining_seconds,
        )
    except Exception:
        return None


async def _capture_on_page(
    page: Page,
    source: ConfirmedCommentSource,
    *,
    output_path: Path,
) -> dict[str, Any]:
    await page.goto(
        source.canonical_url,
        wait_until="domcontentloaded",
        timeout=60000,
    )
    if publication_adapter.tiktok_video_id_from_url(page.url) != source.post_id:
        raise CommentCaptureRecoveryError(
            "TikTok redirected to a different post during screenshot recovery"
        )
    observed_account = _account(
        await active_tiktok_account(page, timeout_ms=15000)
    )
    if not observed_account:
        await page.wait_for_timeout(1000)
        observed_account = _account(
            await active_tiktok_account(page, timeout_ms=10000)
        )
    if not observed_account:
        raise CommentCaptureRecoveryError(
            "active TikTok account could not be resolved on the source post"
        )
    if observed_account != source.posting_account:
        raise CommentCaptureRecoveryError(
            "active TikTok account does not match the confirmed posting account"
        )
    await publication_adapter.open_comments_panel(
        page,
        timeout_ms=COMMENT_PANEL_OPEN_WAIT_MS,
    )
    remote_comment_id = await _comment_id_for_capture(page, source)
    loaded_comment = await _load_exact_confirmed_comment(
        page,
        source,
        remote_comment_id=remote_comment_id,
    )
    if loaded_comment is None:
        raise CommentCaptureRecoveryError(
            "exact receipt-bound comment was not found after bounded comment "
            "panel loading"
        )
    screenshot = await publication_adapter.capture_exact_published_comment(
        page,
        source.comment_text,
        output_path=Path(output_path),
        remote_comment_id=remote_comment_id,
        observed_account=observed_account,
        source_post_id=source.post_id,
        canonical_url=source.canonical_url,
    )
    _validate_capture_proof(screenshot, source)
    return screenshot


async def _capture_from_verified_profile(
    source: ConfirmedCommentSource,
    *,
    state_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    from social_browser import (
        load_engage_profile7_designation,
        load_verified_profile7_state,
        platform_authentication,
        verified_profile_context,
    )

    state_path = Path(state_path).resolve()
    designation = load_engage_profile7_designation(state_path.parent)
    if str(designation.get("profile_directory") or "").casefold() != "profile 7":
        raise CommentCaptureRecoveryError(
            "screenshot recovery requires the designated Edge Profile 7"
        )
    browser_state = load_verified_profile7_state(
        state_path.parent,
        designation,
    )
    cdp_url = str(browser_state.get("cdp_url") or "")
    if not cdp_url:
        raise CommentCaptureRecoveryError(
            "verified Profile 7 state has no live-debugging endpoint"
        )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise CommentCaptureRecoveryError(
                "verified Profile 7 has no active browser context"
            )
        context, _ = await verified_profile_context(browser, designation)
        authentication = await platform_authentication(context, "tiktok")
        if not authentication.get("authenticated"):
            raise CommentCaptureRecoveryError(
                "verified Profile 7 is not authenticated to TikTok"
            )
        page = await context.new_page()
        try:
            return await _capture_on_page(
                page,
                source,
                output_path=output_path,
            )
        finally:
            if not page.is_closed():
                await page.close()


def _enqueue_attached_capture(
    database: Path,
    source: ConfirmedCommentSource,
) -> dict[str, Any]:
    from tiktok_comment_showcase import (
        connect_database,
        enqueue_confirmed_comment,
    )

    conn = connect_database(Path(database).resolve())
    try:
        return enqueue_confirmed_comment(
            conn,
            publication_id=source.publication_id,
            receipt_id=source.receipt_id,
        )
    finally:
        conn.close()


def _prepare_enqueued_showcase(
    database: Path,
    showcase: dict[str, Any],
    *,
    config_path: Path | None,
) -> dict[str, Any]:
    """Resume deterministic media preparation without crossing AI/live gates."""

    showcase_id = str(showcase.get("showcase_id") or "")
    try:
        from tiktok_showcase_auto_prepare import auto_prepare_showcase

        result = auto_prepare_showcase(
            database,
            showcase_id=showcase_id,
            config_path=config_path,
        )
    except Exception as exc:
        # Screenshot recovery already succeeded and the comment receipt is
        # immutable. A preparation implementation failure stays auxiliary.
        return {
            "attempted": True,
            "status": "preparation_failed",
            "showcase_id": showcase_id,
            "error": _safe_error(exc),
            "error_persisted": False,
        }
    # The full row is useful to the caller internally but makes the recovery
    # CLI output excessively large. The durable database remains canonical.
    result.pop("showcase", None)
    return result


def _safe_error(exc: Exception) -> str:
    return (str(exc).strip() or type(exc).__name__)[:1000]


async def recover_capture(
    *,
    database: Path,
    publication_id: str,
    receipt_id: str,
    state_path: Path = DEFAULT_BROWSER_STATE,
    output_path: Path | None = None,
    startup_timeout: float = PROFILE7_STARTUP_TIMEOUT_SECONDS,
    auto_prepare_config: Path | None = None,
    master_database: Path = DEFAULT_MASTER_DATABASE,
) -> dict[str, Any]:
    """Capture, attach, and enqueue one confirmed comment idempotently."""

    database = Path(database).resolve()
    migrate_legacy_master_attempt_binding(
        database,
        publication_id=publication_id,
        receipt_id=receipt_id,
        master_database=Path(master_database),
    )
    source = load_confirmed_comment_source(
        database,
        publication_id=publication_id,
        receipt_id=receipt_id,
    )
    existing = _existing_screenshot(source)
    if existing is not None:
        try:
            _validate_existing_screenshot_file(database, source, existing)
        except CommentCaptureRecoveryError as exc:
            _invalidate_unbound_screenshot_for_recovery(
                database,
                source,
                existing,
                reason=str(exc),
            )
            source = load_confirmed_comment_source(
                database,
                publication_id=publication_id,
                receipt_id=receipt_id,
            )
        else:
            source = _bind_discovered_comment_id(
                database,
                source,
                existing,
                master_database=Path(master_database),
            )
            showcase = _enqueue_attached_capture(database, source)
            preparation = _prepare_enqueued_showcase(
                database,
                showcase,
                config_path=auto_prepare_config,
            )
            return {
                "status": "already_captured",
                "publication_id": source.publication_id,
                "receipt_id": source.receipt_id,
                "showcase_id": str(showcase.get("showcase_id") or ""),
                "showcase_created": bool(showcase.get("created")),
                "screenshot_path": str(existing.get("path") or ""),
                "showcase_preparation": preparation,
            }

    capture_path = (
        Path(output_path).resolve()
        if output_path is not None
        else default_output_path(database, source)
    )
    try:
        preflight = await SocialBrowserPreflight(
            state_path=Path(state_path).resolve(),
            expected_account=source.posting_account,
            startup_timeout=float(startup_timeout),
        ).ensure_ready()
        if _account(preflight.get("observed_account")) != source.posting_account:
            raise CommentCaptureRecoveryError(
                "Profile 7 preflight account does not match the confirmed receipt"
            )
        screenshot = await _capture_from_verified_profile(
            source,
            state_path=Path(state_path),
            output_path=capture_path,
        )
        source = _bind_discovered_comment_id(
            database,
            source,
            screenshot,
            master_database=Path(master_database),
        )
        publication_adapter.attach_comment_showcase_capture(
            database,
            publication_id=source.publication_id,
            receipt_id=source.receipt_id,
            screenshot=screenshot,
        )
        showcase = _enqueue_attached_capture(database, source)
    except Exception as exc:
        try:
            publication_adapter.attach_comment_showcase_capture(
                database,
                publication_id=source.publication_id,
                receipt_id=source.receipt_id,
                error=_safe_error(exc),
            )
        except Exception:
            # Preserve the original actionable failure.  Receipt attachment is
            # auxiliary and must never change or obscure publication truth.
            pass
        raise

    preparation = _prepare_enqueued_showcase(
        database,
        showcase,
        config_path=auto_prepare_config,
    )
    return {
        "status": "captured",
        "publication_id": source.publication_id,
        "receipt_id": source.receipt_id,
        "showcase_id": str(showcase.get("showcase_id") or ""),
        "showcase_created": bool(showcase.get("created")),
        "screenshot_path": str(screenshot.get("path") or ""),
        "screenshot_sha256": str(screenshot.get("sha256") or ""),
        "showcase_preparation": preparation,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Retry one confirmed TikTok comment screenshot and enqueue its "
            "showcase without republishing the comment."
        )
    )
    parser.add_argument("--database", required=True)
    parser.add_argument("--publication-id", required=True)
    parser.add_argument("--receipt-id", required=True)
    parser.add_argument(
        "--social-browser-state",
        default=str(DEFAULT_BROWSER_STATE),
    )
    parser.add_argument("--output-path")
    parser.add_argument(
        "--master-database",
        default=str(DEFAULT_MASTER_DATABASE),
        help=(
            "Workspace master registry used only to bind confirmed legacy "
            "receipts that predate local master-attempt columns."
        ),
    )
    parser.add_argument(
        "--browser-startup-timeout",
        type=float,
        default=PROFILE7_STARTUP_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--showcase-auto-prepare-config",
        help=(
            "Optional explicit non-secret media-preparation config. If "
            "omitted, showcase_auto_prepare.json beside the scripts is used "
            "when present."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = asyncio.run(
            recover_capture(
                database=Path(args.database),
                publication_id=args.publication_id,
                receipt_id=args.receipt_id,
                state_path=Path(args.social_browser_state),
                output_path=(
                    Path(args.output_path) if args.output_path else None
                ),
                startup_timeout=args.browser_startup_timeout,
                auto_prepare_config=(
                    Path(args.showcase_auto_prepare_config)
                    if args.showcase_auto_prepare_config
                    else None
                ),
                master_database=Path(args.master_database),
            )
        )
    except Exception as exc:
        print(
            json.dumps(
                {"status": "failed", "error": _safe_error(exc)},
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
