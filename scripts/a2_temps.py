#!/usr/bin/env python3
"""a2_temps — print die + LM75 temperatures of the board (deg C) and the stop limits (die 108, LM75 78); rc 1 at a limit."""
import subprocess
import sys
from pathlib import Path
from _cli import help_guard  # noqa: E402

LX = Path(__file__).resolve().parent / "lx.py"
CMD = 'for h in /sys/class/hwmon/hwmon*; do echo "$(cat $h/name) $(cat $h/temp1_input)"; done'


def main() -> int:
    r = subprocess.run([sys.executable, str(LX), CMD], capture_output=True, text=True)
    bad = 0
    out = []
    for ln in r.stdout.splitlines():
        n, _, v = ln.partition(" ")
        if not v.strip().isdigit():
            continue
        c = int(v) / 1000
        if n.startswith("die") or n.startswith("lm75"):
            out.append(f"{n} {c:.1f}")
            bad |= c >= (108 if n.startswith("die") else 78)
    print(" ".join(out) + ("  LIMIT" if bad else ""))
    return 1 if bad or not out else 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
