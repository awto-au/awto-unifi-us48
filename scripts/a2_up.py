#!/usr/bin/env python3
"""a2_up — power-cycle, RAM-boot a TFTP image at the awto-uboot window, wait for ssh, mount debugfs (#79).

Usage: a2_up.py [IMAGE]   (default tmp/mainline/us48-mainline-debug.bin; log tmp/logs/a2_up.log)
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/a2_up.log"
# ssh up ~20 s after power-on from flash (Dan 2026-11-15); RAM boot is the same kernel start: 40 s = 2x
SSH_S = 40.0


def run(*a: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *a], capture_output=True, text=True, cwd=ROOT)


def main() -> int:
    img = sys.argv[1] if len(sys.argv) > 1 else "tmp/mainline/us48-mainline-debug.bin"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    for r in (run("scripts/u2_run.py", "echo ready"), run("scripts/dev.py", "u2-boot", "--from", "tftp", "--image", img)):
        LOG.open("a").write(r.stdout + r.stderr)
        if r.returncode:
            print(f"failed: {r.stdout[-300:]}{r.stderr[-300:]}")
            return 1
    t0 = time.monotonic()
    while time.monotonic() - t0 < SSH_S:
        if run("scripts/lx.py", "mountpoint -q /sys/kernel/debug || mount -t debugfs none /sys/kernel/debug").returncode == 0:
            print(f"ssh up {time.monotonic() - t0:.0f} s after handover")
            return 0
        time.sleep(2)
    print(f"no ssh in {SSH_S} s")
    return 1


if __name__ == "__main__":
    sys.exit(main())
