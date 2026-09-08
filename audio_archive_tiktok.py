#!/usr/bin/env python
"""Standalone AUDIO ARCHIVE workflow for locally collected TikTok posts.

The runner reads evidence-ready rows from any compatible project database in
query-only mode, freezes their canonical TikTok URLs, and stores a normalized
M4A plus a locally derived MP3.  It does not require a master-registry match,
the project's original filesystem path, a terminal source-run status, or a
separate authorization/rights/retention ceremony.  The user's request to run
AUDIO ARCHIVE is sufficient authority to execute the requested local scope.

Source workflow data remains read-only, output stays isolated, and this tool
does not discover posts, analyze sound, engage, or publish.  Status and
validation are offline and never touch TikTok or the browser.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import hashlib
import json
import math
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


DEFAULT_MASTER_DATABASE = (
    Path("comments_data") / "tiktok_master" / "state" / "tiktok_master.sqlite"
)
DEFAULT_OUTPUT_ROOT = Path("comments_data") / "audio_archive_runs"
WORKSPACE_ROOT = Path(__file__).resolve().parent
CANONICAL_OUTPUT_ROOT = (WORKSPACE_ROOT / DEFAULT_OUTPUT_ROOT).resolve()
CANONICAL_MASTER_DATABASE = (WORKSPACE_ROOT / DEFAULT_MASTER_DATABASE).resolve()
MANIFEST_SCHEMA = "tiktok-audio-archive-run-v2"
STATE_SCHEMA = "tiktok-audio-archive-state-v1"
RECORD_SCHEMA = "tiktok-audio-archive-record-v1"
REVIEW_SCHEMA = "tiktok-audio-archive-review-v1"
PURGE_SCHEMA = "tiktok-audio-archive-purge-v1"
ARCHIVE_BATCH_SIZE = 60
MAX_FINAL_AUDIO_BYTES = 16 * 1024 * 1024
RUN_LOCK_FILE = "run.lock"
RUN_ROOT_ALLOWED_NAMES = frozenset(
    {
        RUN_LOCK_FILE,
        "manifest.json",
        "state.json",
        "records",
        "audio",
        ".staging",
        "review.json",
        "review.md",
        "purge.json",
    }
)
RUN_ID_PATTERN = re.compile(r"^audio_archive_[a-z0-9_]{20,150}$")
POST_ID_PATTERN = re.compile(r"^[0-9]{5,32}$")
SAFE_ERROR_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
STORAGE_MODES = frozenset({"archive", "permanent", "public-research"})


class AudioArchiveError(RuntimeError):
    """Fail-closed AUDIO ARCHIVE workflow error."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _json_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _bind_hash(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    body = dict(value)
    body.pop(field, None)
    body[field] = _json_hash(body)
    return body


def _verify_hash(value: Mapping[str, Any], field: str) -> bool:
    supplied = str(value.get(field) or "").strip().casefold()
    if re.fullmatch(r"[0-9a-f]{64}", supplied) is None:
        return False
    body = dict(value)
    body.pop(field, None)
    return _json_hash(body) == supplied


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _timestamp(value: dt.datetime | None = None) -> str:
    current = value or _now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    return current.astimezone(dt.timezone.utc).isoformat(timespec="microseconds")


def _parse_timestamp(value: Any, *, field: str) -> dt.datetime:
    raw = str(value or "").strip()
    if not raw:
        raise AudioArchiveError(f"{field}_missing")
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AudioArchiveError(f"{field}_invalid") from exc
    if parsed.tzinfo is None:
        raise AudioArchiveError(f"{field}_timezone_missing")
    return parsed.astimezone(dt.timezone.utc)


def _safe_error(value: Any) -> str:
    raw = str(value or "").strip()
    candidate = raw.casefold().replace("-", "_").replace(" ", "_")
    candidate = re.sub(r"[^a-z0-9_]", "_", candidate)
    candidate = re.sub(r"_+", "_", candidate).strip("_")
    if candidate and SAFE_ERROR_PATTERN.fullmatch(candidate):
        return candidate
    return "audio_archive_error"


def _normalized_handle(value: Any) -> str:
    handle = str(value or "").strip().lstrip("@").casefold()
    if not handle:
        return ""
    if re.fullmatch(r"[a-z0-9._]{1,64}", handle) is None:
        raise AudioArchiveError("expected_account_invalid")
    return handle


def _safe_slug(value: Any, *, maximum: int = 42) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_")
    slug = slug or "source_run"
    if len(slug) <= maximum:
        return slug
    digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:8]
    return f"{slug[: maximum - 9].rstrip('_')}_{digest}"


def _post_id(value: str) -> str:
    normalized = str(value or "").strip()
    if POST_ID_PATTERN.fullmatch(normalized) is None:
        raise argparse.ArgumentTypeError("invalid TikTok post ID")
    return normalized


def _run_id(value: str) -> str:
    normalized = str(value or "").strip()
    if RUN_ID_PATTERN.fullmatch(normalized) is None:
        raise argparse.ArgumentTypeError("invalid AUDIO ARCHIVE run ID")
    return normalized


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Persist normalized M4A and MP3 audio from evidence-ready posts "
            "in any local TikTok project run."
        )
    )
    parser.add_argument(
        "--master-database",
        default=os.fspath(DEFAULT_MASTER_DATABASE),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--output-root",
        default=os.fspath(DEFAULT_OUTPUT_ROOT),
        help="Dedicated AUDIO ARCHIVE runtime root.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="Freeze and execute a new archive run.")
    run.add_argument("--source-database", required=True)
    run.add_argument("--source-run-id", required=True)
    scope = run.add_mutually_exclusive_group(required=False)
    scope.add_argument(
        "--all-evidence-ready",
        action="store_true",
        help="Use every evidence-ready post in the exact source run.",
    )
    scope.add_argument(
        "--post-id",
        action="append",
        type=_post_id,
        help="Exact source-run post ID; repeat for an exact subset.",
    )
    # The following legacy options remain accepted so old command snippets do
    # not break.  They no longer gate run creation or change archive scope.
    run.add_argument(
        "--storage-mode",
        default="archive",
        help=argparse.SUPPRESS,
    )
    run.add_argument(
        "--rights-basis",
        default="",
        help=argparse.SUPPRESS,
    )
    run.add_argument("--retention-days", default=None, help=argparse.SUPPRESS)
    run.add_argument("--authorized-by", default="", help=argparse.SUPPRESS)
    run.add_argument(
        "--authorize-audio-storage",
        nargs="?",
        const=True,
        default=False,
        help=argparse.SUPPRESS,
    )
    run.add_argument("--expected-account", default="")

    resume = commands.add_parser("resume", help="Resume the same frozen run.")
    resume.add_argument("--run-id", required=True, type=_run_id)
    resume.add_argument("--expected-account", default="")

    status = commands.add_parser("status", help="Inspect local state offline.")
    status.add_argument("--run-id", required=True, type=_run_id)

    validate = commands.add_parser("validate", help="Verify local artifacts offline.")
    validate.add_argument("--run-id", required=True, type=_run_id)

    return parser


def _validate_creation_authorization(args: argparse.Namespace) -> dict[str, Any]:
    """Return fixed request metadata; all legacy gate options are inert."""

    return {
        "authorization_kind": "user_requested_audio_archive",
    }


def _assert_regular_directory(path: Path, *, create: bool = False) -> Path:
    candidate = Path(os.path.abspath(os.fspath(Path(path).expanduser())))
    if create and not candidate.exists():
        missing: list[Path] = []
        cursor = candidate
        while not cursor.exists():
            missing.append(cursor)
            parent = cursor.parent
            if parent == cursor:
                raise AudioArchiveError("archive_directory_invalid")
            cursor = parent
        _assert_regular_directory(cursor)
        for directory in reversed(missing):
            try:
                directory.mkdir()
            except OSError as exc:
                raise AudioArchiveError("archive_directory_invalid") from exc
            if _is_reparse_point(directory):
                raise AudioArchiveError("archive_directory_invalid")
    if not candidate.is_dir() or _is_reparse_point(candidate):
        raise AudioArchiveError("archive_directory_invalid")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AudioArchiveError("archive_directory_invalid") from exc
    if os.path.normcase(os.fspath(resolved)) != os.path.normcase(
        os.fspath(candidate)
    ):
        raise AudioArchiveError("archive_directory_resolves_elsewhere")
    return candidate


def _canonical_output_root(value: str | Path, *, create: bool = False) -> Path:
    supplied = Path(value).expanduser()
    if not supplied.is_absolute():
        supplied = WORKSPACE_ROOT / supplied
    return _assert_regular_directory(supplied, create=create)


def _canonical_master_database(value: str | Path) -> Path:
    supplied = Path(value).expanduser()
    if not supplied.is_absolute():
        if supplied != DEFAULT_MASTER_DATABASE:
            raise AudioArchiveError("master_database_must_be_canonical")
        supplied = WORKSPACE_ROOT / supplied
    absolute = Path(os.path.abspath(os.fspath(supplied)))
    for component in (absolute, *absolute.parents):
        try:
            details = component.stat(follow_symlinks=False)
        except OSError as exc:
            raise AudioArchiveError("master_database_missing") from exc
        if component.is_symlink() or int(
            getattr(details, "st_file_attributes", 0)
        ) & 0x400:
            raise AudioArchiveError("master_database_must_be_canonical")
    try:
        resolved = absolute.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AudioArchiveError("master_database_missing") from exc
    if os.path.normcase(os.fspath(resolved)) != os.path.normcase(
        os.fspath(CANONICAL_MASTER_DATABASE)
    ):
        raise AudioArchiveError("master_database_must_be_canonical")
    return resolved


def _validate_supplied_master_database(
    args: argparse.Namespace,
    manifest: Mapping[str, Any],
) -> None:
    # Retained as a compatibility hook for older callers. AUDIO ARCHIVE v2
    # reads only the project database and does not require master identity.
    return None


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = int(getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0))
    except OSError:
        return True
    return bool(attributes & 0x400)


@contextlib.contextmanager
def _run_lock(run_dir: Path):
    """Hold one non-blocking OS lock for every operation on a run."""

    directory = _assert_regular_directory(run_dir)
    lock_path = directory / RUN_LOCK_FILE
    if not lock_path.is_file() or _is_reparse_point(lock_path):
        raise AudioArchiveError("run_lock_missing_or_unsafe")
    try:
        handle = lock_path.open("r+b", buffering=0)
    except OSError as exc:
        raise AudioArchiveError("run_lock_unavailable") from exc
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise AudioArchiveError("audio_archive_run_locked") from exc
        else:  # pragma: no cover - the production workstation is Windows
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise AudioArchiveError("audio_archive_run_locked") from exc
        locked = True
        yield
    finally:
        if locked:
            if os.name == "nt":
                import msvcrt

                with contextlib.suppress(OSError):
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - the production workstation is Windows
                import fcntl

                with contextlib.suppress(OSError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _atomic_write_json(
    path: Path,
    value: Mapping[str, Any],
    *,
    no_clobber: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if _is_reparse_point(path.parent):
        raise AudioArchiveError("artifact_parent_is_reparse_point")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        flags = "x" if no_clobber else "w"
        with temporary.open(flags, encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if no_clobber:
            os.link(
                os.fspath(temporary),
                os.fspath(path),
                follow_symlinks=False,
            )
            temporary.unlink()
        else:
            os.replace(temporary, path)
    except Exception:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def _atomic_write_text(path: Path, value: str, *, no_clobber: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if _is_reparse_point(path.parent):
        raise AudioArchiveError("artifact_parent_is_reparse_point")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            if not value.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if no_clobber:
            os.link(
                os.fspath(temporary),
                os.fspath(path),
                follow_symlinks=False,
            )
            temporary.unlink()
        else:
            os.replace(temporary, path)
    except Exception:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def _read_json(path: Path, *, name: str) -> dict[str, Any]:
    if not path.is_file() or _is_reparse_point(path):
        raise AudioArchiveError(f"{name}_missing")
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise AudioArchiveError(f"{name}_invalid") from exc
    if not isinstance(value, dict):
        raise AudioArchiveError(f"{name}_invalid")
    return value


def _file_hash(path: Path, *, maximum: int = MAX_FINAL_AUDIO_BYTES) -> tuple[int, str]:
    if not path.is_file() or _is_reparse_point(path):
        raise AudioArchiveError("audio_file_missing_or_unsafe")
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    raise AudioArchiveError("audio_file_size_limit_exceeded")
                digest.update(chunk)
    except OSError as exc:
        raise AudioArchiveError("audio_file_unreadable") from exc
    if total < 1:
        raise AudioArchiveError("audio_file_empty")
    return total, digest.hexdigest()


def _run_directory(output_root: str | Path, run_id: str) -> Path:
    if RUN_ID_PATTERN.fullmatch(str(run_id or "")) is None:
        raise AudioArchiveError("run_id_invalid")
    root = _canonical_output_root(output_root)
    candidate = Path(os.path.abspath(os.fspath(root / run_id)))
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise AudioArchiveError("run_directory_escaped_output_root") from exc
    return candidate


def _validate_run_layout(run_dir: Path) -> None:
    directory = _assert_regular_directory(run_dir)
    for child in directory.iterdir():
        if child.name not in RUN_ROOT_ALLOWED_NAMES:
            raise AudioArchiveError("unexpected_run_root_artifact")
        if _is_reparse_point(child):
            if child.name == "audio":
                raise AudioArchiveError("audio_directory_invalid")
            if child.name == "records":
                raise AudioArchiveError("records_directory_invalid")
            if child.name == ".staging":
                raise AudioArchiveError("staging_directory_invalid")
            raise AudioArchiveError("unsafe_run_root_artifact")
    for name in ("records", "audio", ".staging"):
        _assert_regular_directory(directory / name)
    lock_path = directory / RUN_LOCK_FILE
    if not lock_path.is_file() or _is_reparse_point(lock_path):
        raise AudioArchiveError("run_lock_missing_or_unsafe")


def _new_run_id(
    *,
    source_project: str,
    source_run_id: str,
    storage_mode: str,
    selection_count: int,
    created_at: str,
) -> str:
    when = _parse_timestamp(created_at, field="created_at")
    descriptor = _safe_slug(source_project or source_run_id)
    mode = {
        "permanent": "permanent",
        "public-research": "research",
    }.get(storage_mode, "stored")
    stamp = when.strftime("%Y%m%d_%H%M%S_%f")
    scope = f"{selection_count}p"
    digest = hashlib.sha256(
        f"{source_run_id}\x1f{storage_mode}\x1f{selection_count}\x1f{created_at}".encode(
            "utf-8"
        )
    ).hexdigest()[:8]
    value = f"audio_archive_{mode}_{descriptor}_{scope}_{stamp}_{digest}"
    if len(value) > 150:
        descriptor = _safe_slug(descriptor, maximum=24)
        value = f"audio_archive_{mode}_{descriptor}_{scope}_{stamp}_{digest}"
    if RUN_ID_PATTERN.fullmatch(value) is None:
        raise AudioArchiveError("generated_run_id_invalid")
    return value


def _candidate_payload(
    candidate: Mapping[str, Any],
    *,
    archive_ordinal: int,
) -> dict[str, Any]:
    post_id = str(candidate.get("post_id") or "").strip()
    if POST_ID_PATTERN.fullmatch(post_id) is None:
        raise AudioArchiveError("lineage_post_id_invalid")
    creator = str(candidate.get("creator_handle") or "").strip().lstrip("@").casefold()
    if re.fullmatch(r"[a-z0-9._]{1,64}", creator) is None:
        raise AudioArchiveError("lineage_creator_invalid")
    content_type = str(candidate.get("content_type") or "").strip().casefold()
    if content_type not in {"video", "photo"}:
        raise AudioArchiveError("lineage_content_type_invalid")
    source_ordinal = int(candidate.get("ordinal") or candidate.get("position") or 0)
    if source_ordinal < 1:
        raise AudioArchiveError("lineage_ordinal_invalid")
    value = {
        "archive_ordinal": archive_ordinal,
        "source_ordinal": source_ordinal,
        "post_id": post_id,
        "canonical_url": str(candidate.get("canonical_url") or ""),
        "creator_handle": creator,
        "content_type": content_type,
        "snapshot_id": str(candidate.get("snapshot_id") or ""),
        "evidence_hash": str(candidate.get("evidence_hash") or "").casefold(),
        "transport_batch": ((archive_ordinal - 1) // ARCHIVE_BATCH_SIZE) + 1,
        "audio_files": {
            "m4a": f"{archive_ordinal:04d}_{post_id}.m4a",
            "mp3": f"{archive_ordinal:04d}_{post_id}.mp3",
        },
        "record_file": f"{archive_ordinal:04d}_{post_id}.json",
    }
    if not value["snapshot_id"] or re.fullmatch(r"[0-9a-f]{64}", value["evidence_hash"]) is None:
        raise AudioArchiveError("lineage_snapshot_binding_invalid")
    return _bind_hash(value, "candidate_hash")


def _select_candidates(
    lineage: Mapping[str, Any],
    post_ids: Sequence[str] | None,
) -> list[dict[str, Any]]:
    raw = lineage.get("candidates")
    if not isinstance(raw, list) or not raw:
        raise AudioArchiveError("source_run_has_no_evidence_ready_posts")
    by_id: dict[str, Mapping[str, Any]] = {}
    for candidate in raw:
        if not isinstance(candidate, Mapping):
            raise AudioArchiveError("lineage_candidate_invalid")
        post_id = str(candidate.get("post_id") or "")
        if post_id in by_id:
            raise AudioArchiveError("lineage_candidate_duplicate")
        by_id[post_id] = candidate
    requested = list(post_ids or ())
    if requested:
        if len(requested) != len(set(requested)):
            raise AudioArchiveError("requested_post_id_duplicate")
        unknown = [post_id for post_id in requested if post_id not in by_id]
        if unknown:
            raise AudioArchiveError("requested_post_outside_source_run")
        selected_source = [by_id[post_id] for post_id in requested]
    else:
        selected_source = list(raw)
    return [
        _candidate_payload(candidate, archive_ordinal=index)
        for index, candidate in enumerate(selected_source, start=1)
    ]


def _build_manifest(
    *,
    run_id: str,
    lineage: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    authorization: Mapping[str, Any],
    storage_mode: str,
    retention_days: int | None,
    expected_account: str,
    created_at: str,
) -> dict[str, Any]:
    source_run = lineage.get("source_run")
    if not isinstance(source_run, Mapping):
        raise AudioArchiveError("lineage_source_run_invalid")
    expires_at = ""
    if storage_mode == "public-research" and retention_days:
        expires_at = _timestamp(
            _parse_timestamp(created_at, field="created_at")
            + dt.timedelta(days=int(retention_days or 0))
        )
    frozen_candidates = [dict(value) for value in candidates]
    value = {
        "schema_version": MANIFEST_SCHEMA,
        "run_id": run_id,
        "source": {
            "source_database": str(source_run.get("database_path") or ""),
            "master_database": str(source_run.get("master_database_path") or ""),
            "source_id": str(source_run.get("source_id") or ""),
            "master_run_id": str(source_run.get("master_run_id") or ""),
            "local_run_id": str(source_run.get("run_id") or ""),
            "project": str(source_run.get("project") or ""),
            "workflow": str(source_run.get("workflow") or ""),
            "status_at_freeze": str(source_run.get("status") or ""),
            "source_mode": str(source_run.get("source_mode") or ""),
            "collection_policy": str(source_run.get("collection_policy") or ""),
            "source_requested_count": int(source_run.get("requested_count") or 0),
            "source_evidence_ready": int(source_run.get("evidence_ready") or 0),
            "lineage_hash": str(lineage.get("lineage_hash") or ""),
            "source_skipped": list(lineage.get("skipped") or []),
        },
        "selected_count": len(frozen_candidates),
        "selection": frozen_candidates,
        "selection_hash": _json_hash(frozen_candidates),
        "storage": {
            "mode": storage_mode,
            "retention_days": retention_days,
            "expires_at": expires_at,
            "formats": {
                "m4a": {
                    "mime_type": "audio/mp4",
                    "codec": "aac-lc",
                    "target_bitrate_kbps": 192,
                    "sample_rate_hz": 44100,
                    "channels": 2,
                    "role": "normalized_primary",
                },
                "mp3": {
                    "mime_type": "audio/mpeg",
                    "codec": "mp3",
                    "target_bitrate_kbps": 192,
                    "sample_rate_hz": 44100,
                    "channels": 2,
                    "role": "derived_from_normalized_m4a",
                },
            },
            "single_tiktok_acquisition_per_post": True,
            "source_video_persisted": False,
            "signed_url_persisted": False,
        },
        "authorization": dict(authorization),
        "expected_account": expected_account,
        "created_at": created_at,
        "ai_actions": [],
        "outbound_actions": [],
    }
    return _bind_hash(value, "manifest_hash")


def _validate_manifest(manifest: Mapping[str, Any]) -> None:
    if manifest.get("schema_version") != MANIFEST_SCHEMA or not _verify_hash(
        manifest, "manifest_hash"
    ):
        raise AudioArchiveError("manifest_hash_invalid")
    run_id = str(manifest.get("run_id") or "")
    if RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise AudioArchiveError("manifest_run_id_invalid")
    selection = manifest.get("selection")
    if not isinstance(selection, list) or not selection:
        raise AudioArchiveError("manifest_selection_invalid")
    if int(manifest.get("selected_count") or 0) != len(selection):
        raise AudioArchiveError("manifest_selection_count_mismatch")
    if manifest.get("selection_hash") != _json_hash(selection):
        raise AudioArchiveError("manifest_selection_hash_invalid")
    seen: set[str] = set()
    for index, candidate in enumerate(selection, start=1):
        if not isinstance(candidate, Mapping) or not _verify_hash(candidate, "candidate_hash"):
            raise AudioArchiveError("manifest_candidate_hash_invalid")
        if int(candidate.get("archive_ordinal") or 0) != index:
            raise AudioArchiveError("manifest_candidate_order_invalid")
        post_id = str(candidate.get("post_id") or "")
        if post_id in seen:
            raise AudioArchiveError("manifest_candidate_duplicate")
        seen.add(post_id)
        audio_files = candidate.get("audio_files")
        if not isinstance(audio_files, Mapping) or audio_files != {
            "m4a": f"{index:04d}_{post_id}.m4a",
            "mp3": f"{index:04d}_{post_id}.mp3",
        }:
            raise AudioArchiveError("manifest_audio_filenames_invalid")
        if candidate.get("record_file") != f"{index:04d}_{post_id}.json":
            raise AudioArchiveError("manifest_record_filename_invalid")
    storage = manifest.get("storage")
    authorization = manifest.get("authorization")
    source = manifest.get("source")
    if (
        not isinstance(storage, Mapping)
        or not isinstance(authorization, Mapping)
        or not isinstance(source, Mapping)
    ):
        raise AudioArchiveError("manifest_storage_or_authorization_invalid")
    formats = storage.get("formats")
    if (
        not isinstance(formats, Mapping)
        or set(formats) != {"m4a", "mp3"}
        or formats.get("m4a")
        != {
            "mime_type": "audio/mp4",
            "codec": "aac-lc",
            "target_bitrate_kbps": 192,
            "sample_rate_hz": 44100,
            "channels": 2,
            "role": "normalized_primary",
        }
        or formats.get("mp3")
        != {
            "mime_type": "audio/mpeg",
            "codec": "mp3",
            "target_bitrate_kbps": 192,
            "sample_rate_hz": 44100,
            "channels": 2,
            "role": "derived_from_normalized_m4a",
        }
        or storage.get("single_tiktok_acquisition_per_post") is not True
        or storage.get("source_video_persisted") is not False
        or storage.get("signed_url_persisted") is not False
    ):
        raise AudioArchiveError("manifest_audio_profile_invalid")
    source_database = Path(str(source.get("source_database") or ""))
    if (
        not source_database.is_absolute()
        or re.fullmatch(r"[0-9a-f]{32}", str(source.get("source_id") or "")) is None
        or re.fullmatch(r"[0-9a-f]{32}", str(source.get("master_run_id") or "")) is None
        or not str(source.get("local_run_id") or "")
        or re.fullmatch(r"[0-9a-f]{64}", str(source.get("lineage_hash") or "")) is None
    ):
        raise AudioArchiveError("manifest_source_binding_invalid")
    mode = str(storage.get("mode") or "")
    _parse_timestamp(manifest.get("created_at"), field="created_at")
    if mode not in STORAGE_MODES:
        raise AudioArchiveError("manifest_storage_mode_invalid")
    if authorization.get("authorization_kind") not in {
        "user_requested_audio_archive",
        "named_human_exact_run_audio_storage",
    }:
        raise AudioArchiveError("manifest_request_metadata_invalid")
    if manifest.get("ai_actions") != [] or manifest.get("outbound_actions") != []:
        raise AudioArchiveError("manifest_action_boundary_invalid")


def _state_for_records(
    manifest: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    *,
    status: str,
    observed_account: str = "",
    last_error: str = "",
) -> dict[str, Any]:
    archived = sum(record.get("status") == "archived" for record in records)
    unavailable = sum(record.get("status") == "unavailable" for record in records)
    purged = status == "purged"
    value = {
        "schema_version": STATE_SCHEMA,
        "run_id": manifest["run_id"],
        "manifest_hash": manifest["manifest_hash"],
        "status": status,
        "selected": int(manifest["selected_count"]),
        "terminal": len(records),
        "archived": archived,
        "unavailable": unavailable,
        "pending": int(manifest["selected_count"]) - len(records),
        "audio_files_present": 0 if purged else archived * 2,
        "observed_account": observed_account,
        "last_error": _safe_error(last_error) if last_error else "",
        "expired": _manifest_expired(manifest),
        "updated_at": _timestamp(),
    }
    return _bind_hash(value, "state_hash")


def _validate_state(
    manifest: Mapping[str, Any],
    state: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> None:
    if state.get("schema_version") != STATE_SCHEMA or not _verify_hash(state, "state_hash"):
        raise AudioArchiveError("state_hash_invalid")
    if state.get("run_id") != manifest.get("run_id") or state.get(
        "manifest_hash"
    ) != manifest.get("manifest_hash"):
        raise AudioArchiveError("state_manifest_binding_invalid")
    status = str(state.get("status") or "")
    if status not in {
        "planned",
        "running",
        "archive_incomplete",
        "archive_complete",
        "purged",
    }:
        raise AudioArchiveError("state_status_invalid")
    expected = _state_counters(
        manifest,
        records,
        purged=status == "purged",
    )
    for key, value in expected.items():
        if int(state.get(key) or 0) != value:
            raise AudioArchiveError("state_counter_mismatch")
    if status == "planned" and records:
        raise AudioArchiveError("planned_state_has_records")
    if status == "archive_complete" and len(records) != int(
        manifest["selected_count"]
    ):
        raise AudioArchiveError("complete_state_is_not_terminal")


def _state_counters(
    manifest: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    *,
    purged: bool = False,
) -> dict[str, int]:
    return {
        "selected": int(manifest["selected_count"]),
        "terminal": len(records),
        "archived": sum(row.get("status") == "archived" for row in records),
        "unavailable": sum(row.get("status") == "unavailable" for row in records),
        "pending": int(manifest["selected_count"]) - len(records),
        "audio_files_present": (
            0 if purged else 2 * sum(row.get("status") == "archived" for row in records)
        ),
    }


def _verify_recorded_pairs(
    run_dir: Path,
    manifest: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> None:
    for record in records:
        candidate = manifest["selection"][int(record["archive_ordinal"]) - 1]
        paths = _audio_paths(run_dir, candidate)
        if record["status"] == "unavailable":
            if any(path.exists() for path in paths.values()):
                raise AudioArchiveError("unavailable_record_has_audio")
            continue
        for audio_format, path in paths.items():
            byte_count, sha256 = _file_hash(path)
            audio = record["audio_files"][audio_format]
            if (
                byte_count != int(audio["byte_count"])
                or sha256 != str(audio["sha256"])
            ):
                raise AudioArchiveError("audio_file_hash_mismatch")


def _manifest_expired(manifest: Mapping[str, Any], *, at: dt.datetime | None = None) -> bool:
    """Report a parseable legacy expiry without making it an execution gate."""

    storage = manifest.get("storage")
    if not isinstance(storage, Mapping) or storage.get("mode") != "public-research":
        return False
    if not str(storage.get("expires_at") or "").strip():
        return False
    try:
        expires_at = _parse_timestamp(storage.get("expires_at"), field="expires_at")
    except AudioArchiveError:
        # Some pre-simplification manifests contain incomplete retention
        # metadata. It is compatibility-only and must not prevent resume.
        return False
    return (at or _now()).astimezone(dt.timezone.utc) >= expires_at


def _record_path(run_dir: Path, candidate: Mapping[str, Any]) -> Path:
    return run_dir / "records" / str(candidate["record_file"])


def _audio_paths(
    run_dir: Path,
    candidate: Mapping[str, Any],
) -> dict[str, Path]:
    files = candidate.get("audio_files")
    if not isinstance(files, Mapping) or set(files) != {"m4a", "mp3"}:
        raise AudioArchiveError("candidate_audio_files_invalid")
    return {
        audio_format: run_dir / "audio" / str(files[audio_format])
        for audio_format in ("m4a", "mp3")
    }


def _record_for_candidate(
    manifest: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    status: str,
    observed_account: str,
    audio_files: Mapping[str, Any] | None = None,
    transport: Mapping[str, Any] | None = None,
    error: str = "",
) -> dict[str, Any]:
    value = {
        "schema_version": RECORD_SCHEMA,
        "run_id": manifest["run_id"],
        "manifest_hash": manifest["manifest_hash"],
        "candidate_hash": candidate["candidate_hash"],
        "archive_ordinal": candidate["archive_ordinal"],
        "post_id": candidate["post_id"],
        "creator_handle": candidate["creator_handle"],
        "status": status,
        "observed_account": observed_account,
        "audio_files": dict(audio_files or {}),
        "transport": dict(transport or {}),
        "error": _safe_error(error) if error else "",
        "completed_at": _timestamp(),
    }
    return _bind_hash(value, "record_hash")


def _validate_record(
    manifest: Mapping[str, Any],
    candidate: Mapping[str, Any],
    record: Mapping[str, Any],
) -> None:
    if record.get("schema_version") != RECORD_SCHEMA or not _verify_hash(
        record, "record_hash"
    ):
        raise AudioArchiveError("record_hash_invalid")
    if (
        record.get("run_id") != manifest.get("run_id")
        or record.get("manifest_hash") != manifest.get("manifest_hash")
        or record.get("candidate_hash") != candidate.get("candidate_hash")
        or record.get("post_id") != candidate.get("post_id")
        or int(record.get("archive_ordinal") or 0)
        != int(candidate.get("archive_ordinal") or 0)
    ):
        raise AudioArchiveError("record_binding_invalid")
    status = record.get("status")
    if status not in {"archived", "unavailable"}:
        raise AudioArchiveError("record_status_invalid")
    audio_files = record.get("audio_files")
    if not isinstance(audio_files, Mapping):
        raise AudioArchiveError("record_audio_files_invalid")
    if status == "archived":
        expected_profiles = {
            "m4a": ("audio/mp4", "aac-lc"),
            "mp3": ("audio/mpeg", "mp3"),
        }
        candidate_files = candidate.get("audio_files")
        if set(audio_files) != {"m4a", "mp3"} or not isinstance(
            candidate_files, Mapping
        ):
            raise AudioArchiveError("record_archived_audio_pair_invalid")
        for audio_format, (mime_type, codec) in expected_profiles.items():
            audio = audio_files.get(audio_format)
            if (
                not isinstance(audio, Mapping)
                or audio.get("file") != candidate_files.get(audio_format)
                or audio.get("format") != audio_format
                or audio.get("mime_type") != mime_type
                or audio.get("codec") != codec
                or int(audio.get("target_bitrate_kbps") or 0) != 192
                or int(audio.get("sample_rate_hz") or 0) != 44100
                or int(audio.get("channels") or 0) != 2
                or re.fullmatch(
                    r"[0-9a-f]{64}",
                    str(audio.get("sha256") or ""),
                )
                is None
                or int(audio.get("byte_count") or 0) < 1
            ):
                raise AudioArchiveError("record_archived_audio_pair_invalid")
        durations = [
            float(audio_files[audio_format].get("duration_seconds") or 0.0)
            for audio_format in ("m4a", "mp3")
        ]
        if not all(
            math.isfinite(duration) and duration > 0 for duration in durations
        ):
            raise AudioArchiveError("record_audio_duration_invalid")
    elif audio_files:
        raise AudioArchiveError("record_unavailable_audio_must_be_empty")


def _load_records(
    run_dir: Path,
    manifest: Mapping[str, Any],
) -> list[dict[str, Any]]:
    records_dir = _assert_regular_directory(run_dir / "records")
    expected_names = {
        str(candidate["record_file"]) for candidate in manifest["selection"]
    }
    for child in records_dir.iterdir():
        if (
            child.name not in expected_names
            or not child.is_file()
            or _is_reparse_point(child)
        ):
            raise AudioArchiveError("unexpected_or_unsafe_record_artifact")
    records: list[dict[str, Any]] = []
    gap_seen = False
    for candidate in manifest["selection"]:
        path = _record_path(run_dir, candidate)
        if not path.exists():
            gap_seen = True
            continue
        if gap_seen:
            raise AudioArchiveError("record_order_gap")
        record = _read_json(path, name="record")
        _validate_record(manifest, candidate, record)
        records.append(record)
    return records


def _load_run(
    output_root: str | Path,
    run_id: str,
    *,
    repair_interrupted_state: bool = False,
) -> tuple[Path, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    run_dir = _run_directory(output_root, run_id)
    if not run_dir.is_dir() or _is_reparse_point(run_dir):
        raise AudioArchiveError("run_directory_missing_or_unsafe")
    _validate_run_layout(run_dir)
    manifest = _read_json(run_dir / "manifest.json", name="manifest")
    _validate_manifest(manifest)
    if manifest.get("run_id") != run_id:
        raise AudioArchiveError("run_directory_manifest_mismatch")
    records = _load_records(run_dir, manifest)
    state = _read_json(run_dir / "state.json", name="state")
    try:
        _validate_state(manifest, state, records)
    except AudioArchiveError as exc:
        if (
            not repair_interrupted_state
            or str(exc) != "state_counter_mismatch"
            or state.get("status") not in {
                "planned",
                "running",
                "archive_incomplete",
            }
        ):
            raise
        try:
            cached_terminal = int(state.get("terminal"))
        except (TypeError, ValueError) as counter_exc:
            raise AudioArchiveError("state_counter_mismatch") from counter_exc
        if not 0 <= cached_terminal < len(records):
            raise
        cached_expected = _state_counters(
            manifest,
            records[:cached_terminal],
        )
        if any(
            int(state.get(key) or 0) != value
            for key, value in cached_expected.items()
        ):
            raise
        # A record is the durable per-item commit. Repair only the one valid
        # direction: state lagging a longer hash-valid record prefix. Verify
        # every recorded pair before rewriting the derived state cache.
        _verify_recorded_pairs(run_dir, manifest, records)
        state = _state_for_records(
            manifest,
            records,
            status=(
                "running"
                if len(records) == int(manifest["selected_count"])
                else "archive_incomplete"
            ),
            observed_account=str(state.get("observed_account") or ""),
            last_error="state_repaired_after_interrupted_checkpoint",
        )
        _atomic_write_json(run_dir / "state.json", state)
    return run_dir, manifest, state, records


def _candidate_index(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row["post_id"]): dict(row) for row in manifest["selection"]}


def _remove_uncheckpointed_outputs(
    run_dir: Path,
    manifest: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> None:
    records_by_id = {str(record["post_id"]): record for record in records}
    expected_names = {
        str(name)
        for row in manifest["selection"]
        for name in row["audio_files"].values()
    }
    audio_dir = _assert_regular_directory(run_dir / "audio")
    staging = _assert_regular_directory(run_dir / ".staging")
    for child in staging.iterdir():
        if child.is_file() and not _is_reparse_point(child):
            child.unlink()
        else:
            raise AudioArchiveError("unsafe_staging_residue")
    for candidate in manifest["selection"]:
        paths = _audio_paths(run_dir, candidate)
        record = records_by_id.get(str(candidate["post_id"]))
        if record is None:
            for path in paths.values():
                if path.exists():
                    if not path.is_file() or _is_reparse_point(path):
                        raise AudioArchiveError("unsafe_uncheckpointed_audio")
                    path.unlink()
        elif record.get("status") == "unavailable" and any(
            path.exists() for path in paths.values()
        ):
            raise AudioArchiveError("unavailable_record_has_audio")
    for child in audio_dir.iterdir():
        if child.name not in expected_names:
            raise AudioArchiveError("unexpected_audio_file")


def _write_record(
    run_dir: Path,
    candidate: Mapping[str, Any],
    record: Mapping[str, Any],
) -> None:
    _atomic_write_json(
        _record_path(run_dir, candidate),
        record,
        no_clobber=True,
    )


def _review_payload(
    run_dir: Path,
    manifest: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    *,
    observed_account: str,
) -> dict[str, Any]:
    archived = [record for record in records if record.get("status") == "archived"]
    unavailable = [record for record in records if record.get("status") == "unavailable"]
    value = {
        "schema_version": REVIEW_SCHEMA,
        "run_id": manifest["run_id"],
        "manifest_hash": manifest["manifest_hash"],
        "status": "archive_complete",
        "source": dict(manifest["source"]),
        "storage": dict(manifest["storage"]),
        "authorization": dict(manifest["authorization"]),
        "selected": int(manifest["selected_count"]),
        "archived": len(archived),
        "unavailable": len(unavailable),
        "observed_account": observed_account,
        "run_directory": os.fspath(run_dir),
        "audio_files": [
            {
                "archive_ordinal": record["archive_ordinal"],
                "post_id": record["post_id"],
                "format": audio_format,
                "file": record["audio_files"][audio_format].get("file", ""),
                "sha256": record["audio_files"][audio_format].get("sha256", ""),
                "byte_count": record["audio_files"][audio_format].get(
                    "byte_count", 0
                ),
            }
            for record in archived
            for audio_format in ("m4a", "mp3")
        ],
        "ai_actions": [],
        "outbound_actions": [],
        "generated_at": _timestamp(),
    }
    return _bind_hash(value, "review_hash")


def _review_markdown(review: Mapping[str, Any]) -> str:
    source = review["source"]
    storage = review["storage"]
    lines = [
        "# AUDIO ARCHIVE Review",
        "",
        f"- Status: **{review['status']}**",
        f"- Run ID: `{review['run_id']}`",
        f"- Source workflow: `{source.get('workflow', '')}`",
        f"- Source project: `{source.get('project', '')}`",
        f"- Source run ID: `{source.get('local_run_id', '')}`",
        f"- Storage mode: `{storage.get('mode', '')}`",
        f"- Evidence selection: `{review['selected']}`",
        f"- Posts archived: `{review['archived']}`",
        f"- Audio files archived: `{2 * int(review['archived'])}` (matched M4A + MP3)",
        f"- Unavailable: `{review['unavailable']}`",
        f"- Observed account: `{review.get('observed_account', '')}`",
        f"- Run directory: `{review['run_directory']}`",
        f"- Expires at: `{storage.get('expires_at') or 'never'}`",
        "- AI actions: `[]`",
        "- Outbound actions: `[]`",
        "",
        "Each successful post has one normalized M4A and one locally derived MP3; "
        "they are post soundtracks, not standalone TikTok sound assets.",
    ]
    return "\n".join(lines) + "\n"


def _write_review(
    run_dir: Path,
    manifest: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    *,
    observed_account: str,
) -> dict[str, Any]:
    review_path = run_dir / "review.json"
    markdown_path = run_dir / "review.md"
    if review_path.exists() or markdown_path.exists():
        if not review_path.exists():
            raise AudioArchiveError("review_artifact_pair_incomplete")
        review = _read_json(review_path, name="review")
        if (
            review.get("schema_version") != REVIEW_SCHEMA
            or not _verify_hash(review, "review_hash")
            or review.get("manifest_hash") != manifest.get("manifest_hash")
            or review.get("run_id") != manifest.get("run_id")
            or int(review.get("selected") or 0) != len(records)
            or int(review.get("archived") or 0)
            != sum(record.get("status") == "archived" for record in records)
            or int(review.get("unavailable") or 0)
            != sum(record.get("status") == "unavailable" for record in records)
        ):
            raise AudioArchiveError("review_hash_invalid")
        if not markdown_path.exists():
            _atomic_write_text(
                markdown_path,
                _review_markdown(review),
                no_clobber=True,
            )
        elif (
            not markdown_path.is_file()
            or _is_reparse_point(markdown_path)
            or markdown_path.read_text(encoding="utf-8")
            != _review_markdown(review)
        ):
            raise AudioArchiveError("review_markdown_mismatch")
        return review
    review = _review_payload(
        run_dir,
        manifest,
        records,
        observed_account=observed_account,
    )
    _atomic_write_json(review_path, review, no_clobber=True)
    _atomic_write_text(markdown_path, _review_markdown(review), no_clobber=True)
    return review


def _status_packet(
    run_dir: Path,
    manifest: Mapping[str, Any],
    state: Mapping[str, Any],
) -> dict[str, Any]:
    storage = manifest["storage"]
    return {
        "run_id": manifest["run_id"],
        "status": state["status"],
        "selected": state["selected"],
        "terminal": state["terminal"],
        "archived": state["archived"],
        "unavailable": state["unavailable"],
        "pending": state["pending"],
        "audio_files_present": state["audio_files_present"],
        "storage_mode": storage["mode"],
        "expires_at": storage.get("expires_at", ""),
        "expired": _manifest_expired(manifest),
        "observed_account": state.get("observed_account", ""),
        "run_directory": os.fspath(run_dir),
        "manifest_hash": manifest["manifest_hash"],
        "state_hash": state["state_hash"],
        "ai_actions": [],
        "outbound_actions": [],
    }


def _new_run(args: argparse.Namespace) -> dict[str, Any]:
    """Create an archive from evidence-ready rows in one local project run."""

    authorization = _validate_creation_authorization(args)
    requested_expected_account = _normalized_handle(args.expected_account)
    from tiktok_scraper.run_lineage import (
        RunLineageError,
        load_archive_run_posts,
    )

    try:
        lineage = load_archive_run_posts(
            args.source_database,
            args.source_run_id,
        )
    except RunLineageError as exc:
        raise AudioArchiveError(_safe_error(exc)) from exc
    if not isinstance(lineage, Mapping):
        raise AudioArchiveError("source_lineage_invalid")
    candidates = _select_candidates(
        lineage,
        args.post_id if not args.all_evidence_ready else None,
    )
    source_run = lineage.get("source_run")
    if not isinstance(source_run, Mapping):
        raise AudioArchiveError("lineage_source_run_invalid")
    expected_account = requested_expected_account
    created_at = _timestamp()
    authorization = {
        **authorization,
        "scope": (
            "explicit-post-ids" if args.post_id else "all-evidence-ready"
        ),
        "requested_at": created_at,
    }
    storage_mode = "archive"
    run_id = _new_run_id(
        source_project=str(source_run.get("project") or ""),
        source_run_id=str(source_run.get("run_id") or args.source_run_id),
        storage_mode=storage_mode,
        selection_count=len(candidates),
        created_at=created_at,
    )
    manifest = _build_manifest(
        run_id=run_id,
        lineage=lineage,
        candidates=candidates,
        authorization=authorization,
        storage_mode=storage_mode,
        retention_days=None,
        expected_account=expected_account,
        created_at=created_at,
    )
    _validate_manifest(manifest)

    output_root = _canonical_output_root(args.output_root, create=True)
    run_dir = _run_directory(output_root, run_id)
    initialized = False
    try:
        run_dir.mkdir(parents=False, exist_ok=False)
        (run_dir / "records").mkdir()
        (run_dir / "audio").mkdir()
        (run_dir / ".staging").mkdir()
        _atomic_write_text(
            run_dir / RUN_LOCK_FILE,
            "tiktok-audio-archive-run-lock-v1",
            no_clobber=True,
        )
        with _run_lock(run_dir):
            _atomic_write_json(
                run_dir / "manifest.json",
                manifest,
                no_clobber=True,
            )
            state = _state_for_records(manifest, [], status="planned")
            _atomic_write_json(run_dir / "state.json", state)
            initialized = True
            return asyncio.run(
                _execute(
                    run_dir,
                    manifest,
                    expected_account=expected_account,
                )
            )
    except Exception:
        if not initialized:
            # The directory was just created by this invocation and no browser
            # or audio operation has begun. Remove only the owned skeleton;
            # never recurse over an established run.
            with contextlib.suppress(OSError):
                for child_name in ("state.json", "manifest.json", RUN_LOCK_FILE):
                    (run_dir / child_name).unlink(missing_ok=True)
                for child_name in (".staging", "audio", "records"):
                    (run_dir / child_name).rmdir()
                run_dir.rmdir()
        raise


def _resume_unlocked(args: argparse.Namespace) -> dict[str, Any]:
    run_dir, manifest, state, records = _load_run(
        args.output_root,
        args.run_id,
        repair_interrupted_state=True,
    )
    _validate_supplied_master_database(args, manifest)
    if state.get("status") == "purged":
        # Purge is no longer an active workflow transition. Keep historical
        # purged runs readable and hash-verifiable without treating their old
        # retention state as a resume authorization gate.
        purge = _validate_purge(run_dir, manifest)
        if purge is None or purge.get("status") != "purged":
            raise AudioArchiveError("legacy_purged_run_receipt_invalid")
        _verify_audio_artifacts(
            run_dir,
            manifest,
            records,
            purge=purge,
        )
        return {
            **_status_packet(run_dir, manifest, state),
            "resume_performed": False,
            "legacy_purged": True,
        }
    requested_account = _normalized_handle(args.expected_account)
    frozen_account = _normalized_handle(manifest.get("expected_account"))
    observed_account = _normalized_handle(state.get("observed_account"))
    if frozen_account and requested_account and frozen_account != requested_account:
        raise AudioArchiveError("resume_expected_account_mismatch")
    if observed_account and requested_account and observed_account != requested_account:
        raise AudioArchiveError("resume_observed_account_mismatch")
    _revalidate_resume_lineage(manifest)
    return asyncio.run(
        _execute(
            run_dir,
            manifest,
            expected_account=frozen_account or observed_account or requested_account,
        )
    )


def _resume(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = _run_directory(args.output_root, args.run_id)
    with _run_lock(run_dir):
        return _resume_unlocked(args)


def _revalidate_resume_lineage(manifest: Mapping[str, Any]) -> None:
    """Resume from the frozen manifest without reopening legacy databases."""

    return None


def _status_unlocked(args: argparse.Namespace) -> dict[str, Any]:
    run_dir, manifest, state, _ = _load_run(args.output_root, args.run_id)
    _validate_supplied_master_database(args, manifest)
    return _status_packet(run_dir, manifest, state)


def _status(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = _run_directory(args.output_root, args.run_id)
    with _run_lock(run_dir):
        return _status_unlocked(args)


def _validate_purge(
    run_dir: Path,
    manifest: Mapping[str, Any],
) -> dict[str, Any] | None:
    path = run_dir / "purge.json"
    if not path.exists():
        return None
    purge = _read_json(path, name="purge")
    if (
        purge.get("schema_version") != PURGE_SCHEMA
        or not _verify_hash(purge, "purge_hash")
        or purge.get("run_id") != manifest.get("run_id")
        or purge.get("manifest_hash") != manifest.get("manifest_hash")
        or purge.get("status") not in {"planned", "purged"}
    ):
        raise AudioArchiveError("purge_receipt_invalid")
    entries = purge.get("entries")
    if not isinstance(entries, list):
        raise AudioArchiveError("purge_entries_invalid")
    if purge.get("entry_set_hash") != _json_hash(entries):
        raise AudioArchiveError("purge_entry_set_hash_invalid")
    archived_candidates = {
        str(name): (candidate, audio_format)
        for candidate in manifest["selection"]
        for audio_format, name in candidate["audio_files"].items()
    }
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise AudioArchiveError("purge_entry_invalid")
        name = str(entry.get("file") or "")
        if name in seen or name not in archived_candidates:
            raise AudioArchiveError("purge_entry_binding_invalid")
        seen.add(name)
        candidate, audio_format = archived_candidates[name]
        if (
            entry.get("format") != audio_format
            or entry.get("post_id") != candidate.get("post_id")
            or int(entry.get("archive_ordinal") or 0)
            != int(candidate.get("archive_ordinal") or 0)
        ):
            raise AudioArchiveError("purge_entry_binding_invalid")
        if re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256") or "")) is None:
            raise AudioArchiveError("purge_entry_hash_invalid")
        if int(entry.get("byte_count") or 0) < 1:
            raise AudioArchiveError("purge_entry_size_invalid")
    return purge


def _verify_audio_artifacts(
    run_dir: Path,
    manifest: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    *,
    purge: Mapping[str, Any] | None,
) -> dict[str, Any]:
    audio_dir = run_dir / "audio"
    if not audio_dir.is_dir() or _is_reparse_point(audio_dir):
        raise AudioArchiveError("audio_directory_invalid")
    purged = bool(purge and purge.get("status") == "purged")
    expected_files: set[str] = set()
    verified_bytes = 0
    for record in records:
        candidate = manifest["selection"][int(record["archive_ordinal"]) - 1]
        paths = _audio_paths(run_dir, candidate)
        if record["status"] == "unavailable":
            if any(path.exists() for path in paths.values()):
                raise AudioArchiveError("unavailable_record_has_audio")
            continue
        for audio_format, path in paths.items():
            expected_files.add(path.name)
            if purged:
                if path.exists():
                    raise AudioArchiveError("purged_audio_still_present")
                continue
            byte_count, sha256 = _file_hash(path)
            audio = record["audio_files"][audio_format]
            if (
                byte_count != int(audio["byte_count"])
                or sha256 != str(audio["sha256"])
            ):
                raise AudioArchiveError("audio_file_hash_mismatch")
            verified_bytes += byte_count
    present_names: set[str] = set()
    for child in audio_dir.iterdir():
        if not child.is_file() or _is_reparse_point(child):
            raise AudioArchiveError("unexpected_or_unsafe_audio_artifact")
        present_names.add(child.name)
    expected_present = set() if purged else expected_files
    if purge is not None:
        purge_entries = purge.get("entries")
        if not isinstance(purge_entries, list):
            raise AudioArchiveError("purge_entries_invalid")
        purge_by_name = {
            str(entry.get("file") or ""): entry
            for entry in purge_entries
            if isinstance(entry, Mapping)
        }
        if set(purge_by_name) != expected_files:
            raise AudioArchiveError("purge_file_set_mismatch")
        for record in records:
            if record.get("status") != "archived":
                continue
            for audio_format, audio in record["audio_files"].items():
                entry = purge_by_name.get(str(audio["file"]))
                if (
                    not isinstance(entry, Mapping)
                    or entry.get("format") != audio_format
                    or entry.get("post_id") != record.get("post_id")
                    or int(entry.get("archive_ordinal") or 0)
                    != int(record.get("archive_ordinal") or 0)
                    or int(entry.get("byte_count") or 0)
                    != int(audio.get("byte_count") or 0)
                    or entry.get("sha256") != audio.get("sha256")
                ):
                    raise AudioArchiveError("purge_record_binding_mismatch")
    if present_names != expected_present:
        raise AudioArchiveError("audio_file_set_mismatch")
    staging = run_dir / ".staging"
    if not staging.is_dir() or _is_reparse_point(staging):
        raise AudioArchiveError("staging_directory_invalid")
    if any(staging.iterdir()):
        raise AudioArchiveError("staging_residue_present")
    return {
        "verified_audio_files": len(expected_present),
        "verified_audio_bytes": verified_bytes,
        "purged": purged,
    }


def _validate_review(
    run_dir: Path,
    manifest: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    state: Mapping[str, Any],
) -> dict[str, Any] | None:
    review_path = run_dir / "review.json"
    markdown_path = run_dir / "review.md"
    terminal = len(records) == int(manifest["selected_count"])
    if not terminal:
        if review_path.exists() or markdown_path.exists():
            raise AudioArchiveError("incomplete_run_has_review")
        return None
    if (
        not review_path.is_file()
        or _is_reparse_point(review_path)
        or not markdown_path.is_file()
        or _is_reparse_point(markdown_path)
    ):
        raise AudioArchiveError("complete_run_review_missing")
    review = _read_json(review_path, name="review")
    if (
        review.get("schema_version") != REVIEW_SCHEMA
        or not _verify_hash(review, "review_hash")
        or review.get("run_id") != manifest.get("run_id")
        or review.get("manifest_hash") != manifest.get("manifest_hash")
        or int(review.get("selected") or 0) != len(records)
        or int(review.get("archived") or 0)
        != sum(record.get("status") == "archived" for record in records)
        or int(review.get("unavailable") or 0)
        != sum(record.get("status") == "unavailable" for record in records)
    ):
        raise AudioArchiveError("review_binding_invalid")
    expected_review_files = [
        {
            "archive_ordinal": record["archive_ordinal"],
            "post_id": record["post_id"],
            "format": audio_format,
            "file": record["audio_files"][audio_format]["file"],
            "sha256": record["audio_files"][audio_format]["sha256"],
            "byte_count": record["audio_files"][audio_format]["byte_count"],
        }
        for record in records
        if record.get("status") == "archived"
        for audio_format in ("m4a", "mp3")
    ]
    if review.get("audio_files") != expected_review_files:
        raise AudioArchiveError("review_audio_file_binding_invalid")
    try:
        markdown = markdown_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AudioArchiveError("review_markdown_unreadable") from exc
    if markdown != _review_markdown(review):
        raise AudioArchiveError("review_markdown_mismatch")
    if state.get("status") not in {"archive_complete", "purged"}:
        raise AudioArchiveError("terminal_state_status_invalid")
    return review


def _validate_unlocked(args: argparse.Namespace) -> dict[str, Any]:
    run_dir, manifest, state, records = _load_run(args.output_root, args.run_id)
    _validate_supplied_master_database(args, manifest)
    purge = _validate_purge(run_dir, manifest)
    file_result = _verify_audio_artifacts(
        run_dir,
        manifest,
        records,
        purge=purge,
    )
    review = _validate_review(run_dir, manifest, records, state)
    return {
        **_status_packet(run_dir, manifest, state),
        "valid": True,
        "review_hash": str((review or {}).get("review_hash") or ""),
        "purge_hash": str((purge or {}).get("purge_hash") or ""),
        **file_result,
    }


def _validate(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = _run_directory(args.output_root, args.run_id)
    with _run_lock(run_dir):
        return _validate_unlocked(args)


def _purge_plan(
    manifest: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    entries = [
        {
            "archive_ordinal": int(record["archive_ordinal"]),
            "post_id": str(record["post_id"]),
            "format": audio_format,
            "file": str(record["audio_files"][audio_format]["file"]),
            "sha256": str(record["audio_files"][audio_format]["sha256"]),
            "byte_count": int(
                record["audio_files"][audio_format]["byte_count"]
            ),
        }
        for record in records
        if record.get("status") == "archived"
        for audio_format in ("m4a", "mp3")
    ]
    value = {
        "schema_version": PURGE_SCHEMA,
        "run_id": manifest["run_id"],
        "manifest_hash": manifest["manifest_hash"],
        "status": "planned",
        "expires_at": manifest["storage"]["expires_at"],
        "entries": entries,
        "entry_set_hash": _json_hash(entries),
        "planned_at": _timestamp(),
        "purged_at": "",
    }
    return _bind_hash(value, "purge_hash")


def _purge_staging_name(entry: Mapping[str, Any]) -> str:
    return (
        f".purge_{int(entry['archive_ordinal']):04d}_"
        f"{entry['format']}_{str(entry['sha256'])[:16]}.pending"
    )


def _verify_purge_file(path: Path, entry: Mapping[str, Any]) -> None:
    byte_count, sha256 = _file_hash(path)
    if byte_count != int(entry["byte_count"]) or sha256 != entry["sha256"]:
        raise AudioArchiveError("purge_audio_hash_mismatch")


def _purge_expired_unlocked(args: argparse.Namespace) -> dict[str, Any]:
    run_dir, manifest, state, records = _load_run(args.output_root, args.run_id)
    _validate_supplied_master_database(args, manifest)
    audio_dir = _assert_regular_directory(run_dir / "audio")
    if manifest["storage"]["mode"] != "public-research":
        raise AudioArchiveError("permanent_archive_has_no_expiry_purge")
    if not _manifest_expired(manifest):
        raise AudioArchiveError("research_cache_not_expired")
    existing = _validate_purge(run_dir, manifest)
    if existing is not None and existing.get("status") == "purged":
        _verify_audio_artifacts(run_dir, manifest, records, purge=existing)
        next_state = state
        if state.get("status") != "purged":
            next_state = _state_for_records(
                manifest,
                records,
                status="purged",
                observed_account=str(state.get("observed_account") or ""),
            )
            _atomic_write_json(run_dir / "state.json", next_state)
        return {
            **_status_packet(run_dir, manifest, next_state),
            "purged": True,
            "purged_audio_files": len(existing["entries"]),
            "purge_hash": existing["purge_hash"],
        }
    if existing is None:
        # The first pass requires the complete intact set before a durable
        # deletion plan is written. A later planned pass may legitimately see
        # an absent prefix after interruption and resumes from the same hashes.
        try:
            _verify_audio_artifacts(run_dir, manifest, records, purge=None)
        except AudioArchiveError as exc:
            if str(exc) == "audio_file_hash_mismatch":
                raise AudioArchiveError("purge_audio_hash_mismatch") from exc
            raise
        plan = _purge_plan(manifest, records)
        _atomic_write_json(run_dir / "purge.json", plan, no_clobber=True)
    else:
        plan = existing

    record_by_file = {
        str(audio.get("file") or ""): (record, audio_format)
        for record in records
        if record.get("status") == "archived"
        for audio_format, audio in record["audio_files"].items()
    }
    allowed_names = {str(entry["file"]) for entry in plan["entries"]}
    staging_dir = _assert_regular_directory(run_dir / ".staging")
    allowed_staging_names = {
        _purge_staging_name(entry) for entry in plan["entries"]
    }
    for child in audio_dir.iterdir():
        if (
            child.name not in allowed_names
            or not child.is_file()
            or _is_reparse_point(child)
        ):
            raise AudioArchiveError("unexpected_or_unsafe_audio_artifact")
    for child in staging_dir.iterdir():
        if (
            child.name not in allowed_staging_names
            or not child.is_file()
            or _is_reparse_point(child)
        ):
            raise AudioArchiveError("unexpected_or_unsafe_purge_staging")

    # A valid interrupted purge has a deleted prefix, optionally one staged
    # current file, then an intact source suffix. Any other hole fails closed.
    phase = "deleted"
    staged_seen = False
    for entry in plan["entries"]:
        record_binding = record_by_file.get(str(entry["file"]))
        if record_binding is None:
            raise AudioArchiveError("purge_plan_record_missing")
        record, audio_format = record_binding
        if (
            audio_format != entry.get("format")
            or record.get("post_id") != entry.get("post_id")
        ):
            raise AudioArchiveError("purge_plan_record_mismatch")
        path = audio_dir / str(entry["file"])
        staged = staging_dir / _purge_staging_name(entry)
        source_present = path.exists()
        staged_present = staged.exists()
        if source_present and staged_present:
            raise AudioArchiveError("purge_duplicate_source_and_staging")
        if staged_present:
            if phase == "source" or staged_seen:
                raise AudioArchiveError("purge_progress_not_prefix_ordered")
            staged_seen = True
            phase = "staged"
            _verify_purge_file(staged, entry)
        elif source_present:
            phase = "source"
            _verify_purge_file(path, entry)
        elif phase != "deleted":
            raise AudioArchiveError("purge_progress_not_prefix_ordered")

    # Move each remaining target to a private same-filesystem name, reverify
    # the moved inode, then delete it. If bytes were swapped after the initial
    # pass, restore the path and fail without deleting the replacement.
    for entry in plan["entries"]:
        path = audio_dir / str(entry["file"])
        staged = staging_dir / _purge_staging_name(entry)
        if path.exists():
            os.replace(path, staged)
            try:
                _verify_purge_file(staged, entry)
            except Exception:
                if not path.exists() and staged.exists():
                    os.replace(staged, path)
                raise
        if staged.exists():
            _verify_purge_file(staged, entry)
            staged.unlink()
    if any(audio_dir.iterdir()):
        raise AudioArchiveError("purge_audio_directory_not_empty")
    if any(staging_dir.iterdir()):
        raise AudioArchiveError("purge_staging_directory_not_empty")
    completed = dict(plan)
    completed["status"] = "purged"
    completed["purged_at"] = _timestamp()
    completed = _bind_hash(completed, "purge_hash")
    _atomic_write_json(run_dir / "purge.json", completed)
    next_state = _state_for_records(
        manifest,
        records,
        status="purged",
        observed_account=str(state.get("observed_account") or ""),
    )
    _atomic_write_json(run_dir / "state.json", next_state)
    _verify_audio_artifacts(run_dir, manifest, records, purge=completed)
    return {
        **_status_packet(run_dir, manifest, next_state),
        "purged": True,
        "purged_audio_files": len(completed["entries"]),
        "purge_hash": completed["purge_hash"],
    }


def _purge_expired(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = _run_directory(args.output_root, args.run_id)
    with _run_lock(run_dir):
        return _purge_expired_unlocked(args)


async def _execute(
    run_dir: Path,
    manifest: Mapping[str, Any],
    *,
    expected_account: str = "",
    transport: Any = None,
) -> dict[str, Any]:
    """Archive every pending frozen candidate in stable source order."""

    _validate_manifest(manifest)
    state = _read_json(run_dir / "state.json", name="state")
    records = _load_records(run_dir, manifest)
    _validate_state(manifest, state, records)
    if state.get("status") == "purged":
        raise AudioArchiveError("purged_run_cannot_execute")
    _remove_uncheckpointed_outputs(run_dir, manifest, records)
    _verify_audio_artifacts(
        run_dir,
        manifest,
        records,
        purge=None,
    )

    recorded_ids = {str(record["post_id"]) for record in records}
    observed_accounts = {
        _normalized_handle(record.get("observed_account"))
        for record in records
        if str(record.get("observed_account") or "").strip()
    }
    if len(observed_accounts) > 1:
        raise AudioArchiveError("mixed_observed_accounts")
    observed_account = next(iter(observed_accounts), "")
    requested_account = _normalized_handle(expected_account)
    frozen_account = _normalized_handle(manifest.get("expected_account"))
    if frozen_account and requested_account and frozen_account != requested_account:
        raise AudioArchiveError("execution_expected_account_mismatch")
    if observed_account and requested_account and observed_account != requested_account:
        raise AudioArchiveError("execution_observed_account_mismatch")
    effective_account = frozen_account or observed_account or requested_account

    def checkpoint(*, status: str = "running", last_error: str = "") -> None:
        next_state = _state_for_records(
            manifest,
            records,
            status=status,
            observed_account=observed_account,
            last_error=last_error,
        )
        _atomic_write_json(run_dir / "state.json", next_state)

    def append_record(candidate: Mapping[str, Any], record: Mapping[str, Any]) -> None:
        expected_ordinal = len(records) + 1
        if int(candidate["archive_ordinal"]) != expected_ordinal:
            raise AudioArchiveError("terminal_record_order_violation")
        _write_record(run_dir, candidate, record)
        records.append(dict(record))
        recorded_ids.add(str(candidate["post_id"]))
        checkpoint()

    def append_photo_unavailable(candidate: Mapping[str, Any]) -> None:
        record = _record_for_candidate(
            manifest,
            candidate,
            status="unavailable",
            observed_account=observed_account or effective_account,
            error="photo_posts_not_supported_by_audio_archive_v1",
            transport={
                "browser_accessed": False,
                "source_video_persisted": False,
                "signed_url_persisted": False,
            },
        )
        append_record(candidate, record)

    if len(records) == int(manifest["selected_count"]):
        review = _write_review(
            run_dir,
            manifest,
            records,
            observed_account=observed_account,
        )
        complete = _state_for_records(
            manifest,
            records,
            status="archive_complete",
            observed_account=observed_account,
        )
        _atomic_write_json(run_dir / "state.json", complete)
        return {
            **_status_packet(run_dir, manifest, complete),
            "review_hash": review["review_hash"],
        }

    checkpoint()
    if transport is None:
        from audio_archive_transport import AudioArchiveTransport

        transport = AudioArchiveTransport(staging_root=run_dir / ".staging")

    selection = [dict(candidate) for candidate in manifest["selection"]]
    by_id = _candidate_index(manifest)
    try:
        batch_numbers = sorted(
            {
                int(candidate["transport_batch"])
                for candidate in selection
                if str(candidate["post_id"]) not in recorded_ids
            }
        )
        for batch_number in batch_numbers:
            pending_batch = [
                candidate
                for candidate in selection
                if int(candidate["transport_batch"]) == batch_number
                and str(candidate["post_id"]) not in recorded_ids
            ]
            videos = [
                candidate
                for candidate in pending_batch
                if candidate["content_type"] == "video"
            ]
            destinations = {
                str(candidate["post_id"]): _audio_paths(run_dir, candidate)
                for candidate in videos
            }

            def fill_photos_before(ordinal: int) -> None:
                while len(records) + 1 < ordinal:
                    candidate = selection[len(records)]
                    if candidate["content_type"] != "photo":
                        raise AudioArchiveError("transport_receipt_skipped_video")
                    append_photo_unavailable(candidate)

            async def on_receipt(receipt: Any) -> None:
                nonlocal observed_account, effective_account
                post_id = str(getattr(receipt, "post_id", "") or "")
                candidate = by_id.get(post_id)
                if candidate is None or int(candidate["transport_batch"]) != batch_number:
                    raise AudioArchiveError("transport_receipt_candidate_mismatch")
                fill_photos_before(int(candidate["archive_ordinal"]))
                if len(records) + 1 != int(candidate["archive_ordinal"]):
                    raise AudioArchiveError("transport_receipt_order_invalid")
                provenance = getattr(receipt, "provenance", {})
                provenance = dict(provenance) if isinstance(provenance, Mapping) else {}
                receipt_account = _normalized_handle(provenance.get("account_handle"))
                if observed_account and receipt_account and observed_account != receipt_account:
                    raise AudioArchiveError("transport_account_changed")
                if effective_account and receipt_account and effective_account != receipt_account:
                    raise AudioArchiveError("transport_account_mismatch")
                if receipt_account and not observed_account:
                    observed_account = receipt_account
                    effective_account = receipt_account
                receipt_status = str(getattr(receipt, "status", "") or "")
                transport_record = {
                    "transport_owner_run_id": str(
                        getattr(receipt, "transport_owner_run_id", "") or ""
                    ),
                    "provenance": provenance,
                    "timing": dict(getattr(receipt, "timing", {}) or {}),
                    "source_video_persisted": False,
                    "signed_url_persisted": False,
                    "checkpoint_after_temporary_cleanup": True,
                }
                if receipt_status == "completed":
                    receipt_files = getattr(receipt, "files", {})
                    if not isinstance(receipt_files, Mapping) or set(
                        receipt_files
                    ) != {"m4a", "mp3"}:
                        raise AudioArchiveError(
                            "transport_audio_pair_receipt_invalid"
                        )
                    expected_paths = _audio_paths(run_dir, candidate)
                    stored_files: dict[str, dict[str, Any]] = {}
                    profiles = {
                        "m4a": ("audio/mp4", "aac-lc"),
                        "mp3": ("audio/mpeg", "mp3"),
                    }
                    for audio_format in ("m4a", "mp3"):
                        supplied = receipt_files.get(audio_format)
                        if not isinstance(supplied, Mapping):
                            raise AudioArchiveError(
                                "transport_audio_pair_receipt_invalid"
                            )
                        destination = Path(
                            supplied.get("destination_path") or ""
                        ).resolve()
                        expected_destination = expected_paths[
                            audio_format
                        ].resolve()
                        if destination != expected_destination:
                            raise AudioArchiveError(
                                "transport_destination_mismatch"
                            )
                        byte_count, sha256 = _file_hash(expected_destination)
                        if (
                            byte_count
                            != int(supplied.get("byte_count") or 0)
                            or sha256 != str(supplied.get("sha256") or "")
                            or supplied.get("format") != audio_format
                            or supplied.get("mime_type")
                            != profiles[audio_format][0]
                            or supplied.get("codec")
                            != profiles[audio_format][1]
                            or int(
                                supplied.get("target_bitrate_kbps") or 0
                            )
                            != 192
                            or int(supplied.get("sample_rate_hz") or 0)
                            != 44100
                            or int(supplied.get("channels") or 0) != 2
                            or not math.isfinite(
                                float(supplied.get("duration_seconds") or 0.0)
                            )
                            or float(supplied.get("duration_seconds") or 0.0)
                            <= 0
                        ):
                            raise AudioArchiveError(
                                "transport_audio_receipt_mismatch"
                            )
                        stored_files[audio_format] = {
                            "file": candidate["audio_files"][audio_format],
                            "format": audio_format,
                            "mime_type": profiles[audio_format][0],
                            "codec": profiles[audio_format][1],
                            "target_bitrate_kbps": 192,
                            "sample_rate_hz": 44100,
                            "channels": 2,
                            "byte_count": byte_count,
                            "sha256": sha256,
                            "duration_seconds": float(
                                supplied["duration_seconds"]
                            ),
                        }
                    record = _record_for_candidate(
                        manifest,
                        candidate,
                        status="archived",
                        observed_account=observed_account,
                        audio_files=stored_files,
                        transport=transport_record,
                    )
                elif receipt_status == "unavailable":
                    expected_paths = _audio_paths(run_dir, candidate)
                    if any(path.exists() for path in expected_paths.values()):
                        raise AudioArchiveError("unavailable_receipt_has_audio")
                    record = _record_for_candidate(
                        manifest,
                        candidate,
                        status="unavailable",
                        observed_account=observed_account or effective_account,
                        transport=transport_record,
                        error=str(getattr(receipt, "error_code", "") or "transport_unavailable"),
                    )
                else:
                    raise AudioArchiveError("transport_receipt_status_invalid")
                append_record(candidate, record)

            if videos:
                await transport.archive_candidates(
                    archive_run_id=str(manifest["run_id"]),
                    candidates=videos,
                    destinations=destinations,
                    expected_account=effective_account,
                    receipt_processor=on_receipt,
                )
            while pending_batch and len(records) < int(
                pending_batch[-1]["archive_ordinal"]
            ):
                candidate = selection[len(records)]
                if candidate["content_type"] != "photo":
                    raise AudioArchiveError("transport_did_not_return_video_receipt")
                append_photo_unavailable(candidate)
    except BaseException as exc:
        checkpoint(status="archive_incomplete", last_error=_safe_error(exc))
        # A transport callback can fail after the pair was promoted but before
        # its durable record was committed.  Remove only exact uncheckpointed
        # filenames from this immutable manifest so resume begins cleanly.
        _remove_uncheckpointed_outputs(run_dir, manifest, records)
        raise

    if len(records) != int(manifest["selected_count"]):
        checkpoint(
            status="archive_incomplete",
            last_error="terminal_record_count_mismatch",
        )
        raise AudioArchiveError("terminal_record_count_mismatch")
    _remove_uncheckpointed_outputs(run_dir, manifest, records)
    review = _write_review(
        run_dir,
        manifest,
        records,
        observed_account=observed_account,
    )
    complete = _state_for_records(
        manifest,
        records,
        status="archive_complete",
        observed_account=observed_account,
    )
    _atomic_write_json(run_dir / "state.json", complete)
    return {
        **_status_packet(run_dir, manifest, complete),
        "review_hash": review["review_hash"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            result = _new_run(args)
        elif args.command == "resume":
            result = _resume(args)
        elif args.command == "status":
            result = _status(args)
        elif args.command == "validate":
            result = _validate(args)
        else:  # pragma: no cover - argparse owns this gate
            raise AudioArchiveError("command_invalid")
    except (Exception, KeyboardInterrupt) as exc:
        print(
            json.dumps(
                {"status": "error", "error": _safe_error(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
