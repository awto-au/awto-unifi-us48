#!/usr/bin/python3
"""build_us48_cmic — build kernel/us48-cmic/us48_cmic.ko against the OpenWrt hr2 kernel tree.

Usage: scripts/build_us48_cmic.py [--clean]
- The module must match the kernel in the flashed image: build both from the same tmp/openwrt-build tree.
- Output: kernel/us48-cmic/us48_cmic.ko (gitignored build artefacts). Log: tmp/logs/build_us48_cmic.log
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


def repo_root() -> Path:
    env = os.environ.get("US48_REPO")
    if env:
        return Path(env)
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                             cwd=Path(__file__).resolve().parent, check=True)
        return Path(out.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return Path(__file__).resolve().parent.parent


ROOT = repo_root()
OWRT = ROOT / "tmp/openwrt-build"
KDIR = OWRT / "build_dir/target-arm_cortex-a9_musl_eabi/linux-bcm53xx_hr2/linux-6.18.55"
CROSS = OWRT / "staging_dir/toolchain-arm_cortex-a9_gcc-14.4.0_musl_eabi/bin/arm-openwrt-linux-muslgnueabi-"
MOD = ROOT / "kernel/us48-cmic"
LOG = ROOT / "tmp/logs/build_us48_cmic.log"
# One small module: ~10 s cold. 60 s = 6x backstop for a loaded host; expiry = make killed, logged.
MAKE_TIMEOUT_S = 60


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--clean", action="store_true")
    a = ap.parse_args()
    env = dict(os.environ, ARCH="arm", CROSS_COMPILE=str(CROSS), STAGING_DIR=str(OWRT / "staging_dir"))
    cmd = ["make", "-C", str(KDIR), f"M={MOD}", "clean" if a.clean else "modules"]
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} $ {' '.join(cmd)}\n")
        f.flush()
        try:
            r = subprocess.run(cmd, env=env, stdout=f, stderr=subprocess.STDOUT, timeout=MAKE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            f.write(f"make timed out after {MAKE_TIMEOUT_S} s\n")
            print(f"FAILED: make timed out after {MAKE_TIMEOUT_S} s (see {LOG})")
            return 1
    ko = MOD / "us48_cmic.ko"
    if r.returncode or (not a.clean and not ko.exists()):
        print(f"FAILED rc={r.returncode} (see {LOG})")
        return 1
    print("cleaned" if a.clean else f"built {ko.relative_to(ROOT)} ({ko.stat().st_size} B)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
