"""Guarded worker for publishing reviewed TikTok comment showcases.

This module has two deliberately separate paths:

``prepare``
    Enqueue a confirmed comment screenshot, transform the raw capture into an
    API-compatible JPEG under an explicitly configured public directory, and
    bind its stable HTTPS URL before caption drafting/review/authorization.

``publish``
    Publish an already prepared, independently reviewed, presented, and
    explicitly authorized showcase through TikTok's official Content Posting
    API.  It never drafts or changes caption text or media.

Live execution is opt-in with ``--execute``.  The access token is accepted
only from an explicitly named environment variable or token file and is never
logged or written to SQLite.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit

import tiktok_comment_showcase as showcase_state
import tiktok_master_database as master_state
from tiktok_content_posting_api import (
    TikTokContentPostingClient,
    validate_https_media_url,
)


DEFAULT_MASTER_DATABASE = master_state.DEFAULT_MASTER_DATABASE
DEFAULT_BROWSER_STATE = (
    Path("comments_data") / "social_browser" / "state.json"
)
PUBLIC_PRIVACY_LEVEL = "PUBLIC_TO_EVERYONE"
MAX_TIKTOK_IMAGE_BYTES = 20 * 1024 * 1024
PUBLISH_MEDIA_WIDTH = 1080
PUBLISH_MEDIA_HEIGHT = 1920
TOKEN_ENV_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
POST_ID_PATTERN = re.compile(r"^[0-9]{1,20}$")
ACCOUNT_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
PHOTO_URL_PATTERN = re.compile(
    r"^/@(?P<account>[A-Za-z0-9._-]+)/photo/(?P<post_id>[0-9]+)$"
)


class ShowcasePublishWorkerError(RuntimeError):
    """Fail-closed worker error safe to present to an operator."""


@dataclass(frozen=True, slots=True)
class StagedJPEG:
    path: str
    sha256: str
    size_bytes: int
    mime_type: str
    public_url: str
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class PreparedShowcase:
    showcase_id: str
    source_master_attempt_id: str
    source_post_id: str
    source_comment_id: str
    source_publication_id: str
    posting_account: str
    media_path: str
    media_sha256: str
    media_size_bytes: int
    media_public_url: str
    media_binding_hash: str
    caption_text: str
    caption_hash: str
    authorization_hash: str
    canonical_url: str
    attempt_number: int


@dataclass(frozen=True, slots=True)
class ClaimedShowcase:
    showcase_id: str
    attempt_id: str
    attempt_number: int
    master_attempt_id: str
    posting_account: str
    media_path: str
    media_sha256: str
    media_public_url: str
    caption_text: str
    caption_hash: str
    authorization_hash: str
    canonical_url: str


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _safe_error(error: BaseException | str, access_token: str = "") -> str:
    value = str(error or type(error).__name__).replace("\x00", "")
    if access_token:
        value = value.replace(access_token, "[REDACTED]")
    value = " ".join(value.split())
    return (value or "showcase publication failed")[:1_000]


def _normalize_account(value: Any) -> str:
    account = str(value or "").strip()
    if account.startswith("@"):
        account = account[1:]
    account = account.casefold()
    if not account or not ACCOUNT_PATTERN.fullmatch(account):
        raise ShowcasePublishWorkerError(
            "showcase requires one exact verified TikTok posting account"
        )
    return account


def _stable_https_base_url(value: str) -> str:
    raw = str(value or "").strip().rstrip("/")
    parsed = urlsplit(raw)
    try:
        port = parsed.port
    except ValueError:
        raise ShowcasePublishWorkerError(
            "public base URL contains an invalid port"
        ) from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or port not in (None, 443)
    ):
        raise ShowcasePublishWorkerError(
            "public base URL must be stable HTTPS without credentials, "
            "query, fragment, or a nonstandard port"
        )
    hostname = str(parsed.hostname).casefold()
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ShowcasePublishWorkerError(
            "TikTok PULL_FROM_URL media must use a verified domain, not an IP"
        )
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ShowcasePublishWorkerError(
            "TikTok PULL_FROM_URL media must use a public verified domain"
        )
    return raw


def require_url_under_verified_base(
    media_url: str,
    verified_base_url: str,
) -> str:
    """Fence one stored media URL to an operator-confirmed HTTPS prefix."""

    candidate = validate_https_media_url(str(media_url or ""))
    if urlsplit(candidate).query or urlsplit(candidate).fragment:
        raise ShowcasePublishWorkerError(
            "stored publish media URL must be stable without query or fragment"
        )
    base = _stable_https_base_url(verified_base_url)
    parsed_candidate = urlsplit(candidate)
    parsed_base = urlsplit(base)
    candidate_port = parsed_candidate.port or 443
    base_port = parsed_base.port or 443
    base_path = parsed_base.path.rstrip("/")
    candidate_path = parsed_candidate.path
    path_matches = (
        candidate_path.startswith(f"{base_path}/")
        if base_path
        else candidate_path.startswith("/")
    )
    if (
        parsed_candidate.scheme != parsed_base.scheme
        or parsed_candidate.hostname != parsed_base.hostname
        or candidate_port != base_port
        or not path_matches
    ):
        raise ShowcasePublishWorkerError(
            "stored publish media URL is outside the operator-verified "
            "public media base URL"
        )
    return candidate


def _require_within(path: Path, root: Path, *, label: str) -> Path:
    resolved = path.resolve()
    resolved_root = root.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        raise ShowcasePublishWorkerError(
            f"{label} must remain inside its configured root"
        ) from None
    return resolved


def _jpeg_dimensions(path: Path) -> tuple[int, int]:
    """Read dimensions from a JPEG SOF marker without optional dependencies."""

    data = path.read_bytes()
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        raise ShowcasePublishWorkerError("staged publish media is not a JPEG")
    offset = 2
    sof_markers = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    while offset < len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(data):
            break
        segment_length = int.from_bytes(data[offset : offset + 2], "big")
        if segment_length < 2 or offset + segment_length > len(data):
            break
        if marker in sof_markers:
            if segment_length < 7:
                break
            height = int.from_bytes(data[offset + 3 : offset + 5], "big")
            width = int.from_bytes(data[offset + 5 : offset + 7], "big")
            if width < 1 or height < 1:
                break
            return width, height
        offset += segment_length
    raise ShowcasePublishWorkerError(
        "staged JPEG does not contain a valid size marker"
    )


def _validated_jpeg(path: Path) -> tuple[str, int, int, int]:
    if not path.is_file():
        raise ShowcasePublishWorkerError("staged publish JPEG is missing")
    sha256, size = _sha256_file(path)
    if size < 4 or size > MAX_TIKTOK_IMAGE_BYTES:
        raise ShowcasePublishWorkerError(
            "staged publish JPEG exceeds TikTok image-size requirements"
        )
    width, height = _jpeg_dimensions(path)
    if width != PUBLISH_MEDIA_WIDTH or height != PUBLISH_MEDIA_HEIGHT:
        raise ShowcasePublishWorkerError(
            "staged publish JPEG must be normalized to exactly 1080x1920"
        )
    return sha256, size, width, height


def stage_api_ready_jpeg(
    *,
    showcase_id: str,
    source_path: str | Path,
    expected_source_sha256: str,
    capture_root: str | Path,
    public_directory: str | Path,
    public_base_url: str,
    ffmpeg_path: str | Path,
    timeout_seconds: float = 120.0,
) -> StagedJPEG:
    """Create or reuse the deterministic pre-draft API JPEG artifact."""

    source = _require_within(
        Path(source_path),
        Path(capture_root),
        label="raw comment screenshot",
    )
    if not source.is_file():
        raise ShowcasePublishWorkerError("raw comment screenshot is missing")
    actual_source_hash, _ = _sha256_file(source)
    if actual_source_hash != str(expected_source_sha256 or "").casefold():
        raise ShowcasePublishWorkerError(
            "raw comment screenshot hash changed before media preparation"
        )
    ffmpeg = Path(ffmpeg_path).resolve()
    if not ffmpeg.is_file():
        raise ShowcasePublishWorkerError(
            "configured ffmpeg executable does not exist"
        )
    public_root = Path(public_directory).resolve()
    public_root.mkdir(parents=True, exist_ok=True)
    safe_showcase = re.sub(r"[^A-Za-z0-9._-]", "-", str(showcase_id))
    filename = f"{safe_showcase}-{actual_source_hash[:16]}.jpg"
    output = _require_within(
        public_root / filename,
        public_root,
        label="staged publish JPEG",
    )

    if not output.exists():
        temporary = public_root / (
            f".{filename}.{secrets.token_hex(8)}.tmp.jpg"
        )
        command = [
            str(ffmpeg),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-n",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-vf",
            (
                "scale=1080:1920:"
                "force_original_aspect_ratio=decrease,"
                "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black"
            ),
            "-q:v",
            "2",
            str(temporary),
        ]
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=float(timeout_seconds),
                check=False,
            )
            if completed.returncode != 0 or not temporary.is_file():
                diagnostic = " ".join(
                    str(completed.stderr or "").split()
                )[:500]
                raise ShowcasePublishWorkerError(
                    "ffmpeg could not stage the publish JPEG"
                    + (f": {diagnostic}" if diagnostic else "")
                )
            os.replace(temporary, output)
        except subprocess.TimeoutExpired:
            raise ShowcasePublishWorkerError(
                "ffmpeg timed out while staging the publish JPEG"
            ) from None
        finally:
            if temporary.exists():
                temporary.unlink()

    sha256, size, width, height = _validated_jpeg(output)
    base_url = _stable_https_base_url(public_base_url)
    public_url = f"{base_url}/{quote(filename)}"
    validate_https_media_url(public_url)
    return StagedJPEG(
        path=str(output),
        sha256=sha256,
        size_bytes=size,
        mime_type="image/jpeg",
        public_url=public_url,
        width=width,
        height=height,
    )


def prepare_comment_showcase(
    *,
    database: str | Path,
    publication_id: str,
    receipt_id: str,
    capture_root: str | Path,
    public_directory: str | Path,
    public_base_url: str,
    ffmpeg_path: str | Path,
) -> dict[str, Any]:
    """Run the explicit pre-draft screenshot and publish-media preparation."""

    conn = showcase_state.connect_database(database)
    try:
        job = showcase_state.stage_comment_screenshot(
            conn,
            publication_id=str(publication_id),
            receipt_id=str(receipt_id),
        )
        staged = stage_api_ready_jpeg(
            showcase_id=str(job["showcase_id"]),
            source_path=str(job["media_path"]),
            expected_source_sha256=str(job["media_sha256"]),
            capture_root=capture_root,
            public_directory=public_directory,
            public_base_url=public_base_url,
            ffmpeg_path=ffmpeg_path,
        )
        bound = showcase_state.bind_publish_media(
            conn,
            showcase_id=str(job["showcase_id"]),
            media_path=staged.path,
            media_sha256=staged.sha256,
            media_size_bytes=staged.size_bytes,
            media_mime_type=staged.mime_type,
            public_media_url=staged.public_url,
        )
        return {
            "mode": "prepare",
            "showcase_id": str(bound["showcase_id"]),
            "status": str(bound["status"]),
            "publish_media_path": str(bound["publish_media_path"]),
            "publish_media_sha256": str(bound["publish_media_sha256"]),
            "publish_media_size_bytes": int(
                bound["publish_media_size_bytes"]
            ),
            "publish_media_mime_type": str(
                bound["publish_media_mime_type"]
            ),
            "publish_media_public_url": str(
                bound["publish_media_public_url"]
            ),
            "publish_media_binding_hash": str(
                bound["publish_media_binding_hash"]
            ),
            "width": staged.width,
            "height": staged.height,
        }
    finally:
        conn.close()


def load_access_token(
    *,
    token_env: str,
    environ: dict[str, str] | None = None,
) -> str:
    """Load one token into memory from an explicitly named environment key."""

    name = str(token_env or "")
    if not TOKEN_ENV_PATTERN.fullmatch(name):
        raise ShowcasePublishWorkerError(
            "TikTok token environment-variable name is invalid"
        )
    source = os.environ if environ is None else environ
    value = str(source.get(name) or "").strip()
    if not value or any(character.isspace() for character in value):
        raise ShowcasePublishWorkerError(
            "TikTok access token is missing or malformed"
        )
    return value


class HostedMediaChecker:
    """Verify the exact staged JPEG is anonymously reachable before submit."""

    def __init__(
        self,
        *,
        session: Any,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.session = session
        self.timeout_seconds = float(timeout_seconds)

    def verify(
        self,
        media_url: str,
        *,
        expected_sha256: str,
        expected_size_bytes: int,
    ) -> dict[str, Any]:
        """Download and hash the public object without sending TikTok auth."""

        response = self.session.get(
            validate_https_media_url(media_url),
            headers={"Accept": "image/jpeg"},
            allow_redirects=False,
            stream=True,
            timeout=self.timeout_seconds,
        )
        try:
            status_code = int(getattr(response, "status_code", 0) or 0)
            if status_code != 200:
                raise ShowcasePublishWorkerError(
                    "public publish JPEG did not return HTTP 200"
                )
            headers = getattr(response, "headers", {})
            content_type = str(headers.get("Content-Type") or "")
            if content_type.split(";", 1)[0].strip().casefold() != "image/jpeg":
                raise ShowcasePublishWorkerError(
                    "public publish media is not served as image/jpeg"
                )
            declared_length = str(headers.get("Content-Length") or "").strip()
            if declared_length:
                try:
                    declared_size = int(declared_length)
                except ValueError:
                    raise ShowcasePublishWorkerError(
                        "public publish JPEG has an invalid Content-Length"
                    ) from None
                if declared_size != int(expected_size_bytes):
                    raise ShowcasePublishWorkerError(
                        "public publish JPEG size differs from its stored binding"
                    )

            digest = hashlib.sha256()
            downloaded = 0
            prefix = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                if len(prefix) < 3:
                    prefix.extend(chunk[: 3 - len(prefix)])
                downloaded += len(chunk)
                if downloaded > MAX_TIKTOK_IMAGE_BYTES:
                    raise ShowcasePublishWorkerError(
                        "public publish JPEG exceeds the size safety limit"
                    )
                digest.update(chunk)
            if bytes(prefix) != b"\xff\xd8\xff":
                raise ShowcasePublishWorkerError(
                    "public publish media does not contain a JPEG signature"
                )
            if (
                downloaded != int(expected_size_bytes)
                or digest.hexdigest() != str(expected_sha256).casefold()
            ):
                raise ShowcasePublishWorkerError(
                    "public publish JPEG content differs from its stored binding"
                )
            return {
                "status_code": 200,
                "mime_type": "image/jpeg",
                "size_bytes": downloaded,
                "sha256": digest.hexdigest(),
            }
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()


def _prepared_from_row(row: dict[str, Any]) -> PreparedShowcase:
    if str(row.get("status") or "") not in {"authorized", "retryable"}:
        raise ShowcasePublishWorkerError(
            "showcase is not in an authorized publishable state"
        )
    media = showcase_state.verify_publish_media(row)
    actual_sha, actual_size, _, _ = _validated_jpeg(
        Path(str(media["path"]))
    )
    if (
        actual_sha != str(media["sha256"])
        or actual_size != int(media["size_bytes"])
    ):
        raise ShowcasePublishWorkerError(
            "publish JPEG changed during readiness validation"
        )
    account = _normalize_account(row.get("posting_account"))
    required_equal = (
        (
            "authorized media",
            row.get("authorization_media_sha256"),
            media["sha256"],
        ),
        (
            "authorized caption",
            row.get("authorization_caption_hash"),
            row.get("caption_hash"),
        ),
        (
            "authorized account",
            _normalize_account(row.get("authorization_account")),
            account,
        ),
        (
            "authorized link",
            row.get("authorization_link_url"),
            row.get("canonical_url"),
        ),
    )
    for label, observed, expected in required_equal:
        if observed != expected:
            raise ShowcasePublishWorkerError(
                f"showcase {label} binding changed"
            )
    for field in (
        "caption_text",
        "caption_hash",
        "authorization_hash",
        "publish_media_binding_hash",
        "source_master_attempt_id",
        "source_post_id",
        "source_comment_id",
        "source_publication_id",
    ):
        if not str(row.get(field) or ""):
            raise ShowcasePublishWorkerError(
                f"showcase is missing required binding: {field}"
            )
    public_url = str(media["public_url"])
    parsed = urlsplit(public_url)
    if parsed.query or parsed.fragment:
        raise ShowcasePublishWorkerError(
            "authorized publish media URL is not stable"
        )
    return PreparedShowcase(
        showcase_id=str(row["showcase_id"]),
        source_master_attempt_id=str(row["source_master_attempt_id"]),
        source_post_id=str(row["source_post_id"]),
        source_comment_id=str(row["source_comment_id"]),
        source_publication_id=str(row["source_publication_id"]),
        posting_account=account,
        media_path=str(media["path"]),
        media_sha256=str(media["sha256"]),
        media_size_bytes=int(media["size_bytes"]),
        media_public_url=public_url,
        media_binding_hash=str(row["publish_media_binding_hash"]),
        caption_text=str(row["caption_text"]),
        caption_hash=str(row["caption_hash"]),
        authorization_hash=str(row["authorization_hash"]),
        canonical_url=str(row["canonical_url"]),
        attempt_number=int(row.get("attempt_count") or 0) + 1,
    )


class SQLiteShowcaseBackend:
    """Cross-database local/master state adapter using transaction hooks."""

    def __init__(
        self,
        *,
        database: str | Path,
        master_database: str | Path = DEFAULT_MASTER_DATABASE,
    ) -> None:
        self.database = Path(database).resolve()
        self.conn = showcase_state.connect_database(self.database)
        self.master_schema = master_state.attach_master_database(
            self.conn,
            master_database,
        )

    def close(self) -> None:
        self.conn.close()

    def prepare(self, showcase_id: str) -> PreparedShowcase:
        row = showcase_state.get_showcase(self.conn, showcase_id)
        prepared = _prepared_from_row(row)
        guard = master_state.showcase_target_guard(
            self.conn,
            self.master_schema,
            source_comment_attempt_id=prepared.source_master_attempt_id,
            posting_account=prepared.posting_account,
            source_post_id=prepared.source_post_id,
            source_comment_id=prepared.source_comment_id,
            source_publication_id=prepared.source_publication_id,
            media_hash=prepared.media_sha256,
            caption_hash=prepared.caption_hash,
        )
        if guard.get("blocked"):
            raise ShowcasePublishWorkerError(
                "workspace master database blocks another showcase for this "
                "published comment"
            )
        return prepared

    def list_authorized(self, *, run_id: str = "") -> list[str]:
        requested_run = str(run_id or "").strip()
        rows = showcase_state.list_showcases(self.conn)
        return [
            str(row["showcase_id"])
            for row in rows
            if str(row.get("status") or "") in {"authorized", "retryable"}
            and (
                not requested_run
                or str(row.get("source_run_id") or "") == requested_run
            )
        ]

    def claim(self, prepared: PreparedShowcase) -> ClaimedShowcase:
        def reserve_master(
            transaction_conn: sqlite3.Connection,
            local_claim: dict[str, Any],
        ) -> str:
            guard = master_state.showcase_target_guard(
                transaction_conn,
                self.master_schema,
                source_comment_attempt_id=prepared.source_master_attempt_id,
                posting_account=prepared.posting_account,
                source_post_id=prepared.source_post_id,
                source_comment_id=prepared.source_comment_id,
                source_publication_id=prepared.source_publication_id,
                media_hash=prepared.media_sha256,
                caption_hash=prepared.caption_hash,
            )
            if guard.get("blocked"):
                raise ShowcasePublishWorkerError(
                    "workspace master showcase fence became active before claim"
                )
            return master_state.register_showcase_claim(
                transaction_conn,
                self.master_schema,
                source_comment_attempt_id=prepared.source_master_attempt_id,
                posting_account=prepared.posting_account,
                source_post_id=prepared.source_post_id,
                source_comment_id=prepared.source_comment_id,
                source_publication_id=prepared.source_publication_id,
                media_hash=prepared.media_sha256,
                caption_hash=prepared.caption_hash,
                local_job_id=prepared.showcase_id,
                source_path=self.database,
                local_attempt_number=int(local_claim["attempt_number"]),
                metadata={
                    "authorization_hash": prepared.authorization_hash,
                    "publish_media_binding_hash": prepared.media_binding_hash,
                    "publish_media_public_url": prepared.media_public_url,
                },
            )

        local = showcase_state.claim_showcase(
            self.conn,
            showcase_id=prepared.showcase_id,
            expected_media_sha256=prepared.media_sha256,
            expected_caption_hash=prepared.caption_hash,
            expected_authorization_hash=prepared.authorization_hash,
            reserve_hook=reserve_master,
        )
        master_attempt_id = str(local.get("master_attempt_id") or "")
        if not master_attempt_id:
            raise ShowcasePublishWorkerError(
                "atomic master showcase claim returned no attempt identifier"
            )
        return ClaimedShowcase(
            showcase_id=str(local["showcase_id"]),
            attempt_id=str(local["attempt_id"]),
            attempt_number=int(local["attempt_number"]),
            master_attempt_id=master_attempt_id,
            posting_account=_normalize_account(local["posting_account"]),
            media_path=str(local["media_path"]),
            media_sha256=str(local["media_sha256"]),
            media_public_url=str(local["media_public_url"]),
            caption_text=str(local["caption_text"]),
            caption_hash=str(local["caption_hash"]),
            authorization_hash=str(local["authorization_hash"]),
            canonical_url=str(local["canonical_url"]),
        )

    def active_attempt(self, showcase_id: str) -> dict[str, Any]:
        """Load and verify one interrupted attempt for guarded reconciliation."""

        row = showcase_state.get_showcase(self.conn, str(showcase_id))
        status = str(row.get("status") or "")
        if status not in {"reserved", "submit_intent", "uncertain"}:
            raise ShowcasePublishWorkerError(
                "showcase has no active interrupted attempt to reconcile"
            )
        # Reuse every authorized artifact check without weakening the stored
        # status. The copied value only selects the verifier's readiness path.
        prepared = _prepared_from_row({**row, "status": "retryable"})
        attempt_id = str(row.get("active_attempt_id") or "")
        if not attempt_id:
            raise ShowcasePublishWorkerError(
                "interrupted showcase has no active local attempt"
            )
        attempt = self.conn.execute(
            """
            SELECT * FROM tiktok_comment_showcase_attempts
            WHERE attempt_id=? AND showcase_id=?
            """,
            (attempt_id, str(showcase_id)),
        ).fetchone()
        if not attempt:
            raise ShowcasePublishWorkerError(
                "interrupted showcase attempt row is missing"
            )
        item = {key: attempt[key] for key in attempt.keys()}
        state = str(item.get("state") or "")
        if state != status:
            raise ShowcasePublishWorkerError(
                "showcase job and active attempt states do not match"
            )
        master_attempt_id = str(
            item.get("master_showcase_attempt_id") or ""
        )
        if (
            not master_attempt_id
            or master_attempt_id
            != str(row.get("active_master_showcase_attempt_id") or "")
            or item.get("posting_account") != prepared.posting_account
            or item.get("media_sha256") != prepared.media_sha256
            or item.get("caption_hash") != prepared.caption_hash
            or item.get("authorization_hash") != prepared.authorization_hash
        ):
            raise ShowcasePublishWorkerError(
                "interrupted showcase attempt bindings changed"
            )
        try:
            response = json.loads(str(item.get("response_json") or "{}"))
        except json.JSONDecodeError as exc:
            raise ShowcasePublishWorkerError(
                "interrupted showcase response JSON is invalid"
            ) from exc
        if not isinstance(response, dict):
            raise ShowcasePublishWorkerError(
                "interrupted showcase response must be an object"
            )
        claim = ClaimedShowcase(
            showcase_id=prepared.showcase_id,
            attempt_id=attempt_id,
            attempt_number=int(item.get("attempt_number") or 0),
            master_attempt_id=master_attempt_id,
            posting_account=prepared.posting_account,
            media_path=prepared.media_path,
            media_sha256=prepared.media_sha256,
            media_public_url=prepared.media_public_url,
            caption_text=prepared.caption_text,
            caption_hash=prepared.caption_hash,
            authorization_hash=prepared.authorization_hash,
            canonical_url=prepared.canonical_url,
        )
        return {
            "state": state,
            "prepared": prepared,
            "claim": claim,
            "publish_id": str(item.get("publish_id") or ""),
            "publish_id_checkpoint_at": str(
                item.get("publish_id_checkpoint_at") or ""
            ),
            "response": response,
            "error": str(item.get("error") or ""),
        }

    def mark_submit_intent(self, claim: ClaimedShowcase) -> dict[str, Any]:
        def mark_master(
            transaction_conn: sqlite3.Connection,
            _payload: dict[str, Any],
        ) -> None:
            master_state.mark_showcase_submit_intent(
                transaction_conn,
                self.master_schema,
                claim.master_attempt_id,
            )

        return showcase_state.mark_submit_intent(
            self.conn,
            showcase_id=claim.showcase_id,
            attempt_id=claim.attempt_id,
            submit_intent_hook=mark_master,
        )

    def checkpoint_publish_id(
        self,
        claim: ClaimedShowcase,
        publish_id: str,
    ) -> dict[str, Any]:
        """Atomically checkpoint TikTok's publish ID in local and master state."""

        def checkpoint_master(
            transaction_conn: sqlite3.Connection,
            payload: dict[str, Any],
        ) -> None:
            master_state.checkpoint_showcase_publish_id(
                transaction_conn,
                self.master_schema,
                claim.master_attempt_id,
                str(payload["publish_id"]),
                checkpointed_at=str(payload["publish_id_checkpoint_at"]),
            )

        return showcase_state.checkpoint_publish_id(
            self.conn,
            showcase_id=claim.showcase_id,
            attempt_id=claim.attempt_id,
            publish_id=publish_id,
            checkpoint_hook=checkpoint_master,
        )

    def record_outcome(
        self,
        claim: ClaimedShowcase,
        *,
        outcome: str,
        remote_post_id: str = "",
        remote_post_url: str = "",
        visible: bool = False,
        persisted: bool = False,
        response: dict[str, Any] | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        def record_master(
            transaction_conn: sqlite3.Connection,
            _payload: dict[str, Any],
        ) -> str:
            response_payload = response or {}
            return master_state.register_showcase_outcome(
                transaction_conn,
                self.master_schema,
                attempt_id=claim.master_attempt_id,
                outcome=outcome,
                receipt_id=claim.attempt_id,
                remote_post_id=remote_post_id,
                remote_post_url=remote_post_url,
                media_hash=claim.media_sha256,
                caption_hash=claim.caption_hash,
                publish_id=str(response_payload.get("publish_id") or ""),
                error=error,
            )

        return showcase_state.record_outcome(
            self.conn,
            showcase_id=claim.showcase_id,
            attempt_id=claim.attempt_id,
            outcome=outcome,
            remote_post_id=remote_post_id,
            remote_post_url=remote_post_url,
            visible=visible,
            persisted=persisted,
            response=response,
            error=error,
            outcome_hook=record_master,
        )


def canonical_remote_photo_url(account: str, post_id: str) -> str:
    normalized_account = _normalize_account(account)
    normalized_post_id = str(post_id or "")
    if (
        not POST_ID_PATTERN.fullmatch(normalized_post_id)
        or int(normalized_post_id) <= 0
    ):
        raise ShowcasePublishWorkerError(
            "TikTok returned an invalid public post ID"
        )
    return (
        f"https://www.tiktok.com/@{normalized_account}/photo/"
        f"{normalized_post_id}"
    )


class _TikTokPostEvidenceParser(HTMLParser):
    """Extract only non-sensitive identity evidence from one TikTok page."""

    _STRUCTURED_SCRIPT_IDS = {
        "__universal_data_for_rehydration__",
        "sigi_state",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.og_urls: list[str] = []
        self.structured_scripts: list[tuple[str, str]] = []
        self.title_parts: list[str] = []
        self.visible_text_parts: list[str] = []
        self.shell_attributes: list[str] = []
        self._title_depth = 0
        self._suppressed_depth = 0
        self._script_label = ""
        self._script_parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        lowered_tag = tag.casefold()
        normalized = {
            str(key).casefold(): str(value or "")
            for key, value in attrs
        }
        if lowered_tag == "meta" and (
            normalized.get("property", "").casefold() == "og:url"
        ):
            self.og_urls.append(normalized.get("content", "").strip())
        if lowered_tag == "title":
            self._title_depth += 1
        if lowered_tag in {"script", "style", "noscript"}:
            self._suppressed_depth += 1
        if lowered_tag == "script":
            script_id = normalized.get("id", "").casefold()
            script_type = normalized.get("type", "").casefold()
            if (
                script_id in self._STRUCTURED_SCRIPT_IDS
                or script_type == "application/ld+json"
            ):
                self._script_label = script_id or "application/ld+json"
                self._script_parts = []
        identity = " ".join(
            (
                normalized.get("id", ""),
                normalized.get("class", ""),
                normalized.get("data-e2e", ""),
            )
        ).casefold()
        if any(
            marker in identity
            for marker in (
                "captcha",
                "challenge-container",
                "security-check",
                "verify-center",
                "verifycenter",
            )
        ):
            self.shell_attributes.append(identity[:240])

    def handle_endtag(self, tag: str) -> None:
        lowered_tag = tag.casefold()
        if lowered_tag == "script":
            if self._script_label:
                self.structured_scripts.append(
                    (self._script_label, "".join(self._script_parts))
                )
            self._script_label = ""
            self._script_parts = []
        if lowered_tag in {"script", "style", "noscript"}:
            self._suppressed_depth = max(0, self._suppressed_depth - 1)
        if lowered_tag == "title":
            self._title_depth = max(0, self._title_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._script_label:
            self._script_parts.append(data)
        if self._title_depth:
            self.title_parts.append(data)
        if not self._suppressed_depth and data.strip():
            self.visible_text_parts.append(data.strip())


def _direct_scalar(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (str, int)):
        return str(value).strip()
    return ""


def _direct_post_ids(node: dict[str, Any], parent_key: str) -> set[str]:
    candidates = {
        _direct_scalar(node.get(key))
        for key in (
            "id",
            "aweme_id",
            "awemeId",
            "item_id",
            "itemId",
            "video_id",
            "videoId",
        )
    }
    if parent_key:
        candidates.add(parent_key)
    return {value for value in candidates if POST_ID_PATTERN.fullmatch(value)}


def _direct_author_accounts(node: dict[str, Any]) -> set[str]:
    values: list[Any] = []
    for key in ("author", "authorInfo", "creator", "creatorInfo"):
        author = node.get(key)
        if isinstance(author, dict):
            values.extend(
                author.get(identity_key)
                for identity_key in (
                    "uniqueId",
                    "unique_id",
                    "username",
                    "userName",
                )
            )
        else:
            values.append(author)
    values.extend(
        node.get(key)
        for key in (
            "authorUniqueId",
            "author_unique_id",
            "creatorUsername",
            "creator_username",
        )
    )
    accounts: set[str] = set()
    for value in values:
        candidate = _direct_scalar(value)
        if candidate.startswith("@"):
            candidate = candidate[1:]
        if candidate and ACCOUNT_PATTERN.fullmatch(candidate):
            accounts.add(candidate.casefold())
    return accounts


def _structured_post_identity(
    value: Any,
    *,
    expected_account: str,
    expected_post_id: str,
    path: str = "$",
    parent_key: str = "",
) -> dict[str, str] | None:
    if isinstance(value, dict):
        post_ids = _direct_post_ids(value, parent_key)
        accounts = _direct_author_accounts(value)
        if expected_post_id in post_ids and expected_account in accounts:
            return {
                "post_id": expected_post_id,
                "account": expected_account,
                "json_path": path[:500],
            }
        for key, child in value.items():
            result = _structured_post_identity(
                child,
                expected_account=expected_account,
                expected_post_id=expected_post_id,
                path=f"{path}.{str(key)[:80]}",
                parent_key=str(key),
            )
            if result:
                return result
    elif isinstance(value, list):
        for index, child in enumerate(value):
            result = _structured_post_identity(
                child,
                expected_account=expected_account,
                expected_post_id=expected_post_id,
                path=f"{path}[{index}]",
            )
            if result:
                return result
    return None


def _page_shell_reason(parser: _TikTokPostEvidenceParser) -> str:
    title = " ".join(parser.title_parts).strip().casefold()
    visible_text = " ".join(parser.visible_text_parts).casefold()
    if parser.shell_attributes:
        return "challenge_shell"
    if any(
        marker in title
        for marker in (
            "log in | tiktok",
            "login | tiktok",
            "security check",
            "verify to continue",
            "captcha",
            "page not available",
        )
    ):
        return "login_challenge_or_error_shell"
    if any(
        marker in visible_text
        for marker in (
            "video currently unavailable",
            "post is unavailable",
            "couldn't find this account",
            "page not available",
            "verify to continue",
            "complete the captcha",
            "log in to tiktok",
        )
    ):
        return "login_challenge_or_error_shell"
    return ""


class Profile7RemotePostVerifier:
    """Verify the new public photo twice through the existing Profile 7."""

    def __init__(self, state_path: str | Path = DEFAULT_BROWSER_STATE) -> None:
        self.state_path = Path(state_path).resolve()

    @staticmethod
    async def _visible(
        page: Any,
        *,
        response: Any,
        account: str,
        post_id: str,
    ) -> dict[str, Any]:
        expected_url = canonical_remote_photo_url(account, post_id)
        status = int(getattr(response, "status", 0) or 0) if response else 0
        proof: dict[str, Any] = {
            "visible": False,
            "http_status": status,
            "observed_url": str(getattr(page, "url", "") or ""),
            "expected_url": expected_url,
            "proof_type": "",
            "rejection_reason": "",
        }
        if response is None or status < 200 or status >= 400:
            proof["rejection_reason"] = "http_response_not_successful"
            return proof
        current = urlsplit(str(page.url))
        match = PHOTO_URL_PATTERN.fullmatch(current.path.rstrip("/"))
        if (
            current.scheme != "https"
            or str(current.hostname or "").casefold() != "www.tiktok.com"
            or current.username
            or current.password
            or not match
            or match.group("account").casefold() != account
            or match.group("post_id") != post_id
        ):
            proof["rejection_reason"] = "canonical_page_url_mismatch"
            return proof
        html = await page.content()
        parser = _TikTokPostEvidenceParser()
        try:
            parser.feed(html)
            parser.close()
        except Exception:
            proof["rejection_reason"] = "page_html_could_not_be_parsed"
            return proof
        shell_reason = _page_shell_reason(parser)
        if shell_reason:
            proof["rejection_reason"] = shell_reason
            return proof

        unique_og_urls = sorted(set(parser.og_urls))
        if unique_og_urls:
            proof["observed_og_urls"] = unique_og_urls[:5]
            if unique_og_urls == [expected_url]:
                proof.update(
                    visible=True,
                    proof_type="exact_canonical_og_url",
                    rejection_reason="",
                )
                return proof

        for script_label, raw_json in parser.structured_scripts:
            try:
                payload = json.loads(raw_json)
            except (TypeError, ValueError):
                continue
            identity = _structured_post_identity(
                payload,
                expected_account=account,
                expected_post_id=post_id,
            )
            if identity:
                proof.update(
                    visible=True,
                    proof_type="structured_author_post_identity",
                    structured_script=script_label,
                    structured_identity=identity,
                    rejection_reason="",
                )
                return proof
        proof["rejection_reason"] = "exact_post_identity_evidence_missing"
        return proof

    async def verify(
        self,
        remote_post_url: str,
        *,
        expected_account: str,
        expected_post_id: str,
    ) -> dict[str, Any]:
        account = _normalize_account(expected_account)
        expected_url = canonical_remote_photo_url(account, expected_post_id)
        if remote_post_url != expected_url:
            raise ShowcasePublishWorkerError(
                "remote verification target changed"
            )
        from playwright.async_api import async_playwright
        from social_browser import (
            load_engage_profile7_designation,
            load_verified_profile7_state,
            verified_profile_context,
        )

        designation = load_engage_profile7_designation(
            self.state_path.parent
        )
        state = load_verified_profile7_state(
            self.state_path.parent,
            designation,
        )
        cdp_url = str(state.get("cdp_url") or "")
        if not cdp_url:
            raise ShowcasePublishWorkerError(
                "verified Profile 7 connection disappeared"
            )
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(cdp_url)
            context, _ = await verified_profile_context(browser, designation)
            page = await context.new_page()
            try:
                first_response = await page.goto(
                    expected_url,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                first_check = await self._visible(
                    page,
                    response=first_response,
                    account=account,
                    post_id=expected_post_id,
                )
                if first_check.get("visible") is not True:
                    return {
                        "visible": False,
                        "persisted": False,
                        "verification": "profile7_public_post_twice",
                        "canonical_url": expected_url,
                        "expected_account": account,
                        "expected_post_id": expected_post_id,
                        "first_check": first_check,
                        "second_check": None,
                    }
                second_response = await page.reload(
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                second_check = await self._visible(
                    page,
                    response=second_response,
                    account=account,
                    post_id=expected_post_id,
                )
                return {
                    "visible": True,
                    "persisted": second_check.get("visible") is True,
                    "verification": "profile7_public_post_twice",
                    "canonical_url": expected_url,
                    "expected_account": account,
                    "expected_post_id": expected_post_id,
                    "first_check": first_check,
                    "second_check": second_check,
                }
            finally:
                if not page.is_closed():
                    await page.close()


def _safe_status_payload(
    *,
    publish_id: str,
    status: Any | None,
    verification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "tiktok-comment-showcase-api-receipt-v1",
        "transport": "official_tiktok_content_posting_api",
        "publish_id": str(publish_id or ""),
        "status": str(getattr(status, "status", "") or ""),
        "fail_reason": str(getattr(status, "fail_reason", "") or ""),
        "public_post_ids": list(
            getattr(status, "publicly_available_post_ids", ()) or ()
        ),
        "downloaded_bytes": getattr(status, "downloaded_bytes", None),
        "verification": dict(verification or {}),
    }


async def publish_authorized_showcase(
    *,
    backend: Any,
    showcase_id: str,
    execute: bool = False,
    access_token: str = "",
    preflight: Any | None = None,
    api_factory: Callable[[str, str], Any] | None = None,
    hosted_media_checker: Any | None = None,
    remote_verifier: Any | None = None,
    poll_timeout_seconds: float = 300.0,
    poll_interval_seconds: float = 2.0,
    poll_max_attempts: int = 150,
    verified_public_media_base_url: str = "",
) -> dict[str, Any]:
    """Dry-run or publish one exact, stored and authorized showcase."""

    prepared: PreparedShowcase = backend.prepare(str(showcase_id))
    prepared_media_url = require_url_under_verified_base(
        prepared.media_public_url,
        verified_public_media_base_url,
    )
    if not execute:
        return {
            "mode": "dry_run",
            "ready": True,
            "showcase_id": prepared.showcase_id,
            "posting_account": prepared.posting_account,
            "media_sha256": prepared.media_sha256,
            "media_public_url": prepared.media_public_url,
            "caption_hash": prepared.caption_hash,
            "authorization_hash": prepared.authorization_hash,
            "privacy_level": PUBLIC_PRIVACY_LEVEL,
        }
    if not access_token:
        raise ShowcasePublishWorkerError(
            "execute mode requires an in-memory TikTok access token"
        )
    if (
        preflight is None
        or api_factory is None
        or hosted_media_checker is None
        or remote_verifier is None
    ):
        raise ShowcasePublishWorkerError(
            "execute mode requires preflight, hosted-media verification, "
            "official API, and Profile 7 verification dependencies"
        )

    claim: ClaimedShowcase | None = None
    stage = "atomic_claim"
    submit_intent_started = False
    publish_id = ""
    final_status: Any | None = None
    try:
        claim = backend.claim(prepared)
        stage = "browser_preflight"
        current_preflight = (
            preflight(claim.posting_account)
            if callable(preflight)
            else preflight
        )
        preflight_result = await current_preflight.ensure_ready()
        observed = _normalize_account(
            (preflight_result or {}).get("observed_account")
        )
        if observed != claim.posting_account:
            raise ShowcasePublishWorkerError(
                "Profile 7 posting account changed after the atomic claim"
            )

        stage = "creator_info"
        api = api_factory(access_token, claim.posting_account)
        creator = api.query_creator_info()
        if str(creator.creator_username) != claim.posting_account:
            raise ShowcasePublishWorkerError(
                "Content Posting API creator does not match the authorized "
                "TikTok account"
            )

        stage = "hosted_media_check"
        hosted_media_checker.verify(
            prepared_media_url,
            expected_sha256=claim.media_sha256,
            expected_size_bytes=prepared.media_size_bytes,
        )

        def before_submit() -> None:
            nonlocal submit_intent_started, stage
            stage = "submit_intent"
            # Set first: if either attached-database hook reports an error,
            # reconciliation must still fail closed.
            submit_intent_started = True
            backend.mark_submit_intent(claim)
            stage = "photo_direct_post"

        stage = "fresh_creator_and_photo_init"
        initialization = api.initialize_photo_direct_post(
            photo_urls=[
                require_url_under_verified_base(
                    claim.media_public_url,
                    verified_public_media_base_url,
                )
            ],
            privacy_level=PUBLIC_PRIVACY_LEVEL,
            title="",
            caption=claim.caption_text,
            photo_cover_index=0,
            allow_comments=True,
            auto_add_music=False,
            brand_content=False,
            brand_organic=False,
            before_submit=before_submit,
        )
        publish_id = str(initialization.publish_id or "").strip()
        stage = "publish_id_checkpoint"
        backend.checkpoint_publish_id(claim, publish_id)
        stage = "photo_init_account_binding"
        if str(initialization.creator_username) != claim.posting_account:
            raise ShowcasePublishWorkerError(
                "photo initialization account binding changed"
            )

        stage = "status_poll"
        final_status = api.poll_publish_status(
            publish_id,
            timeout_seconds=poll_timeout_seconds,
            interval_seconds=poll_interval_seconds,
            max_attempts=poll_max_attempts,
        )
        public_ids = tuple(
            str(item)
            for item in (
                final_status.publicly_available_post_ids or ()
            )
        )
        if (
            str(final_status.status) != "PUBLISH_COMPLETE"
            or len(public_ids) != 1
        ):
            reason = (
                str(final_status.fail_reason or "")
                or "TikTok did not return one publicly available post ID"
            )
            response = _safe_status_payload(
                publish_id=publish_id,
                status=final_status,
            )
            outcome = backend.record_outcome(
                claim,
                outcome="uncertain",
                response=response,
                error=_safe_error(reason, access_token),
            )
            return {
                "mode": "execute",
                "showcase_id": claim.showcase_id,
                "status": str(outcome["status"]),
                "publish_id": publish_id,
                "reason": "pending_reconciliation",
            }

        stage = "publish_complete_media_revalidation"
        reported_downloaded_bytes = getattr(
            final_status,
            "downloaded_bytes",
            None,
        )
        media_delivery_proof: dict[str, Any] = {
            "approved_size_bytes": int(prepared.media_size_bytes),
            "tiktok_downloaded_bytes_supplied": (
                reported_downloaded_bytes is not None
            ),
            "tiktok_downloaded_bytes": reported_downloaded_bytes,
        }
        if reported_downloaded_bytes is not None:
            if isinstance(reported_downloaded_bytes, bool):
                raise ShowcasePublishWorkerError(
                    "TikTok returned an invalid downloaded byte count"
                )
            try:
                normalized_downloaded_bytes = int(reported_downloaded_bytes)
            except (TypeError, ValueError):
                raise ShowcasePublishWorkerError(
                    "TikTok returned an invalid downloaded byte count"
                ) from None
            if normalized_downloaded_bytes != int(prepared.media_size_bytes):
                raise ShowcasePublishWorkerError(
                    "TikTok downloaded byte count differs from the approved "
                    "publish media size"
                )
            media_delivery_proof["tiktok_downloaded_bytes"] = (
                normalized_downloaded_bytes
            )
        post_complete_media_url = require_url_under_verified_base(
            claim.media_public_url,
            verified_public_media_base_url,
        )
        if post_complete_media_url != prepared_media_url:
            raise ShowcasePublishWorkerError(
                "claimed publish media URL changed after preparation"
            )
        media_delivery_proof["post_complete_hosted_media"] = dict(
            hosted_media_checker.verify(
                post_complete_media_url,
                expected_sha256=claim.media_sha256,
                expected_size_bytes=prepared.media_size_bytes,
            )
            or {}
        )

        remote_post_id = public_ids[0]
        remote_post_url = canonical_remote_photo_url(
            claim.posting_account,
            remote_post_id,
        )
        stage = "profile7_remote_verification"
        verification = dict(await remote_verifier.verify(
            remote_post_url,
            expected_account=claim.posting_account,
            expected_post_id=remote_post_id,
        ))
        verification["media_delivery"] = media_delivery_proof
        visible = verification.get("visible") is True
        persisted = verification.get("persisted") is True
        response = _safe_status_payload(
            publish_id=publish_id,
            status=final_status,
            verification=verification,
        )
        if not (visible and persisted):
            outcome = backend.record_outcome(
                claim,
                outcome="uncertain",
                remote_post_id=remote_post_id,
                remote_post_url=remote_post_url,
                visible=visible,
                persisted=persisted,
                response=response,
                error="public post could not be verified twice in Profile 7",
            )
            return {
                "mode": "execute",
                "showcase_id": claim.showcase_id,
                "status": str(outcome["status"]),
                "publish_id": publish_id,
                "remote_post_id": remote_post_id,
                "remote_post_url": remote_post_url,
                "reason": "pending_reconciliation",
            }

        stage = "confirmed_outcome"
        outcome = backend.record_outcome(
            claim,
            outcome="published",
            remote_post_id=remote_post_id,
            remote_post_url=remote_post_url,
            visible=True,
            persisted=True,
            response=response,
            error="",
        )
        return {
            "mode": "execute",
            "showcase_id": claim.showcase_id,
            "status": str(outcome["status"]),
            "publish_id": publish_id,
            "remote_post_id": remote_post_id,
            "remote_post_url": remote_post_url,
        }
    except Exception as exc:
        safe = _safe_error(exc, access_token)
        if claim is None:
            raise ShowcasePublishWorkerError(
                f"{stage} failed: {safe}"
            ) from None
        outcome_name = (
            "uncertain" if submit_intent_started else "pre_submit_failed"
        )
        response = {
            **_safe_status_payload(
                publish_id=publish_id,
                status=final_status,
            ),
            "failed_stage": stage,
        }
        try:
            backend.record_outcome(
                claim,
                outcome=outcome_name,
                response=response,
                error=safe,
            )
        except Exception as outcome_error:
            safe_outcome = _safe_error(outcome_error, access_token)
            raise ShowcasePublishWorkerError(
                f"{stage} failed; the durable fence remains active because "
                f"outcome reconciliation also failed: {safe_outcome}"
            ) from None
        raise ShowcasePublishWorkerError(
            f"{stage} failed: {safe}"
        ) from None


async def reconcile_interrupted_showcase(
    *,
    backend: Any,
    showcase_id: str,
    execute: bool = False,
    release_reserved: bool = False,
    publish_id: str = "",
    access_token: str = "",
    preflight: Any | None = None,
    api_factory: Callable[[str, str], Any] | None = None,
    hosted_media_checker: Any | None = None,
    remote_verifier: Any | None = None,
    poll_timeout_seconds: float = 300.0,
    poll_interval_seconds: float = 2.0,
    poll_max_attempts: int = 150,
    verified_public_media_base_url: str = "",
) -> dict[str, Any]:
    """Release a pre-submit crash or verify an already submitted API post.

    This path never initializes a new TikTok post. A reserved attempt can only
    be released because durable submit intent is absent. Submit-intent and
    uncertain attempts remain fenced unless the existing TikTok publish ID is
    proven complete and its public post passes the normal exact checks.
    """

    active = backend.active_attempt(str(showcase_id))
    state = str(active["state"])
    prepared: PreparedShowcase = active["prepared"]
    claim: ClaimedShowcase = active["claim"]
    stored_response = dict(active.get("response") or {})
    checkpointed_publish_id = str(active.get("publish_id") or "").strip()
    legacy_response_publish_id = str(
        stored_response.get("publish_id") or ""
    ).strip()
    supplied_publish_id = str(publish_id or "").strip()
    if (
        checkpointed_publish_id
        and legacy_response_publish_id
        and checkpointed_publish_id != legacy_response_publish_id
    ):
        raise ShowcasePublishWorkerError(
            "checkpointed and outcome publish IDs do not match"
        )
    durable_publish_id = (
        checkpointed_publish_id or legacy_response_publish_id
    )
    if (
        durable_publish_id
        and supplied_publish_id
        and durable_publish_id != supplied_publish_id
    ):
        raise ShowcasePublishWorkerError(
            "operator publish ID differs from the durable checkpoint"
        )
    existing_publish_id = durable_publish_id or supplied_publish_id
    if not execute:
        return {
            "mode": "dry_run",
            "showcase_id": claim.showcase_id,
            "state": state,
            "attempt_id": claim.attempt_id,
            "publish_id": existing_publish_id,
            "allowed_action": (
                "release_reserved"
                if state == "reserved"
                else "verify_existing_publish"
            ),
        }

    if state == "reserved":
        if not release_reserved:
            raise ShowcasePublishWorkerError(
                "reserved recovery requires explicit --release-reserved"
            )
        outcome = backend.record_outcome(
            claim,
            outcome="pre_submit_failed",
            response={
                "schema_version": "tiktok-comment-showcase-recovery-v1",
                "recovery": "released_before_submit_intent",
            },
            error=(
                "operator released an interrupted reservation after verifying "
                "that durable submit intent was never recorded"
            ),
        )
        return {
            "mode": "execute",
            "showcase_id": claim.showcase_id,
            "status": str(outcome["status"]),
            "recovery": "released_before_submit_intent",
        }

    if release_reserved:
        raise ShowcasePublishWorkerError(
            "submit-intent or uncertain attempts cannot be released"
        )
    if state not in {"submit_intent", "uncertain"}:
        raise ShowcasePublishWorkerError(
            "only interrupted active attempts can be reconciled"
        )
    if not existing_publish_id:
        raise ShowcasePublishWorkerError(
            "post-submit reconciliation requires the existing TikTok publish ID"
        )
    if not access_token:
        raise ShowcasePublishWorkerError(
            "post-submit reconciliation requires an in-memory TikTok token"
        )
    if (
        preflight is None
        or api_factory is None
        or hosted_media_checker is None
        or remote_verifier is None
    ):
        raise ShowcasePublishWorkerError(
            "post-submit reconciliation requires account, media, API, and "
            "Profile 7 verification dependencies"
        )

    stage = "reconciliation_preflight"
    final_status: Any | None = None
    try:
        current_preflight = (
            preflight(claim.posting_account)
            if callable(preflight)
            else preflight
        )
        preflight_result = await current_preflight.ensure_ready()
        observed = _normalize_account(
            (preflight_result or {}).get("observed_account")
        )
        if observed != claim.posting_account:
            raise ShowcasePublishWorkerError(
                "Profile 7 account differs from the interrupted showcase"
            )
        stage = "reconciliation_creator_info"
        api = api_factory(access_token, claim.posting_account)
        creator = api.query_creator_info()
        if str(creator.creator_username) != claim.posting_account:
            raise ShowcasePublishWorkerError(
                "Content Posting API creator differs from the interrupted "
                "showcase account"
            )
        media_url = require_url_under_verified_base(
            claim.media_public_url,
            verified_public_media_base_url,
        )
        stage = "reconciliation_hosted_media"
        hosted_before = hosted_media_checker.verify(
            media_url,
            expected_sha256=claim.media_sha256,
            expected_size_bytes=prepared.media_size_bytes,
        )
        stage = "reconciliation_status_poll"
        final_status = api.poll_publish_status(
            existing_publish_id,
            timeout_seconds=poll_timeout_seconds,
            interval_seconds=poll_interval_seconds,
            max_attempts=poll_max_attempts,
        )
        public_ids = tuple(
            str(item)
            for item in (
                final_status.publicly_available_post_ids or ()
            )
        )
        if (
            str(final_status.status) != "PUBLISH_COMPLETE"
            or len(public_ids) != 1
        ):
            response = _safe_status_payload(
                publish_id=existing_publish_id,
                status=final_status,
            )
            outcome = backend.record_outcome(
                claim,
                outcome="uncertain",
                response=response,
                error=(
                    str(final_status.fail_reason or "")
                    or "TikTok has not proven exactly one completed public post"
                ),
            )
            return {
                "mode": "execute",
                "showcase_id": claim.showcase_id,
                "status": str(outcome["status"]),
                "publish_id": existing_publish_id,
                "reason": "pending_reconciliation",
            }

        downloaded = getattr(final_status, "downloaded_bytes", None)
        if downloaded is not None:
            if isinstance(downloaded, bool):
                raise ShowcasePublishWorkerError(
                    "TikTok returned an invalid downloaded byte count"
                )
            try:
                downloaded = int(downloaded)
            except (TypeError, ValueError):
                raise ShowcasePublishWorkerError(
                    "TikTok returned an invalid downloaded byte count"
                ) from None
            if downloaded != int(prepared.media_size_bytes):
                raise ShowcasePublishWorkerError(
                    "TikTok downloaded byte count differs from approved media"
                )
        hosted_after = hosted_media_checker.verify(
            media_url,
            expected_sha256=claim.media_sha256,
            expected_size_bytes=prepared.media_size_bytes,
        )
        remote_post_id = public_ids[0]
        remote_post_url = canonical_remote_photo_url(
            claim.posting_account,
            remote_post_id,
        )
        stage = "reconciliation_profile7_verification"
        verification = dict(
            await remote_verifier.verify(
                remote_post_url,
                expected_account=claim.posting_account,
                expected_post_id=remote_post_id,
            )
        )
        verification["media_delivery"] = {
            "approved_size_bytes": int(prepared.media_size_bytes),
            "tiktok_downloaded_bytes": downloaded,
            "hosted_before": dict(hosted_before or {}),
            "hosted_after": dict(hosted_after or {}),
        }
        response = _safe_status_payload(
            publish_id=existing_publish_id,
            status=final_status,
            verification=verification,
        )
        visible = verification.get("visible") is True
        persisted = verification.get("persisted") is True
        if not (visible and persisted):
            outcome = backend.record_outcome(
                claim,
                outcome="uncertain",
                remote_post_id=remote_post_id,
                remote_post_url=remote_post_url,
                visible=visible,
                persisted=persisted,
                response=response,
                error="existing public post was not verified twice in Profile 7",
            )
            return {
                "mode": "execute",
                "showcase_id": claim.showcase_id,
                "status": str(outcome["status"]),
                "publish_id": existing_publish_id,
                "reason": "pending_reconciliation",
            }
        outcome = backend.record_outcome(
            claim,
            outcome="published",
            remote_post_id=remote_post_id,
            remote_post_url=remote_post_url,
            visible=True,
            persisted=True,
            response=response,
            error="",
        )
        return {
            "mode": "execute",
            "showcase_id": claim.showcase_id,
            "status": str(outcome["status"]),
            "publish_id": existing_publish_id,
            "remote_post_id": remote_post_id,
            "remote_post_url": remote_post_url,
        }
    except Exception as exc:
        safe = _safe_error(exc, access_token)
        response = {
            **_safe_status_payload(
                publish_id=existing_publish_id,
                status=final_status,
            ),
            "failed_stage": stage,
        }
        try:
            backend.record_outcome(
                claim,
                outcome="uncertain",
                response=response,
                error=safe,
            )
        except Exception as outcome_error:
            raise ShowcasePublishWorkerError(
                f"{stage} failed and the durable uncertain fence could not "
                f"be refreshed: {_safe_error(outcome_error, access_token)}"
            ) from None
        raise ShowcasePublishWorkerError(
            f"{stage} failed: {safe}"
        ) from None


async def publish_showcase_batch(
    *,
    backend: Any,
    showcase_ids: list[str],
    execute: bool,
    access_token: str = "",
    preflight: Any | None = None,
    api_factory: Callable[[str, str], Any] | None = None,
    hosted_media_checker: Any | None = None,
    remote_verifier: Any | None = None,
    poll_timeout_seconds: float = 300.0,
    poll_interval_seconds: float = 2.0,
    poll_max_attempts: int = 150,
    verified_public_media_base_url: str,
    cadence_seconds: float = 0.0,
    max_items: int = 0,
    continue_on_error: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Process an arbitrary authorized set sequentially, one claim at a time."""

    if max_items < 0:
        raise ShowcasePublishWorkerError("max_items cannot be negative")
    if cadence_seconds < 0:
        raise ShowcasePublishWorkerError(
            "cadence_seconds cannot be negative"
        )
    selected = list(showcase_ids)
    if max_items > 0:
        selected = selected[:max_items]
    results: list[dict[str, Any]] = []
    stopped = False
    for index, item_id in enumerate(selected):
        try:
            item_result = await publish_authorized_showcase(
                backend=backend,
                showcase_id=item_id,
                execute=execute,
                access_token=access_token,
                preflight=preflight,
                api_factory=api_factory,
                hosted_media_checker=hosted_media_checker,
                remote_verifier=remote_verifier,
                poll_timeout_seconds=poll_timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
                poll_max_attempts=poll_max_attempts,
                verified_public_media_base_url=(
                    verified_public_media_base_url
                ),
            )
            results.append(item_result)
            successful = (
                not execute
                or str(item_result.get("status") or "") == "published"
            )
            if not successful and not continue_on_error:
                stopped = True
                break
        except Exception as exc:
            results.append(
                {
                    "showcase_id": str(item_id),
                    "status": "failed",
                    "error": _safe_error(exc, access_token),
                }
            )
            if not continue_on_error:
                stopped = True
                break
        if (
            execute
            and cadence_seconds > 0
            and index + 1 < len(selected)
        ):
            await asyncio.to_thread(sleep, cadence_seconds)
    return {
        "mode": "execute" if execute else "dry_run",
        "requested": len(showcase_ids),
        "selected": len(selected),
        "processed": len(results),
        "published": sum(
            1 for item in results if item.get("status") == "published"
        ),
        "uncertain": sum(
            1 for item in results if item.get("status") == "uncertain"
        ),
        "failed": sum(
            1 for item in results if item.get("status") == "failed"
        ),
        "stopped_early": stopped,
        "results": results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare or guardedly publish a TikTok comment showcase. "
            "Publishing defaults to dry-run."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser(
        "prepare",
        help="stage and bind the API-ready JPEG before AI drafting",
    )
    prepare.add_argument("--database", required=True)
    prepare.add_argument("--publication-id", required=True)
    prepare.add_argument("--receipt-id", required=True)
    prepare.add_argument("--capture-root", required=True)
    prepare.add_argument("--public-directory", required=True)
    prepare.add_argument("--public-base-url", required=True)
    prepare.add_argument("--ffmpeg", required=True)

    publish = subparsers.add_parser(
        "publish",
        help="dry-run or execute one stored, authorized showcase",
    )
    publish.add_argument("--database", required=True)
    publish.add_argument("--master-database", default=DEFAULT_MASTER_DATABASE)
    selection = publish.add_mutually_exclusive_group(required=True)
    selection.add_argument("--showcase-id")
    selection.add_argument(
        "--all-authorized",
        action="store_true",
        help="process every authorized/retryable showcase sequentially",
    )
    publish.add_argument(
        "--run-id",
        help="optional run scope for --all-authorized",
    )
    publish.add_argument(
        "--public-media-base-url",
        required=True,
        help="operator-verified HTTPS prefix hosting the staged JPEGs",
    )
    publish.add_argument("--execute", action="store_true")
    publish.add_argument(
        "--token-env",
        help="name of the environment variable containing the token",
    )
    publish.add_argument(
        "--social-browser-state",
        default=DEFAULT_BROWSER_STATE,
    )
    publish.add_argument("--browser-startup-timeout", type=float, default=45.0)
    publish.add_argument("--poll-timeout", type=float, default=300.0)
    publish.add_argument("--poll-interval", type=float, default=2.0)
    publish.add_argument("--poll-max-attempts", type=int, default=150)
    publish.add_argument(
        "--max-items",
        type=int,
        default=0,
        help="explicit optional cap; 0 means no cap",
    )
    publish.add_argument(
        "--cadence-seconds",
        type=float,
        default=0.0,
        help="explicit delay after each confirmed post",
    )
    publish.add_argument("--continue-on-error", action="store_true")

    reconcile = subparsers.add_parser(
        "reconcile",
        help=(
            "inspect/release an interrupted pre-submit reservation or verify "
            "an existing post-submit TikTok publish"
        ),
    )
    reconcile.add_argument("--database", required=True)
    reconcile.add_argument(
        "--master-database",
        default=DEFAULT_MASTER_DATABASE,
    )
    reconcile.add_argument("--showcase-id", required=True)
    reconcile.add_argument("--execute", action="store_true")
    reconcile.add_argument(
        "--release-reserved",
        action="store_true",
        help=(
            "release only a reserved attempt that has no durable submit intent"
        ),
    )
    reconcile.add_argument(
        "--publish-id",
        default="",
        help=(
            "existing TikTok publish ID when it was not stored before a crash"
        ),
    )
    reconcile.add_argument("--token-env")
    reconcile.add_argument(
        "--public-media-base-url",
        default="",
        help=(
            "operator-verified media prefix; required for post-submit "
            "reconciliation"
        ),
    )
    reconcile.add_argument(
        "--social-browser-state",
        default=DEFAULT_BROWSER_STATE,
    )
    reconcile.add_argument(
        "--browser-startup-timeout",
        type=float,
        default=45.0,
    )
    reconcile.add_argument("--poll-timeout", type=float, default=300.0)
    reconcile.add_argument("--poll-interval", type=float, default=2.0)
    reconcile.add_argument("--poll-max-attempts", type=int, default=150)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    token_value = ""
    backend: SQLiteShowcaseBackend | None = None
    session: Any | None = None

    def configure_live_dependencies() -> tuple[Any, Any, Any]:
        nonlocal token_value, session
        token_value = load_access_token(
            token_env=str(args.token_env or ""),
        )
        import requests
        from engage_tiktok import SocialBrowserPreflight

        session = requests.Session()
        hosted = HostedMediaChecker(session=session)

        def api_factory(
            access_token: str,
            expected_account: str,
        ) -> TikTokContentPostingClient:
            return TikTokContentPostingClient(
                session=session,
                access_token=access_token,
                expected_creator_username=expected_account,
            )

        def preflight(expected_account: str) -> Any:
            return SocialBrowserPreflight(
                state_path=Path(args.social_browser_state),
                expected_account=expected_account,
                startup_timeout=float(args.browser_startup_timeout),
            )

        return (
            preflight,
            api_factory,
            hosted,
        )

    try:
        if args.command == "prepare":
            result = prepare_comment_showcase(
                database=args.database,
                publication_id=args.publication_id,
                receipt_id=args.receipt_id,
                capture_root=args.capture_root,
                public_directory=args.public_directory,
                public_base_url=args.public_base_url,
                ffmpeg_path=args.ffmpeg,
            )
        elif args.command == "publish":
            if args.run_id and not args.all_authorized:
                raise ShowcasePublishWorkerError(
                    "--run-id requires --all-authorized"
                )
            backend = SQLiteShowcaseBackend(
                database=args.database,
                master_database=args.master_database,
            )
            if args.execute:
                (
                    preflight,
                    api_factory,
                    hosted_media_checker,
                ) = configure_live_dependencies()
                verifier = Profile7RemotePostVerifier(
                    args.social_browser_state
                )
            else:
                api_factory = None
                preflight = None
                hosted_media_checker = None
                verifier = None
            if args.all_authorized:
                showcase_ids = backend.list_authorized(
                    run_id=str(args.run_id or "")
                )
            else:
                showcase_ids = [str(args.showcase_id)]
            result = asyncio.run(
                publish_showcase_batch(
                    backend=backend,
                    showcase_ids=showcase_ids,
                    execute=bool(args.execute),
                    access_token=token_value,
                    preflight=preflight,
                    api_factory=api_factory,
                    hosted_media_checker=hosted_media_checker,
                    remote_verifier=verifier,
                    poll_timeout_seconds=float(args.poll_timeout),
                    poll_interval_seconds=float(args.poll_interval),
                    poll_max_attempts=int(args.poll_max_attempts),
                    verified_public_media_base_url=str(
                        args.public_media_base_url
                    ),
                    cadence_seconds=float(args.cadence_seconds),
                    max_items=int(args.max_items),
                    continue_on_error=bool(args.continue_on_error),
                )
            )
        else:
            backend = SQLiteShowcaseBackend(
                database=args.database,
                master_database=args.master_database,
            )
            if args.execute and not args.release_reserved:
                (
                    preflight,
                    api_factory,
                    hosted_media_checker,
                ) = configure_live_dependencies()
                verifier = Profile7RemotePostVerifier(
                    args.social_browser_state
                )
            else:
                preflight = None
                api_factory = None
                hosted_media_checker = None
                verifier = None
            result = asyncio.run(
                reconcile_interrupted_showcase(
                    backend=backend,
                    showcase_id=str(args.showcase_id),
                    execute=bool(args.execute),
                    release_reserved=bool(args.release_reserved),
                    publish_id=str(args.publish_id or ""),
                    access_token=token_value,
                    preflight=preflight,
                    api_factory=api_factory,
                    hosted_media_checker=hosted_media_checker,
                    remote_verifier=verifier,
                    poll_timeout_seconds=float(args.poll_timeout),
                    poll_interval_seconds=float(args.poll_interval),
                    poll_max_attempts=int(args.poll_max_attempts),
                    verified_public_media_base_url=str(
                        args.public_media_base_url or ""
                    ),
                )
            )
        print(_canonical_json(result))
        return 0
    except Exception as exc:
        print(
            _canonical_json(
                {
                    "ok": False,
                    "error": _safe_error(exc, token_value),
                }
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        if session is not None:
            session.close()
        if backend is not None:
            backend.close()
        token_value = ""


if __name__ == "__main__":
    raise SystemExit(main())
