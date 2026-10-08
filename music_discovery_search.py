"""Live MUSIC DISCOVERY orchestration through the existing guarded MUSIC AUDIT.

Planning and status are offline. ``collect-next`` may start one explicitly
planned, bounded topic batch; only the guarded operator owns collection and
Profile 7. Existing children are polled, never replaced or automatically resumed.
The separate music_discovery.py helper remains an offline evidence reviewer.
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
from typing import Any, Iterator, Sequence
import uuid

import music_discovery as discovery


WORKSPACE = Path(__file__).resolve().parent
REQUIRED_PYTHON = Path(r"C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe")
OPERATOR = WORKSPACE / ".agents" / "skills" / "google-3.1-music-audit-instructions" / "scripts" / "music_audit_operator.py"
MANIFEST_FILE = "live_search.json"
SCHEMA = "music-discovery-live-search-v1"
_STAGES = {"planned", "launch_requested", "awaiting_operator", "imported"}
_OPERATOR_STATES = {"RUNNING", "INTERRUPTED", "RESUME_READY", "COLLECTION_COMPLETE_NEEDS_FINALIZE", "COMPLETE", "RESTART_PENDING", "BLOCKED"}
_MANIFEST_KEYS = {"schema", "run_id", "brief_sha256", "workspace", "created_at", "updated_at", "revision", "jobs", "sha256"}
_JOB_KEYS = {"query_job_id", "ordinal", "query", "posts", "project", "created_at", "spec_sha256",
             "stage", "launch_requested_at", "handoff", "run_id", "last_operator_status",
             "last_polled_at", "imported_at", "validation_receipt"}
_RECEIPT_KEYS = {"run_id", "database", "handoff", "intent_hash", "export_sha256", "requested_posts", "validated_at"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _positive(value: Any) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)) or int(value) <= 0:
        raise ValueError("posts must be an explicit positive integer, not ALL")
    return int(value)


def _query(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or value.strip().startswith("-"):
        raise ValueError("query must be nonempty topic text, not command-line options")
    if any(ord(char) < 32 for char in value):
        raise ValueError("query cannot contain control characters")
    return value.strip()


def _run(run_dir: Path | str) -> tuple[Path, dict[str, Any]]:
    root, brief, db = discovery._open_run(run_dir)
    db.close()
    return root, brief


def _manifest_path(root: Path) -> Path:
    return discovery._safe_path(root, MANIFEST_FILE)


def _project_root(project: str) -> Path:
    if not re.fullmatch(r"music_audit_discovery_[a-f0-9]{16}_q[0-9]{3,}", project):
        raise ValueError("invalid discovery child project identity")
    comments = WORKSPACE / "comments_data"
    path = comments / ("project_" + project)
    if comments.resolve().parent != WORKSPACE.resolve() or path.is_symlink() or path.resolve().parent != comments.resolve():
        raise ValueError("discovery child project escaped the canonical workspace")
    return path.resolve()


def _spec(run_id: str, ordinal: int, query: str, posts: int) -> dict[str, Any]:
    binding = {"run_id": run_id, "ordinal": ordinal, "query": query, "posts": posts}
    return {"query_job_id": f"query_{ordinal:03d}_" + _hash(binding)[:12], "ordinal": ordinal,
            "query": query, "posts": posts,
            "project": f"music_audit_discovery_{hashlib.sha256(run_id.encode()).hexdigest()[:16]}_q{ordinal:03d}"}


def _new_job(run_id: str, ordinal: int, query: str, posts: int) -> dict[str, Any]:
    spec = _spec(run_id, ordinal, query, posts)
    return {**spec, "created_at": _now(), "spec_sha256": _hash(spec), "stage": "planned",
            "launch_requested_at": None, "handoff": None, "run_id": None,
            "last_operator_status": None, "last_polled_at": None, "imported_at": None,
            "validation_receipt": None}


def _check_timestamp(value: Any, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str):
        raise ValueError("invalid live-search timestamp")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError()
    except (ValueError, OverflowError):
        raise ValueError("invalid live-search timestamp") from None


def _read_manifest(root: Path, brief: dict[str, Any]) -> dict[str, Any]:
    try:
        manifest = json.loads(_manifest_path(root).read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS:
            raise ValueError("unexpected live-search manifest fields")
        body = dict(manifest)
        digest = body.pop("sha256")
        if digest != _hash(body) or manifest["schema"] != SCHEMA:
            raise ValueError("live-search manifest integrity mismatch")
        if (manifest["run_id"] != brief["run_id"] or manifest["brief_sha256"] != _hash(brief)
                or manifest["workspace"] != str(WORKSPACE.resolve())):
            raise ValueError("live-search parent binding mismatch")
        _check_timestamp(manifest["created_at"])
        _check_timestamp(manifest["updated_at"])
        if type(manifest["revision"]) is not int or manifest["revision"] < 1:
            raise ValueError("invalid live-search manifest revision")
        if not isinstance(manifest["jobs"], list) or not manifest["jobs"]:
            raise ValueError("live-search manifest needs planned query jobs")
        for ordinal, job in enumerate(manifest["jobs"], 1):
            if not isinstance(job, dict) or set(job) != _JOB_KEYS:
                raise ValueError("unexpected live-search job fields")
            spec = _spec(brief["run_id"], ordinal, _query(job["query"]), _positive(job["posts"]))
            if any(job[key] != value for key, value in spec.items()) or job["spec_sha256"] != _hash(spec):
                raise ValueError("live-search job scope binding mismatch")
            if job["stage"] not in _STAGES or job["last_operator_status"] not in (_OPERATOR_STATES | {None}):
                raise ValueError("invalid recorded live-search job stage")
            for key in ("created_at", "launch_requested_at", "last_polled_at", "imported_at"):
                _check_timestamp(job[key], optional=key != "created_at")
            if job["run_id"] is not None and not re.fullmatch(r"engage_[a-f0-9]{16}", str(job["run_id"])):
                raise ValueError("invalid guarded child run ID")
            if job["handoff"] is not None:
                path = Path(job["handoff"])
                if path.is_symlink() or path.resolve().parent != _project_root(job["project"]) or not path.name.endswith("_handoff.json"):
                    raise ValueError("live-search handoff escaped its prebound project")
            if job["stage"] == "planned" and any(job[key] is not None for key in (
                    "launch_requested_at", "handoff", "run_id", "last_operator_status", "last_polled_at", "imported_at", "validation_receipt")):
                raise ValueError("planned query contains execution state")
            receipt = job["validation_receipt"]
            if job["stage"] == "imported":
                if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_KEYS or not job["imported_at"]:
                    raise ValueError("imported query lacks a validation receipt")
                if (receipt["run_id"] != job["run_id"] or receipt["handoff"] != job["handoff"]
                        or receipt["requested_posts"] != job["posts"]):
                    raise ValueError("imported query receipt binding mismatch")
                for key in ("intent_hash", "export_sha256"):
                    if not re.fullmatch(r"[0-9a-f]{64}", str(receipt[key])):
                        raise ValueError("invalid guarded validation hash")
                if Path(receipt["database"]).resolve().parent != _project_root(job["project"]) / "state":
                    raise ValueError("import receipt database escaped its project")
                _check_timestamp(receipt["validated_at"])
            elif receipt is not None or job["imported_at"] is not None:
                raise ValueError("unimported query contains an import receipt")
        return manifest
    except (OSError, TypeError, KeyError, json.JSONDecodeError, UnicodeError, RecursionError):
        raise ValueError("cannot read a valid live-search manifest") from None


def _save_manifest(root: Path, manifest: dict[str, Any], *, initial: bool = False) -> None:
    path = _manifest_path(root)
    manifest["updated_at"] = _now()
    manifest["revision"] = manifest.get("revision", 0) + 1
    body = {key: value for key, value in manifest.items() if key != "sha256"}
    manifest["sha256"] = _hash(body)
    if initial:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(_json(manifest) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return
    temporary = discovery._safe_path(root, f".live_search_{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(_json(manifest) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def _search_lock(root: Path) -> Iterator[None]:
    """OS-owned lock survives neither process exit nor a stale marker file."""
    path = discovery._safe_path(root, ".live_search.lock")
    handle = path.open("a+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if not handle.tell():
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ValueError("live-search coordinator is already active; preserve and wait") from None
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ValueError("live-search coordinator is already active; preserve and wait") from None
        locked = True
        yield
    finally:
        if locked:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _summary(root: Path, brief: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    jobs = manifest["jobs"]
    imported = sum(job["stage"] == "imported" for job in jobs)
    pending = [job for job in jobs if job["stage"] != "imported"]
    return {"schema": SCHEMA, "run_dir": str(root), "manifest": str(_manifest_path(root)),
            "input_mode": "live_tiktok_search", "collection_path": "guarded_music_audit_topic_batches",
            "requested_artists": brief["requested_artists"], "planned_batches": len(jobs),
            "pending_batches": len(pending), "imported_batches": imported,
            "all_imported": imported == len(jobs), "planned_posts": sum(job["posts"] for job in jobs),
            "imported_requested_posts": sum(job["posts"] for job in jobs if job["stage"] == "imported"),
            "queries": [dict(job) for job in jobs], "live_process_status": "not_checked",
            "next_query_job_id": pending[0]["query_job_id"] if pending else None,
            "next_action": "collect_next_or_follow_preserved_guarded_action" if pending else "review_imported_evidence_or_explicitly_add_query",
            "coverage_claim": "planned_topic_batches_only",
            "query_exhaustion_verified": False, "all_artists_found": False,
            "limitations": [
                "Post counts are bounded collection batches, not artist counts or total search ceilings.",
                "ALL is the artist-review target; finite topic batches cannot exhaust TikTok or all Indonesian artists.",
                "Canonical topic discovery may use related search variants and the global new_only fence.",
                "Recorded operator statuses are dated observations, not proof that a worker is still alive.",
            ]}


def live_search_status(run_dir: Path | str) -> dict[str, Any] | None:
    """Offline summary hook; never launches a child or infers current liveness."""
    root, brief = _run(run_dir)
    path = _manifest_path(root)
    if not path.exists():
        return None
    return _summary(root, brief, _read_manifest(root, brief))


def plan_search(*, queries: Sequence[str], posts_per_query: int, artists: int | str,
                focus: str = "", label: str = "search", lookback_days: int = 7,
                output_root: Path | None = None) -> dict[str, Any]:
    posts = _positive(posts_per_query)
    if isinstance(queries, str):
        raise ValueError("queries must be a sequence of explicit topic queries")
    normalized = [_query(value) for value in queries]
    if not normalized or len({query.casefold() for query in normalized}) != len(normalized):
        raise ValueError("plan requires nonempty, distinct topic queries")
    result = discovery.init_run(artists=artists, focus=focus, label=label,
                                lookback_days=lookback_days, output_root=output_root,
                                input_mode="live_search")
    root, brief = _run(result["run_dir"])
    manifest = {"schema": SCHEMA, "run_id": brief["run_id"], "brief_sha256": _hash(brief),
                "workspace": str(WORKSPACE.resolve()), "created_at": _now(),
                "jobs": [_new_job(brief["run_id"], index, query, posts) for index, query in enumerate(normalized, 1)]}
    _save_manifest(root, manifest, initial=True)
    return _summary(root, brief, manifest)


def add_query(run_dir: Path | str, *, query: str, posts: int) -> dict[str, Any]:
    query, posts = _query(query), _positive(posts)
    root, brief = _run(run_dir)
    with _search_lock(root):
        manifest = _read_manifest(root, brief)
        if any(job["query"].casefold() == query.casefold() and job["stage"] != "imported" for job in manifest["jobs"]):
            raise ValueError("that query already has an unfinished batch; preserve its original handoff")
        manifest["jobs"].append(_new_job(brief["run_id"], len(manifest["jobs"]) + 1, query, posts))
        _save_manifest(root, manifest)
    return _summary(root, brief, manifest)


def _operator_module() -> Any:
    name = "_music_discovery_guarded_operator"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, OPERATOR)
        if spec is None or spec.loader is None:
            raise ValueError("guarded MUSIC AUDIT operator is unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _bound_handoff(path: Path, job: dict[str, Any]) -> dict[str, Any]:
    if path.is_symlink() or path.resolve().parent != _project_root(job["project"]):
        raise ValueError("handoff does not belong to its prebound query project")
    operator = _operator_module()
    paths = operator.OperatorPaths(workspace=WORKSPACE.resolve(), python=REQUIRED_PYTHON.resolve(),
        engage_script=(WORKSPACE / "engage_tiktok.py").resolve(),
        master_database=(WORKSPACE / "comments_data" / "tiktok_master" / "state" / "tiktok_master.sqlite").resolve())
    try:
        handoff = operator.load_handoff(path, paths)
    except (operator.OperatorError, OSError, ValueError, TypeError):
        raise ValueError("guarded child handoff failed hash/path/intent validation; preserve the child") from None
    if (handoff["project"] != job["project"] or handoff["source_mode"] != "topic"
            or handoff["source_target"] != job["query"] or handoff["requested_count"] != job["posts"]
            or handoff["all_posts"] is not False or handoff["collection_policy"] != "new_only"
            or (job["run_id"] is not None and job["run_id"] != handoff["run_id"])):
        raise ValueError("guarded child drifted from the planned query/count/run binding")
    return handoff


def _find_handoff(job: dict[str, Any]) -> Path | None:
    project = _project_root(job["project"])
    if job["handoff"] is not None:
        path = Path(job["handoff"])
        if not path.is_file():
            raise ValueError("preserved child handoff is missing; never start a replacement")
        return path
    if not project.exists():
        return None
    matches = list(project.glob("*_handoff.json"))
    if len(matches) != 1:
        raise ValueError("prebound child project exists without one identifiable handoff; preserve it")
    return matches[0]


def _run_operator(arguments: Sequence[str], *, foreground: bool = False) -> tuple[int, dict[str, Any] | None]:
    """Invoke only the fixed trusted operator; never execute returned safe actions."""
    if not arguments or arguments[0] not in {"start", "poll", "validate"} or foreground != (arguments[0] == "start"):
        raise ValueError("unsupported guarded bridge operation")
    if not REQUIRED_PYTHON.is_file() or not OPERATOR.is_file():
        raise ValueError("required Python interpreter or guarded operator is unavailable")
    command = [str(REQUIRED_PYTHON.resolve()), str(OPERATOR.resolve()), *arguments]
    try:
        if foreground:
            # Deliberately inherit stdout/stderr and apply no wall-clock timeout:
            # MUSIC_AUDIT_HEARTBEAT remains visible to the owning managed task.
            child = subprocess.Popen(command, cwd=str(WORKSPACE), shell=False)
            return child.wait(), None
        child = subprocess.Popen(command, cwd=str(WORKSPACE), shell=False,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, encoding="utf-8")
        stdout, _stderr = child.communicate()
    except OSError:
        raise ValueError("guarded operator could not be invoked; preserve the planned child identity") from None
    try:
        payload = json.loads(stdout)
        if not isinstance(payload, dict):
            raise ValueError()
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("guarded operator returned no valid JSON; preserve its handoff") from None
    return child.returncode, payload


def _poll(path: Path, handoff: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    code, payload = _run_operator(["poll", "--handoff", str(path.resolve())])
    if code != 0 or not isinstance(payload, dict) or payload.get("operator_status") not in _OPERATOR_STATES:
        raise ValueError("offline guarded poll failed; preserve the same child")
    if payload.get("project") != job["project"] or payload.get("requested") != job["posts"]:
        raise ValueError("guarded poll query/count binding mismatch")
    if handoff["run_id"] and payload.get("run_id") != handoff["run_id"]:
        raise ValueError("guarded poll run identity mismatch")
    action = payload.get("safe_same_handoff_action")
    if action is not None:
        expected = [str(REQUIRED_PYTHON.resolve()), str(OPERATOR.resolve()), "resume", "--handoff", str(path.resolve())]
        if not isinstance(action, dict) or action.get("operation") != "guarded_resume" or action.get("argv") != expected:
            raise ValueError("guarded poll returned an unexpected same-handoff action")
    return payload


def _validated_receipt(path: Path, handoff: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    code, review = _run_operator(["validate", "--handoff", str(path.resolve())])
    if code != 0 or not isinstance(review, dict):
        raise ValueError("guarded completed-artifact validation failed; nothing imported")
    status = review.get("status")
    artifacts = review.get("artifacts")
    status = status if isinstance(status, dict) else {}
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    exported = artifacts.get("evidence_export")
    exported = exported if isinstance(exported, dict) else {}
    checks = (
        review.get("schema_version") == "google-music-audit-review-v1",
        review.get("task_outcome") == "COMPLETE", review.get("offline_validation") is True,
        review.get("workflow") == "listen", review.get("source_mode") == "topic",
        review.get("source_target") == job["query"], review.get("project") == job["project"],
        review.get("run_id") == handoff["run_id"] and bool(handoff["run_id"]),
        review.get("collection_policy") == "new_only", review.get("database") == handoff["database"],
        review.get("intent_hash") == handoff["intent_hash"], review.get("intent") == handoff["intent"],
        status.get("status") == "collection_complete", status.get("requested") == job["posts"],
        status.get("evidence_ready") == job["posts"], exported.get("records") == job["posts"],
        exported.get("path") == handoff["export_file"],
        exported.get("schema_version") == "tiktok-listen-evidence-export-v1",
        bool(re.fullmatch(r"[0-9a-f]{64}", str(exported.get("sha256", "")))),
        review.get("ai_actions") == [], review.get("outbound_actions") == [],
    )
    if not all(checks):
        raise ValueError("guarded validation does not prove this exact planned batch complete")
    return {"run_id": handoff["run_id"], "database": handoff["database"], "handoff": str(path.resolve()),
            "intent_hash": handoff["intent_hash"], "export_sha256": exported["sha256"],
            "requested_posts": job["posts"], "validated_at": _now()}


def _append_source(run_dir: Path, job: dict[str, Any], handoff: dict[str, Any], path: Path) -> Any:
    from music_discovery_corpus import append_source_batch
    return append_source_batch(run_dir, [Path(handoff["database"])], run_ids=[handoff["run_id"]],
        origin={"kind": "live_search", "query": job["query"], "project": job["project"],
                "run_id": handoff["run_id"], "handoff": str(path.resolve()), "query_job_id": job["query_job_id"]})


def collect_next(run_dir: Path | str) -> dict[str, Any]:
    root, brief = _run(run_dir)
    with _search_lock(root):
        manifest = _read_manifest(root, brief)
        job = next((item for item in manifest["jobs"] if item["stage"] != "imported"), None)
        if job is None:
            return _summary(root, brief, manifest)
        path = _find_handoff(job)
        launched = False
        if path is None:
            if job["stage"] != "planned":
                raise ValueError("launch was already requested but no handoff is available; preserve the child, do not replace it")
            job["stage"], job["launch_requested_at"] = "launch_requested", _now()
            _save_manifest(root, manifest)  # Crash-safe prebinding before launch.
            _run_operator(["start", "--project", job["project"], "--topic", job["query"],
                           "--posts", str(job["posts"])], foreground=True)
            launched = True
            path = _find_handoff(job)
            if path is None:
                raise ValueError("guarded start returned without a handoff; preserve the prebound child project")
        handoff = _bound_handoff(path, job)
        job["handoff"] = str(path.resolve())
        job["run_id"] = handoff["run_id"] or None
        job["stage"] = "awaiting_operator"
        _save_manifest(root, manifest)
        poll = _poll(path, handoff, job)
        job["last_operator_status"], job["last_polled_at"] = poll["operator_status"], _now()
        _save_manifest(root, manifest)
        imported = False
        if poll["operator_status"] == "COMPLETE":
            # The handoff may have acquired its run binding while the poll ran.
            handoff = _bound_handoff(path, job)
            receipt = _validated_receipt(path, handoff, job)
            _append_source(root, job, handoff, path)
            job["stage"], job["run_id"], job["imported_at"] = "imported", handoff["run_id"], _now()
            job["validation_receipt"] = receipt
            _save_manifest(root, manifest)
            imported = True
        result = _summary(root, brief, manifest)
        result.update({"query_job_id": job["query_job_id"], "started_new_child": launched,
                       "imported_child": imported, "operator": poll,
                       "operator_status": poll["operator_status"],
                       "safe_same_handoff_action": poll.get("safe_same_handoff_action")})
        if not imported:
            result["next_action"] = poll.get("next_action")
        return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="Plan explicit bounded live TikTok topic batches offline")
    plan.add_argument("--query", action="append", required=True)
    plan.add_argument("--posts-per-query", type=_positive, required=True)
    plan.add_argument("--artists", required=True)
    plan.add_argument("--focus", default="")
    plan.add_argument("--label", default="search")
    plan.add_argument("--lookback-days", type=_positive, default=7)
    plan.add_argument("--output-root", type=Path)
    for name in ("collect-next", "status", "add-query"):
        command = commands.add_parser(name)
        command.add_argument("--run-dir", required=True, type=Path)
        if name == "add-query":
            command.add_argument("--query", required=True)
            command.add_argument("--posts", type=_positive, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "plan":
            result = plan_search(queries=args.query, posts_per_query=args.posts_per_query,
                artists=args.artists, focus=args.focus, label=args.label,
                lookback_days=args.lookback_days, output_root=args.output_root)
        elif args.command == "collect-next":
            result = collect_next(args.run_dir)
        elif args.command == "add-query":
            result = add_query(args.run_dir, query=args.query, posts=args.posts)
        else:
            result = live_search_status(args.run_dir)
            if result is None:
                raise ValueError("this discovery run has no live-search plan")
        print(json.dumps(result, ensure_ascii=True, indent=2), flush=True)
        return 0
    except (ValueError, OSError):
        # Exception details may contain child stdout, database paths, or transport
        # data. Preserve scope and give a bounded diagnostic, never raw subprocess text.
        print(json.dumps({"schema": SCHEMA, "status": "attention_required",
                          "reason": "live_search_operation_failed",
                          "next_action": "inspect_preserved_manifest_and_guarded_handoff; never create a replacement"}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
