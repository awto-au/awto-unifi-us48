"""flash_lock - tmp/flash_write.lock: a flash write is in progress (pid + region); tmp/flash_write.stop: stop after it (#139).

Library only. dev._uboot_cmd takes the lock at `sf erase` and drops it when the following `sf write/update` ends or fails.
- storm_watch must not cut power while held: it calls request_stop() and keeps watching.
- Flash flows call stop_requested() before the next `sf erase` and abort instead of starting another write.
- A lock whose pid is dead is stale and ignored.
- Log (real lock only, not the selftest paths): tmp/logs/flash_lock.log
"""
import json
import os
import time
from pathlib import Path

TMP = Path(__file__).resolve().parent.parent / "tmp"
LOCK = TMP / "flash_write.lock"
STOP = TMP / "flash_write.stop"
LOG = TMP / "logs/flash_lock.log"
_T0 = time.time()  # a stop request older than this process is stale


def _log(lock: Path, msg: str) -> None:
    if lock == LOCK:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} pid {os.getpid()} {msg}\n")


def acquire(region: str, lock: Path = LOCK) -> None:
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": os.getpid(), "region": region}))
    _log(lock, f"acquire: {region}")


def release(lock: Path = LOCK) -> None:
    if lock.exists():
        _log(lock, "release")
    lock.unlink(missing_ok=True)


def held(lock: Path = LOCK) -> dict | None:
    """{'pid', 'region'} of a live holder, else None."""
    try:
        info = json.loads(lock.read_text())
        os.kill(info["pid"], 0)
        return info
    except (OSError, ValueError, KeyError):
        return None


def request_stop(stop: Path = STOP) -> None:
    stop.parent.mkdir(parents=True, exist_ok=True)
    stop.write_text("storm")


def stop_requested(stop: Path = STOP) -> bool:
    return stop.exists() and stop.stat().st_mtime >= _T0
