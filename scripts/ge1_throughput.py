#!/usr/bin/env python3
"""ge1_throughput: TCP bulk rate + board CPU through ge1 (the CPU-path baseline for #174/#118).

Usage: scripts/ge1_throughput.py [--mb 100]
- Host -> board (board `nc -l` into /dev/null; host ncat needs --send-only to see EOF). Rate = MB / host wall time.
- CPU = busybox `top -bn1` idle% sampled mid-transfer (1 sample); board address from _ipreg.
- Waits: transfer limit 30 s (100 MB at the 1 Gb/s line rate is 0.8 s, 37x); expiry = FAIL, logged.
Log: tmp/logs/ge1_throughput.log
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _ipreg  # noqa: E402
from _cli import help_guard  # noqa: E402
from lx import OPTS  # noqa: E402

LOG = Path(__file__).resolve().parent.parent / "tmp/logs/ge1_throughput.log"
PORT = 5001
LIMIT_S = 30


def ssh(cmd, timeout=LIMIT_S + 20):
    ip = _ipreg.ip("us48-boot")
    r = subprocess.run(["ssh", *OPTS, f"root@{ip}", cmd], capture_output=True, text=True, timeout=timeout)
    return (r.stdout + r.stderr).strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mb", type=int, default=100)
    a = ap.parse_args()
    ip = _ipreg.ip("us48-boot")
    LOG.parent.mkdir(parents=True, exist_ok=True)
    srv = subprocess.Popen(["ssh", *OPTS, f"root@{ip}", f"nc -l -p {PORT} > /dev/null"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)   # listener up: one ssh round trip, measured ~0.3 s; 1 s = about 3x
    t0 = time.monotonic()
    src = subprocess.Popen(f"dd if=/dev/zero bs=1M count={a.mb} 2>/dev/null | nc --send-only {ip} {PORT}", shell=True)
    time.sleep(0.3)
    cpu = ssh("top -bn1 | head -3")
    try:
        src.wait(timeout=LIMIT_S)
    except subprocess.TimeoutExpired:
        src.kill()
        srv.terminate()
        print(f"FAIL: transfer over {LIMIT_S} s")
        return 1
    dt = time.monotonic() - t0
    srv.terminate()
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} rx {a.mb} MB in {dt:.2f} s = {a.mb * 8 / dt:.0f} Mb/s | {cpu.replace(chr(10), ' | ')}"
    print(line)
    with LOG.open("a") as fh:
        fh.write(line + "\n")
    return 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
