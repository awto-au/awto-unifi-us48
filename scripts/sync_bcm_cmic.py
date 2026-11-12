#!/usr/bin/env python3
"""sync_bcm_cmic — stage common/bcm-cmic/ into a consumer tree as a byte-identical read-only copy.

Usage: sync_bcm_cmic.py stage uboot [--dest DIR]   copy core + uboot/{Kconfig,Makefile} (default dest: U-Boot tree lib/bcm-cmic)
       sync_bcm_cmic.py check [--dest DIR]         every staged file equals common/ (rc 1 on drift); skipped if dest absent
- Single source: common/bcm-cmic/ (docs/uboot-second-stage.md section 9). Never edit the staged copy.
- Log: tmp/logs/sync_bcm_cmic.log
"""
import argparse
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path


def repo_root() -> Path:
    env = os.environ.get("US48_REPO")
    if env:
        return Path(env)
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                       cwd=Path(__file__).resolve().parent)
    if r.returncode == 0 and r.stdout.strip():
        return Path(r.stdout.strip())
    return Path(__file__).resolve().parent.parent


ROOT = repo_root()
SRC = ROOT / "common/bcm-cmic"
LOG = ROOT / "tmp/logs/sync_bcm_cmic.log"
DEFAULT_DEST = ROOT / "tmp/u-boot-v2026.07/lib/bcm-cmic"
# staged name -> source path under common/bcm-cmic/ (per-OS build files are renamed, not core)
UBOOT = {"Kconfig": "uboot/Kconfig", "Makefile": "uboot/Makefile"}


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def mapping() -> dict:
    m = {p.name: p.relative_to(SRC).as_posix() for p in sorted(SRC.glob("*")) if p.suffix in (".c", ".h")}
    return {**m, **UBOOT}


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def stage(dest: Path) -> int:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for name, rel in mapping().items():
        d = dest / name
        shutil.copyfile(SRC / rel, d)
        d.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    log(f"staged {len(mapping())} files -> {dest}")
    return check(dest)


def check(dest: Path) -> int:
    if not dest.exists():
        log(f"check: {dest} absent, skipped")
        return 0
    bad = [n for n, rel in mapping().items() if not (dest / n).exists() or sha(dest / n) != sha(SRC / rel)]
    if bad:
        log(f"DRIFT in {dest}: {bad}")
        return 1
    log(f"check: {dest} identical to common/bcm-cmic ({len(mapping())} files)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["stage", "check"])
    ap.add_argument("target", nargs="?", default="uboot", choices=["uboot"])
    ap.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    a = ap.parse_args()
    return stage(a.dest) if a.cmd == "stage" else check(a.dest)


if __name__ == "__main__":
    sys.exit(main())
