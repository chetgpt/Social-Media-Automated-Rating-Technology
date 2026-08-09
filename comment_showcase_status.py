#!/usr/bin/env python
"""Read-only COMMENT SHOWCASE discovery and resume guidance.

This command deliberately opens the selected SQLite database in read-only,
query-only mode. It resolves one publication ID, one showcase ID, or every
pending confirmed-comment showcase and reports the next guarded stage. It does
not recover a screenshot, prepare media, invoke AI, present, authorize, open a
browser, call TikTok, or publish anything.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import sqlite3
from typing import Any, Sequence
from urllib.parse import urlsplit, urlunsplit

from tiktok_comment_showcase import (
    TIKTOK_MUSIC_USAGE_CONSENT,
    showcase_post_settings,
)
from tiktok_master_database import DEFAULT_MASTER_DATABASE
from tiktok_showcase_auto_prepare import (
    ShowcaseAutoPrepareError,
    load_showcase_auto_prepare_config,
)


SCRIPT_ROOT = Path(__file__).resolve().parent
REQUIRED_PYTHON = Path(
    r"C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe"
)
DEFAULT_AUTO_PREPARE_CONFIG = SCRIPT_ROOT / "showcase_auto_prepare.json"
REPORT_SCHEMA = "tiktok-comment-showcase-resume-report-v1"


class ShowcaseStatusError(RuntimeError):
    """A read-only discovery request could not be resolved safely."""


def now_iso() -> str:
    return dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def _powershell_quote(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _command(script: str, *arguments: Any) -> str:
    tokens = ["&", _powershell_quote(REQUIRED_PYTHON)]
    tokens.append(_powershell_quote(SCRIPT_ROOT / script))
    for argument in arguments:
        value = str(argument)
        tokens.append(value if value.startswith("--") else _powershell_quote(value))
    return " ".join(tokens)


def open_read_only_database(path: str | Path) -> sqlite3.Connection:
    database = Path(path).resolve()
    if not database.is_file():
        raise ShowcaseStatusError(f"database does not exist: {database}")
    conn = sqlite3.connect(
        database.as_uri() + "?mode=ro",
        uri=True,
        timeout=10,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (str(table),),
        ).fetchone()
        is not None
    )


def _row(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {key: row[key] for key in row.keys()}


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def inspect_auto_prepare_config(path: str | Path | None) -> dict[str, Any]:
    selected = (
        Path(path).resolve()
        if str(path or "").strip()
        else DEFAULT_AUTO_PREPARE_CONFIG.resolve()
    )
    result = {"path": str(selected), "status": "not_configured", "ready": False}
    if not selected.is_file():
        return result
    try:
        config = load_showcase_auto_prepare_config(selected)
    except (OSError, ShowcaseAutoPrepareError) as exc:
        return {**result, "status": "invalid", "error": str(exc)}
    if not config.enabled:
        return {**result, "status": "disabled"}
    return {**result, "status": "ready", "ready": True}


def _publication_row(
    conn: sqlite3.Connection,
    publication_id: str,
) -> dict[str, Any]:
    if not _table_exists(conn, "publication_queue"):
        raise ShowcaseStatusError("publication_queue table is missing")
    rows = conn.execute(
        "SELECT * FROM publication_queue WHERE publication_id=?",
        (str(publication_id),),
    ).fetchall()
    if len(rows) != 1:
        raise ShowcaseStatusError(
            f"publication ID did not resolve exactly once: {publication_id}"
        )
    return _row(rows[0])


def _confirmed_receipt(
    conn: sqlite3.Connection,
    publication_id: str,
) -> dict[str, Any]:
    if not _table_exists(conn, "publication_receipts"):
        return {}
    rows = conn.execute(
        """
        SELECT * FROM publication_receipts
        WHERE publication_id=? AND status='published'
        ORDER BY receipt_id
        """,
        (str(publication_id),),
    ).fetchall()
    if len(rows) > 1:
        raise ShowcaseStatusError(
            "publication has multiple confirmed receipts; automatic resume "
            "selection is unsafe"
        )
    return _row(rows[0]) if rows else {}


def _job_by_publication(
    conn: sqlite3.Connection,
    publication_id: str,
) -> dict[str, Any]:
    if not _table_exists(conn, "tiktok_comment_showcase_jobs"):
        return {}
    rows = conn.execute(
        """
        SELECT * FROM tiktok_comment_showcase_jobs
        WHERE source_publication_id=?
        """,
        (str(publication_id),),
    ).fetchall()
    if len(rows) > 1:
        raise ShowcaseStatusError(
            "publication resolves to multiple showcase jobs"
        )
    return _row(rows[0]) if rows else {}


def _job_by_id(conn: sqlite3.Connection, showcase_id: str) -> dict[str, Any]:
    if not _table_exists(conn, "tiktok_comment_showcase_jobs"):
        raise ShowcaseStatusError("no showcase jobs exist in this database")
    rows = conn.execute(
        "SELECT * FROM tiktok_comment_showcase_jobs WHERE showcase_id=?",
        (str(showcase_id),),
    ).fetchall()
    if len(rows) != 1:
        raise ShowcaseStatusError(
            f"showcase ID did not resolve exactly once: {showcase_id}"
        )
    return _row(rows[0])


def _public_media_base_url(job: dict[str, Any]) -> str:
    raw = str(job.get("publish_media_public_url") or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme != "https" or not parsed.netloc or "/" not in parsed.path:
        return "<same-verified-HTTPS-prefix>"
    directory = parsed.path.rsplit("/", 1)[0].rstrip("/") + "/"
    return urlunsplit((parsed.scheme, parsed.netloc, directory, "", ""))


def _recovery_command(
    database: Path,
    publication_id: str,
    receipt_id: str,
    preparation: dict[str, Any],
) -> str:
    arguments: list[Any] = [
        "--database",
        database,
        "--publication-id",
        publication_id,
        "--receipt-id",
        receipt_id,
    ]
    if preparation.get("ready"):
        arguments.extend(
            ["--showcase-auto-prepare-config", preparation["path"]]
        )
    return _command("recover_tiktok_comment_capture.py", *arguments)


def _job_report(
    job: dict[str, Any],
    *,
    database: Path,
    master_database: Path,
    preparation: dict[str, Any],
) -> dict[str, Any]:
    status = str(job.get("status") or "").strip()
    showcase_id = str(job.get("showcase_id") or "")
    publication_id = str(job.get("source_publication_id") or "")
    receipt_id = str(job.get("source_receipt_id") or "")
    common = {
        "showcase_id": showcase_id,
        "publication_id": publication_id,
        "receipt_id": receipt_id,
        "source_post_id": str(job.get("source_post_id") or ""),
        "source_url": str(job.get("canonical_url") or ""),
        "posting_account": str(job.get("posting_account") or ""),
        "current_status": status,
        "attention_required": False,
        "next_stage": "",
        "next_command": "",
        "command_template": "",
        "required_inputs": [],
    }

    if status == "captured":
        if preparation.get("ready"):
            return {
                **common,
                "next_stage": "deterministic_media_preparation",
                "next_command": _recovery_command(
                    database,
                    publication_id,
                    receipt_id,
                    preparation,
                ),
                "auto_prepare": preparation,
            }
        return {
            **common,
            "attention_required": True,
            "next_stage": "media_configuration_required",
            "auto_prepare": preparation,
            "command_template": _command(
                "publish_comment_showcase.py",
                "prepare",
                "--database",
                database,
                "--publication-id",
                publication_id,
                "--receipt-id",
                receipt_id,
                "--capture-root",
                "<project-capture-root>",
                "--public-directory",
                "<verified-domain-served-directory>",
                "--public-base-url",
                "<TikTok-verified-HTTPS-prefix>",
                "--ffmpeg",
                "<ffmpeg-executable>",
            ),
            "required_inputs": [
                "capture_root",
                "public_directory",
                "TikTok-verified public HTTPS prefix",
                "FFmpeg executable",
            ],
        }

    if status in {"publish_media_ready", "review_rejected"}:
        next_stage = (
            "builtin_ai_caption_draft"
            if status == "publish_media_ready"
            else "builtin_ai_caption_redraft"
        )
        return {
            **common,
            "next_stage": next_stage,
            "next_command": _command(
                "tiktok_comment_showcase.py",
                "--database",
                database,
                "caption-input",
                "--showcase-id",
                showcase_id,
            ),
            "command_template": _command(
                "tiktok_comment_showcase.py",
                "--database",
                database,
                "store-caption",
                "--showcase-id",
                showcase_id,
                "--caption-file",
                "<built-in-AI-caption-file>",
                "--drafted-by",
                "codex-showcase-drafter",
                "--draft-context-id",
                "<unique-draft-context>",
            ),
            "required_inputs": ["built-in AI caption", "unique draft context"],
        }

    if status == "drafted":
        return {
            **common,
            "next_stage": "independent_builtin_ai_caption_review",
            "next_command": _command(
                "tiktok_comment_showcase.py",
                "--database",
                database,
                "caption-input",
                "--showcase-id",
                showcase_id,
            ),
            "command_template": _command(
                "tiktok_comment_showcase.py",
                "--database",
                database,
                "review",
                "--showcase-id",
                showcase_id,
                "--reviewer",
                "codex-showcase-independent-reviewer",
                "--review-context-id",
                "<different-review-context>",
                "--decision-file",
                "<review-decision-json>",
            ),
            "required_inputs": [
                "independent built-in AI review",
                "different review context",
            ],
        }

    if status == "reviewed":
        return {
            **common,
            "next_stage": "named_human_presentation",
            "command_template": _command(
                "tiktok_comment_showcase.py",
                "--database",
                database,
                "show",
                "--showcase-id",
                showcase_id,
                "--presented-to",
                "<named-human>",
            ),
            "required_inputs": ["named non-AI human identity"],
            "presentation_must_show": {
                "exact_image": True,
                "exact_caption": True,
                "source_url": str(job.get("canonical_url") or ""),
                "posting_account": str(job.get("posting_account") or ""),
                "post_settings": showcase_post_settings(),
                "music_usage_consent": TIKTOK_MUSIC_USAGE_CONSENT,
            },
        }

    if status == "presented":
        presented_to = str(job.get("presented_to") or "<same-named-human>")
        return {
            **common,
            "next_stage": "separate_named_human_authorization",
            "command_template": _command(
                "tiktok_comment_showcase.py",
                "--database",
                database,
                "authorize",
                "--showcase-id",
                showcase_id,
                "--authorized-by",
                presented_to,
                "--presentation-hash",
                str(job.get("presentation_hash") or "<presentation-hash>"),
                "--approval-token",
                "<one-time-token-returned-by-show>",
                "--media-sha256",
                str(job.get("publish_media_sha256") or "<shown-media-hash>"),
                "--caption-hash",
                str(job.get("caption_hash") or "<shown-caption-hash>"),
                "--review-hash",
                str(job.get("review_hash") or "<shown-review-hash>"),
            ),
            "required_inputs": [
                "explicit approval from the same named human",
                "one-time token returned by the exact presentation",
            ],
            "approval_binds": {
                "post_settings": showcase_post_settings(),
                "music_usage_consent": TIKTOK_MUSIC_USAGE_CONSENT,
            },
        }

    if status in {"authorized", "retryable"}:
        return {
            **common,
            "next_stage": "guarded_publication_dry_run",
            "next_command": _command(
                "publish_comment_showcase.py",
                "publish",
                "--database",
                database,
                "--master-database",
                master_database,
                "--showcase-id",
                showcase_id,
                "--public-media-base-url",
                _public_media_base_url(job),
            ),
            "required_inputs": (
                []
                if _public_media_base_url(job) != "<same-verified-HTTPS-prefix>"
                else ["same operator-verified public media HTTPS prefix"]
            ),
        }

    if status in {"reserved", "submit_intent", "uncertain"}:
        stage = (
            "interrupted_pre_submit_reconciliation"
            if status == "reserved"
            else "interrupted_post_submit_reconciliation"
        )
        return {
            **common,
            "attention_required": True,
            "next_stage": stage,
            "next_command": _command(
                "publish_comment_showcase.py",
                "reconcile",
                "--database",
                database,
                "--master-database",
                master_database,
                "--showcase-id",
                showcase_id,
            ),
        }

    if status == "published":
        return {
            **common,
            "next_stage": "complete",
            "remote_post_id": str(job.get("remote_post_id") or ""),
            "remote_post_url": str(job.get("remote_post_url") or ""),
        }

    return {
        **common,
        "attention_required": True,
        "next_stage": "operator_attention",
        "error": str(job.get("error") or "unknown showcase state"),
    }


def publication_report(
    conn: sqlite3.Connection,
    publication_id: str,
    *,
    database: Path,
    master_database: Path,
    preparation: dict[str, Any],
) -> dict[str, Any]:
    publication = _publication_row(conn, publication_id)
    if (
        str(publication.get("platform") or "").casefold() != "tiktok"
        or str(publication.get("status") or "") != "published"
    ):
        return {
            "publication_id": str(publication_id),
            "current_status": str(publication.get("status") or ""),
            "attention_required": True,
            "next_stage": "confirmed_comment_receipt_required",
            "error": "publication is not a confirmed published TikTok comment",
        }
    job = _job_by_publication(conn, publication_id)
    if job:
        return _job_report(
            job,
            database=database,
            master_database=master_database,
            preparation=preparation,
        )
    receipt = _confirmed_receipt(conn, publication_id)
    if not receipt:
        return {
            "publication_id": str(publication_id),
            "current_status": "published_without_confirmed_receipt",
            "attention_required": True,
            "next_stage": "receipt_reconciliation_required",
            "error": "published queue row has no unique confirmed receipt",
        }
    receipt_id = str(receipt.get("receipt_id") or "")
    response = _json_object(receipt.get("response_json"))
    screenshot = response.get("exact_comment_screenshot")
    capture = response.get("comment_showcase_capture")
    captured = isinstance(screenshot, dict) and bool(screenshot)
    return {
        "publication_id": str(publication_id),
        "receipt_id": receipt_id,
        "showcase_id": "",
        "source_url": str(
            response.get("target_url") or publication.get("target_url") or ""
        ),
        "posting_account": str(
            response.get("observed_account")
            or publication.get("expected_account")
            or ""
        ),
        "current_status": (
            "screenshot_captured_not_enqueued"
            if captured
            else "screenshot_missing"
        ),
        "attention_required": True,
        "next_stage": (
            "enqueue_and_prepare"
            if captured
            else "exact_comment_screenshot_recovery"
        ),
        "next_command": _recovery_command(
            database,
            str(publication_id),
            receipt_id,
            preparation,
        ),
        "auto_prepare": preparation,
        "capture_error": (
            str(capture.get("error") or "")
            if isinstance(capture, dict)
            else ""
        ),
    }


def all_pending_reports(
    conn: sqlite3.Connection,
    *,
    database: Path,
    master_database: Path,
    preparation: dict[str, Any],
) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    covered_publications: set[str] = set()
    if _table_exists(conn, "tiktok_comment_showcase_jobs"):
        rows = conn.execute(
            """
            SELECT * FROM tiktok_comment_showcase_jobs
            WHERE status <> 'published'
            ORDER BY created_at, showcase_id
            """
        ).fetchall()
        for raw in rows:
            job = _row(raw)
            covered_publications.add(str(job.get("source_publication_id") or ""))
            reports.append(
                _job_report(
                    job,
                    database=database,
                    master_database=master_database,
                    preparation=preparation,
                )
            )
    if _table_exists(conn, "publication_queue"):
        rows = conn.execute(
            """
            SELECT publication_id FROM publication_queue
            WHERE platform='tiktok' AND status='published'
            ORDER BY publication_id
            """
        ).fetchall()
        for raw in rows:
            publication_id = str(raw[0])
            if publication_id in covered_publications:
                continue
            try:
                report = publication_report(
                    conn,
                    publication_id,
                    database=database,
                    master_database=master_database,
                    preparation=preparation,
                )
            except ShowcaseStatusError as exc:
                # One ambiguous legacy publication must remain visible for
                # operator repair without hiding every other pending item.
                report = {
                    "publication_id": publication_id,
                    "current_status": "ambiguous_local_binding",
                    "attention_required": True,
                    "next_stage": "operator_attention",
                    "error": str(exc),
                }
            if report.get("next_stage") != "complete":
                reports.append(report)
    return reports


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only COMMENT SHOWCASE status and deterministic resume "
            "discovery. This command performs no workflow transition."
        )
    )
    parser.add_argument("--database", required=True)
    parser.add_argument(
        "--master-database",
        default=str(DEFAULT_MASTER_DATABASE),
    )
    parser.add_argument("--auto-prepare-config", default="")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--publication-id")
    selection.add_argument("--showcase-id")
    selection.add_argument("--all-pending", action="store_true")
    return parser


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    database = Path(args.database).resolve()
    master_database = Path(args.master_database).resolve()
    preparation = inspect_auto_prepare_config(args.auto_prepare_config)
    conn = open_read_only_database(database)
    try:
        if args.publication_id:
            mode = "publication"
            items = [
                publication_report(
                    conn,
                    str(args.publication_id),
                    database=database,
                    master_database=master_database,
                    preparation=preparation,
                )
            ]
        elif args.showcase_id:
            mode = "showcase"
            items = [
                _job_report(
                    _job_by_id(conn, str(args.showcase_id)),
                    database=database,
                    master_database=master_database,
                    preparation=preparation,
                )
            ]
        else:
            mode = "all_pending"
            items = all_pending_reports(
                conn,
                database=database,
                master_database=master_database,
                preparation=preparation,
            )
    finally:
        conn.close()
    return {
        "schema_version": REPORT_SCHEMA,
        "mode": mode,
        "read_only": True,
        "database": str(database),
        "generated_at": now_iso(),
        "count": len(items),
        "needs_attention": sum(
            1 for item in items if item.get("attention_required") is True
        ),
        "items": items,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = build_report(args)
    except (ShowcaseStatusError, sqlite3.Error, OSError) as exc:
        print(
            json.dumps(
                {"schema_version": REPORT_SCHEMA, "status": "failed", "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
