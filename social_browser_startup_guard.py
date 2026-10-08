"""Serialize shared browser startup with an OS-released process lock."""
from contextlib import contextmanager
import os
from pathlib import Path
import time


@contextmanager
def startup_guard(runtime_dir: Path, timeout: float = 30):
    runtime_dir.mkdir(parents=True, exist_ok=True)
    path = runtime_dir / "profile-startup.lock"
    handle = path.open("a+b")
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + max(0, timeout)
        while True:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("profile_startup_busy: another caller owns shared browser startup") from None
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
