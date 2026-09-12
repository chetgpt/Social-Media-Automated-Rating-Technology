"""Durable sequential coordinator for exact-query multi-topic MUSIC AUDIT.

The coordinator owns no TikTok transport.  It freezes an ordered quota plan and
delegates one topic at a time to the guarded canonical MUSIC AUDIT operator.
Every child remains an ordinary ``workflow=listen`` / ``new_only`` run.  The
shared master registry therefore assigns an overlapping post to the earliest
completed topic; quotas are never borrowed or redistributed.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
from typing import Any, Mapping, Sequence


WORKSPACE = Path(__file__).resolve().parent
REQUIRED_PYTHON = Path(
    r"C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe"
)
OPERATOR = (
    WORKSPACE
    / ".agents/skills/google-3.1-music-audit-instructions/scripts/music_audit_operator.py"
)
DEFAULT_OUTPUT_ROOT = WORKSPACE / "comments_data/music_audit_topic_runs"
SCHEMA = "music-audit-topics-run-v1"
REVIEW_SCHEMA = "music-audit-topics-review-v1"
QUERY_POLICY = "exact"
COLLECTION_POLICY = "new_only"
COUNT_MODES = frozenset({"total", "each", "custom"})
ALLOCATION_RULES = {
    "total": "floor_remainder_input_order",
    "each": "same_positive_quota_per_topic",
    "custom": "explicit_positive_quota_per_topic",
}
PARENT_STATES = frozenset(
    {"planned", "running", "awaiting_continue", "blocked", "complete"}
)
CHILD_STATES = frozenset({"planned", "launch_requested", "bound", "complete"})
OPERATOR_STATES = frozenset(
    {
        "RUNNING",
        "INTERRUPTED",
        "RESUME_READY",
        "COLLECTION_COMPLETE_NEEDS_FINALIZE",
        "COMPLETE",
        "RESTART_PENDING",
        "BLOCKED",
    }
)
RUN_PATTERN = re.compile(
    r"music_audit_topics_(?:total|each|custom)_\d{8}T\d{6}Z_[a-f0-9]{8}"
)
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
SPEC_KEYS = (
    "run_id",
    "created_at",
    "workspace",
    "count_mode",
    "requested_posts",
    "requested_total",
    "expected_account",
    "collection_policy",
    "topic_query_policy",
    "allocation_rule",
    "operator_identity",
    "topics",
)
TOP_LEVEL_KEYS = set(SPEC_KEYS) | {
    "schema_version",
    "workflow",
    "spec_sha256",
    "state",
    "children",
    "parent_validation_sha256",
    "updated_at",
    "sha256",
}
TOPIC_KEYS = {"ordinal", "topic", "quota", "child_project"}
CHILD_KEYS = {
    "ordinal",
    "state",
    "handoff",
    "child_run_id",
    "last_operator",
    "receipt",
}
OPERATOR_IDENTITY_KEYS = {"path", "sha256"}
RECEIPT_KEYS = {
    "run_id",
    "intent_hash",
    "database",
    "export_file",
    "export_sha256",
    "review_file",
    "review_sha256",
    "post_ids",
    "post_ids_sha256",
    "validation_sha256",
    "evidence_ready",
    "validated_at",
}


class MusicAuditTopicsError(ValueError):
    """Fail-closed coordinator input, state, or binding error."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _error(code: str, message: str) -> None:
    raise MusicAuditTopicsError(code, message)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _operator_identity() -> dict[str, str]:
    if OPERATOR.is_symlink() or not OPERATOR.is_file():
        _error("operator_unavailable", "The canonical guarded operator is unavailable")
    return {"path": str(OPERATOR.resolve()), "sha256": _file_hash(OPERATOR)}


def _receipt_post_ids(receipt: Mapping[str, Any], quota: int) -> tuple[str, ...]:
    if set(receipt) != RECEIPT_KEYS:
        raise ValueError("receipt keys")
    post_ids = receipt.get("post_ids")
    if (
        not isinstance(post_ids, list)
        or len(post_ids) != quota
        or any(not isinstance(value, str) or not value.isdigit() for value in post_ids)
        or len(set(post_ids)) != len(post_ids)
        or receipt.get("post_ids_sha256") != _hash(post_ids)
        or receipt.get("evidence_ready") != quota
        or not isinstance(receipt.get("run_id"), str)
        or not isinstance(receipt.get("database"), str)
        or not isinstance(receipt.get("export_file"), str)
        or not isinstance(receipt.get("review_file"), str)
        or not isinstance(receipt.get("validated_at"), str)
        or any(
            not SHA256_PATTERN.fullmatch(str(receipt.get(field) or ""))
            for field in (
                "intent_hash",
                "export_sha256",
                "review_sha256",
                "post_ids_sha256",
                "validation_sha256",
            )
        )
    ):
        raise ValueError("receipt binding")
    return tuple(post_ids)


def _positive(value: Any, field: str = "posts") -> int:
    normalized = str(value).strip()
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]*", normalized):
        _error("invalid_post_count", f"{field} must be a positive integer")
    return int(normalized)


def _topic(value: Any) -> str:
    if not isinstance(value, str):
        _error("invalid_topic", "Every topic must be text")
    normalized = " ".join(value.split())
    if (
        not normalized
        or len(normalized) > 500
        or normalized.startswith("-")
        or any(ord(character) < 32 for character in normalized)
    ):
        _error("invalid_topic", "Every topic must be nonempty safe query text")
    return normalized


def _account(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or any(ord(character) < 32 for character in value):
        _error("invalid_account", "Expected account must be an exact TikTok handle")
    normalized = value.strip().lstrip("@").casefold()
    if normalized and not re.fullmatch(r"[a-z0-9._]{1,64}", normalized):
        _error("invalid_account", "Expected account must be an exact TikTok handle")
    return normalized


def _child_project(run_id: str, ordinal: int) -> str:
    binding = hashlib.sha256(f"{run_id}:{ordinal}".encode("utf-8")).hexdigest()[:20]
    return f"music_audit_topics_{binding}_{ordinal:03d}"


def _normalize_plan(
    *,
    count_mode: str | None,
    topics: Sequence[str] | None,
    posts: Any,
    topic_quotas: Sequence[str] | None,
) -> tuple[str, list[tuple[str, int]], int | None, int]:
    mode = str(count_mode or "").casefold()
    supplied_topics = list(topics or ())
    supplied_quotas = list(topic_quotas or ())
    if not mode and supplied_quotas:
        mode = "custom"
    if mode not in {"total", "each", "custom"}:
        _error(
            "invalid_count_mode",
            "TOTAL/EACH require count_mode total or each; CUSTOM uses topic-quota",
        )
    if count_mode and mode == "custom":
        _error(
            "invalid_count_mode",
            "CUSTOM omits count_mode and uses only repeatable topic-quota values",
        )
    pairs: list[tuple[str, int]] = []
    requested_posts: int | None
    if mode in {"total", "each"}:
        if supplied_quotas:
            _error(
                "ambiguous_topic_scope",
                "TOTAL/EACH use repeatable topics plus posts, not topic-quota",
            )
        if posts is None:
            _error("post_count_required", "TOTAL/EACH require --posts")
        requested_posts = _positive(posts)
        normalized_topics = [_topic(value) for value in supplied_topics]
        if mode == "total" and requested_posts < len(normalized_topics):
            _error(
                "total_smaller_than_topic_count",
                "TOTAL must allocate at least one post to every topic",
            )
        if normalized_topics:
            if mode == "total":
                base, remainder = divmod(requested_posts, len(normalized_topics))
                pairs = [
                    (topic, base + (1 if index < remainder else 0))
                    for index, topic in enumerate(normalized_topics)
                ]
            else:
                pairs = [(topic, requested_posts) for topic in normalized_topics]
    else:
        if supplied_topics or posts is not None:
            _error(
                "ambiguous_topic_scope",
                "CUSTOM uses only repeatable TOPIC=N topic-quota values",
            )
        requested_posts = None
        for raw in supplied_quotas:
            if not isinstance(raw, str) or "=" not in raw:
                _error("invalid_topic_quota", "Each custom quota must use TOPIC=N")
            topic_text, count_text = raw.rsplit("=", 1)
            pairs.append((_topic(topic_text), _positive(count_text, "topic quota")))
    if len(pairs) < 2:
        _error("multiple_topics_required", "Provide at least two unique topics")
    folded = [topic.casefold() for topic, _ in pairs]
    if len(set(folded)) != len(folded):
        _error("duplicate_topic", "Topics must be unique after normalization")
    return mode, pairs, requested_posts, sum(quota for _, quota in pairs)


def _manifest_path(root: Path) -> Path:
    path = root / "manifest.json"
    if path.is_symlink() or path.resolve().parent != root.resolve():
        _error("manifest_path_invalid", "The manifest must stay inside its run directory")
    return path


def _parent_artifact_path(root: Path, name: str) -> Path:
    if name not in {"review.json", "validation.json"}:
        _error("artifact_path_invalid", "Unknown multi-topic parent artifact")
    path = root / name
    if path.is_symlink() or path.resolve().parent != root.resolve():
        _error("artifact_path_invalid", "Parent artifact escaped its run directory")
    return path


def _write_parent_artifact(
    root: Path, name: str, payload: Mapping[str, Any]
) -> dict[str, Any]:
    body = dict(payload)
    body["artifact_sha256"] = _hash(payload)
    path = _parent_artifact_path(root, name)
    _atomic_text(path, _json(body) + "\n")
    return {
        "path": str(path.resolve()),
        "sha256": _file_hash(path),
        "content_sha256": body["artifact_sha256"],
    }


def _existing_parent_artifacts(root: Path) -> dict[str, dict[str, Any]]:
    artifacts: dict[str, dict[str, Any]] = {
        "manifest": {
            "path": str(_manifest_path(root).resolve()),
            "sha256": _file_hash(_manifest_path(root)),
        }
    }
    for key, name in (("review", "review.json"), ("validation", "validation.json")):
        path = _parent_artifact_path(root, name)
        if path.is_file():
            artifacts[key] = {"path": str(path.resolve()), "sha256": _file_hash(path)}
    return artifacts


def _atomic_text(path: Path, text: str) -> None:
    temporary = path.parent / f".{uuid.uuid4().hex}.tmp"
    if temporary.resolve().parent != path.parent.resolve():
        _error("artifact_path_invalid", "Temporary artifact escaped its run directory")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _spec(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {key: manifest[key] for key in SPEC_KEYS}


def _save(root: Path, manifest: dict[str, Any], *, initial: bool = False) -> None:
    manifest["updated_at"] = _now().isoformat()
    manifest["sha256"] = _hash(
        {key: value for key, value in manifest.items() if key != "sha256"}
    )
    payload = _json(manifest) + "\n"
    path = _manifest_path(root)
    if initial:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(payload)
    else:
        _atomic_text(path, payload)


def _child_root(topic_spec: Mapping[str, Any]) -> Path:
    comments = (WORKSPACE / "comments_data").resolve()
    child = comments / ("project_" + str(topic_spec["child_project"]))
    if (
        child.is_symlink()
        or child.resolve().parent != comments
        or comments.parent != WORKSPACE.resolve()
    ):
        _error("child_path_invalid", "A child must stay in canonical workspace storage")
    return child.resolve()


def _validate_manifest(root: Path, manifest: Any) -> dict[str, Any]:
    try:
        if DEFAULT_OUTPUT_ROOT.is_symlink():
            raise ValueError("parent symlink")
        canonical_parent = DEFAULT_OUTPUT_ROOT.resolve()
        if (
            root.is_symlink()
            or root.resolve().parent != canonical_parent
            or not root.is_dir()
        ):
            raise ValueError("parent path")
        if not isinstance(manifest, dict) or set(manifest) != TOP_LEVEL_KEYS:
            raise ValueError("keys")
        if (
            manifest["schema_version"] != SCHEMA
            or manifest["workflow"] != "music_audit_topics"
            or manifest["run_id"] != root.name
            or manifest["workspace"] != str(WORKSPACE.resolve())
            or manifest["state"] not in PARENT_STATES
            or manifest["topic_query_policy"] != QUERY_POLICY
            or manifest["collection_policy"] != COLLECTION_POLICY
            or manifest["count_mode"] not in COUNT_MODES
            or manifest["allocation_rule"]
            != ALLOCATION_RULES[manifest["count_mode"]]
            or not isinstance(manifest["operator_identity"], dict)
            or set(manifest["operator_identity"]) != OPERATOR_IDENTITY_KEYS
            or manifest["operator_identity"] != _operator_identity()
        ):
            raise ValueError("identity")
        if manifest["sha256"] != _hash(
            {key: value for key, value in manifest.items() if key != "sha256"}
        ):
            raise ValueError("manifest hash")
        if manifest["spec_sha256"] != _hash(_spec(manifest)):
            raise ValueError("spec hash")
        if _account(manifest["expected_account"]) != manifest["expected_account"]:
            raise ValueError("account")
        topic_specs = manifest["topics"]
        children = manifest["children"]
        if (
            not isinstance(topic_specs, list)
            or not isinstance(children, list)
            or len(topic_specs) < 2
            or len(children) != len(topic_specs)
        ):
            raise ValueError("topic count")
        seen: set[str] = set()
        quotas: list[int] = []
        completed_prefix = True
        incomplete_seen = False
        for ordinal, (topic_spec, child) in enumerate(
            zip(topic_specs, children), start=1
        ):
            if (
                not isinstance(topic_spec, dict)
                or set(topic_spec) != TOPIC_KEYS
                or topic_spec["ordinal"] != ordinal
                or _topic(topic_spec["topic"]) != topic_spec["topic"]
                or _positive(topic_spec["quota"], "quota") != topic_spec["quota"]
                or topic_spec["child_project"] != _child_project(root.name, ordinal)
            ):
                raise ValueError("topic spec")
            folded = topic_spec["topic"].casefold()
            if folded in seen:
                raise ValueError("duplicate")
            seen.add(folded)
            quotas.append(topic_spec["quota"])
            if (
                not isinstance(child, dict)
                or set(child) != CHILD_KEYS
                or child["ordinal"] != ordinal
                or child["state"] not in CHILD_STATES
                or child["last_operator"] is not None
                and not isinstance(child["last_operator"], dict)
                or child["receipt"] is not None
                and not isinstance(child["receipt"], dict)
                or child["child_run_id"] is not None
                and not isinstance(child["child_run_id"], str)
            ):
                raise ValueError("child")
            if child["state"] == "complete":
                if not completed_prefix or not child["handoff"] or not child["receipt"]:
                    raise ValueError("completion order")
                _receipt_post_ids(child["receipt"], topic_spec["quota"])
            else:
                completed_prefix = False
                if incomplete_seen and child["state"] != "planned":
                    raise ValueError("parallel child")
                incomplete_seen = True
                if child["receipt"] is not None:
                    raise ValueError("premature receipt")
            if child["handoff"] is not None:
                handoff_path = Path(child["handoff"])
                if (
                    handoff_path.is_symlink()
                    or handoff_path.resolve().parent != _child_root(topic_spec)
                    or not handoff_path.name.endswith("_handoff.json")
                ):
                    raise ValueError("handoff path")
            if child["state"] in {"bound", "complete"} and not child["handoff"]:
                raise ValueError("handoff missing")
        if sum(quotas) != manifest["requested_total"]:
            raise ValueError("total")
        mode = manifest["count_mode"]
        requested_posts = manifest["requested_posts"]
        if mode == "custom":
            if requested_posts is not None:
                raise ValueError("custom count")
        else:
            requested = _positive(requested_posts)
            if mode == "each" and quotas != [requested] * len(quotas):
                raise ValueError("each allocation")
            if mode == "total":
                if requested != manifest["requested_total"] or requested < len(quotas):
                    raise ValueError("total allocation")
                base, remainder = divmod(requested, len(quotas))
                if quotas != [base + (index < remainder) for index in range(len(quotas))]:
                    raise ValueError("total order")
        if manifest["state"] == "complete" and any(
            child["state"] != "complete" for child in children
        ):
            raise ValueError("parent completion")
        parent_validation = manifest["parent_validation_sha256"]
        if manifest["state"] == "complete":
            if not SHA256_PATTERN.fullmatch(str(parent_validation or "")):
                raise ValueError("parent validation")
        elif parent_validation is not None:
            raise ValueError("premature parent validation")
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        _error(
            "manifest_invalid",
            "The saved multi-topic identity, allocation, or integrity check failed; preserve it",
        )
    return manifest


def _read(run_dir: Path | str) -> tuple[Path, dict[str, Any]]:
    supplied = Path(run_dir).expanduser()
    if supplied.is_symlink():
        _error("not_multi_topic_run", "Use the exact multi-topic MUSIC AUDIT run directory")
    root = supplied.resolve()
    if (
        root.parent != DEFAULT_OUTPUT_ROOT.resolve()
        or not RUN_PATTERN.fullmatch(root.name)
    ):
        _error("not_multi_topic_run", "Use the exact multi-topic MUSIC AUDIT run directory")
    path = _manifest_path(root)
    if not path.is_file():
        _error("manifest_missing", "The run's original manifest is missing")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _error("manifest_invalid", "The saved multi-topic manifest cannot be read")
    return root, _validate_manifest(root, manifest)


@contextmanager
def _lock(root: Path):
    path = root / ".music_audit_topics.lock"
    stream = path.open("a+b")
    held = False
    try:
        if os.name == "nt":
            import msvcrt

            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                _error("coordinator_running", "This multi-topic coordinator is still running")
        else:
            import fcntl

            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                _error("coordinator_running", "This multi-topic coordinator is still running")
        held = True
        yield
    finally:
        if held:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def plan_topics(
    *,
    count_mode: str | None = None,
    topics: Sequence[str] | None = None,
    posts: Any = None,
    topic_quotas: Sequence[str] | None = None,
    expected_account: str = "",
) -> dict[str, Any]:
    mode, pairs, requested_posts, requested_total = _normalize_plan(
        count_mode=count_mode,
        topics=topics,
        posts=posts,
        topic_quotas=topic_quotas,
    )
    now = _now()
    suffix = uuid.uuid4().hex[:8]
    run_id = f"music_audit_topics_{mode}_{now.strftime('%Y%m%dT%H%M%SZ')}_{suffix}"
    if DEFAULT_OUTPUT_ROOT.is_symlink():
        _error("parent_path_invalid", "The canonical multi-topic parent root is a symlink")
    parent = DEFAULT_OUTPUT_ROOT.resolve()
    root = parent / run_id
    topic_specs = [
        {
            "ordinal": ordinal,
            "topic": topic,
            "quota": quota,
            "child_project": _child_project(run_id, ordinal),
        }
        for ordinal, (topic, quota) in enumerate(pairs, start=1)
    ]
    children = [
        {
            "ordinal": topic_spec["ordinal"],
            "state": "planned",
            "handoff": None,
            "child_run_id": None,
            "last_operator": None,
            "receipt": None,
        }
        for topic_spec in topic_specs
    ]
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA,
        "workflow": "music_audit_topics",
        "run_id": run_id,
        "created_at": now.isoformat(),
        "workspace": str(WORKSPACE.resolve()),
        "count_mode": mode,
        "requested_posts": requested_posts,
        "requested_total": requested_total,
        "expected_account": _account(expected_account),
        "collection_policy": COLLECTION_POLICY,
        "topic_query_policy": QUERY_POLICY,
        "allocation_rule": ALLOCATION_RULES[mode],
        "operator_identity": _operator_identity(),
        "topics": topic_specs,
        "spec_sha256": "",
        "state": "planned",
        "children": children,
        "parent_validation_sha256": None,
        "updated_at": now.isoformat(),
        "sha256": "",
    }
    manifest["spec_sha256"] = _hash(_spec(manifest))
    root.mkdir(parents=True, exist_ok=False)
    _save(root, manifest, initial=True)
    return _summary(root, manifest)


def _operator_module() -> Any:
    name = "_music_audit_topics_guarded_operator"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, OPERATOR)
        if spec is None or spec.loader is None:
            _error("operator_unavailable", "The guarded MUSIC AUDIT operator is unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _operator_call(arguments: Sequence[str], *, foreground: bool = False) -> tuple[int, Any]:
    permitted = {"start", "poll", "finalize", "validate"}
    if (
        not arguments
        or arguments[0] not in permitted
        or foreground != (arguments[0] == "start")
    ):
        _error("invalid_operator_action", "The coordinator rejected an unsafe operator action")
    if not REQUIRED_PYTHON.is_file() or not OPERATOR.is_file():
        _error("runtime_unavailable", "Required Python or the guarded operator is unavailable")
    argv = [str(REQUIRED_PYTHON.resolve()), str(OPERATOR.resolve()), *arguments]
    try:
        if foreground:
            child = subprocess.Popen(argv, cwd=str(WORKSPACE), shell=False)
            return child.wait(), None
        child = subprocess.Popen(
            argv,
            cwd=str(WORKSPACE),
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        stdout, _ = child.communicate()
        payload = json.loads(stdout)
        if not isinstance(payload, dict):
            raise ValueError("operator payload")
        return int(child.returncode), payload
    except OSError:
        _error("operator_launch_failed", "The guarded operator could not be launched")
    except (ValueError, UnicodeError, json.JSONDecodeError):
        _error("operator_output_invalid", "The guarded operator returned no valid JSON result")


def _find_handoff(topic_spec: Mapping[str, Any], child: Mapping[str, Any]) -> Path | None:
    if child["handoff"]:
        path = Path(str(child["handoff"]))
        if not path.is_file():
            _error("handoff_missing", "The saved child handoff is missing; do not replace it")
        return path
    child_root = _child_root(topic_spec)
    if not child_root.exists():
        return None
    matches = list(child_root.glob("*_handoff.json"))
    if len(matches) != 1:
        _error(
            "child_identity_unresolved",
            "The prebound child folder lacks one identifiable handoff; do not replace it",
        )
    return matches[0]


def _bind_handoff(
    path: Path,
    manifest: Mapping[str, Any],
    topic_spec: Mapping[str, Any],
    child: Mapping[str, Any],
) -> dict[str, Any]:
    if path.is_symlink() or path.resolve().parent != _child_root(topic_spec):
        _error("handoff_path_mismatch", "The handoff is outside its frozen child project")
    operator = _operator_module()
    paths = operator.OperatorPaths(
        workspace=WORKSPACE.resolve(),
        python=REQUIRED_PYTHON.resolve(),
        engage_script=(WORKSPACE / "engage_tiktok.py").resolve(),
        master_database=(
            WORKSPACE / "comments_data/tiktok_master/state/tiktok_master.sqlite"
        ).resolve(),
    )
    try:
        handoff = operator.load_handoff(path.resolve(), paths)
    except (operator.OperatorError, OSError, ValueError, TypeError, KeyError):
        _error("handoff_invalid", "The guarded handoff failed integrity validation")
    if (
        handoff.get("project") != topic_spec["child_project"]
        or handoff.get("source_mode") != "topic"
        or handoff.get("source_target") != topic_spec["topic"]
        or handoff.get("requested_count") != topic_spec["quota"]
        or handoff.get("collection_policy") != COLLECTION_POLICY
        or handoff.get("topic_query_policy") != QUERY_POLICY
        or handoff.get("all_posts") is not False
        or str(handoff.get("expected_account") or "") != manifest["expected_account"]
        or child["child_run_id"]
        and handoff.get("run_id") != child["child_run_id"]
    ):
        _error("child_scope_mismatch", "The child differs from its exact frozen topic/quota/account")
    return handoff


def _expected_resume_argv(path: Path) -> list[str]:
    return [
        str(REQUIRED_PYTHON.resolve()),
        str(OPERATOR.resolve()),
        "resume",
        "--handoff",
        str(path.resolve()),
    ]


def _poll(
    path: Path,
    manifest: Mapping[str, Any],
    topic_spec: Mapping[str, Any],
    handoff: Mapping[str, Any],
) -> dict[str, Any]:
    code, result = _operator_call(["poll", "--handoff", str(path.resolve())])
    if (
        code
        or result.get("operator_status") not in OPERATOR_STATES
        or result.get("project") != topic_spec["child_project"]
        or result.get("requested") != topic_spec["quota"]
        or handoff.get("run_id")
        and result.get("run_id") != handoff["run_id"]
        or result.get("topic_query_policy") != QUERY_POLICY
        or isinstance(result.get("evidence_ready"), bool)
        or not isinstance(result.get("evidence_ready"), int)
        or not 0 <= result["evidence_ready"] <= topic_spec["quota"]
    ):
        _error("poll_binding_failed", "Guarded poll did not match this exact child")
    action = result.get("safe_same_handoff_action")
    resume_status = result["operator_status"] in {"INTERRUPTED", "RESUME_READY"}
    if resume_status != isinstance(action, dict):
        _error("resume_action_invalid", "Poll continuation availability is inconsistent")
    if action is not None and (
        not isinstance(action, dict)
        or action.get("operation") != "guarded_resume"
        or action.get("argv") != _expected_resume_argv(path)
        or action.get("first_browser_touching_operation") is not True
        or action.get("after_restart") is not False
        or action.get("replacement_project_allowed") is not False
        or action.get("requires_explicit_user_direction")
        is not (result["operator_status"] == "RESUME_READY")
    ):
        _error("resume_action_invalid", "Poll did not return an exact safe same-handoff action")
    return result


def _validated_child(
    path: Path,
    manifest: Mapping[str, Any],
    topic_spec: Mapping[str, Any],
    handoff: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[str, ...], str]:
    code, review = _operator_call(["validate", "--handoff", str(path.resolve())])
    status = review.get("status", {})
    export = review.get("artifacts", {}).get("evidence_export", {})
    runtime_operator = review.get("runtime", {}).get("operator", {})
    quota = topic_spec["quota"]
    if (
        code
        or review.get("task_outcome") != "COMPLETE"
        or review.get("offline_validation") is not True
        or review.get("schema_version") != "google-music-audit-review-v1"
        or review.get("project") != topic_spec["child_project"]
        or review.get("run_id") != handoff.get("run_id")
        or review.get("source_mode") != "topic"
        or review.get("source_target") != topic_spec["topic"]
        or review.get("workflow") != "listen"
        or review.get("collection_policy") != COLLECTION_POLICY
        or review.get("intent_hash") != handoff.get("intent_hash")
        or review.get("database") != handoff.get("database")
        or status.get("status") != "collection_complete"
        or status.get("requested") != quota
        or status.get("evidence_ready") != quota
        or status.get("topic_query_policy") != QUERY_POLICY
        or export.get("records") != quota
        or export.get("schema_version") != "tiktok-listen-evidence-export-v1"
        or export.get("path") != handoff.get("export_file")
        or runtime_operator != manifest["operator_identity"]
        or review.get("ai_actions") != []
        or review.get("outbound_actions") != []
    ):
        _error("child_not_validated", "The exact-query child did not pass guarded validation")
    export_path = Path(str(handoff["export_file"]))
    review_path = Path(str(handoff.get("review_json") or ""))
    if (
        export_path.is_symlink()
        or export_path.resolve().parent != _child_root(topic_spec)
        or not export_path.is_file()
        or _file_hash(export_path) != export.get("sha256")
        or review_path.is_symlink()
        or review_path.resolve().parent != _child_root(topic_spec)
        or not review_path.is_file()
    ):
        _error("child_export_invalid", "The guarded evidence export is missing or changed")
    posts = review.get("posts")
    if not isinstance(posts, list) or len(posts) != quota:
        _error("child_post_ids_invalid", "The guarded review has no exact post-ID set")
    post_ids: list[str] = []
    for post in posts:
        post_id = str(post.get("post_id") or "") if isinstance(post, Mapping) else ""
        if not post_id.isdigit():
            _error("child_post_ids_invalid", "The guarded review contains an invalid post ID")
        post_ids.append(post_id)
    if len(set(post_ids)) != len(post_ids):
        _error("child_post_ids_invalid", "A guarded child contains duplicate post IDs")
    validation_binding = {
        "schema_version": review["schema_version"],
        "project": review["project"],
        "run_id": review["run_id"],
        "source_mode": review["source_mode"],
        "source_target": review["source_target"],
        "collection_policy": review["collection_policy"],
        "intent_hash": review["intent_hash"],
        "database": review["database"],
        "status": review["status"],
        "operator_identity": runtime_operator,
        "evidence_export": export,
        "post_ids": post_ids,
        "ai_actions": review["ai_actions"],
        "outbound_actions": review["outbound_actions"],
    }
    return review, tuple(post_ids), _hash(validation_binding)


def _receipt(
    handoff: Mapping[str, Any],
    review: Mapping[str, Any],
    quota: int,
    post_ids: Sequence[str],
    validation_sha256: str,
) -> dict[str, Any]:
    export = review["artifacts"]["evidence_export"]
    return {
        "run_id": handoff["run_id"],
        "intent_hash": handoff["intent_hash"],
        "database": handoff["database"],
        "export_file": handoff["export_file"],
        "export_sha256": export["sha256"],
        "review_file": handoff["review_json"],
        "review_sha256": _file_hash(Path(str(handoff["review_json"]))),
        "post_ids": list(post_ids),
        "post_ids_sha256": _hash(list(post_ids)),
        "validation_sha256": validation_sha256,
        "evidence_ready": quota,
        "validated_at": _now().isoformat(),
    }


def _verify_receipt(
    receipt: Mapping[str, Any],
    *,
    handoff: Mapping[str, Any],
    review: Mapping[str, Any],
    quota: int,
    post_ids: Sequence[str],
    validation_sha256: str,
) -> None:
    export = review["artifacts"]["evidence_export"]
    try:
        saved_ids = _receipt_post_ids(receipt, quota)
    except (TypeError, ValueError):
        _error("receipt_binding_failed", "A child completion receipt is invalid")
    if (
        receipt.get("run_id") != handoff["run_id"]
        or receipt.get("intent_hash") != handoff["intent_hash"]
        or receipt.get("database") != handoff["database"]
        or receipt.get("export_file") != handoff["export_file"]
        or receipt.get("export_sha256") != export["sha256"]
        or receipt.get("review_file") != handoff["review_json"]
        or receipt.get("review_sha256")
        != _file_hash(Path(str(handoff["review_json"])))
        or tuple(saved_ids) != tuple(post_ids)
        or receipt.get("validation_sha256") != validation_sha256
    ):
        _error("receipt_binding_failed", "A child changed after parent completion")


def _assert_no_overlap(
    manifest: Mapping[str, Any], post_ids: Sequence[str], *, before: int
) -> None:
    prior: set[str] = set()
    for topic_spec, child in zip(
        manifest["topics"][:before], manifest["children"][:before]
    ):
        if child["state"] != "complete":
            continue
        try:
            prior.update(_receipt_post_ids(child["receipt"], topic_spec["quota"]))
        except (TypeError, ValueError):
            _error("receipt_binding_failed", "A prior child receipt is invalid")
    overlap = prior.intersection(post_ids)
    if overlap:
        _error(
            "cross_topic_overlap",
            "A later topic duplicated a post already owned by an earlier topic",
        )


def _set_operator_state(manifest: dict[str, Any], result: Mapping[str, Any]) -> None:
    status = result["operator_status"]
    manifest["state"] = {
        "RUNNING": "running",
        "INTERRUPTED": "awaiting_continue",
        "RESUME_READY": "awaiting_continue",
        "COLLECTION_COMPLETE_NEEDS_FINALIZE": "awaiting_continue",
        "RESTART_PENDING": "blocked",
        "BLOCKED": "blocked",
        "COMPLETE": "running",
    }[status]


def _launch_child(
    root: Path,
    manifest: dict[str, Any],
    topic_spec: Mapping[str, Any],
    child: dict[str, Any],
) -> Path:
    if child["state"] != "planned" or _find_handoff(topic_spec, child) is not None:
        _error("replacement_forbidden", "Only a never-launched planned child may start")
    arguments = [
        "start",
        "--project",
        topic_spec["child_project"],
        "--topic",
        topic_spec["topic"],
        "--posts",
        str(topic_spec["quota"]),
    ]
    if manifest["expected_account"]:
        arguments.extend(("--expected-account", manifest["expected_account"]))
    child["state"] = "launch_requested"
    manifest["state"] = "running"
    _save(root, manifest)
    _operator_call(arguments, foreground=True)
    path = _find_handoff(topic_spec, child)
    if path is None:
        _error(
            "handoff_not_created",
            "Guarded start ended without the frozen child handoff; do not launch a replacement",
        )
    return path


def _run_exact_resume(action: Mapping[str, Any], path: Path) -> int:
    expected = _expected_resume_argv(path)
    if action.get("argv") != expected:
        _error("resume_action_invalid", "Only the exact operator-supplied resume may run")
    supplied = list(action["argv"])
    try:
        child = subprocess.Popen(supplied, cwd=str(WORKSPACE), shell=False)
        return int(child.wait())
    except OSError:
        _error("operator_launch_failed", "The exact same-handoff resume could not be launched")


def _finalize(path: Path) -> None:
    code, result = _operator_call(["finalize", "--handoff", str(path.resolve())])
    if code or result.get("task_outcome") != "COMPLETE":
        _error("finalize_failed", "Offline guarded finalization did not complete")


def _current_index(manifest: Mapping[str, Any]) -> int | None:
    for index, child in enumerate(manifest["children"]):
        if child["state"] != "complete":
            return index
    return None


def _revalidate_all_children(
    root: Path, manifest: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], str]:
    """Revalidate every child and prove one globally unique ordered ID set."""

    bindings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for topic_spec, child in zip(manifest["topics"], manifest["children"]):
        if child["state"] != "complete":
            _error("not_complete", "Every fixed child must complete before validation")
        path = _find_handoff(topic_spec, child)
        if path is None:
            _error("handoff_missing", "A completed child handoff is missing")
        handoff = _bind_handoff(path, manifest, topic_spec, child)
        review, post_ids, validation_sha256 = _validated_child(
            path, manifest, topic_spec, handoff
        )
        _verify_receipt(
            child["receipt"],
            handoff=handoff,
            review=review,
            quota=topic_spec["quota"],
            post_ids=post_ids,
            validation_sha256=validation_sha256,
        )
        overlap = seen.intersection(post_ids)
        if overlap:
            _error(
                "cross_topic_overlap",
                "Two topic children contain the same globally-owned post ID",
            )
        seen.update(post_ids)
        bindings.append(
            {
                "ordinal": topic_spec["ordinal"],
                "topic": topic_spec["topic"],
                "quota": topic_spec["quota"],
                "run_id": handoff["run_id"],
                "post_ids_sha256": _hash(list(post_ids)),
                "validation_sha256": validation_sha256,
                "export_sha256": review["artifacts"]["evidence_export"]["sha256"],
            }
        )
    parent_validation_sha256 = _hash(
        {
            "schema_version": REVIEW_SCHEMA,
            "run_id": manifest["run_id"],
            "spec_sha256": manifest["spec_sha256"],
            "requested_total": manifest["requested_total"],
            "unique_post_ids": len(seen),
            "children": bindings,
        }
    )
    if len(seen) != manifest["requested_total"]:
        _error("parent_exact_count_failed", "The parent unique post count is not exact")
    return bindings, parent_validation_sha256


def _drive(run_dir: Path | str, *, allow_continue: bool) -> dict[str, Any]:
    root, manifest = _read(run_dir)
    with _lock(root):
        root, manifest = _read(root)
        resumed = False
        while True:
            index = _current_index(manifest)
            if index is None:
                bindings, validation_sha256 = _revalidate_all_children(root, manifest)
                manifest["state"] = "complete"
                manifest["parent_validation_sha256"] = validation_sha256
                _save(root, manifest)
                result = {
                    **_summary(root, manifest),
                    "validated": True,
                    "validation_result": "pass",
                    "validation_sha256": validation_sha256,
                    "child_validations": bindings,
                }
                artifact_payload = dict(result)
                artifact_payload["parent_artifacts"] = {
                    key: value
                    for key, value in result["parent_artifacts"].items()
                    if key != "review"
                }
                artifact = _write_parent_artifact(
                    root, "review.json", artifact_payload
                )
                return {**result, "parent_review": artifact}
            topic_spec = manifest["topics"][index]
            child = manifest["children"][index]
            path = _find_handoff(topic_spec, child)
            if path is None:
                if child["state"] != "planned":
                    _error(
                        "launch_interrupted",
                        "Launch was already requested without a handoff; preserve this child identity",
                    )
                path = _launch_child(root, manifest, topic_spec, child)
            handoff = _bind_handoff(path, manifest, topic_spec, child)
            child.update(
                state="bound",
                handoff=str(path.resolve()),
                child_run_id=str(handoff.get("run_id") or "") or None,
            )
            _save(root, manifest)
            result = _poll(path, manifest, topic_spec, handoff)
            child["last_operator"] = result
            _set_operator_state(manifest, result)
            _save(root, manifest)
            status = result["operator_status"]
            if status == "COMPLETE":
                handoff = _bind_handoff(path, manifest, topic_spec, child)
                review, post_ids, validation_sha256 = _validated_child(
                    path, manifest, topic_spec, handoff
                )
                _assert_no_overlap(manifest, post_ids, before=index)
                child.update(
                    state="complete",
                    child_run_id=handoff["run_id"],
                    receipt=_receipt(
                        handoff,
                        review,
                        topic_spec["quota"],
                        post_ids,
                        validation_sha256,
                    ),
                )
                manifest["state"] = "running"
                _save(root, manifest)
                continue
            if status == "COLLECTION_COMPLETE_NEEDS_FINALIZE" and allow_continue:
                _finalize(path)
                next_result = _poll(path, manifest, topic_spec, handoff)
                child["last_operator"] = next_result
                _set_operator_state(manifest, next_result)
                _save(root, manifest)
                if next_result["operator_status"] == "COMPLETE":
                    continue
                return _summary(root, manifest)
            if status in {"INTERRUPTED", "RESUME_READY"} and allow_continue and not resumed:
                action = result.get("safe_same_handoff_action")
                if not isinstance(action, dict):
                    _error("resume_action_missing", "No exact guarded same-handoff action is available")
                _run_exact_resume(action, path)
                resumed = True
                next_result = _poll(path, manifest, topic_spec, handoff)
                child["last_operator"] = next_result
                _set_operator_state(manifest, next_result)
                _save(root, manifest)
                if next_result["operator_status"] in {
                    "COMPLETE",
                    "COLLECTION_COMPLETE_NEEDS_FINALIZE",
                }:
                    continue
                return _summary(root, manifest)
            return _summary(root, manifest)


def collect_topics(run_dir: Path | str) -> dict[str, Any]:
    """Launch planned children sequentially; existing children are poll-only."""

    return _drive(run_dir, allow_continue=False)


def continue_topics(run_dir: Path | str) -> dict[str, Any]:
    """User-explicit same-handoff resume/finalize, then sequential progress."""

    return _drive(run_dir, allow_continue=True)


def _operator_evidence(child: Mapping[str, Any], override: Mapping[str, Any] | None) -> int:
    if child["state"] == "complete":
        return int(child["receipt"]["evidence_ready"])
    result = override or child.get("last_operator") or {}
    value = result.get("evidence_ready", 0)
    return int(value) if isinstance(value, int) and value >= 0 else 0


def _summary(
    root: Path,
    manifest: Mapping[str, Any],
    *,
    current_override: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    current = _current_index(manifest)
    per_topic = []
    total_ready = 0
    for index, (topic_spec, child) in enumerate(
        zip(manifest["topics"], manifest["children"])
    ):
        override = current_override if index == current else None
        ready = _operator_evidence(child, override)
        total_ready += ready
        operator = override or child.get("last_operator") or {}
        receipt = child.get("receipt") or {}
        per_topic.append(
            {
                **topic_spec,
                "state": child["state"],
                "operator_status": operator.get("operator_status", "not_polled"),
                "evidence_ready": ready,
                "child_run_id": child["child_run_id"],
                "handoff": child["handoff"],
                "database": receipt.get("database"),
                "evidence_export": receipt.get("export_file"),
                "evidence_export_sha256": receipt.get("export_sha256"),
                "review_file": receipt.get("review_file"),
                "review_sha256": receipt.get("review_sha256"),
                "post_ids_sha256": receipt.get("post_ids_sha256"),
                "validation_sha256": receipt.get("validation_sha256"),
                "progress_error": operator.get("progress_error"),
                "blocker": (
                    operator.get("next_action")
                    if operator.get("operator_status")
                    in {"BLOCKED", "RESTART_PENDING"}
                    else None
                ),
            }
        )
    active_operator = (
        current_override
        or (manifest["children"][current].get("last_operator") if current is not None else None)
        or {}
    )
    complete = manifest["state"] == "complete"
    return {
        "schema_version": REVIEW_SCHEMA,
        "workflow": "music_audit_topics",
        "run_id": manifest["run_id"],
        "run_dir": str(root),
        "count_mode": manifest["count_mode"],
        "allocation_rule": manifest["allocation_rule"],
        "collection_policy": manifest["collection_policy"],
        "topic_query_policy": manifest["topic_query_policy"],
        "operator_identity": manifest["operator_identity"],
        "expected_account": manifest["expected_account"],
        "requested_total": manifest["requested_total"],
        "evidence_ready": total_ready,
        "status": "collection_complete" if complete else "collection_incomplete",
        "coordinator_state": manifest["state"],
        "current_topic_ordinal": current + 1 if current is not None else None,
        "operator_status": active_operator.get("operator_status", "not_polled"),
        "next_action": (
            "validate_or_stop"
            if complete
            else active_operator.get("next_action", "collect_same_frozen_plan")
        ),
        "safe_same_handoff_action": active_operator.get("safe_same_handoff_action"),
        "topics": per_topic,
        "spec_sha256": manifest["spec_sha256"],
        "validation_sha256": manifest["parent_validation_sha256"],
        "parent_artifacts": _existing_parent_artifacts(root),
        "deduplication": "workspace_global_new_only_first_completed_topic_wins",
        "quota_redistribution": False,
        "ai_actions": [],
        "outbound_actions": [],
    }


def status_topics(run_dir: Path | str) -> dict[str, Any]:
    """Read parent state and guarded child progress without mutating either."""

    root, manifest = _read(run_dir)
    current = _current_index(manifest)
    if current is None:
        return _summary(root, manifest)
    topic_spec = manifest["topics"][current]
    child = manifest["children"][current]
    path = _find_handoff(topic_spec, child)
    if path is None:
        return _summary(root, manifest)
    handoff = _bind_handoff(path, manifest, topic_spec, child)
    result = _poll(path, manifest, topic_spec, handoff)
    return _summary(root, manifest, current_override=result)


def validate_topics(run_dir: Path | str) -> dict[str, Any]:
    """Offline revalidation of every exact completed child and parent binding."""

    root, manifest = _read(run_dir)
    if manifest["state"] != "complete" or any(
        child["state"] != "complete" for child in manifest["children"]
    ):
        _error("not_complete", "Every fixed child must complete before parent validation")
    bindings, validation_sha256 = _revalidate_all_children(root, manifest)
    if manifest["parent_validation_sha256"] != validation_sha256:
        _error("parent_validation_drift", "The saved parent validation binding changed")
    summary = _summary(root, manifest)
    result = {
        **summary,
        "validated": True,
        "validation_result": "pass",
        "validation_sha256": validation_sha256,
        "child_validations": bindings,
    }
    artifact_payload = dict(result)
    artifact_payload["parent_artifacts"] = {
        key: value
        for key, value in result["parent_artifacts"].items()
        if key != "validation"
    }
    artifact = _write_parent_artifact(root, "validation.json", artifact_payload)
    return {**result, "parent_validation": artifact}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="Freeze a multi-topic exact-query plan; offline")
    plan.add_argument("--count-mode", choices=("total", "each"))
    plan.add_argument("--topic", action="append", default=[])
    plan.add_argument("--posts")
    plan.add_argument("--topic-quota", action="append", default=[])
    plan.add_argument("--expected-account", default="")
    for name in ("collect", "continue", "status", "validate"):
        command = commands.add_parser(name)
        command.add_argument("--run-dir", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            result = plan_topics(
                count_mode=args.count_mode,
                topics=args.topic,
                posts=args.posts,
                topic_quotas=args.topic_quota,
                expected_account=args.expected_account,
            )
        else:
            action = {
                "collect": collect_topics,
                "continue": continue_topics,
                "status": status_topics,
                "validate": validate_topics,
            }[args.command]
            result = action(args.run_dir)
        print(json.dumps(result, ensure_ascii=True, indent=2), flush=True)
        if args.command in {"collect", "continue"} and result["status"] != "collection_complete":
            return 2
        return 0
    except MusicAuditTopicsError as exc:
        print(
            json.dumps(
                {
                    "workflow": "music_audit_topics",
                    "status": "attention_required",
                    "reason": exc.code,
                    "message": str(exc),
                }
            ),
            flush=True,
        )
        return 2
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        print(
            json.dumps(
                {
                    "workflow": "music_audit_topics",
                    "status": "attention_required",
                    "reason": "stored_or_operator_data_invalid",
                    "message": "A saved or guarded artifact failed validation; preserve the exact plan",
                }
            ),
            flush=True,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
