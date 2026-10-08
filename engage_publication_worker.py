"""Owned publication workers with bounded waits and non-secret progress.

This module never starts, stops, or repairs a browser. A timeout cancels only
the Python child this supervisor created, and never authorizes another submit.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import psutil


STATE_ENV = "ENGAGE_PUBLICATION_WORKER_STATE"
NONCE_ENV = "ENGAGE_PUBLICATION_WORKER_NONCE"
PROGRESS_PREFIX = "ENGAGE_PUBLICATION_PROGRESS "
SCHEMA = "engage-publication-worker-v1"
DEFAULT_TOTAL_TIMEOUT = 1200.0
DEFAULT_STALL_TIMEOUT = 600.0
DEFAULT_CANCEL_GRACE = 10.0
MAX_STDOUT_BYTES = 2 * 1024 * 1024
_PHASE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class WorkerCancellationRequested(RuntimeError):
    """A supervisor requested a stop; it does not imply submit was absent."""


class WorkerBusyError(RuntimeError):
    pass


def process_identity(pid: int | None = None) -> dict[str, Any]:
    process = psutil.Process(os.getpid() if pid is None else pid)
    return {"pid": process.pid, "create_time": process.create_time()}


def identity_alive(identity: dict[str, Any]) -> bool | None:
    """False proves the original process is gone; uncertainty is not False."""
    try:
        pid = int(identity["pid"])
        created = float(identity["create_time"])
        if pid <= 0 or not math.isfinite(created) or created <= 0:
            return None
        return psutil.Process(pid).create_time() == created
    except psutil.NoSuchProcess:
        return False
    except (psutil.Error, OSError, ValueError, TypeError, KeyError):
        return None


def worker_state_path(
    database: Path | str, publication_id: str, social_browser_state: Path | str
) -> Path:
    key = hashlib.sha256(
        (os.path.normcase(str(Path(database).resolve())) + "\0" + publication_id).encode()
    ).hexdigest()
    return Path(social_browser_state).resolve().parent / "publication_workers" / f"{key}.json"


def _read(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > 32768:
            return {}
        result = json.loads(path.read_text(encoding="utf-8"))
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=True, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        # Windows can briefly deny replacement while the supervisor has the
        # small progress file open. Retry only that sharing failure, bounded.
        deadline = time.monotonic() + 0.5
        while True:
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
    finally:
        temporary.unlink(missing_ok=True)


def _close_guard(guard: Any) -> None:
    if guard is not None:
        guard.close()  # Kernel releases the advisory lock even after a process crash.


def _claim_lock(path: Path, nonce: str, owner: dict[str, Any]) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True)
    guard = path.with_suffix(".guard").open("a+b")
    try:
        guard.seek(0)
        if not guard.read(1):
            guard.write(b"0")
            guard.flush()
        guard.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(guard.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        guard.close()
        raise WorkerBusyError("publication_worker_guard_held") from None
    payload = {"schema": SCHEMA, "nonce": nonce, "owner": owner}
    try:
        if path.exists():
            previous = _read(path)
            # A dead supervisor does not imply its publishing child is dead.
            identities = [previous.get("owner", {})]
            if previous.get("worker"):
                identities.append(previous["worker"])
            state = _read(path.with_suffix(".json"))
            if state.get("nonce") == previous.get("nonce") and state.get("worker"):
                identities.append(state["worker"])
            if not previous or any(identity_alive(item) is not False for item in identities):
                raise WorkerBusyError("publication_worker_alive_or_unverifiable")
            path.unlink()
        _atomic_write(path, payload)
        return guard
    except BaseException:
        guard.close()
        raise


def _release_lock(path: Path, nonce: str, guard: Any = None) -> None:
    try:
        if _read(path).get("nonce") == nonce:
            path.unlink(missing_ok=True)
    finally:
        _close_guard(guard)


def _safe_progress(value: dict[str, Any]) -> dict[str, Any] | None:
    phase = value.get("phase")
    if value.get("schema") != SCHEMA or not isinstance(phase, str) or not _PHASE.fullmatch(phase):
        return None
    result: dict[str, Any] = {"schema": SCHEMA, "phase": phase}
    for key in ("submit_intent", "publication_confirmed"):
        result[key] = value.get(key) is True
    for key in ("progress_sequence", "heartbeat_at", "phase_started_at"):
        number = value.get(key)
        if isinstance(number, (int, float)) and not isinstance(number, bool) and math.isfinite(number):
            result[key] = number
    return result


class WorkerProgress:
    def __init__(self, path: Path | None, nonce: str, *, owns_lock: bool = False, guard: Any = None,
                 publication_id: str = "", database: Path | str | None = None):
        self.path = path
        self.nonce = nonce
        self._owns_lock = owns_lock
        self._guard = guard
        self._mutex = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {
            "schema": SCHEMA, "nonce": nonce, "worker": process_identity(),
            "phase": "starting", "status": "running", "progress_sequence": 0,
            "phase_started_at": time.time(), "heartbeat_at": time.time(),
            "submit_intent": False, "publication_confirmed": False,
            "publication_id": publication_id, "database": str(Path(database).resolve()) if database else "",
            "attempt_id": "",
        }
        if path:
            self._write()
            self._thread = threading.Thread(target=self._heartbeat, daemon=True)
            self._thread.start()

    @classmethod
    def from_environment(
        cls, *, publication_id: str = "", database: Path | str | None = None,
        social_browser_state: Path | str | None = None,
    ) -> "WorkerProgress":
        path_value = os.environ.get(STATE_ENV, "")
        nonce = os.environ.get(NONCE_ENV, "")
        if path_value and re.fullmatch(r"[0-9a-f]{32}", nonce):
            path = Path(path_value)
            lock = _read(path.with_suffix(".lock"))
            if lock.get("nonce") != nonce or identity_alive(lock.get("owner", {})) is not True:
                raise WorkerBusyError("publication_supervisor_owner_unverifiable")
            return cls(path, nonce, publication_id=publication_id, database=database)
        if database is not None and social_browser_state is not None and publication_id:
            path = worker_state_path(database, publication_id, social_browser_state)
            nonce = uuid.uuid4().hex
            guard = _claim_lock(path.with_suffix(".lock"), nonce, process_identity())
            try:
                return cls(path, nonce, owns_lock=True, guard=guard, publication_id=publication_id, database=database)
            except BaseException:
                _release_lock(path.with_suffix(".lock"), nonce, guard)
                raise
        return cls(None, "")

    def _write(self) -> None:
        if self.path:
            _atomic_write(self.path, self._state)

    def _heartbeat(self) -> None:
        while not self._stop.wait(2.0):
            with self._mutex:
                self._state["heartbeat_at"] = time.time()
                try:
                    self._write()
                except OSError:
                    # Missing progress cannot extend the supervisor's deadline.
                    pass

    def checkpoint(self) -> None:
        if self.path:
            cancellation = _read(self.path.with_suffix(".cancel"))
            if cancellation.get("nonce") == self.nonce:
                raise WorkerCancellationRequested("publication_worker_cancel_requested")

    def phase(
        self, name: str, *, submit_intent: bool = False,
        publication_confirmed: bool = False,
        attempt_id: str = "",
    ) -> None:
        if not _PHASE.fullmatch(name):
            raise ValueError("Invalid internal publication phase")
        if attempt_id and not re.fullmatch(r"[0-9a-f]{32}", attempt_id):
            raise ValueError("Invalid publication attempt identity")
        self.checkpoint()
        with self._mutex:
            self._state.update(phase=name, phase_started_at=time.time(), heartbeat_at=time.time())
            self._state["progress_sequence"] += 1
            self._state["submit_intent"] |= submit_intent
            self._state["publication_confirmed"] |= publication_confirmed
            if attempt_id:
                self._state["attempt_id"] = attempt_id
            self._write()
            print(PROGRESS_PREFIX + json.dumps(_safe_progress(self._state)), file=sys.stderr, flush=True)

    def close(self, status: str = "complete") -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)
        with self._mutex:
            self._state["status"] = status if status in {"complete", "failed", "cancelled"} else "failed"
            self._state["heartbeat_at"] = time.time()
            try:
                self._write()
            finally:
                if self.path and self._owns_lock:
                    _release_lock(self.path.with_suffix(".lock"), self.nonce, self._guard)
                    self._guard = None


def _reader(stream: Any, chunks: list[str], *, stdout: bool) -> None:
    used = 0
    try:
        for line in iter(lambda: stream.readline(8192), ""):
            if stdout:
                # Bounded retention; continue draining so a noisy child cannot block.
                remaining = MAX_STDOUT_BYTES - used
                if remaining > 0:
                    chunks.append(line[:remaining])
                    used += len(chunks[-1])
            elif line.startswith(PROGRESS_PREFIX) and len(line) <= 2048:
                try:
                    safe = _safe_progress(json.loads(line[len(PROGRESS_PREFIX):]))
                    if safe:
                        print(PROGRESS_PREFIX + json.dumps(safe), file=sys.stderr, flush=True)
                except (ValueError, TypeError, AttributeError):
                    pass
            # Raw stderr can contain tokens, signed URLs, and CDP error payloads.
    finally:
        stream.close()


def _stop_owned_child(child: subprocess.Popen[str], identity: dict[str, Any]) -> bool:
    if child.poll() is not None:
        return True
    if child.pid != identity.get("pid") or identity_alive(identity) is not True:
        return False
    child.terminate()
    try:
        child.wait(timeout=5.0)
        return True
    except subprocess.TimeoutExpired:
        if identity_alive(identity) is not True:
            return child.poll() is not None
        child.kill()
        try:
            child.wait(timeout=5.0)
            return True
        except subprocess.TimeoutExpired:
            return False


def supervise_adapter(
    command: list[str], *, database: Path | str, publication_id: str,
    social_browser_state: Path | str, total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
    stall_timeout: float = DEFAULT_STALL_TIMEOUT, cancel_grace: float = DEFAULT_CANCEL_GRACE,
) -> subprocess.CompletedProcess[str]:
    for value in (total_timeout, stall_timeout, cancel_grace):
        if not math.isfinite(value) or value <= 0:
            raise ValueError("Worker deadlines must be positive finite seconds")
    path = worker_state_path(database, publication_id, social_browser_state)
    nonce = uuid.uuid4().hex
    owner = process_identity()
    lock_path = path.with_suffix(".lock")
    try:
        guard = _claim_lock(lock_path, nonce, owner)
    except WorkerBusyError:
        return subprocess.CompletedProcess(command, 1, json.dumps({
            "status": "worker_busy", "reason": "publication_worker_alive_or_unverifiable",
            "worker_progress_path": str(path),
        }), "")
    child = None
    identity: dict[str, Any] = {}
    readers: list[threading.Thread] = []
    stdout: list[str] = []
    retain_lock = False
    try:
        environment = dict(os.environ)
        environment.update({STATE_ENV: str(path), NONCE_ENV: nonce, "PYTHONUNBUFFERED": "1"})
        # Only this exact child handle can be terminated by this supervisor.
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, encoding="utf-8", errors="replace", env=environment)
        try:
            identity = process_identity(child.pid)
        except psutil.NoSuchProcess:
            identity = {}
        _atomic_write(lock_path, {"schema": SCHEMA, "nonce": nonce, "owner": owner, "worker": identity})
        for stream, is_stdout in ((child.stdout, True), (child.stderr, False)):
            reader = threading.Thread(target=_reader, args=(stream, stdout), kwargs={"stdout": is_stdout}, daemon=True)
            readers.append(reader)
            reader.start()
        started = progressed = time.monotonic()
        last_sequence = -1
        last_notice = started
        reason = ""
        while child.poll() is None:
            now = time.monotonic()
            state = _read(path)
            if state.get("nonce") == nonce:
                sequence = state.get("progress_sequence")
                if isinstance(sequence, int) and sequence > last_sequence:
                    progressed, last_sequence = now, sequence
                if now - last_notice >= 15.0:
                    safe = _safe_progress(state)
                    if safe:
                        print(PROGRESS_PREFIX + json.dumps(safe), file=sys.stderr, flush=True)
                    last_notice = now
            if now - started >= total_timeout:
                reason = "worker_total_deadline_exceeded"
                break
            if now - progressed >= stall_timeout:
                reason = "worker_phase_deadline_exceeded"
                break
            time.sleep(0.1)
        if reason:
            _atomic_write(path.with_suffix(".cancel"), {"nonce": nonce, "reason": reason})
            try:
                child.wait(timeout=cancel_grace)
            except subprocess.TimeoutExpired:
                retain_lock = not _stop_owned_child(child, identity)
            state = _read(path)
            confirmed = state.get("nonce") == nonce and state.get("publication_confirmed") is True
            return subprocess.CompletedProcess(command, 1, json.dumps({
                "status": "reconcile_required", "reason": reason,
                "publication_confirmed": confirmed,
                "worker_stopped": child.poll() is not None,
                "worker_progress_path": str(path),
                "next_action": "inspect_durable_receipt_and_reconcile_before_any_retry",
            }), "")
        for reader in readers:
            reader.join(timeout=2.0)
        return subprocess.CompletedProcess(command, child.returncode, "".join(stdout), "")
    except KeyboardInterrupt:
        if child is not None:
            _atomic_write(path.with_suffix(".cancel"), {"nonce": nonce, "reason": "supervisor_interrupted"})
            try:
                child.wait(timeout=cancel_grace)
            except subprocess.TimeoutExpired:
                retain_lock = not _stop_owned_child(child, identity)
        return subprocess.CompletedProcess(command, 1, json.dumps({
            "status": "reconcile_required", "reason": "supervisor_interrupted",
            "worker_progress_path": str(path),
        }), "")
    except (OSError, psutil.Error) as exc:
        if child is not None and child.poll() is None:
            retain_lock = not _stop_owned_child(child, identity)
        return subprocess.CompletedProcess(command, 1, json.dumps({
            "status": "reconcile_required" if child is not None else "worker_start_failed",
            "error_class": type(exc).__name__, "worker_progress_path": str(path),
        }), "")
    finally:
        if not retain_lock:
            _release_lock(lock_path, nonce, guard)
        else:
            _close_guard(guard)
