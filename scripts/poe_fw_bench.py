#!/usr/bin/env python3
"""poe_fw_bench: bench step 2 of docs/poe-fw-rewrite.md - read-only register compare of a test-image chip against a stock chip.

Usage: scripts/poe_fw_bench.py [--test 8-002d] [--stock 8-002c]
- Reads regs 0x00..0x75 over i2c (i2cget -f) from both chips; skips the clear-on-read set (0x00 0x03 0x05 0x07 0x09 0x0b) and 0x60..0x6f (counters).
- Prints every register where the two chips differ; expected: 0x41 (FW_REV 0xa1 vs 0x15). No writes. Board address from _ipreg.
Log: tmp/logs/poe_fw_bench.log
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

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/poe_fw_bench.log"


def run(cmd):
    # 60 s: 118 i2cget forks x ~0.1 s per chip, plus ssh round trip; longer = board path gone.
    r = subprocess.run(["ssh", *OPTS, f"root@{_ipreg.ip('us48-boot')}", cmd], capture_output=True, text=True, timeout=60)
    return r.stdout + r.stderr


SKIP = {0x00, 0x03, 0x05, 0x07, 0x09, 0x0B} | set(range(0x60, 0x70))


def dump(chip):
    bus, addr = chip.split("-")
    cmd = "for r in " + " ".join(f"{r:#04x}" for r in range(0x76) if r not in SKIP) + \
          f"; do echo $r $(i2cget -f -y {int(bus)} 0x{addr[-2:]} $r 2>&1); done"
    out = run(cmd)
    vals = {}
    for line in out.splitlines():
        p = line.split()
        if len(p) == 2 and p[0].startswith("0x"):
            vals[int(p[0], 16)] = p[1]
    return vals


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--test", default="8-002d")
    ap.add_argument("--stock", default="8-002c")
    a = ap.parse_args()
    t, s = dump(a.test), dump(a.stock)
    if len(t) < 100 or len(s) < 100:
        print(f"FAIL: only {len(t)} / {len(s)} registers read (board path down or i2cget failing)", file=sys.stderr)
        return 1
    lines = [f"# {time.strftime('%Y-%m-%dT%H:%M:%S%z')} test {a.test} vs stock {a.stock}: {len(t)} / {len(s)} regs read"]
    for r in sorted(set(t) | set(s)):
        if t.get(r) != s.get(r):
            lines.append(f"reg {r:#04x}: test {t.get(r, '-')} stock {s.get(r, '-')}")
    if len(lines) == 1:
        lines.append("no differences")
    out = "\n".join(lines)
    print(out)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(out + "\n")
    return 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
