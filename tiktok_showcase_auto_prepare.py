"""Opt-in local preparation for a captured TikTok comment showcase.

This module performs only deterministic, local media work.  It does not draft
or review a caption, open a browser, call TikTok, authorize a post, or publish
anything.  A configuration file is required explicitly so comment publication
continues to work when showcase hosting has not been configured.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import json
from pathlib import Path
import re
import sqlite3
from typing import Any

from tiktok_comment_showcase import (
    bind_publish_media,
    connect_database,
    get_showcase,
)
from tiktok_showcase_media import (
    stage_comment_screenshot,
    validate_public_base_url,
)


CONFIG_SCHEMA_VERSION = "tiktok-showcase-auto-prepare-v1"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name(
    "showcase_auto_prepare.json"
)
MAX_CONFIG_BYTES = 64 * 1024
MAX_STORED_ERROR_LENGTH = 2000
SECRET_FIELD_PATTERN = re.compile(
    r"(?:token|secret|password|credential|authorization|api[_-]?key|cookie)",
    re.IGNORECASE,
)
CONFIG_FIELDS = frozenset(
    {
        "schema_version",
        "enabled",
        "public_media_verified",
        "capture_root",
        "public_directory",
        "public_base_url",
        "ffmpeg_path",
    }
)


class ShowcaseAutoPrepareError(RuntimeError):
    """Raised for an invalid or unsafe automatic-preparation configuration."""


@dataclass(frozen=True)
class ShowcaseAutoPrepareConfig:
    enabled: bool
    capture_root: Path | None = None
    public_directory: Path | None = None
    public_base_url: str = ""
    ffmpeg_path: Path | None = None


def _now_iso() -> str:
    return dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def _safe_error(value: Any) -> str:
    text = " ".join(str(value or "").replace("\x00", "").split())
    if not text:
        text = "showcase media preparation failed"
    return text[:MAX_STORED_ERROR_LENGTH]


def _absolute_directory(value: Any, label: str) -> Path:
    raw = str(value or "").strip()
    path = Path(raw)
    if not raw or not path.is_absolute():
        raise ShowcaseAutoPrepareError(f"{label} must be an absolute directory")
    resolved = path.resolve()
    if not resolved.is_dir():
        raise ShowcaseAutoPrepareError(f"{label} must already exist")
    return resolved


def _absolute_file(value: Any, label: str) -> Path:
    raw = str(value or "").strip()
    path = Path(raw)
    if not raw or not path.is_absolute():
        raise ShowcaseAutoPrepareError(f"{label} must be an absolute file")
    resolved = path.resolve()
    if not resolved.is_file():
        raise ShowcaseAutoPrepareError(f"{label} does not exist")
    return resolved


def load_showcase_auto_prepare_config(
    path: str | Path,
) -> ShowcaseAutoPrepareConfig:
    """Load a small, secret-free, explicitly enabled preparation config."""

    config_path = Path(path).resolve()
    if not config_path.is_file():
        raise ShowcaseAutoPrepareError(
            "showcase auto-prepare configuration file does not exist"
        )
    if config_path.stat().st_size > MAX_CONFIG_BYTES:
        raise ShowcaseAutoPrepareError(
            "showcase auto-prepare configuration is unexpectedly large"
        )
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ShowcaseAutoPrepareError(
            "showcase auto-prepare configuration is not valid JSON "
            f"(line {exc.lineno}, column {exc.colno})"
        ) from None
    if not isinstance(payload, dict):
        raise ShowcaseAutoPrepareError(
            "showcase auto-prepare configuration must be a JSON object"
        )

    secret_fields = sorted(
        str(key) for key in payload if SECRET_FIELD_PATTERN.search(str(key))
    )
    if secret_fields:
        raise ShowcaseAutoPrepareError(
            "showcase auto-prepare configuration must not contain secret fields"
        )
    unknown = sorted(str(key) for key in set(payload) - CONFIG_FIELDS)
    if unknown:
        raise ShowcaseAutoPrepareError(
            "showcase auto-prepare configuration contains unsupported fields: "
            + ", ".join(unknown)
        )
    if payload.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise ShowcaseAutoPrepareError(
            f"showcase auto-prepare schema_version must be {CONFIG_SCHEMA_VERSION}"
        )
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        raise ShowcaseAutoPrepareError(
            "showcase auto-prepare enabled must be true or false"
        )
    if not enabled:
        return ShowcaseAutoPrepareConfig(enabled=False)
    if payload.get("public_media_verified") is not True:
        raise ShowcaseAutoPrepareError(
            "public_media_verified must be true after the directory and HTTPS "
            "prefix are verified for the TikTok developer application"
        )

    try:
        base_url = validate_public_base_url(payload.get("public_base_url", ""))
    except Exception as exc:
        raise ShowcaseAutoPrepareError(str(exc)) from None
    return ShowcaseAutoPrepareConfig(
        enabled=True,
        capture_root=_absolute_directory(
            payload.get("capture_root"),
            "capture_root",
        ),
        public_directory=_absolute_directory(
            payload.get("public_directory"),
            "public_directory",
        ),
        public_base_url=base_url,
        ffmpeg_path=_absolute_file(payload.get("ffmpeg_path"), "ffmpeg_path"),
    )


def record_showcase_preparation_error(
    conn: sqlite3.Connection,
    *,
    showcase_id: str,
    error: Any,
) -> str:
    """Persist an auxiliary failure without changing the showcase job status."""

    message = _safe_error(error)
    if conn.in_transaction:
        raise ShowcaseAutoPrepareError(
            "preparation error requires a connection with no open transaction"
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        changed = conn.execute(
            """
            UPDATE tiktok_comment_showcase_jobs
            SET error=?, updated_at=?
            WHERE showcase_id=?
            """,
            (message, _now_iso(), str(showcase_id)),
        ).rowcount
        if changed != 1:
            raise ShowcaseAutoPrepareError(
                "showcase preparation error could not be bound to its job"
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return message


def _prepare(
    conn: sqlite3.Connection,
    *,
    showcase_id: str,
    config: ShowcaseAutoPrepareConfig,
) -> dict[str, Any]:
    if not config.enabled:
        return {
            "attempted": False,
            "status": "disabled",
            "showcase_id": str(showcase_id),
            "error": "",
        }
    if not all(
        (config.capture_root, config.public_directory, config.ffmpeg_path)
    ):
        raise ShowcaseAutoPrepareError(
            "enabled showcase preparation configuration is incomplete"
        )

    job = get_showcase(conn, showcase_id)
    staged = stage_comment_screenshot(
        job["media_path"],
        expected_source_hash=str(job["media_sha256"]),
        allowed_source_root=config.capture_root,
        public_directory=config.public_directory,
        public_base_url=config.public_base_url,
        ffmpeg_path=config.ffmpeg_path,
    )
    bound = bind_publish_media(
        conn,
        showcase_id=str(showcase_id),
        media_path=staged["media_path"],
        media_sha256=staged["media_sha256"],
        media_size_bytes=int(staged["media_bytes"]),
        media_mime_type=str(staged["mime_type"]),
        public_media_url=str(staged["media_url"]),
    )
    return {
        "attempted": True,
        "status": "publish_media_ready",
        "showcase_id": str(showcase_id),
        "media_path": str(staged["media_path"]),
        "media_sha256": str(staged["media_sha256"]),
        "media_size_bytes": int(staged["media_bytes"]),
        "media_public_url": str(staged["media_url"]),
        "error": "",
        "showcase": bound,
    }


def auto_prepare_showcase(
    database: str | Path,
    *,
    showcase_id: str,
    config_path: str | Path | None,
) -> dict[str, Any]:
    """Prepare one job, returning a failure record instead of escaping upward.

    This boundary is deliberately non-throwing so a confirmed source comment
    can never be changed to failed because its optional showcase was not ready.
    """

    if config_path in (None, ""):
        if not DEFAULT_CONFIG_PATH.is_file():
            return {
                "attempted": False,
                "status": "not_configured",
                "showcase_id": str(showcase_id),
                "error": "",
            }
        config_path = DEFAULT_CONFIG_PATH

    conn: sqlite3.Connection | None = None
    try:
        conn = connect_database(database)
        config = load_showcase_auto_prepare_config(config_path)
        result = _prepare(conn, showcase_id=str(showcase_id), config=config)
        if "showcase" not in result:
            result["showcase"] = get_showcase(conn, showcase_id)
        return result
    except Exception as exc:
        message = _safe_error(exc)
        persistence_error = ""
        error_persisted = False
        if conn is not None:
            try:
                record_showcase_preparation_error(
                    conn,
                    showcase_id=str(showcase_id),
                    error=message,
                )
                error_persisted = True
            except Exception as record_exc:
                persistence_error = _safe_error(record_exc)
        result = {
            "attempted": True,
            "status": "preparation_failed",
            "showcase_id": str(showcase_id),
            "error": message,
            "error_persisted": error_persisted,
        }
        if persistence_error:
            result["persistence_error"] = persistence_error
        elif conn is not None:
            try:
                result["showcase"] = get_showcase(conn, showcase_id)
            except Exception:
                pass
        return result
    finally:
        if conn is not None:
            conn.close()
