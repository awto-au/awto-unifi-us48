#!/usr/bin/python3
"""fan_sweep — step the MSPI fan frame through byte-0 / port-byte values from the U-Boot prompt, one line per step.

Usage: scripts/fan_sweep.py [--hold S] [--steps 0x30,0x00,0x40,...] [--ports 0x00]
- Each step prints `STEP n/N byte0=0x.. ports=0x..` BEFORE it sends, so a listener can match the sound to the line.
- Default sweep: 0x30 (loud ref), 0x00 (50 % ref), then single bits 7:6 and 3:0, then 0xff.
- Chain (1,0) only = unit-0 fans (2 of 4). Board must be at u-boot> (scripts/dev.py uboot). Log: tmp/logs/fan_sweep.log
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/fan_sweep.log"
DEFAULT = "0x30,0x00,0x40,0x80,0xc0,0x01,0x02,0x04,0x08,0x0f,0xff,0x00"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # 8 s: a person hears a fan settle within ~2-3 s; 8 s leaves time to say "now" without losing the step.
    ap.add_argument("--hold", type=float, default=8.0)
    ap.add_argument("--steps", default=DEFAULT)
    ap.add_argument("--ports", default="0x00")
    a = ap.parse_args()
    steps = [int(x, 0) for x in a.steps.split(",")]
    LOG.parent.mkdir(parents=True, exist_ok=True)
    for i, b in enumerate(steps, 1):
        line = f"STEP {i}/{len(steps)} byte0=0x{b:02x} ports={a.ports}"
        print(line, flush=True)
        with LOG.open("a") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S%z ") + line + "\n")
        r = subprocess.run([sys.executable, str(ROOT / "scripts/fan_frame.py"), f"0x{b:02x}", "--ports", a.ports],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  frame failed: {r.stdout.strip()} {r.stderr.strip()}", flush=True)
            return 1
        time.sleep(a.hold)
    print("SWEEP DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
