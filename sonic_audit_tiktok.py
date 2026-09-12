#!/usr/bin/env python
"""Standalone, local-first SONIC AUDIT pilot runner.

The runner selects known TikTok videos from the master registry, obtains each
video's audio only inside an owned temporary directory, derives local numeric
features, deletes the media/audio, and persists hash-bound feature records and
evaluation results.  It never mutates the TikTok master registry and exposes no
AI, response, authorization, or publication stage.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from sonic_audit.contracts import (
    DEFAULT_OUTPUT_ROOT,
    MAX_PILOT_POSTS,
    REPORT_SCHEMA,
    SonicAuditContractError,
    bind_feature_record,
    bind_hash,
    build_initial_state,
    build_run_manifest,
    canonical_json,
    canonical_sha256,
    load_creator_registry_candidates,
    normalize_creator,
    select_non_overlapping_candidates,
    select_pilot_candidates,
    utc_now,
    validate_feature_record,
    validate_symbolic_analysis_config,
    verify_hash,
)
from sonic_audit.corpus import (
    CORPUS_PLAN_SCHEMA,
    CorpusPlanningError,
    plan_corpus,
    validate_corpus_plan_batch,
)


DEFAULT_MASTER_DATABASE = (
    Path("comments_data") / "tiktok_master" / "state" / "tiktok_master.sqlite"
)
RUN_ID_PATTERN = re.compile(r"sonic_[0-9a-f]{16}")
BATCH_ID_PATTERN = re.compile(r"sonic_batch_[0-9a-f]{16}")
STATISTICAL_VALIDATION_SCHEMA = "tiktok-sonic-audit-statistical-validation-v1"
STATISTICAL_VALIDATION_FILE = "statistical_validation.json"
STATISTICAL_VALIDATION_SUITE_SCHEMA = (
    "tiktok-sonic-audit-statistical-validation-suite-v1"
)
MIRELO_SYMBOLIC_CONFIG_SCHEMA = "tiktok-sonic-mirelo-config-v1"
MIRELO_CREDIT_LEDGER_SCHEMA = "tiktok-sonic-mirelo-credit-ledger-v1"
MIRELO_CREDIT_LEDGER_FILE = "mirelo_credit_ledger.json"
MIRELO_NOT_IMPLEMENTED_ERROR = (
    "Mirelo Audio-to-MIDI is not implemented; no provider request or SONIC run "
    "was created"
)


class SonicAuditError(RuntimeError):
    """A fail-closed standalone SONIC AUDIT failure."""


def _emit_event(event: str, **values: Any) -> None:
    print(
        json.dumps({"event": event, **values}, ensure_ascii=False, allow_nan=False),
        file=sys.stderr,
        flush=True,
    )


def _positive_posts(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("posts must be an integer") from exc
    if not 1 <= parsed <= MAX_PILOT_POSTS:
        raise argparse.ArgumentTypeError(
            f"posts must be between 1 and {MAX_PILOT_POSTS}"
        )
    return parsed


def _positive_plan_count(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("value must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def _positive_credit_budget(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "Mirelo credit budget must be a positive integer"
        ) from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError(
            "Mirelo credit budget must be a positive integer"
        )
    return parsed


def _add_mirelo_options(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--mirelo-audio-to-midi",
        action="store_true",
        help=(
            "Reserved for a future Mirelo adapter; currently rejected before "
            "SONIC run creation."
        ),
    )
    command.add_argument(
        "--authorize-mirelo-upload",
        action="store_true",
        help=(
            "Reserved for a future Mirelo adapter; currently rejected before "
            "SONIC run creation."
        ),
    )
    command.add_argument(
        "--mirelo-max-credits",
        type=_positive_credit_budget,
        help="Reserved for a future Mirelo adapter; currently unavailable.",
    )


def _reject_unimplemented_mirelo_request(args: argparse.Namespace) -> None:
    """Fail closed before any state or provider access for public Mirelo options."""

    if (
        bool(getattr(args, "mirelo_audio_to_midi", False))
        or bool(getattr(args, "authorize_mirelo_upload", False))
        or getattr(args, "mirelo_max_credits", None) is not None
    ):
        raise SonicAuditError(MIRELO_NOT_IMPLEMENTED_ERROR)


def _mirelo_config_from_args(args: argparse.Namespace) -> dict[str, Any] | None:
    enabled = bool(getattr(args, "mirelo_audio_to_midi", False))
    authorized = bool(getattr(args, "authorize_mirelo_upload", False))
    max_credits = getattr(args, "mirelo_max_credits", None)
    if not enabled:
        if authorized or max_credits is not None:
            raise SonicAuditError(
                "Mirelo authorization/budget requires --mirelo-audio-to-midi"
            )
        return None
    if not authorized:
        raise SonicAuditError("--authorize-mirelo-upload is required")
    if max_credits is None:
        raise SonicAuditError("--mirelo-max-credits is required")
    config = {
        "schema_version": MIRELO_SYMBOLIC_CONFIG_SCHEMA,
        "provider": "mirelo",
        "model": "audio-to-midi-v1.0",
        "endpoint_version": "v2",
        "timing": "performance",
        "input_basis": "transient_decoded_wav",
        "max_run_credits": int(max_credits),
        "preflight_required": True,
        "server_asset_retention": "up_to_24_hours",
        "credential_environment_variable": "MIRELO_API_KEY",
        "persist_raw_notes": False,
        "persist_midi": False,
        "persist_musicxml": False,
        "changes_primary_recording_score": False,
    }
    return bind_hash(config, "config_hash")


def _load_mirelo_api_key() -> str:
    """Load a Mirelo secret without placing it in CLI arguments or artifacts."""

    value = str(os.environ.get("MIRELO_API_KEY") or "").strip()
    if not value and os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                stored, _ = winreg.QueryValueEx(key, "MIRELO_API_KEY")
            value = str(stored or "").strip()
        except (FileNotFoundError, OSError):
            value = ""
    if not value or not value.startswith("sk-") or len(value) < 40:
        raise SonicAuditError(
            "MIRELO_API_KEY is missing or invalid; configure it in the environment"
        )
    if any(character.isspace() for character in value):
        raise SonicAuditError("MIRELO_API_KEY is missing or invalid")
    return value


def _safe_project(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    normalized = normalized.strip("._-")
    if not normalized or len(normalized) > 100:
        raise argparse.ArgumentTypeError("project must contain a safe short name")
    return normalized


def _run_id(value: str) -> str:
    normalized = str(value or "").strip()
    if RUN_ID_PATTERN.fullmatch(normalized) is None:
        raise argparse.ArgumentTypeError("invalid SONIC AUDIT run ID")
    return normalized


def _batch_id(value: str) -> str:
    normalized = str(value or "").strip()
    if BATCH_ID_PATTERN.fullmatch(normalized) is None:
        raise argparse.ArgumentTypeError("invalid SONIC corpus batch ID")
    return normalized


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a bounded, local-feature SONIC AUDIT over known TikTok videos."
    )
    parser.add_argument(
        "--master-database",
        default=os.fspath(DEFAULT_MASTER_DATABASE),
        help="Existing TikTok master registry (opened query-only).",
    )
    parser.add_argument(
        "--output-root",
        default=os.fspath(DEFAULT_OUTPUT_ROOT),
        help="Isolated SONIC AUDIT artifact root.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="Freeze and execute a new known-post pilot.")
    run.add_argument("--project", required=True, type=_safe_project)
    run.add_argument("--creator", required=True)
    run.add_argument("--posts", required=True, type=_positive_posts)
    run.add_argument(
        "--authorize-transient-audio",
        action="store_true",
        help="Required explicit authorization for bounded transient media/audio use.",
    )
    run.add_argument("--expected-account", default="")
    _add_mirelo_options(run)
    run.add_argument(
        "--exclude-run-id",
        action="append",
        default=[],
        type=_run_id,
        help=(
            "Completed same-creator SONIC AUDIT run to exclude; repeat for "
            "multiple prior frozen sets."
        ),
    )

    run_plan_batch = commands.add_parser(
        "run-plan-batch",
        help="Execute one exact, hash-bound corpus-plan batch as a SONIC run.",
    )
    run_plan_batch.add_argument("--project", required=True, type=_safe_project)
    run_plan_batch.add_argument("--plan-file", required=True)
    run_plan_batch.add_argument("--batch-id", required=True, type=_batch_id)
    run_plan_batch.add_argument(
        "--authorize-transient-audio",
        action="store_true",
        help="Required explicit authorization for this one frozen batch run.",
    )
    run_plan_batch.add_argument("--expected-account", default="")
    _add_mirelo_options(run_plan_batch)

    resume = commands.add_parser("resume", help="Resume pending frozen posts.")
    resume.add_argument("--run-id", required=True, type=_run_id)
    resume.add_argument("--expected-account", default="")

    status = commands.add_parser("status", help="Read one local run without a browser.")
    status.add_argument("--run-id", required=True, type=_run_id)

    validate = commands.add_parser(
        "validate",
        help="Build a hash-bound offline statistical validation artifact.",
    )
    validate.add_argument("--run-id", required=True, type=_run_id)

    validate_suite = commands.add_parser(
        "validate-suite",
        help="Combine non-overlapping complete runs in one offline validation.",
    )
    validate_suite.add_argument(
        "--run-id",
        action="append",
        required=True,
        type=_run_id,
        help="Complete SONIC AUDIT run; repeat in the intended suite order.",
    )
    validate_suite.add_argument("--file", required=True)

    plan_corpus_command = commands.add_parser(
        "plan-corpus",
        help=(
            "Plan a hash-bound positive-pair corpus from resolved music "
            "observations without browser, media, audio, or AI access."
        ),
    )
    plan_scope = plan_corpus_command.add_mutually_exclusive_group(required=True)
    plan_scope.add_argument(
        "--creator",
        action="append",
        help="Known exact creator; repeat to plan across multiple creators.",
    )
    plan_scope.add_argument(
        "--post-id",
        action="append",
        help="Exact known post ID; repeat for an exact feasibility scope.",
    )
    plan_corpus_command.add_argument(
        "--exclude-run-id",
        action="append",
        default=[],
        type=_run_id,
        help="Completed SONIC run whose frozen posts must be excluded; repeatable.",
    )
    plan_corpus_command.add_argument(
        "--min-repeated-groups",
        required=True,
        type=_positive_plan_count,
    )
    plan_corpus_command.add_argument(
        "--min-positive-pairs",
        required=True,
        type=_positive_plan_count,
    )
    plan_corpus_command.add_argument(
        "--max-posts",
        required=True,
        type=_positive_plan_count,
    )
    plan_corpus_command.add_argument(
        "--max-posts-per-reference",
        required=True,
        type=_positive_plan_count,
    )
    plan_corpus_command.add_argument("--file", required=True)

    export = commands.add_parser("export", help="Export hash-verified local artifacts.")
    export.add_argument("--run-id", required=True, type=_run_id)
    export.add_argument("--file", required=True)
    return parser


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _atomic_create_json(path: Path, value: Mapping[str, Any]) -> None:
    """Atomically create a JSON artifact without replacing an existing path."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise SonicAuditError("output file already exists")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ).encode("utf-8") + b"\n"
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # A hard link publishes the fully flushed temporary inode in one
        # directory operation and, unlike os.replace, cannot clobber a target
        # that appeared after the initial collision check.
        os.link(temporary, path)
    except FileExistsError as exc:
        raise SonicAuditError("output file already exists") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _read_json(path: Path, *, name: str) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SonicAuditError(f"invalid {name}: {path}") from exc
    if not isinstance(parsed, dict):
        raise SonicAuditError(f"invalid {name}: expected an object")
    return parsed


def _root(value: str | Path) -> Path:
    root = Path(value).expanduser().resolve()
    if root == Path(root.anchor) or root.parent == root:
        raise SonicAuditError("output root is too broad")
    return root


def _run_directory(output_root: str | Path, run_id: str) -> Path:
    root = _root(output_root)
    if RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise SonicAuditError("invalid run ID")
    run_dir = (root / run_id).resolve()
    try:
        run_dir.relative_to(root)
    except ValueError as exc:
        raise SonicAuditError("run directory escaped output root") from exc
    return run_dir


def _record_path(run_dir: Path, post_id: str) -> Path:
    if re.fullmatch(r"\d+", str(post_id or "")) is None:
        raise SonicAuditError("invalid post ID")
    return run_dir / "records" / f"{post_id}.json"


def _load_run(output_root: str | Path, run_id: str) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    run_dir = _run_directory(output_root, run_id)
    manifest = _read_json(run_dir / "manifest.json", name="manifest")
    state = _read_json(run_dir / "state.json", name="state")
    if not verify_hash(manifest, "manifest_hash"):
        raise SonicAuditError("manifest hash mismatch")
    if not verify_hash(state, "state_hash"):
        raise SonicAuditError("state hash mismatch")
    if manifest.get("run_id") != run_id or state.get("run_id") != run_id:
        raise SonicAuditError("run ID binding mismatch")
    if state.get("manifest_hash") != manifest.get("manifest_hash"):
        raise SonicAuditError("state/manifest binding mismatch")
    _manifest_symbolic_config(manifest)
    _load_mirelo_credit_ledger(run_dir, manifest)
    return run_dir, manifest, state


def _manifest_symbolic_config(
    manifest: Mapping[str, Any],
) -> dict[str, Any] | None:
    config = manifest.get("symbolic_analysis")
    authorization = manifest.get("authorization")
    if not isinstance(authorization, Mapping):
        raise SonicAuditError("manifest authorization is invalid")
    if config is None:
        if authorization.get("third_party_audio_upload") is True or str(
            authorization.get("third_party_provider") or ""
        ):
            raise SonicAuditError("manifest third-party authorization is unbound")
        return None
    if not isinstance(config, Mapping):
        raise SonicAuditError("manifest symbolic analysis config is invalid")
    try:
        validate_symbolic_analysis_config(config)
    except SonicAuditContractError as exc:
        raise SonicAuditError(str(exc)) from exc
    if authorization.get("third_party_audio_upload") is not True or str(
        authorization.get("third_party_provider") or ""
    ) != str(config.get("provider") or ""):
        raise SonicAuditError("manifest symbolic authorization binding mismatch")
    return dict(config)


def _mirelo_credit_ledger_path(run_dir: Path) -> Path:
    return run_dir / MIRELO_CREDIT_LEDGER_FILE


def _initial_mirelo_credit_ledger(manifest: Mapping[str, Any]) -> dict[str, Any]:
    config = _manifest_symbolic_config(manifest)
    if config is None:
        raise SonicAuditError("cannot create a Mirelo ledger for a provider-free run")
    return bind_hash(
        {
            "schema_version": MIRELO_CREDIT_LEDGER_SCHEMA,
            "run_id": str(manifest.get("run_id") or ""),
            "manifest_hash": str(manifest.get("manifest_hash") or ""),
            "symbolic_config_hash": str(config.get("config_hash") or ""),
            "max_run_credits": int(config.get("max_run_credits") or 0),
            "reserved_credit_total": 0,
            "entries": [],
        },
        "ledger_hash",
    )


def _validate_mirelo_credit_ledger(
    ledger: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> None:
    config = _manifest_symbolic_config(manifest)
    if config is None:
        raise SonicAuditError("unexpected Mirelo credit ledger")
    if ledger.get("schema_version") != MIRELO_CREDIT_LEDGER_SCHEMA or not verify_hash(
        ledger, "ledger_hash"
    ):
        raise SonicAuditError("invalid Mirelo credit ledger")
    if ledger.get("run_id") != manifest.get("run_id") or ledger.get(
        "manifest_hash"
    ) != manifest.get("manifest_hash"):
        raise SonicAuditError("Mirelo credit ledger run binding mismatch")
    if ledger.get("symbolic_config_hash") != config.get("config_hash") or int(
        ledger.get("max_run_credits") or 0
    ) != int(config.get("max_run_credits") or 0):
        raise SonicAuditError("Mirelo credit ledger config binding mismatch")
    entries = ledger.get("entries")
    if not isinstance(entries, list):
        raise SonicAuditError("Mirelo credit ledger entries are invalid")
    candidate_ids = {
        str(row.get("post_id") or "")
        for row in manifest.get("candidates", [])
        if isinstance(row, Mapping)
    }
    seen: set[str] = set()
    quoted_total = 0
    for entry in entries:
        if not isinstance(entry, Mapping) or not verify_hash(entry, "entry_hash"):
            raise SonicAuditError("invalid Mirelo credit ledger entry")
        post_id = str(entry.get("post_id") or "")
        if post_id not in candidate_ids or post_id in seen:
            raise SonicAuditError("Mirelo credit ledger post binding mismatch")
        seen.add(post_id)
        if entry.get("status") not in {"reserved", "terminal"}:
            raise SonicAuditError("Mirelo credit ledger status is invalid")
        quoted = entry.get("quoted_credits")
        if isinstance(quoted, bool) or not isinstance(quoted, int) or quoted < 1:
            raise SonicAuditError("Mirelo credit reservation is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", str(entry.get("preflight_hash") or "")) is None:
            raise SonicAuditError("Mirelo preflight hash is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", str(entry.get("audio_sha256") or "")) is None:
            raise SonicAuditError("Mirelo reservation audio binding is invalid")
        result = entry.get("symbolic_result")
        if entry.get("status") == "terminal" and not isinstance(result, Mapping):
            raise SonicAuditError("terminal Mirelo ledger entry has no result")
        if entry.get("status") == "reserved" and result is not None:
            raise SonicAuditError("reserved Mirelo ledger entry has a result")
        quoted_total += quoted
    if quoted_total != int(ledger.get("reserved_credit_total") or 0):
        raise SonicAuditError("Mirelo credit ledger total mismatch")
    if quoted_total > int(config.get("max_run_credits") or 0):
        raise SonicAuditError("Mirelo credit ledger exceeds the frozen budget")


def _load_mirelo_credit_ledger(
    run_dir: Path,
    manifest: Mapping[str, Any],
) -> dict[str, Any] | None:
    config = _manifest_symbolic_config(manifest)
    path = _mirelo_credit_ledger_path(run_dir)
    if config is None:
        if path.exists():
            raise SonicAuditError("provider-free run contains a Mirelo credit ledger")
        return None
    if not path.is_file():
        raise SonicAuditError("Mirelo-enabled run is missing its credit ledger")
    ledger = _read_json(path, name="Mirelo credit ledger")
    _validate_mirelo_credit_ledger(ledger, manifest)
    return ledger


def _mirelo_ledger_entry(
    ledger: Mapping[str, Any], post_id: str
) -> dict[str, Any] | None:
    for entry in ledger.get("entries", []):
        if isinstance(entry, Mapping) and str(entry.get("post_id") or "") == post_id:
            return dict(entry)
    return None


def _reserve_mirelo_credits(
    run_dir: Path,
    manifest: Mapping[str, Any],
    *,
    post_id: str,
    audio_sha256: str,
    quoted_credits: int,
    preflight_hash: str,
) -> bool:
    ledger = _load_mirelo_credit_ledger(run_dir, manifest)
    if ledger is None:
        raise SonicAuditError("Mirelo reservation requested for a provider-free run")
    if _mirelo_ledger_entry(ledger, post_id) is not None:
        return False
    budget = int(ledger["max_run_credits"])
    next_total = int(ledger["reserved_credit_total"]) + int(quoted_credits)
    if next_total > budget:
        return False
    entry = bind_hash(
        {
            "post_id": post_id,
            "audio_sha256": audio_sha256,
            "status": "reserved",
            "quoted_credits": int(quoted_credits),
            "preflight_hash": str(preflight_hash),
            "reserved_at": utc_now(),
            "symbolic_result": None,
        },
        "entry_hash",
    )
    body = dict(ledger)
    body.pop("ledger_hash", None)
    body["entries"] = [*ledger["entries"], entry]
    body["reserved_credit_total"] = next_total
    updated = bind_hash(body, "ledger_hash")
    _validate_mirelo_credit_ledger(updated, manifest)
    _atomic_write_json(_mirelo_credit_ledger_path(run_dir), updated)
    return True


def _complete_mirelo_reservation(
    run_dir: Path,
    manifest: Mapping[str, Any],
    *,
    post_id: str,
    audio_sha256: str,
    symbolic_result: Mapping[str, Any],
) -> None:
    ledger = _load_mirelo_credit_ledger(run_dir, manifest)
    if ledger is None:
        raise SonicAuditError("Mirelo completion requested for a provider-free run")
    entries: list[dict[str, Any]] = []
    matched = False
    for existing in ledger["entries"]:
        entry = dict(existing)
        if str(entry.get("post_id") or "") == post_id:
            if entry.get("status") != "reserved" or entry.get("audio_sha256") != audio_sha256:
                raise SonicAuditError("Mirelo reservation completion binding mismatch")
            entry.pop("entry_hash", None)
            entry["status"] = "terminal"
            entry["symbolic_result"] = dict(symbolic_result)
            entry["terminal_at"] = utc_now()
            entry = bind_hash(entry, "entry_hash")
            matched = True
        entries.append(entry)
    if not matched:
        raise SonicAuditError("Mirelo reservation was not found")
    body = dict(ledger)
    body.pop("ledger_hash", None)
    body["entries"] = entries
    updated = bind_hash(body, "ledger_hash")
    _validate_mirelo_credit_ledger(updated, manifest)
    _atomic_write_json(_mirelo_credit_ledger_path(run_dir), updated)


def _load_exclusion_context(
    output_root: str | Path,
    run_ids: Sequence[str],
    *,
    creator: str,
    master_database: str | Path,
) -> tuple[set[str], dict[str, Any]]:
    """Validate completed local runs and freeze their prior candidate sets."""

    normalized_creator = normalize_creator(creator)
    expected_master = Path(master_database).expanduser().resolve()
    if len(set(run_ids)) != len(run_ids):
        raise SonicAuditError("excluded run IDs must be unique")

    excluded_ids: set[str] = set()
    excluded_runs: list[dict[str, Any]] = []
    for run_id in sorted(run_ids):
        run_dir, manifest, state = _load_run(output_root, run_id)
        if str(state.get("status") or "") != "sonic_complete":
            raise SonicAuditError(f"excluded run is not sonic_complete: {run_id}")
        records = _load_records(run_dir, manifest)
        _validate_state_counters(manifest, state, records)
        if int(state.get("pending") or 0) != 0 or len(records) != int(
            manifest.get("selected_count") or 0
        ):
            raise SonicAuditError(f"excluded run is not terminal: {run_id}")
        try:
            prior_creator = normalize_creator(manifest.get("creator"))
        except SonicAuditContractError as exc:
            raise SonicAuditError(f"excluded run has invalid creator: {run_id}") from exc
        if prior_creator != normalized_creator:
            raise SonicAuditError(f"excluded run creator mismatch: {run_id}")
        prior_master = Path(str(manifest.get("master_database") or "")).expanduser().resolve()
        if prior_master != expected_master:
            raise SonicAuditError(f"excluded run master database mismatch: {run_id}")

        candidates = manifest.get("candidates")
        if not isinstance(candidates, list) or len(candidates) != int(
            manifest.get("selected_count") or 0
        ):
            raise SonicAuditError(f"excluded run candidate binding mismatch: {run_id}")
        prior_ids: list[str] = []
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                raise SonicAuditError(f"excluded run candidate binding mismatch: {run_id}")
            post_id = str(candidate.get("post_id") or "")
            if re.fullmatch(r"\d+", post_id) is None:
                raise SonicAuditError(f"excluded run candidate binding mismatch: {run_id}")
            if normalize_creator(candidate.get("creator_handle")) != normalized_creator:
                raise SonicAuditError(f"excluded run candidate owner mismatch: {run_id}")
            if str(candidate.get("content_type") or "").casefold() != "video":
                raise SonicAuditError(f"excluded run contains a non-video: {run_id}")
            prior_ids.append(post_id)
        if len(prior_ids) != len(set(prior_ids)):
            raise SonicAuditError(f"excluded run candidate IDs are duplicated: {run_id}")
        sorted_ids = sorted(prior_ids)
        excluded_ids.update(sorted_ids)
        excluded_runs.append(
            {
                "run_id": run_id,
                "manifest_hash": str(manifest.get("manifest_hash") or ""),
                "candidate_count": len(sorted_ids),
                "candidate_set_hash": canonical_sha256(sorted_ids),
            }
        )

    sorted_excluded_ids = sorted(excluded_ids)
    return excluded_ids, {
        "excluded_runs": excluded_runs,
        "excluded_candidate_count": len(sorted_excluded_ids),
        "excluded_candidate_set_hash": canonical_sha256(sorted_excluded_ids),
    }


def _load_records(run_dir: Path, manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    record_dir = run_dir / "records"
    if not record_dir.exists():
        return records
    expected_names = {
        f"{candidate.get('post_id')}.json"
        for candidate in manifest.get("candidates", [])
        if isinstance(candidate, Mapping)
    }
    for path in sorted(record_dir.glob("*.json")):
        if path.name not in expected_names:
            raise SonicAuditError("unknown feature record artifact")
        record = _read_json(path, name="feature record")
        validate_feature_record(record, manifest=manifest)
        records.append(record)
    post_ids = [str(row.get("post_id") or "") for row in records]
    if len(post_ids) != len(set(post_ids)):
        raise SonicAuditError("duplicate feature records")
    return records


def _load_corpus_plan_exclusions(
    output_root: str | Path,
    run_ids: Sequence[str],
    *,
    creators: Sequence[str],
    master_database: str | Path,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Verify prior complete runs and return their exact frozen post union."""

    if len(run_ids) != len(set(run_ids)):
        raise SonicAuditError("excluded run IDs must be unique")
    allowed_creators = {normalize_creator(value) for value in creators}
    expected_master = Path(master_database).expanduser().resolve()
    excluded_ids: set[str] = set()
    bindings: list[dict[str, Any]] = []
    for run_id in sorted(run_ids):
        run_dir, manifest, state = _load_run(output_root, run_id)
        if str(state.get("status") or "") != "sonic_complete":
            raise SonicAuditError(f"excluded run is not sonic_complete: {run_id}")
        records = _load_records(run_dir, manifest)
        _validate_state_counters(manifest, state, records)
        if int(state.get("pending") or 0) != 0 or len(records) != int(
            manifest.get("selected_count") or 0
        ):
            raise SonicAuditError(f"excluded run is not terminal: {run_id}")
        creator = normalize_creator(manifest.get("creator"))
        if creator not in allowed_creators:
            raise SonicAuditError(f"excluded run creator is outside plan scope: {run_id}")
        frozen_master = Path(
            str(manifest.get("master_database") or "")
        ).expanduser().resolve()
        if frozen_master != expected_master:
            raise SonicAuditError(f"excluded run master database mismatch: {run_id}")
        candidates = manifest.get("candidates")
        if not isinstance(candidates, list) or len(candidates) != int(
            manifest.get("selected_count") or 0
        ):
            raise SonicAuditError(f"excluded run candidate binding mismatch: {run_id}")
        post_ids: list[str] = []
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                raise SonicAuditError(
                    f"excluded run candidate binding mismatch: {run_id}"
                )
            post_id = str(candidate.get("post_id") or "")
            if re.fullmatch(r"\d+", post_id) is None:
                raise SonicAuditError(
                    f"excluded run candidate binding mismatch: {run_id}"
                )
            if normalize_creator(candidate.get("creator_handle")) != creator:
                raise SonicAuditError(f"excluded run candidate owner mismatch: {run_id}")
            post_ids.append(post_id)
        if len(post_ids) != len(set(post_ids)):
            raise SonicAuditError(f"excluded run candidate IDs are duplicated: {run_id}")
        sorted_ids = sorted(post_ids)
        excluded_ids.update(sorted_ids)
        bindings.append(
            {
                "run_id": run_id,
                "creator_handle": creator,
                "manifest_hash": str(manifest.get("manifest_hash") or ""),
                "state_hash": str(state.get("state_hash") or ""),
                "candidate_count": len(sorted_ids),
                "candidate_set_hash": canonical_sha256(sorted_ids),
            }
        )
    return sorted(excluded_ids), bindings


def _safe_error(exc: BaseException | str) -> str:
    if isinstance(exc, BaseException):
        type_name = type(exc).__name__
        message = str(exc).strip()
    else:
        type_name = "item_error"
        message = str(exc).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", message):
        message = "redacted_error"
    return f"{type_name}:{message}"[:220]


_RAW_MEDIA_SUFFIXES = {
    ".aac",
    ".avi",
    ".flac",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".mid",
    ".midi",
    ".musicxml",
    ".ogg",
    ".opus",
    ".pcm",
    ".wav",
    ".webm",
    ".xml",
}


def _persistent_raw_media_count(run_dir: Path) -> int:
    """Count forbidden durable media by extension inside one fenced run."""

    return sum(
        1
        for path in run_dir.rglob("*")
        if path.is_file() and path.suffix.casefold() in _RAW_MEDIA_SUFFIXES
    )


def _validate_report(
    report: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
) -> None:
    if report.get("schema_version") != REPORT_SCHEMA or not verify_hash(
        report, "report_hash"
    ):
        raise SonicAuditError("invalid SONIC AUDIT report")
    if report.get("run_id") != manifest.get("run_id") or report.get(
        "manifest_hash"
    ) != manifest.get("manifest_hash"):
        raise SonicAuditError("report/manifest binding mismatch")


def _feature_record_set_hash(records: Sequence[Mapping[str, Any]]) -> str:
    return canonical_sha256(
        [
            {
                "post_id": str(record.get("post_id") or ""),
                "feature_record_hash": str(record.get("feature_record_hash") or ""),
            }
            for record in sorted(records, key=lambda item: str(item.get("post_id") or ""))
        ]
    )


def _validate_statistical_artifact(
    artifact: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
    report: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> None:
    if artifact.get("schema_version") != STATISTICAL_VALIDATION_SCHEMA or not verify_hash(
        artifact, "statistical_validation_hash"
    ):
        raise SonicAuditError("invalid SONIC AUDIT statistical validation artifact")
    if artifact.get("run_id") != manifest.get("run_id") or artifact.get(
        "manifest_hash"
    ) != manifest.get("manifest_hash"):
        raise SonicAuditError("statistical validation/manifest binding mismatch")
    if artifact.get("source_report_hash") != report.get("report_hash"):
        raise SonicAuditError("statistical validation/report binding mismatch")
    if artifact.get("feature_record_set_hash") != _feature_record_set_hash(records):
        raise SonicAuditError("statistical validation/feature-set binding mismatch")
    evaluation = artifact.get("evaluation")
    if not isinstance(evaluation, Mapping) or evaluation.get(
        "schema_version"
    ) != "sonic-statistical-validation-v1":
        raise SonicAuditError("invalid SONIC AUDIT statistical evaluation")


def _feature_contract(
    feature_inputs: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
) -> dict[str, Any]:
    contracts: set[str] = set()
    for feature in feature_inputs:
        schema = str(feature.get("schema_version") or "")
        algorithm = str(feature.get("algorithm_version") or "")
        config = feature.get("config")
        if not schema or not algorithm or not isinstance(config, Mapping):
            raise SonicAuditError(
                f"suite run has an incomplete feature contract: {run_id}"
            )
        contracts.add(
            canonical_json(
                {
                    "schema_version": schema,
                    "algorithm_version": algorithm,
                    "config_hash": canonical_sha256(config),
                }
            )
        )
    if not contracts:
        raise SonicAuditError(
            f"suite run has no completed feature records: {run_id}"
        )
    if len(contracts) != 1:
        raise SonicAuditError(f"suite run has mixed feature contracts: {run_id}")
    contract = json.loads(next(iter(contracts)))
    return contract


def _suite_candidate_ids(
    manifest: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> list[str]:
    candidates = manifest.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != int(
        manifest.get("selected_count") or 0
    ):
        raise SonicAuditError("suite run candidate binding mismatch")
    candidate_ids: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise SonicAuditError("suite run candidate binding mismatch")
        post_id = str(candidate.get("post_id") or "")
        if re.fullmatch(r"\d+", post_id) is None:
            raise SonicAuditError("suite run candidate binding mismatch")
        candidate_ids.append(post_id)
    if len(candidate_ids) != len(set(candidate_ids)):
        raise SonicAuditError("suite run candidate IDs are duplicated")
    record_ids = {str(record.get("post_id") or "") for record in records}
    if set(candidate_ids) != record_ids:
        raise SonicAuditError("suite run record/candidate binding mismatch")
    return sorted(candidate_ids)


def _validate_statistical_suite_artifact(
    artifact: Mapping[str, Any],
    *,
    ordered_run_ids: Sequence[str],
    source_run_bindings: Sequence[Mapping[str, Any]],
    feature_contract: Mapping[str, Any],
    combined_feature_set_hash: str,
) -> None:
    if artifact.get("schema_version") != STATISTICAL_VALIDATION_SUITE_SCHEMA or not verify_hash(
        artifact,
        "statistical_validation_suite_hash",
    ):
        raise SonicAuditError("invalid SONIC AUDIT statistical validation suite")
    if artifact.get("ordered_run_ids") != list(ordered_run_ids):
        raise SonicAuditError("statistical validation suite run-order mismatch")
    if canonical_json(artifact.get("source_run_bindings")) != canonical_json(
        list(source_run_bindings)
    ):
        raise SonicAuditError("statistical validation suite source binding mismatch")
    if canonical_json(artifact.get("feature_contract")) != canonical_json(
        dict(feature_contract)
    ):
        raise SonicAuditError("statistical validation suite feature contract mismatch")
    if artifact.get("combined_feature_set_hash") != combined_feature_set_hash:
        raise SonicAuditError("statistical validation suite feature-set mismatch")
    evaluation = artifact.get("evaluation")
    if not isinstance(evaluation, Mapping) or evaluation.get(
        "schema_version"
    ) != "sonic-statistical-validation-v1":
        raise SonicAuditError("invalid SONIC AUDIT suite statistical evaluation")


def _validate_state_counters(
    manifest: Mapping[str, Any],
    state: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> None:
    derived = _state_for_records(
        manifest,
        records,
        status=str(state.get("status") or ""),
        last_error=str(state.get("last_error") or ""),
    )
    for key in ("selected", "completed", "unavailable", "pending"):
        if int(derived[key]) != int(state.get(key) or 0):
            raise SonicAuditError("state counters do not match feature records")


def _state_for_records(
    manifest: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    *,
    status: str,
    last_error: str = "",
) -> dict[str, Any]:
    completed = sum(row.get("status") == "completed" for row in records)
    unavailable = sum(row.get("status") == "unavailable" for row in records)
    observed_accounts = {
        normalize_creator((row.get("transport") or {}).get("provenance", {}).get("account_handle"))
        for row in records
        if isinstance(row.get("transport"), Mapping)
        and isinstance(row["transport"].get("provenance"), Mapping)
        and str(row["transport"]["provenance"].get("account_handle") or "").strip()
    }
    if len(observed_accounts) > 1:
        raise SonicAuditError("feature records bind more than one active TikTok account")
    observed_account = next(iter(observed_accounts), "")
    selected = int(manifest.get("selected_count") or 0)
    return bind_hash(
        {
            "schema_version": "tiktok-sonic-audit-state-v1",
            "run_id": str(manifest.get("run_id") or ""),
            "manifest_hash": str(manifest.get("manifest_hash") or ""),
            "status": status,
            "selected": selected,
            "completed": completed,
            "unavailable": unavailable,
            "pending": max(0, selected - len(records)),
            "observed_account": observed_account,
            "last_error": last_error,
            "updated_at": utc_now(),
        },
        "state_hash",
    )


def _candidate_index(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    candidates = manifest.get("candidates")
    if not isinstance(candidates, list):
        raise SonicAuditError("manifest candidates are invalid")
    result = {
        str(row.get("post_id") or ""): dict(row)
        for row in candidates
        if isinstance(row, Mapping)
    }
    if len(result) != len(candidates):
        raise SonicAuditError("manifest candidate identities are invalid")
    if manifest.get("selection_method") == "positive-pair-corpus-plan-batch-v1":
        for row in result.values():
            frozen = row.get("plan_candidate")
            if not isinstance(frozen, Mapping) or not verify_hash(
                frozen, "candidate_hash"
            ):
                raise SonicAuditError("plan-bound manifest candidate hash is invalid")
            if str(row.get("plan_candidate_hash") or "") != str(
                frozen.get("candidate_hash") or ""
            ):
                raise SonicAuditError("plan-bound candidate hash binding mismatch")
            for key in (
                "post_id",
                "canonical_url",
                "creator_handle",
                "content_type",
                "base_snapshot_id",
                "base_evidence_hash",
                "music_observation_id",
                "music_observation_hash",
                "music_evidence_hash",
                "tt2dsp_resolution_hash",
                "reference_provider",
                "reference_storefront",
                "reference_track_id",
                "reference_label",
                "reference_title",
                "reference_artist",
                "group_id",
                "batch_id",
                "batch_position",
                "selection_position",
            ):
                if row.get(key) != frozen.get(key):
                    raise SonicAuditError(
                        f"plan-bound candidate mapping mismatch: {key}"
                    )
    return result


def _write_terminal_record(
    run_dir: Path,
    manifest: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    status: str,
    transport: Mapping[str, Any] | None = None,
    features: Mapping[str, Any] | None = None,
    symbolic_features: Mapping[str, Any] | None = None,
    error: str = "",
) -> dict[str, Any]:
    reference = {
        key: candidate.get(key)
        for key in (
            "reference_kind",
            "reference_id",
            "reference_label",
            "reference_title",
            "reference_artist",
            "reference_basis",
            "reference_provider",
            "reference_storefront",
            "reference_track_id",
            "reference_group_size",
            "selection_bucket",
            "music_observation_id",
            "music_observation_hash",
            "music_evidence_hash",
            "tt2dsp_resolution_hash",
            "platform_music_id",
            "platform_music_title",
            "platform_music_author",
            "platform_music_original",
        )
    }
    selection_provenance: dict[str, Any] = {}
    if candidate.get("corpus_plan_hash"):
        selection_provenance = {
            key: candidate.get(key)
            for key in (
                "corpus_plan_hash",
                "corpus_batch_id",
                "corpus_batch_hash",
                "plan_candidate_hash",
                "group_id",
                "corpus_group_hash",
                "batch_position",
                "selection_position",
            )
        }
    record_body: dict[str, Any] = {
        "run_id": manifest["run_id"],
        "manifest_hash": manifest["manifest_hash"],
        "post_id": candidate["post_id"],
        "creator_handle": candidate["creator_handle"],
        "base_snapshot_id": candidate["base_snapshot_id"],
        "base_evidence_hash": candidate["base_evidence_hash"],
        "status": status,
        "observed_at": utc_now(),
        "reference": reference,
        "selection_provenance": selection_provenance,
        "transport": dict(transport or {}),
        "features": dict(features or {}),
        "error": error,
        "raw_media_retained": False,
        "decoded_audio_retained": False,
    }
    if symbolic_features is not None:
        record_body["symbolic_features"] = dict(symbolic_features)
    record = bind_feature_record(record_body)
    validate_feature_record(record, manifest=manifest)
    path = _record_path(run_dir, str(candidate["post_id"]))
    if path.exists():
        existing = _read_json(path, name="feature record")
        if canonical_json(existing) != canonical_json(record):
            raise SonicAuditError("attempted to replace an immutable feature record")
        return existing
    _atomic_write_json(path, record)
    return record


def _feature_inputs(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    from sonic_audit.features import validate_feature_record as validate_sonic_feature

    inputs: list[dict[str, Any]] = []
    for record in records:
        if record.get("status") != "completed":
            continue
        features = record.get("features")
        reference = record.get("reference")
        if not isinstance(features, Mapping) or not isinstance(reference, Mapping):
            raise SonicAuditError("completed record is missing features/reference")
        validate_sonic_feature(features)
        item = dict(features)
        item["post_id"] = str(record.get("post_id") or "")
        item["reference_label"] = str(
            reference.get("reference_label") or reference.get("reference_id") or ""
        )
        item["reference_kind"] = str(reference.get("reference_kind") or "")
        inputs.append(item)
    return inputs


def _build_report(
    manifest: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    *,
    run_timing: Mapping[str, Any] | None = None,
    persistent_raw_media_count: int = 0,
) -> dict[str, Any]:
    from sonic_audit.features import (
        build_similarity_report,
        evaluate_clustering_stability,
    )

    completed = [row for row in records if row.get("status") == "completed"]
    unavailable = [row for row in records if row.get("status") == "unavailable"]
    feature_inputs = _feature_inputs(records)
    cluster_inputs = [
        item
        for item in feature_inputs
        if isinstance(item.get("quality"), Mapping)
        and item["quality"].get("status") == "usable"
    ]
    comparison_started = time.monotonic()
    similarity_started = time.monotonic()
    evaluation = build_similarity_report(feature_inputs)
    similarity_ms = round((time.monotonic() - similarity_started) * 1000.0, 3)
    cluster_started = time.monotonic()
    if len(cluster_inputs) >= 3:
        cluster_count = min(
            8,
            len(cluster_inputs) - 1,
            max(2, int(round(math.sqrt(len(cluster_inputs))))),
        )
        cluster_stability = evaluate_clustering_stability(
            cluster_inputs,
            n_clusters=cluster_count,
        )
    else:
        cluster_stability = {
            "schema_version": "sonic-cluster-stability-v1",
            "status": "not_evaluable",
            "reason": "fewer_than_three_completed_audio_records",
            "record_count": len(cluster_inputs),
            "quality_excluded_record_count": len(feature_inputs) - len(cluster_inputs),
        }
    cluster_ms = round((time.monotonic() - cluster_started) * 1000.0, 3)
    comparison_timing = {
        "similarity_ms": similarity_ms,
        "cluster_stability_ms": cluster_ms,
        "total_comparison_ms": round(
            (time.monotonic() - comparison_started) * 1000.0,
            3,
        ),
    }
    timing_fields = (
        "html_fetch_ms",
        "media_download_ms",
        "inspect_transcode_ms",
        "processor_ms",
        "total_item_ms",
    )
    item_timing: dict[str, dict[str, float | int]] = {}
    for field in timing_fields:
        values = [
            float(row["transport"]["timing"][field])
            for row in records
            if isinstance(row.get("transport"), Mapping)
            and isinstance(row["transport"].get("timing"), Mapping)
            and isinstance(row["transport"]["timing"].get(field), (int, float))
            and not isinstance(row["transport"]["timing"].get(field), bool)
        ]
        item_timing[field] = {
            "sample_size": len(values),
            "total_ms": round(sum(values), 3),
            "mean_ms": round(sum(values) / len(values), 3) if values else 0.0,
        }
    bucket_counts: dict[str, int] = {}
    for candidate in manifest.get("candidates", []):
        if isinstance(candidate, Mapping):
            bucket = str(candidate.get("selection_bucket") or "unclassified")
            bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
    feature_contracts = sorted(
        {
            canonical_json(
                {
                    "schema_version": features.get("schema_version"),
                    "algorithm_version": features.get("algorithm_version"),
                    "config_hash": canonical_sha256(features.get("config", {})),
                }
            )
            for row in completed
            for features in [row.get("features")]
            if isinstance(features, Mapping)
        }
    )
    body = {
        "schema_version": REPORT_SCHEMA,
        "run_id": manifest["run_id"],
        "manifest_hash": manifest["manifest_hash"],
        "generated_at": utc_now(),
        "workflow": "sonic_audit",
        "sample_design": manifest["selection_method"],
        "realized_selection_bucket_counts": bucket_counts,
        "requested": manifest["requested_count"],
        "completed_audio": len(completed),
        "unavailable_audio": len(unavailable),
        "coverage_percent": round(
            100.0 * len(completed) / max(1, int(manifest["requested_count"])), 1
        ),
        "evaluation": evaluation,
        "cluster_stability": cluster_stability,
        "comparison_timing": comparison_timing,
        "item_timing_aggregates": item_timing,
        "feature_contracts": [json.loads(value) for value in feature_contracts],
        "persistent_raw_media_count": int(persistent_raw_media_count),
        "retention_verified": all(
            row.get("raw_media_retained") is False
            and row.get("decoded_audio_retained") is False
            for row in records
        )
        and int(persistent_raw_media_count) == 0,
        "transport_cleanup_verified": all(
            isinstance(row.get("transport"), Mapping)
            and row["transport"].get("cleanup_before_checkpoint") is True
            for row in records
        ),
        "run_timing": dict(run_timing or {}),
        "limitations": [
            "This is a stratified validation pilot, not a random or full-portfolio estimate.",
            "Apple track IDs are external reference labels, not labels inferred by the feature engine.",
            "Unresolved clusters are internal similarity groups and do not reveal artist or title.",
            "Local feature similarity does not prove licensing, creative intent, or causal engagement impact.",
            "Raw media and decoded audio were transient and are not present in this report.",
        ],
        "publication_eligible": False,
        "ai_analysis_performed": False,
    }
    return bind_hash(body, "report_hash")


def _status_packet(
    run_dir: Path,
    manifest: Mapping[str, Any],
    state: Mapping[str, Any],
) -> dict[str, Any]:
    report_path = run_dir / "report.json"
    report = _read_json(report_path, name="report") if report_path.exists() else {}
    if report:
        _validate_report(report, manifest=manifest)
    validation_path = run_dir / STATISTICAL_VALIDATION_FILE
    statistical_validation_hash = ""
    if validation_path.exists():
        records = _load_records(run_dir, manifest)
        artifact = _read_json(validation_path, name="statistical validation")
        _validate_statistical_artifact(
            artifact,
            manifest=manifest,
            report=report,
            records=records,
        )
        statistical_validation_hash = str(
            artifact.get("statistical_validation_hash") or ""
        )
    return {
        "run_id": manifest["run_id"],
        "workflow": "sonic_audit",
        "project": manifest["project"],
        "creator": manifest["creator"],
        "status": state["status"],
        "selected": state["selected"],
        "completed": state["completed"],
        "unavailable": state["unavailable"],
        "pending": state["pending"],
        "last_error": state["last_error"],
        "observed_account": state.get("observed_account", ""),
        "run_directory": os.fspath(run_dir),
        "report_hash": report.get("report_hash", ""),
        "statistical_validation_hash": statistical_validation_hash,
        "raw_media_persisted": False,
        "decoded_audio_persisted": False,
        "ai_analysis_performed": False,
    }


async def _execute(
    run_dir: Path,
    manifest: Mapping[str, Any],
    *,
    expected_account: str = "",
    transport: Any = None,
) -> dict[str, Any]:
    from sonic_audit.features import SonicFeatureError, analyze_audio
    from sonic_audio_transport import (
        AcquisitionLimits,
        SonicAudioTransport,
        SonicTransportError,
    )

    records = _load_records(run_dir, manifest)
    existing_accounts = {
        normalize_creator((row.get("transport") or {}).get("provenance", {}).get("account_handle"))
        for row in records
        if isinstance(row.get("transport"), Mapping)
        and isinstance(row["transport"].get("provenance"), Mapping)
        and str(row["transport"]["provenance"].get("account_handle") or "").strip()
    }
    if len(existing_accounts) > 1:
        raise SonicAuditError("run contains mixed active TikTok accounts")
    bound_account = next(iter(existing_accounts), "")
    requested_account = (
        normalize_creator(expected_account) if str(expected_account or "").strip() else ""
    )
    if bound_account and requested_account and bound_account != requested_account:
        raise SonicAuditError("resume active-account binding mismatch")
    effective_expected_account = bound_account or requested_account
    existing_ids = {str(row.get("post_id") or "") for row in records}
    candidates_by_id = _candidate_index(manifest)
    pending = [
        candidate
        for post_id, candidate in candidates_by_id.items()
        if post_id not in existing_ids
    ]
    runner = transport
    if runner is None:
        consumed_duration = sum(
            float((row.get("transport") or {}).get("duration_seconds") or 0.0)
            for row in records
            if isinstance(row.get("transport"), Mapping)
        )
        remaining_duration = 3600.0 - consumed_duration
        if pending and remaining_duration < 0.1:
            state = _state_for_records(
                manifest,
                records,
                status="sonic_incomplete",
                last_error="SonicAuditError:pilot_total_duration_limit_exhausted",
            )
            _atomic_write_json(run_dir / "state.json", state)
            raise SonicAuditError("pilot_total_duration_limit_exhausted")
        runner = SonicAudioTransport(
            limits=AcquisitionLimits(
                max_total_duration_seconds=max(0.1, remaining_duration)
            )
        )
    if not pending:
        saved_state = _read_json(run_dir / "state.json", name="state")
        if not runner.cleanup_stale_run(str(manifest["run_id"])) or not runner.verify_no_run_residue(
            str(manifest["run_id"])
        ):
            state = _state_for_records(
                manifest,
                records,
                status="sonic_incomplete",
                last_error="SonicAuditError:transport_temporary_cleanup_unverified",
            )
            _atomic_write_json(run_dir / "state.json", state)
            raise SonicAuditError("transport_temporary_cleanup_unverified")
        existing_report_path = run_dir / "report.json"
        if saved_state.get("status") == "sonic_complete" and existing_report_path.exists():
            existing_report = _read_json(existing_report_path, name="report")
            _validate_report(existing_report, manifest=manifest)
            _validate_state_counters(manifest, saved_state, records)
            return _status_packet(run_dir, manifest, saved_state)
        report = _build_report(
            manifest,
            records,
            persistent_raw_media_count=_persistent_raw_media_count(run_dir),
        )
        _atomic_write_json(run_dir / "report.json", report)
        state = _state_for_records(manifest, records, status="sonic_complete")
        _atomic_write_json(run_dir / "state.json", state)
        return _status_packet(run_dir, manifest, state)

    state = _state_for_records(manifest, records, status="running")
    _atomic_write_json(run_dir / "state.json", state)

    def checkpoint_state() -> None:
        current = _load_records(run_dir, manifest)
        next_state = _state_for_records(manifest, current, status="running")
        _atomic_write_json(
            run_dir / "state.json",
            next_state,
        )
        _emit_event(
            "sonic_item_checkpoint",
            run_id=manifest["run_id"],
            terminal=next_state["completed"] + next_state["unavailable"],
            selected=next_state["selected"],
            completed=next_state["completed"],
            unavailable=next_state["unavailable"],
        )

    def processor(item: Any) -> dict[str, Any]:
        try:
            return analyze_audio(
                item.audio_path,
                post_id=str(item.post_id),
                audio_sha256=str(item.audio_sha256),
            )
        except SonicFeatureError as exc:
            recoverable_input_codes = {
                "audio_unreadable",
                "audio_empty",
                "audio_too_short",
                "audio_decode_failed",
            }
            if exc.code in recoverable_input_codes:
                raise SonicTransportError(f"feature_{exc.code}") from exc
            raise

    def error_processor(candidate_value: Any, error_value: Any) -> dict[str, Any]:
        del candidate_value
        return {"error_code": _safe_error(error_value)}

    def receipt_processor(receipt: Any) -> None:
        nonlocal bound_account
        candidate = candidates_by_id.get(str(receipt.post_id))
        if candidate is None:
            raise SonicAuditError("transport receipt referenced an unknown post")
        transport_record = {
            "schema_version": "tiktok-sonic-transport-receipt-v1",
            "duration_seconds": float(receipt.duration_seconds),
            "source_byte_count": int(receipt.source_byte_count),
            "audio_byte_count": int(receipt.audio_byte_count),
            "source_sha256": str(receipt.source_sha256),
            "audio_sha256": str(receipt.audio_sha256),
            "provenance": dict(receipt.provenance),
            "timing": dict(receipt.timing),
            "cleanup_before_checkpoint": True,
        }
        receipt_account = normalize_creator(
            transport_record["provenance"].get("account_handle")
        )
        if bound_account and receipt_account != bound_account:
            raise SonicAuditError("transport receipt active-account binding mismatch")
        if not bound_account:
            bound_account = receipt_account
        if receipt.status == "completed":
            if not isinstance(receipt.derived_result, Mapping):
                raise SonicAuditError("completed transport receipt has no features")
            _write_terminal_record(
                run_dir,
                manifest,
                candidate,
                status="completed",
                transport=transport_record,
                features=receipt.derived_result,
            )
        elif receipt.status == "unavailable":
            _write_terminal_record(
                run_dir,
                manifest,
                candidate,
                status="unavailable",
                transport=transport_record,
                error=_safe_error(receipt.error_code),
            )
        else:
            raise SonicAuditError("transport receipt status is not terminal")
        checkpoint_state()

    try:
        await runner.process_candidates(
            run_id=str(manifest["run_id"]),
            candidates=pending,
            processor=processor,
            expected_account=effective_expected_account,
            continue_on_item_error=True,
            error_processor=error_processor,
            receipt_processor=receipt_processor,
        )
        if getattr(runner, "last_cleanup_verified", False) is not True:
            raise SonicAuditError("transport temporary cleanup was not verified")
    except BaseException as exc:
        current = _load_records(run_dir, manifest)
        state = _state_for_records(
            manifest,
            current,
            status="sonic_incomplete",
            last_error=_safe_error(exc),
        )
        _atomic_write_json(run_dir / "state.json", state)
        raise
    records = _load_records(run_dir, manifest)
    terminal_status = (
        "sonic_complete"
        if len(records) == int(manifest["selected_count"])
        else "sonic_incomplete"
    )
    report = _build_report(
        manifest,
        records,
        run_timing=dict(getattr(runner, "last_run_timing", {}) or {}),
        persistent_raw_media_count=_persistent_raw_media_count(run_dir),
    )
    _atomic_write_json(run_dir / "report.json", report)
    state = _state_for_records(manifest, records, status=terminal_status)
    _atomic_write_json(run_dir / "state.json", state)
    return _status_packet(run_dir, manifest, state)


def _assert_plan_batch_not_registered(
    output_root: str | Path,
    *,
    plan_hash: str,
    batch_id: str,
) -> None:
    """Reject a second run for a plan batch; resume the existing run instead."""

    root = _root(output_root)
    if not root.exists():
        return
    for run_dir in root.glob("sonic_*"):
        if not run_dir.is_dir() or RUN_ID_PATTERN.fullmatch(run_dir.name) is None:
            continue
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = _read_json(manifest_path, name="manifest")
        except SonicAuditError:
            continue
        if not verify_hash(manifest, "manifest_hash"):
            continue
        context = manifest.get("selection_context")
        if not isinstance(context, Mapping):
            continue
        if (
            str(context.get("corpus_plan_hash") or "") == plan_hash
            and str(context.get("batch_id") or "") == batch_id
        ):
            raise SonicAuditError(
                f"corpus plan batch already has run {manifest.get('run_id')}; resume it"
            )


def _new_plan_batch_run(args: argparse.Namespace) -> dict[str, Any]:
    """Create and execute one exact corpus-plan batch after offline gates."""

    _reject_unimplemented_mirelo_request(args)
    if not args.authorize_transient_audio:
        raise SonicAuditError("--authorize-transient-audio is required")
    symbolic_config = _mirelo_config_from_args(args)
    if symbolic_config is not None:
        _load_mirelo_api_key()
    plan_path = Path(args.plan_file).expanduser().resolve()
    plan = _read_json(plan_path, name="SONIC corpus plan")
    try:
        binding = validate_corpus_plan_batch(
            plan,
            batch_id=args.batch_id,
            master_database=Path(args.master_database).expanduser().resolve(),
        )
    except CorpusPlanningError as exc:
        reason = re.sub(r"[^A-Za-z0-9]+", "_", str(exc)).strip("_").lower()
        raise SonicAuditError(
            f"corpus_plan_batch_invalid:{reason[:150]}"
        ) from None

    batch = binding["batch"]
    groups_by_id = binding["groups_by_id"]
    selected: list[dict[str, Any]] = []
    for frozen in binding["candidates"]:
        group = groups_by_id.get(str(frozen.get("group_id") or ""))
        if not isinstance(group, Mapping):
            raise SonicAuditError("corpus_plan_batch_invalid:unknown_group")
        item = dict(frozen)
        item["plan_candidate"] = dict(frozen)
        item["plan_candidate_hash"] = str(frozen.get("candidate_hash") or "")
        item.pop("candidate_hash", None)
        item.update(
            {
                "reference_kind": "apple_track_id",
                "reference_id": str(frozen.get("reference_track_id") or ""),
                "reference_label": str(frozen.get("reference_label") or ""),
                "reference_title": str(frozen.get("reference_title") or ""),
                "reference_artist": str(frozen.get("reference_artist") or ""),
                "reference_basis": "tt2dsp_exact_apple_id_resolution",
                "reference_group_size": int(group.get("selected_count") or 0),
                "selection_bucket": "corpus_repeated_catalog_reference",
                "corpus_plan_hash": binding["plan_hash"],
                "corpus_batch_id": str(batch.get("batch_id") or ""),
                "corpus_batch_hash": str(batch.get("batch_hash") or ""),
                "corpus_group_hash": str(group.get("group_hash") or ""),
            }
        )
        selected.append(item)
    if not selected:
        raise SonicAuditError("corpus_plan_batch_invalid:empty_batch")

    creator = normalize_creator(batch.get("creator_handle"))
    frozen_expected_account = (
        normalize_creator(args.expected_account)
        if str(args.expected_account or "").strip()
        else ""
    )
    _assert_plan_batch_not_registered(
        args.output_root,
        plan_hash=binding["plan_hash"],
        batch_id=str(batch.get("batch_id") or ""),
    )
    run_id = f"sonic_{uuid.uuid4().hex[:16]}"
    run_dir = _run_directory(args.output_root, run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    selection_context = {
        "corpus_plan_schema": CORPUS_PLAN_SCHEMA,
        "corpus_plan_hash": binding["plan_hash"],
        "corpus_source_scope_hash": str(
            (plan.get("source_scope") or {}).get("scope_hash") or ""
        ),
        "corpus_candidate_set_hash": str(plan.get("candidate_set_hash") or ""),
        "batch_id": str(batch.get("batch_id") or ""),
        "batch_hash": str(batch.get("batch_hash") or ""),
        "batch_candidate_set_hash": str(batch.get("candidate_set_hash") or ""),
        "group_hashes": list(batch.get("group_hashes") or []),
        "master_bindings_revalidated_before_browser": True,
        "expected_account": frozen_expected_account,
        "authorization_scope": {
            "corpus_plan_hash": binding["plan_hash"],
            "batch_id": str(batch.get("batch_id") or ""),
            "batch_hash": str(batch.get("batch_hash") or ""),
            "batch_candidate_set_hash": str(
                batch.get("candidate_set_hash") or ""
            ),
            "creator": creator,
            "selected_count": len(selected),
        },
    }
    if symbolic_config is not None:
        selection_context["authorization_scope"].update(
            {
                "third_party_audio_provider": "mirelo",
                "symbolic_config_hash": symbolic_config["config_hash"],
                "max_run_credits": symbolic_config["max_run_credits"],
            }
        )
    manifest = build_run_manifest(
        run_id=run_id,
        project=args.project,
        creator=creator,
        requested_count=len(selected),
        master_database=binding["master_database"],
        candidates=selected,
        transient_audio_authorized=True,
        selection_method="positive-pair-corpus-plan-batch-v1",
        selection_context=selection_context,
        third_party_audio_upload_authorized=symbolic_config is not None,
        symbolic_analysis=symbolic_config,
    )
    _atomic_write_json(run_dir / "manifest.json", manifest)
    if symbolic_config is not None:
        _atomic_write_json(
            _mirelo_credit_ledger_path(run_dir),
            _initial_mirelo_credit_ledger(manifest),
        )
    _atomic_write_json(run_dir / "state.json", build_initial_state(manifest))
    _emit_event(
        "sonic_plan_batch_run_created",
        run_id=run_id,
        selected=len(selected),
        creator=creator,
        corpus_plan_hash=binding["plan_hash"],
        batch_id=batch["batch_id"],
        run_directory=os.fspath(run_dir),
    )
    return asyncio.run(
        _execute(
            run_dir,
            manifest,
            expected_account=frozen_expected_account,
        )
    )


def _new_run(args: argparse.Namespace) -> dict[str, Any]:
    _reject_unimplemented_mirelo_request(args)
    if not args.authorize_transient_audio:
        raise SonicAuditError("--authorize-transient-audio is required")
    symbolic_config = _mirelo_config_from_args(args)
    if symbolic_config is not None:
        _load_mirelo_api_key()
    creator = normalize_creator(args.creator)
    master_database = Path(args.master_database).expanduser().resolve()
    candidates = load_creator_registry_candidates(master_database, creator)
    exclude_run_ids = list(getattr(args, "exclude_run_id", ()) or ())
    selection_method: str | None = None
    selection_context: Mapping[str, Any] | None = None
    if exclude_run_ids:
        excluded_ids, selection_context = _load_exclusion_context(
            args.output_root,
            exclude_run_ids,
            creator=creator,
            master_database=master_database,
        )
        selected = select_non_overlapping_candidates(
            candidates,
            args.posts,
            creator=creator,
            excluded_post_ids=excluded_ids,
        )
        selection_method = "externally-labelled-first-non-overlapping-v1"
    else:
        selected = select_pilot_candidates(candidates, args.posts, creator=creator)
    run_id = f"sonic_{uuid.uuid4().hex[:16]}"
    run_dir = _run_directory(args.output_root, run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    manifest = build_run_manifest(
        run_id=run_id,
        project=args.project,
        creator=creator,
        requested_count=args.posts,
        master_database=master_database,
        candidates=selected,
        transient_audio_authorized=True,
        selection_method=selection_method,
        selection_context=selection_context,
        third_party_audio_upload_authorized=symbolic_config is not None,
        symbolic_analysis=symbolic_config,
    )
    _atomic_write_json(run_dir / "manifest.json", manifest)
    if symbolic_config is not None:
        _atomic_write_json(
            _mirelo_credit_ledger_path(run_dir),
            _initial_mirelo_credit_ledger(manifest),
        )
    _atomic_write_json(run_dir / "state.json", build_initial_state(manifest))
    _emit_event(
        "sonic_run_created",
        run_id=run_id,
        selected=len(selected),
        creator=creator,
        run_directory=os.fspath(run_dir),
    )
    return asyncio.run(
        _execute(
            run_dir,
            manifest,
            expected_account=args.expected_account,
        )
    )


def _resume(args: argparse.Namespace) -> dict[str, Any]:
    run_dir, manifest, _ = _load_run(args.output_root, args.run_id)
    requested_account = (
        normalize_creator(args.expected_account)
        if str(args.expected_account or "").strip()
        else ""
    )
    context = manifest.get("selection_context")
    frozen_account = (
        normalize_creator(context.get("expected_account"))
        if isinstance(context, Mapping)
        and str(context.get("expected_account") or "").strip()
        else ""
    )
    if frozen_account and requested_account and frozen_account != requested_account:
        raise SonicAuditError("resume expected-account binding mismatch")
    return asyncio.run(
        _execute(
            run_dir,
            manifest,
            expected_account=frozen_account or requested_account,
        )
    )


def _status(args: argparse.Namespace) -> dict[str, Any]:
    run_dir, manifest, state = _load_run(args.output_root, args.run_id)
    records = _load_records(run_dir, manifest)
    _validate_state_counters(manifest, state, records)
    return _status_packet(run_dir, manifest, state)


def _run_statistical_validation(args: argparse.Namespace) -> dict[str, Any]:
    from sonic_audit.evaluation import build_statistical_validation

    run_dir, manifest, state = _load_run(args.output_root, args.run_id)
    records = _load_records(run_dir, manifest)
    _validate_state_counters(manifest, state, records)
    if state.get("status") != "sonic_complete" or int(state.get("pending") or 0):
        raise SonicAuditError("statistical validation requires a complete SONIC AUDIT run")
    report = _read_json(run_dir / "report.json", name="report")
    _validate_report(report, manifest=manifest)
    path = run_dir / STATISTICAL_VALIDATION_FILE
    if path.exists():
        artifact = _read_json(path, name="statistical validation")
        _validate_statistical_artifact(
            artifact,
            manifest=manifest,
            report=report,
            records=records,
        )
    else:
        evaluation = build_statistical_validation(_feature_inputs(records))
        artifact = bind_hash(
            {
                "schema_version": STATISTICAL_VALIDATION_SCHEMA,
                "run_id": manifest["run_id"],
                "manifest_hash": manifest["manifest_hash"],
                "source_report_hash": report["report_hash"],
                "feature_record_set_hash": _feature_record_set_hash(records),
                "evaluation": evaluation,
                "media_access_performed": False,
                "ai_analysis_performed": False,
                "master_registry_mutated": False,
            },
            "statistical_validation_hash",
        )
        _atomic_write_json(path, artifact)
    return {
        "run_id": manifest["run_id"],
        "status": artifact["evaluation"].get("status", ""),
        "file": os.fspath(path),
        "statistical_validation_hash": artifact["statistical_validation_hash"],
        "media_access_performed": False,
        "ai_analysis_performed": False,
    }


def _run_statistical_validation_suite(args: argparse.Namespace) -> dict[str, Any]:
    """Validate multiple frozen runs without browser, media, master, or AI access."""

    from sonic_audit.evaluation import build_statistical_validation

    ordered_run_ids = list(getattr(args, "run_id", ()) or ())
    if len(ordered_run_ids) < 2:
        raise SonicAuditError("validate-suite requires at least two --run-id values")
    if len(ordered_run_ids) != len(set(ordered_run_ids)):
        raise SonicAuditError("validate-suite run IDs must be unique")

    source_run_bindings: list[dict[str, Any]] = []
    source_run_dirs: list[Path] = []
    source_master_paths: list[Path] = []
    combined_feature_inputs: list[dict[str, Any]] = []
    combined_feature_bindings: list[dict[str, Any]] = []
    seen_post_ids: dict[str, str] = {}
    expected_creator = ""
    expected_master: Path | None = None
    expected_contract: dict[str, Any] | None = None

    for run_id in ordered_run_ids:
        run_dir, manifest, state = _load_run(args.output_root, run_id)
        records = _load_records(run_dir, manifest)
        _validate_state_counters(manifest, state, records)
        if manifest.get("workflow") != "sonic_audit":
            raise SonicAuditError(f"suite source is not a SONIC AUDIT run: {run_id}")
        if state.get("status") != "sonic_complete" or int(
            state.get("pending") or 0
        ):
            raise SonicAuditError(f"suite source run is not complete: {run_id}")
        if len(records) != int(manifest.get("selected_count") or 0):
            raise SonicAuditError(f"suite source run is not terminal: {run_id}")

        report = _read_json(run_dir / "report.json", name="report")
        _validate_report(report, manifest=manifest)
        try:
            creator = normalize_creator(manifest.get("creator"))
        except SonicAuditContractError as exc:
            raise SonicAuditError(f"suite source creator is invalid: {run_id}") from exc
        master_value = str(manifest.get("master_database") or "").strip()
        if not master_value:
            raise SonicAuditError(f"suite source master binding is missing: {run_id}")
        master_path = Path(master_value).expanduser().resolve()
        if expected_creator and creator != expected_creator:
            raise SonicAuditError("validate-suite requires the same creator")
        if expected_master is not None and master_path != expected_master:
            raise SonicAuditError("validate-suite requires the same master database")
        expected_creator = expected_creator or creator
        expected_master = expected_master or master_path

        candidate_ids = _suite_candidate_ids(manifest, records)
        for post_id in candidate_ids:
            prior_run = seen_post_ids.get(post_id)
            if prior_run is not None:
                raise SonicAuditError(
                    f"validate-suite post IDs overlap: {prior_run}:{run_id}"
                )
            seen_post_ids[post_id] = run_id

        feature_inputs = _feature_inputs(records)
        contract = _feature_contract(feature_inputs, run_id=run_id)
        if expected_contract is not None and canonical_json(contract) != canonical_json(
            expected_contract
        ):
            raise SonicAuditError(
                "validate-suite requires the same feature schema, algorithm, and config"
            )
        expected_contract = expected_contract or contract
        combined_feature_inputs.extend(feature_inputs)
        for record in sorted(records, key=lambda row: str(row.get("post_id") or "")):
            if record.get("status") != "completed":
                continue
            features = record.get("features")
            if not isinstance(features, Mapping):
                raise SonicAuditError("completed suite record is missing features")
            combined_feature_bindings.append(
                {
                    "run_id": run_id,
                    "post_id": str(record.get("post_id") or ""),
                    "feature_record_hash": str(
                        record.get("feature_record_hash") or ""
                    ),
                    "feature_hash": str(features.get("feature_hash") or ""),
                }
            )

        source_run_bindings.append(
            {
                "run_id": run_id,
                "manifest_hash": str(manifest.get("manifest_hash") or ""),
                "state_hash": str(state.get("state_hash") or ""),
                "report_hash": str(report.get("report_hash") or ""),
                "feature_record_set_hash": _feature_record_set_hash(records),
                "candidate_set_hash": canonical_sha256(candidate_ids),
                "selected_record_count": len(records),
                "completed_feature_count": len(feature_inputs),
            }
        )
        source_run_dirs.append(run_dir)
        source_master_paths.append(master_path)

    if expected_contract is None or expected_master is None:
        raise SonicAuditError("validate-suite has no usable source features")
    combined_feature_set_hash = canonical_sha256(combined_feature_bindings)
    evaluation = build_statistical_validation(combined_feature_inputs)
    if not isinstance(evaluation, Mapping) or evaluation.get(
        "schema_version"
    ) != "sonic-statistical-validation-v1":
        raise SonicAuditError("invalid SONIC AUDIT suite statistical evaluation")
    artifact = bind_hash(
        {
            "schema_version": STATISTICAL_VALIDATION_SUITE_SCHEMA,
            "generated_at": utc_now(),
            "workflow": "sonic_audit_statistical_validation_suite",
            "creator": expected_creator,
            "master_database": os.fspath(expected_master),
            "ordered_run_ids": ordered_run_ids,
            "source_run_bindings": source_run_bindings,
            "source_run_binding_hash": canonical_sha256(source_run_bindings),
            "feature_contract": expected_contract,
            "combined_feature_record_count": len(combined_feature_inputs),
            "combined_feature_set_hash": combined_feature_set_hash,
            "evaluation": dict(evaluation),
            "offline_only": True,
            "browser_access_performed": False,
            "media_access_performed": False,
            "master_registry_read": False,
            "master_registry_mutated": False,
            "ai_analysis_performed": False,
            "publication_eligible": False,
        },
        "statistical_validation_suite_hash",
    )
    _validate_statistical_suite_artifact(
        artifact,
        ordered_run_ids=ordered_run_ids,
        source_run_bindings=source_run_bindings,
        feature_contract=expected_contract,
        combined_feature_set_hash=combined_feature_set_hash,
    )

    target = Path(args.file).expanduser().resolve()
    if target == Path(target.anchor) or target.parent == target:
        raise SonicAuditError("validate-suite output target is too broad")
    for run_dir in source_run_dirs:
        if target == run_dir or run_dir in target.parents:
            raise SonicAuditError("validate-suite output overlaps protected run state")
    protected_master_paths = {
        path
        for master_path in source_master_paths
        for path in (
            master_path,
            Path(os.fspath(master_path) + "-journal"),
            Path(os.fspath(master_path) + "-shm"),
            Path(os.fspath(master_path) + "-wal"),
        )
    }
    if target in protected_master_paths:
        raise SonicAuditError("validate-suite output overlaps protected master state")
    _atomic_create_json(target, artifact)
    return {
        "ordered_run_ids": ordered_run_ids,
        "status": evaluation.get("status", ""),
        "file": os.fspath(target),
        "combined_feature_record_count": len(combined_feature_inputs),
        "statistical_validation_suite_hash": artifact[
            "statistical_validation_suite_hash"
        ],
        "browser_access_performed": False,
        "media_access_performed": False,
        "master_registry_mutated": False,
        "ai_analysis_performed": False,
    }


def _plan_corpus_artifact(args: argparse.Namespace) -> dict[str, Any]:
    creators = list(args.creator or [])
    post_ids = list(args.post_id or [])
    if args.exclude_run_id and not creators:
        raise SonicAuditError("exclude_run_id_requires_creator_scope")
    master_database = Path(args.master_database).expanduser().resolve()
    excluded_post_ids: list[str] = []
    excluded_run_bindings: list[dict[str, Any]] = []
    if args.exclude_run_id:
        excluded_post_ids, excluded_run_bindings = _load_corpus_plan_exclusions(
            args.output_root,
            args.exclude_run_id,
            creators=creators,
            master_database=master_database,
        )
    try:
        artifact = plan_corpus(
            master_database,
            creators=creators,
            post_ids=post_ids,
            min_repeated_groups=args.min_repeated_groups,
            min_positive_pairs=args.min_positive_pairs,
            max_posts=args.max_posts,
            max_posts_per_reference=args.max_posts_per_reference,
            excluded_post_ids=excluded_post_ids,
        )
    except CorpusPlanningError as exc:
        reason = re.sub(r"[^A-Za-z0-9]+", "_", str(exc)).strip("_").lower()
        raise SonicAuditError(f"corpus_plan_infeasible:{reason[:150]}") from None

    # Preserve why the exact exclusion union exists, not merely its post IDs.
    scope_body = dict(artifact.get("source_scope") or {})
    scope_body.pop("scope_hash", None)
    scope_body["excluded_runs"] = excluded_run_bindings
    artifact_body = dict(artifact)
    artifact_body.pop("plan_hash", None)
    artifact_body["source_scope"] = bind_hash(scope_body, "scope_hash")
    artifact = bind_hash(artifact_body, "plan_hash")
    if not verify_hash(artifact, "plan_hash"):
        raise SonicAuditError("corpus_plan_hash_invalid")

    target = Path(args.file).expanduser().resolve()
    if target == Path(target.anchor) or target.parent == target:
        raise SonicAuditError("corpus_plan_output_target_too_broad")
    output_root = _root(args.output_root)
    if (
        target == output_root
        or output_root in target.parents
        or target in output_root.parents
    ):
        raise SonicAuditError("corpus_plan_output_overlaps_run_state")
    protected_master_paths = {
        master_database,
        Path(os.fspath(master_database) + "-journal"),
        Path(os.fspath(master_database) + "-shm"),
        Path(os.fspath(master_database) + "-wal"),
    }
    if target in protected_master_paths:
        raise SonicAuditError("corpus_plan_output_overlaps_master_state")
    _atomic_create_json(target, artifact)
    return {
        "schema_version": artifact.get("schema_version"),
        "file": os.fspath(target),
        "selected_posts": artifact.get("selected_candidate_count"),
        "repeated_groups": artifact.get("selected_repeated_group_count"),
        "positive_pairs": artifact.get("selected_positive_pair_count"),
        "batches": len(artifact.get("batches") or []),
        "plan_hash": artifact.get("plan_hash"),
        "browser_access_performed": False,
        "media_access_performed": False,
        "master_registry_mutated": False,
        "ai_analysis_performed": False,
        "sonic_run_created": False,
    }


def _export(args: argparse.Namespace) -> dict[str, Any]:
    run_dir, manifest, state = _load_run(args.output_root, args.run_id)
    records = _load_records(run_dir, manifest)
    _validate_state_counters(manifest, state, records)
    report = _read_json(run_dir / "report.json", name="report")
    _validate_report(report, manifest=manifest)
    validation_path = run_dir / STATISTICAL_VALIDATION_FILE
    statistical_validation: dict[str, Any] | None = None
    if validation_path.exists():
        statistical_validation = _read_json(
            validation_path,
            name="statistical validation",
        )
        _validate_statistical_artifact(
            statistical_validation,
            manifest=manifest,
            report=report,
            records=records,
        )
    target = Path(args.file).expanduser().resolve()
    protected = {
        Path(manifest["master_database"]).resolve(),
        (run_dir / "manifest.json").resolve(),
        (run_dir / "state.json").resolve(),
        (run_dir / "report.json").resolve(),
        _mirelo_credit_ledger_path(run_dir).resolve(),
        validation_path.resolve(),
    }
    if target in protected or run_dir in target.parents:
        raise SonicAuditError("export target overlaps protected workflow state")
    envelope = bind_hash(
        {
            "schema_version": "tiktok-sonic-audit-export-v1",
            "exported_at": utc_now(),
            "manifest": manifest,
            "state": state,
            "records": records,
            "report": report,
            "statistical_validation": statistical_validation,
        },
        "export_hash",
    )
    _atomic_write_json(target, envelope)
    return {
        "run_id": args.run_id,
        "file": os.fspath(target),
        "records": len(records),
        "export_hash": envelope["export_hash"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            result = _new_run(args)
        elif args.command == "run-plan-batch":
            result = _new_plan_batch_run(args)
        elif args.command == "resume":
            result = _resume(args)
        elif args.command == "status":
            result = _status(args)
        elif args.command == "validate":
            result = _run_statistical_validation(args)
        elif args.command == "validate-suite":
            result = _run_statistical_validation_suite(args)
        elif args.command == "plan-corpus":
            result = _plan_corpus_artifact(args)
        elif args.command == "export":
            result = _export(args)
        else:  # pragma: no cover - argparse owns this gate
            raise SonicAuditError("unknown command")
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
