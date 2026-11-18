#!/usr/bin/env python3
"""sync_shared — vendored copies of awto-dan scripts, byte-identical (never edit the copy).

Usage: sync_shared.py check   every copy equals its awto-dan source (rc 1 on drift; rc 0 + log line if awto-dan is absent)
       sync_shared.py stage   copy the sources over the vendored files
- Copies: scripts/_lab.py <- awto-dan scripts/lab.py (bench topology reader, docs/lab/lab.toml, awto-dan#109)
- awto-dan location: $AWTO_DAN_REPO, else the checkout beside this repo's main checkout. Log: tmp/logs/sync_shared.log
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def repo_root() -> Path:
    env = os.environ.get("US48_REPO")
    if env:
        return Path(env)
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, cwd=HERE)
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else HERE.parent


ROOT = repo_root()
LOG = ROOT / "tmp/logs/sync_shared.log"
COPIES = {"scripts/_lab.py": "scripts/lab.py"}  # vendored path here -> path in awto-dan


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def dan_repo() -> Path | None:
    sys.path.insert(0, str(HERE))
    import _lab
    try:
        return _lab.dan_repo()
    except FileNotFoundError:
        return None


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def check(dan: Path | None) -> int:
    if dan is None:
        log("check: awto-dan not found, skipped")
        return 0
    bad = [v for v, s in COPIES.items() if not (ROOT / v).exists() or sha(ROOT / v) != sha(dan / s)]
    if bad:
        log(f"DRIFT: {bad} differ from awto-dan; run sync_shared.py stage")
        return 1
    log(f"check: {len(COPIES)} vendored file(s) identical to awto-dan")
    return 0


def stage(dan: Path | None) -> int:
    if dan is None:
        log("stage: awto-dan not found; set AWTO_DAN_REPO")
        return 1
    for v, s in COPIES.items():
        shutil.copyfile(dan / s, ROOT / v)
        log(f"staged {v} <- {dan / s}")
    return check(dan)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["check", "stage"])
    a = ap.parse_args()
    dan = dan_repo()
    return check(dan) if a.cmd == "check" else stage(dan)


if __name__ == "__main__":
    sys.exit(main())
