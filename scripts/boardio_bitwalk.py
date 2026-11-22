#!/usr/bin/env python3
"""boardio_bitwalk: set each unused bit of fan-frame byte 0 alone (us48_boardio.test_bits) and diff board state before/after (#12).

Usage: scripts/boardio_bitwalk.py [--bits 0x01,0x02,0x04,0x08,0x40,0x80]
- Needs us48_boardio (param test_bits, bench only). Bits 5:4 (fan level) are refused by the module.
- Per bit: write test_bits, 1 snapshot (netdev carrier + speed, PoE port status of both cameras, hwmon temps, new kernel log lines), restore 0.
- Prints only the lines that differ from the baseline taken with test_bits = 0 right before. Same frame goes to both chains ((1,0) and (1,1)).
- Settle: 2 s per bit = link-state and PoE detect changes show within one 1 s PoE scan + SFP/PHY polls; not a measured value.
Log: tmp/logs/boardio_bitwalk.log. Board address from _ipreg (US48_IP_US48_BOOT override).
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

LOG = Path(__file__).resolve().parent.parent / "tmp/logs/boardio_bitwalk.log"
PARAM = "/sys/module/us48_boardio/parameters/test_bits"
SNAP = (
    "for n in /sys/class/net/*; do echo net $(basename $n) $(cat $n/carrier 2>&1) $(cat $n/speed 2>/dev/null); done; "
    "for p in /sys/bus/i2c/devices/*/port*_status; do echo poe $p $(cat $p); done; "
    "for h in /sys/class/hwmon/hwmon*; do for t in $h/temp*_input; do [ -r $t ] && echo temp $t $(( $(cat $t) / 5000 )); done; done; "
    "dmesg | wc -l"
)


def sh(cmd):
    # 30 s: one ssh round trip plus a /sys walk, ~1 s normally; longer = board path gone.
    r = subprocess.run(["ssh", *OPTS, f"root@{_ipreg.ip('us48-boot')}", cmd], capture_output=True, text=True, timeout=30)
    return r.stdout + r.stderr


def snap():
    return [ln for ln in sh(SNAP).splitlines() if ln.strip()]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bits", default="0x01,0x02,0x04,0x08,0x40,0x80")
    a = ap.parse_args()
    bits = [int(x, 0) for x in a.bits.split(",")]
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        def say(s):
            line = f"{time.strftime('%H:%M:%S')} {s}"
            print(line, flush=True)
            fh.write(line + "\n")
            fh.flush()
        if "No such file" in sh(f"ls {PARAM}"):
            say("FAIL: us48_boardio has no test_bits param")
            return 1
        sh(f"echo 0 > {PARAM}")
        base = snap()
        say(f"baseline {len(base)} lines")
        for b in bits:
            out = sh(f"echo {b} > {PARAM}; echo rc=$?")
            time.sleep(2)
            s = snap()
            sh(f"echo 0 > {PARAM}")
            diff = [ln for ln in s if ln not in base]
            gone = [ln for ln in base if ln not in s and not ln.startswith(("temp", "5", "6", "7", "8", "9"))]
            say(f"bit 0x{b:02x} write {out.strip()} | changed: " + ("none" if not diff and not gone else "; ".join(diff + ["-" + g for g in gone])))
    return 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
