"""Optional GPU lock shared by the GPU steps (reuse_file.py, diarize.py). Stdlib only.

Set env AUDIO_CLEANUP_GPU_LOCK to a directory path to serialise GPU work between processes (several agents or
tools on one GPU). The lock is an atomic mkdir; a waiting process retries every 15 s. Holders touch it while they
work; a lock written by this module and untouched for 20 min is treated as stale (hard-killed run) and removed.
A lock created by another tool is never removed: delete it by hand when nothing runs.
Unset = no locking.

usage (from another script):  from gpu_lock import gpu_lock;  with gpu_lock() as touch: ...; touch()
"""
import contextlib
import os
import time
from pathlib import Path

OWNER = "audio-cleanup"
STALE_S = 20 * 60


@contextlib.contextmanager
def gpu_lock(label=OWNER):
    path = os.environ.get("AUDIO_CLEANUP_GPU_LOCK")
    if not path:
        yield lambda: None
        return
    lock = Path(path)
    while True:
        try:
            lock.mkdir(parents=False)
            (lock / "owner.txt").write_text(f"{OWNER}: {label} pid {os.getpid()}\n", encoding="utf-8")
            break
        except FileExistsError:
            try:
                owner = (lock / "owner.txt").read_text(encoding="utf-8") if (lock / "owner.txt").exists() else ""
                if owner.startswith(OWNER) and time.time() - lock.stat().st_mtime > STALE_S:
                    print(f"stale GPU lock {lock}, removing", flush=True)
                    for p in lock.iterdir():
                        p.unlink()
                    lock.rmdir()
                    continue
            except FileNotFoundError:
                continue
            print(f"waiting for GPU lock {lock} ...", flush=True)
            time.sleep(15)

    def touch():
        try:
            os.utime(lock)
        except FileNotFoundError:
            pass

    try:
        yield touch
    finally:
        try:
            for p in lock.iterdir():
                p.unlink()
            lock.rmdir()
        except FileNotFoundError:
            pass
