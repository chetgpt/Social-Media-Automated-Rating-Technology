"""Recency-first POSTS DISCOVERY through the guarded TikTok collector.

Plan freezes an explicit publication window. Collection owns one prebound
guarded child; reports and validation are offline. No artist-scoring stage,
audio download, alternate browser, or replacement run is introduced here.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import uuid

from tiktok_scraper.publication_window import normalize_publication_window, publication_decision


WORKSPACE = Path(__file__).resolve().parent
REQUIRED_PYTHON = Path(r"C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe")
OPERATOR = WORKSPACE / ".agents/skills/google-3.1-music-audit-instructions/scripts/music_audit_operator.py"
SCHEMA = "posts-discovery-run-v1"
EXPORT_SCHEMA = "posts-discovery-evidence-v1"
APPLICATION_ID = 0x50445343
RUN_PATTERN = re.compile(r"posts_discovery_[a-z0-9_]{1,20}_\d+p_\d{8}T\d{6}Z_[a-f0-9]{8}")
STATES = {"planned", "launch_requested", "awaiting_operator", "complete"}
OPERATOR_STATES = {"RUNNING", "INTERRUPTED", "RESUME_READY", "COMPLETE", "COLLECTION_COMPLETE_NEEDS_FINALIZE", "BLOCKED", "RESTART_PENDING"}
SPEC_KEYS = ("run_id", "created_at", "workspace", "topic", "requested_posts", "publication_window", "expected_account", "artifact_stem", "child_project")


class PostsDiscoveryError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _error(code, message):
    raise PostsDiscoveryError(code, message)


def _now():
    return datetime.now(timezone.utc)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text(value, field, *, required=False):
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > 1000 or any(ord(c) < 32 for c in value):
        _error("invalid_input", field + " must be valid text")
    return " ".join(value.split())


def _count(value):
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]*", str(value)):
        _error("invalid_post_count", "Provide a positive post count; ALL is not an exact-count request")
    return int(value)


def _safe(root, name):
    path = root / name
    if path.is_symlink() or path.resolve().parent != root.resolve():
        _error("artifact_path_invalid", "Generated artifacts must stay inside their run directory")
    return path


def _paths(root, manifest):
    stem = manifest["artifact_stem"]
    return {key: _safe(root, stem + suffix) for key, suffix in {
        "manifest": "_manifest.json", "database": "_posts.sqlite", "export": "_posts.jsonl",
        "review_json": "_review.json", "review_markdown": "_review.md"}.items()}


def _atomic_text(path, text):
    temporary = _safe(path.parent, "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _save(root, manifest, *, initial=False):
    manifest["updated_at"] = _now().isoformat()
    manifest["sha256"] = _hash({key: value for key, value in manifest.items() if key != "sha256"})
    path = _paths(root, manifest)["manifest"]
    if initial:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(_json(manifest) + "\n")
    else:
        _atomic_text(path, _json(manifest) + "\n")


def _child_root(manifest):
    comments = WORKSPACE / "comments_data"
    child = comments / ("project_" + manifest["child_project"])
    if comments.resolve().parent != WORKSPACE.resolve() or child.is_symlink() or child.resolve().parent != comments.resolve():
        _error("child_path_invalid", "The collection child must stay in canonical workspace storage")
    return child.resolve()


def _read(run_dir):
    root = Path(run_dir).expanduser().resolve()
    if not RUN_PATTERN.fullmatch(root.name):
        _error("not_posts_discovery", "Use the exact POSTS DISCOVERY run directory; legacy music runs are not renamed in place")
    matches = list(root.glob("*_manifest.json"))
    if len(matches) != 1 or matches[0].is_symlink():
        _error("manifest_missing", "The run must contain its one original manifest")
    try:
        manifest = json.loads(matches[0].read_text(encoding="utf-8"))
        keys = set(SPEC_KEYS) | {"schema", "workflow", "spec_sha256", "state", "handoff", "child_run_id", "last_operator", "receipt", "updated_at", "sha256"}
        if not isinstance(manifest, dict) or set(manifest) != keys:
            raise ValueError()
        if manifest["schema"] != SCHEMA or manifest["workflow"] != "posts_discovery" or manifest["run_id"] != root.name:
            raise ValueError()
        if manifest["workspace"] != str(WORKSPACE.resolve()) or manifest["state"] not in STATES:
            raise ValueError()
        if manifest["sha256"] != _hash({key: value for key, value in manifest.items() if key != "sha256"}):
            raise ValueError()
        if manifest["spec_sha256"] != _hash({key: manifest[key] for key in SPEC_KEYS}):
            raise ValueError()
        _count(manifest["requested_posts"])
        _text(manifest["topic"], "topic", required=True)
        window = manifest["publication_window"]
        if window != normalize_publication_window(window["start"], window["end"]):
            raise ValueError()
        suffix = root.name.rsplit("_", 1)[1]
        if not re.fullmatch(r"posts_[a-z0-9_]{1,20}_\d{8}_" + suffix, manifest["artifact_stem"]):
            raise ValueError()
        if manifest["child_project"] != "music_audit_posts_discovery_" + hashlib.sha256(root.name.encode()).hexdigest()[:16]:
            raise ValueError()
        if matches[0] != _paths(root, manifest)["manifest"]:
            raise ValueError()
        handoff = manifest["handoff"]
        if handoff is not None:
            path = Path(handoff)
            if path.is_symlink() or path.resolve().parent != _child_root(manifest) or not path.name.endswith("_handoff.json"):
                raise ValueError()
        if manifest["state"] == "complete" and not isinstance(manifest["receipt"], dict):
            raise ValueError()
    except (ValueError, TypeError, KeyError, OSError, OverflowError):
        _error("manifest_invalid", "The saved run identity/window/integrity check failed; preserve its files")
    return root, manifest


@contextmanager
def _lock(root):
    stream = _safe(root, ".posts_discovery.lock").open("a+b")
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
                _error("coordinator_running", "This POSTS DISCOVERY command is still running; keep waiting")
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                _error("coordinator_running", "This POSTS DISCOVERY command is still running; keep waiting")
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


def plan_posts(*, topic, posts, last_hours=None, since=None, until=None, label="", expected_account="", output_root=None):
    topic, posts = _text(topic, "topic", required=True), _count(posts)
    if topic.startswith("-"):
        _error("invalid_topic", "Topic text must not be a command-line option")
    now = _now()
    if last_hours is not None:
        if since is not None or until is not None:
            _error("ambiguous_window", "Choose last-hours or an absolute since/until pair, not both")
        try:
            hours = float(last_hours)
            if isinstance(last_hours, bool) or not math.isfinite(hours) or hours <= 0:
                raise ValueError()
            since, until = (now - timedelta(hours=hours)).isoformat(), now.isoformat()
        except (ValueError, TypeError, OverflowError):
            _error("invalid_window", "last-hours must be a positive finite duration")
    try:
        window = normalize_publication_window(since or "", until or "")
        if not window:
            raise ValueError()
    except ValueError:
        _error("window_required", "Set last-hours or both timezone-aware since and until timestamps")
    expected = _text(expected_account, "expected_account").lstrip("@").lower()
    if expected and not re.fullmatch(r"[a-z0-9._]{1,64}", expected):
        _error("invalid_account", "Expected account must be an exact TikTok handle")
    # Leave Windows room for the repeated artifact stem and SQLite journal.
    slug = re.sub(r"[^a-z0-9]+", "_", _text(label or topic, "label", required=True).lower()).strip("_")[:16].rstrip("_") or "topic"
    suffix = uuid.uuid4().hex[:8]
    run_id = f"posts_discovery_{slug}_{posts}p_{now.strftime('%Y%m%dT%H%M%SZ')}_{suffix}"
    parent = Path(output_root).expanduser().resolve() if output_root else WORKSPACE / "comments_data/posts_discovery_runs"
    root = parent / run_id
    stem = f"posts_{slug}_{now.strftime('%Y%m%d')}_{suffix}"
    if len(str(root / (stem + "_posts.sqlite-journal")).encode("utf-16-le")) // 2 >= 240:
        _error("path_too_long", "Choose a shorter topic label or output root for Windows")
    manifest = {"schema": SCHEMA, "workflow": "posts_discovery", "run_id": run_id, "created_at": now.isoformat(),
                "workspace": str(WORKSPACE.resolve()), "topic": topic, "requested_posts": posts,
                "publication_window": window, "expected_account": expected, "artifact_stem": stem,
                "child_project": "music_audit_posts_discovery_" + hashlib.sha256(run_id.encode()).hexdigest()[:16],
                "state": "planned", "handoff": None, "child_run_id": None, "last_operator": None, "receipt": None}
    manifest["spec_sha256"] = _hash({key: manifest[key] for key in SPEC_KEYS})
    root.mkdir(parents=True, exist_ok=False)
    _save(root, manifest, initial=True)
    return _summary(root, manifest)


def _operator_module():
    name = "_posts_discovery_guarded_operator"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, OPERATOR)
        if spec is None or spec.loader is None:
            _error("operator_unavailable", "The guarded collector is unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _operator_call(arguments, *, foreground=False):
    if not arguments or arguments[0] not in {"start", "poll", "validate"} or foreground != (arguments[0] == "start"):
        _error("invalid_operator_action", "Only the guarded collection and read-only validation paths are supported")
    if not REQUIRED_PYTHON.is_file() or not OPERATOR.is_file():
        _error("runtime_unavailable", "The required Python interpreter or guarded collector is unavailable")
    argv = [str(REQUIRED_PYTHON.resolve()), str(OPERATOR.resolve()), *arguments]
    try:
        if foreground:
            child = subprocess.Popen(argv, cwd=str(WORKSPACE), shell=False)
            return child.wait(), None  # Inherit heartbeats; never cancel on silence.
        child = subprocess.Popen(argv, cwd=str(WORKSPACE), shell=False, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, encoding="utf-8")
        stdout, _ = child.communicate()
        payload = json.loads(stdout)
        if not isinstance(payload, dict):
            raise ValueError()
        return child.returncode, payload
    except OSError:
        _error("operator_launch_failed", "The guarded collector could not be launched; preserve this run")
    except (ValueError, UnicodeError):
        _error("operator_output_invalid", "The guarded command returned no valid structured result; preserve its handoff")


def _find_handoff(manifest):
    if manifest["handoff"]:
        path = Path(manifest["handoff"])
        if not path.is_file():
            _error("handoff_missing", "The saved child handoff is missing; do not create a replacement")
        return path
    child = _child_root(manifest)
    if not child.exists():
        return None
    matches = list(child.glob("*_handoff.json"))
    if len(matches) != 1:
        _error("child_identity_unresolved", "The prebound child folder lacks one identifiable handoff; preserve it")
    return matches[0]


def _bind_handoff(path, manifest):
    if path.is_symlink() or path.resolve().parent != _child_root(manifest):
        _error("handoff_path_mismatch", "The child handoff does not belong to this run")
    operator = _operator_module()
    paths = operator.OperatorPaths(workspace=WORKSPACE.resolve(), python=REQUIRED_PYTHON.resolve(),
        engage_script=(WORKSPACE / "engage_tiktok.py").resolve(),
        master_database=(WORKSPACE / "comments_data/tiktok_master/state/tiktok_master.sqlite").resolve())
    try:
        handoff = operator.load_handoff(path, paths)
    except (operator.OperatorError, OSError, ValueError, TypeError):
        _error("handoff_invalid", "The guarded handoff failed its integrity/path validation")
    if (handoff["project"] != manifest["child_project"] or handoff["source_mode"] != "topic"
            or handoff["source_target"] != manifest["topic"] or handoff["requested_count"] != manifest["requested_posts"]
            or handoff["collection_policy"] != "new_only" or handoff["all_posts"] is not False
            or handoff.get("publication_window") != manifest["publication_window"]
            or (manifest["child_run_id"] and handoff["run_id"] != manifest["child_run_id"])
            or (manifest["expected_account"] and handoff.get("expected_account") != manifest["expected_account"])):
        _error("child_scope_mismatch", "The child query/count/publication window/account differs from the frozen plan")
    return handoff


def _poll(path, manifest, handoff):
    code, result = _operator_call(["poll", "--handoff", str(path.resolve())])
    if (code or result.get("operator_status") not in OPERATOR_STATES
            or result.get("project") != manifest["child_project"] or result.get("requested") != manifest["requested_posts"]
            or (handoff["run_id"] and result.get("run_id") != handoff["run_id"])):
        _error("poll_binding_failed", "The guarded status does not match this exact child")
    action = result.get("safe_same_handoff_action")
    if action is not None and (not isinstance(action, dict) or action.get("operation") != "guarded_resume" or action.get("argv") !=
            [str(REQUIRED_PYTHON.resolve()), str(OPERATOR.resolve()), "resume", "--handoff", str(path.resolve())]):
        _error("resume_action_invalid", "The returned action is not an exact same-handoff continuation")
    return result


def _verified_records(path, manifest, handoff):
    code, review = _operator_call(["validate", "--handoff", str(path.resolve())])
    status = review.get("status", {})
    exported = review.get("artifacts", {}).get("evidence_export", {})
    count = manifest["requested_posts"]
    if (code or review.get("task_outcome") != "COMPLETE" or review.get("offline_validation") is not True
            or review.get("schema_version") != "google-music-audit-review-v1"
            or review.get("project") != manifest["child_project"] or review.get("run_id") != handoff["run_id"]
            or review.get("source_target") != manifest["topic"] or review.get("source_mode") != "topic"
            or review.get("workflow") != "listen" or review.get("collection_policy") != "new_only"
            or review.get("intent_hash") != handoff["intent_hash"] or review.get("database") != handoff["database"]
            or status.get("publication_window") != manifest["publication_window"]
            or status.get("status") != "collection_complete" or status.get("evidence_ready") != count
            or status.get("requested") != count or exported.get("records") != count
            or exported.get("schema_version") != "tiktok-listen-evidence-export-v1"
            or exported.get("path") != handoff["export_file"] or review.get("ai_actions") != [] or review.get("outbound_actions") != []):
        _error("child_not_validated", "The guarded validator did not confirm this exact complete collection")
    export = Path(handoff["export_file"])
    if export.is_symlink() or export.resolve().parent != _child_root(manifest):
        _error("export_path_mismatch", "The source export must belong to the bound collection child")
    if _file_hash(export) != exported.get("sha256"):
        _error("export_hash_mismatch", "The source export changed after guarded validation")
    records, seen = [], set()
    with export.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            packet = row.get("evidence_packet")
            if (not isinstance(packet, dict) or row.get("schema_version") != "tiktok-listen-evidence-export-v1"
                    or row.get("run_id") != handoff["run_id"] or row.get("project") != manifest["child_project"]
                    or not re.fullmatch(r"[0-9]+", str(row.get("post_id", ""))) or row["post_id"] in seen
                    or packet.get("post_id") != row["post_id"] or packet.get("source_evidence_hash") != row.get("evidence_hash")
                    or _hash(packet) != row.get("evidence_projection_hash")):
                _error("source_record_invalid", "A source row failed identity, duplication or evidence-hash validation")
            decision = publication_decision(packet, manifest["publication_window"])
            if not decision["eligible"]:
                _error("source_recency_failed", "A counted source post has an unknown or out-of-window publication time")
            seen.add(row["post_id"])
            records.append({"schema_version": EXPORT_SCHEMA, "discovery_run_id": manifest["run_id"],
                "post_id": row["post_id"], "published_at": decision["published_at"],
                "source_run_id": handoff["run_id"], "source_project": manifest["child_project"],
                "evidence_hash": row["evidence_hash"], "evidence_projection_hash": row["evidence_projection_hash"],
                "evidence_packet": packet})
    if len(records) != count:
        _error("exact_count_failed", "Only unique verified in-window posts may satisfy the requested count")
    records.sort(key=lambda row: (datetime.fromisoformat(row["published_at"]), int(row["post_id"])), reverse=True)
    return records, review


def _store_records(root, manifest, records, handoff, review):
    paths = _paths(root, manifest)
    temporary = _safe(root, "." + uuid.uuid4().hex + ".sqlite")
    try:
        with closing(sqlite3.connect(temporary)) as db, db:
            db.execute(f"PRAGMA application_id={APPLICATION_ID}")
            db.execute("CREATE TABLE run_binding (run_id TEXT PRIMARY KEY,spec_sha256 TEXT,records_sha256 TEXT)")
            db.execute("CREATE TABLE posts (ordinal INTEGER PRIMARY KEY,post_id TEXT UNIQUE,published_at TEXT,payload_json TEXT,payload_sha256 TEXT)")
            db.execute("INSERT INTO run_binding VALUES (?,?,?)", (manifest["run_id"], manifest["spec_sha256"], _hash(records)))
            db.executemany("INSERT INTO posts VALUES (?,?,?,?,?)", [(index, row["post_id"], row["published_at"], _json(row), _hash(row)) for index, row in enumerate(records, 1)])
        temporary.replace(paths["database"])
    finally:
        if temporary.exists():
            temporary.unlink()
    _atomic_text(paths["export"], "".join(_json(row) + "\n" for row in records))
    manifest["receipt"] = {"source_database": handoff["database"], "source_export": handoff["export_file"],
        "source_export_sha256": review["artifacts"]["evidence_export"]["sha256"], "source_intent_hash": handoff["intent_hash"],
        "accepted_posts": len(records), "records_sha256": _hash(records), "database_sha256": _file_hash(paths["database"]),
        "export_sha256": _file_hash(paths["export"]), "validated_at": _now().isoformat(),
        "publication_window_exclusions": review.get("publication_window_exclusions", review.get("status", {}).get("publication_window_exclusions", {})),
        "browser_preflight": review.get("browser_preflight", {})}
    manifest["state"] = "complete"
    _save(root, manifest)


def collect_posts(run_dir):
    root, manifest = _read(run_dir)
    with _lock(root):
        root, manifest = _read(root)
        if manifest["state"] == "complete":
            # A crash after committing validated data can leave only the derived
            # reviews missing. Rebuilding them is offline, never recollection.
            report_run(root)
            return validate_run(root)
        path = _find_handoff(manifest)
        if path is None:
            if manifest["state"] != "planned":
                _error("launch_interrupted", "Launch was already requested without a saved handoff; preserve this child identity")
            if not REQUIRED_PYTHON.is_file() or not OPERATOR.is_file():
                _error("runtime_unavailable", "Required Python or the guarded collector is unavailable; no launch was attempted")
            window = manifest["publication_window"]
            argv = ["start", "--project", manifest["child_project"], "--topic", manifest["topic"],
                "--posts", str(manifest["requested_posts"]), "--published-after", window["start"], "--published-before", window["end"]]
            if manifest["expected_account"]:
                argv += ["--expected-account", manifest["expected_account"]]
            manifest["state"] = "launch_requested"
            _save(root, manifest)
            _operator_call(argv, foreground=True)
            path = _find_handoff(manifest)
            if path is None:
                _error("handoff_not_created", "The guarded start ended without a handoff; preserve the plan and inspect its terminal result")
        handoff = _bind_handoff(path, manifest)
        manifest.update(handoff=str(path.resolve()), child_run_id=handoff["run_id"] or None, state="awaiting_operator")
        _save(root, manifest)
        manifest["last_operator"] = _poll(path, manifest, handoff)
        _save(root, manifest)
        if manifest["last_operator"]["operator_status"] == "COMPLETE":
            handoff = _bind_handoff(path, manifest)
            manifest["child_run_id"] = handoff["run_id"]
            records, review = _verified_records(path, manifest, handoff)
            _store_records(root, manifest, records, handoff, review)
            report_run(root)
            return validate_run(root)
        return _summary(root, manifest)


def _summary(root, manifest):
    receipt = manifest["receipt"] or {}
    operator = manifest["last_operator"] or {}
    return {"schema_version": "posts-discovery-review-v1", "workflow": "posts_discovery", "run_id": manifest["run_id"],
        "run_dir": str(root), "topic": manifest["topic"], "publication_window": manifest["publication_window"],
        "requested_posts": manifest["requested_posts"], "accepted_posts": receipt.get("accepted_posts", 0),
        "collector_evidence_ready": operator.get("evidence_ready", receipt.get("accepted_posts", 0)),
        "publication_window_exclusions": receipt.get("publication_window_exclusions", operator.get("publication_window_exclusions", {})),
        "exclusion_count_basis": "distinct_post_ids_per_reason_may_overlap_across_reasons",
        "status": "collection_complete" if manifest["state"] == "complete" else "collection_incomplete",
        "coordinator_state": manifest["state"], "operator_status": operator.get("operator_status", "not_polled"),
        "operator_status_observed_at": manifest["updated_at"], "current_process_liveness": "not_checked",
        "child_project": manifest["child_project"], "child_run_id": manifest["child_run_id"], "handoff": manifest["handoff"],
        "next_action": ("validate_or_stop" if manifest["state"] == "complete" else operator.get("next_action", "collect_same_plan")),
        "safe_same_handoff_action": operator.get("safe_same_handoff_action"),
        "paths": {key: str(path) for key, path in _paths(root, manifest).items()}, "receipt": manifest["receipt"],
        "artist_analysis_performed": False, "ai_actions": [], "outbound_actions": [],
        "limitations": ["Publication time, not collection time or database novelty, determines recency.",
            "Eligible observed candidate batches and the final post list are ordered newest first; this is not proof of globally newest TikTok results or exhaustive coverage.",
            "Topic search is not a verified nationality or artist-category judgment. No audio analysis or download is part of POSTS DISCOVERY."]}


def status_run(run_dir):
    return _summary(*_read(run_dir))


def _read_local_records(root, manifest):
    paths, receipt = _paths(root, manifest), manifest["receipt"]
    if not receipt or receipt.get("accepted_posts") != manifest["requested_posts"]:
        _error("not_complete", "No complete exact-count recency receipt is stored yet")
    for key, hash_key in (("database", "database_sha256"), ("export", "export_sha256")):
        if not paths[key].is_file() or _file_hash(paths[key]) != receipt.get(hash_key):
            _error("artifact_integrity_failed", "A stored post artifact is missing or changed")
    with closing(sqlite3.connect(paths["database"].as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        if (db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
                or db.execute("PRAGMA quick_check").fetchall() != [("ok",)]
                or dict(db.execute("SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")) != {"run_binding": "table", "posts": "table"}
                or db.execute("SELECT * FROM run_binding").fetchall() != [(manifest["run_id"], manifest["spec_sha256"], receipt["records_sha256"])]):
            _error("database_binding_failed", "The isolated post database does not match this frozen run")
        records = []
        for ordinal, post_id, published, payload, digest in db.execute("SELECT * FROM posts ORDER BY ordinal"):
            row = json.loads(payload)
            decision = publication_decision(row.get("evidence_packet", {}), manifest["publication_window"])
            if (ordinal != len(records) + 1 or _hash(row) != digest or row.get("post_id") != post_id
                    or row.get("published_at") != published or not decision["eligible"] or decision["published_at"] != published
                    or row.get("discovery_run_id") != manifest["run_id"] or row.get("source_run_id") != manifest["child_run_id"]
                    or row.get("source_project") != manifest["child_project"]
                    or _hash(row["evidence_packet"]) != row.get("evidence_projection_hash")):
                _error("post_binding_failed", "A stored post failed publication-window or evidence validation")
            records.append(row)
    if len(records) != manifest["requested_posts"] or _hash(records) != receipt["records_sha256"]:
        _error("exact_count_failed", "Stored verified posts do not match the requested count")
    expected_order = sorted(records, key=lambda row: (datetime.fromisoformat(row["published_at"]), int(row["post_id"])), reverse=True)
    if records != expected_order:
        _error("post_order_failed", "The stored post list is not newest first")
    with paths["export"].open(encoding="utf-8") as stream:
        exported = [json.loads(line) for line in stream if line.strip()]
    if exported != records:
        _error("export_binding_failed", "The post export differs from its isolated database")
    return records


def _markdown(summary, records):
    window = summary["publication_window"]
    topic = summary["topic"].replace("<", "&lt;").replace(">", "&gt;")
    lines = ["# POSTS DISCOVERY", "", f"Topic: {topic}", "", f"Run: `{summary['run_id']}`", "",
        f"Status: {summary['status']}; verified posts: {summary['accepted_posts']}/{summary['requested_posts']}.", "",
        f"Publication window: {window['start']} inclusive to {window['end']} exclusive.", "",
        "Publication recency is required. This list is newest-first among collected eligible results, not an exhaustive global TikTok feed.", "", "## Posts", ""]
    for row in records:
        packet = row["evidence_packet"]
        lines.append(f"- {row['published_at']} — @{packet.get('creator', '')} — <{packet.get('url', '')}> — post {row['post_id']}.")
    if not records:
        lines.append("No completed validated post export yet. Preserve the same plan and child handoff.")
    lines.extend(["", "## Collection", "", f"Child project: `{summary['child_project']}`.", "",
        "No artist qualification, audio download, AI analysis or outbound action was performed.", "", "## Limitations", "",
        *["- " + limitation for limitation in summary["limitations"]], ""])
    return "\n".join(lines)


def report_run(run_dir):
    root, manifest = _read(run_dir)
    summary = _summary(root, manifest)
    records = _read_local_records(root, manifest) if manifest["state"] == "complete" else []
    summary["posts"] = _post_summary(records)
    paths = _paths(root, manifest)
    _atomic_text(paths["review_json"], json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    _atomic_text(paths["review_markdown"], _markdown(summary, records))
    return summary


def validate_run(run_dir):
    root, manifest = _read(run_dir)
    summary = _summary(root, manifest)
    records = _read_local_records(root, manifest)
    summary["posts"] = _post_summary(records)
    paths = _paths(root, manifest)
    if (not paths["review_json"].is_file() or not paths["review_markdown"].is_file()
            or json.loads(paths["review_json"].read_text(encoding="utf-8")) != summary
            or paths["review_markdown"].read_text(encoding="utf-8") != _markdown(summary, records)):
        _error("report_stale", "Generate the report from current stored state before validating")
    return {**summary, "validated": True, "validation_result": "pass"}


def _post_summary(records):
    return [{"post_id": row["post_id"], "published_at": row["published_at"],
             "creator": row["evidence_packet"].get("creator", ""),
             "url": row["evidence_packet"].get("url", "")}
            for row in records]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="Freeze a topic, post count and required publication-time window; offline")
    plan.add_argument("--topic", required=True)
    plan.add_argument("--posts", required=True, type=_count)
    window = plan.add_mutually_exclusive_group(required=True)
    window.add_argument("--last-hours", type=float, help="Positive hours before plan time; freezes the absolute UTC window once")
    window.add_argument("--since", help="Inclusive timezone-aware publication start; requires --until")
    plan.add_argument("--until", help="Exclusive timezone-aware publication end; required with --since, invalid with --last-hours")
    plan.add_argument("--label", default="")
    plan.add_argument("--expected-account", default="")
    plan.add_argument("--output-root", type=Path)
    for name in ("collect", "status", "report", "validate"):
        command = commands.add_parser(name)
        command.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            result = plan_posts(topic=args.topic, posts=args.posts, last_hours=args.last_hours,
                since=args.since, until=args.until, label=args.label, expected_account=args.expected_account, output_root=args.output_root)
        else:
            result = {"collect": collect_posts, "status": status_run, "report": report_run, "validate": validate_run}[args.command](args.run_dir)
        print(json.dumps(result, ensure_ascii=True, indent=2), flush=True)
        return 2 if args.command == "collect" and result.get("status") != "collection_complete" else 0
    except PostsDiscoveryError as exc:
        print(json.dumps({"workflow": "posts_discovery", "status": "attention_required", "reason": exc.code, "message": str(exc)}), flush=True)
        return 2
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, OverflowError):
        print(json.dumps({"workflow": "posts_discovery", "status": "attention_required", "reason": "stored_or_operator_data_invalid",
            "message": "A source, stored artifact or structured result could not be validated; preserve the run and inspect its bound handoff"}), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
