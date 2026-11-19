#!/usr/bin/env python3
"""led_cycle: step the logo (system) LED and all port LEDs through every colour combination, fixed dwell each.

Usage: scripts/led_cycle.py [--dwell 0.5] [--rounds 2] [--only logo|ports|chase]
- --only chase: running light, one LED at a time: green odd 1-47 then even 48-2, yellow the same, then the cages
  SFP+2, SFP+1, SFP1, SFP2 in green then yellow (panel labels; front 50, 49, 51, 52), then repeat.
- Logo LED: white:status / blue:status brightness 00, 10, 01, 11 (only the two cited pins, docs/leds.md).
- Port LEDs: pattern for all 52 columns with bit0 / bit1 = 00, 01 (green), 10 (amber), 11.
- One ssh session; commands are piped with host-side timing. Restores white steady + pattern hw at the end.
Log: tmp/logs/led_cycle.log (one line per step with the time it was sent).
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
LOG = ROOT / "tmp/logs/led_cycle.log"
L = "/sys/devices/platform/us48-leds"
W, B = "/sys/class/leds/white:status", "/sys/class/leds/blue:status"
NCOL = 52


def logo_steps():
    yield "logo trigger none", f"echo none > {W}/trigger"
    for w, b in ((0, 0), (1, 0), (0, 1), (1, 1)):
        yield f"logo white={w} blue={b}", f"echo {w} > {W}/brightness; echo {b} > {B}/brightness"


def port_steps():
    for b0, b1, name in ((0, 0, "off"), (1, 0, "bit0 green"), (0, 1, "bit1 amber"), (1, 1, "both")):
        pat = ("7" if b0 else "0") * NCOL + ("7" if b1 else "0") * NCOL
        yield f"ports {name}", f"echo {pat} > {L}/pattern"


def one_led(front, bit):
    """Pattern with only front port `front` lit: bit 0 = green, bit 1 = amber (column = front - 1)."""
    pat = ["0"] * (2 * NCOL)
    pat[bit * NCOL + front - 1] = "7"
    return "".join(pat)


def chase_steps():
    """Running light: odd 1..47 up, even 48..2 down, then cages SFP+2, SFP+1, SFP1, SFP2 (front 50, 49, 51, 52)."""
    copper = list(range(1, 48, 2)) + list(range(48, 1, -2))
    cages = (50, 49, 51, 52)
    for bit, colour in ((0, "green"), (1, "yellow")):
        for f in copper:
            yield f"chase {colour} port {f}", f"echo {one_led(f, bit)} > {L}/pattern"
    for bit, colour in ((0, "green"), (1, "yellow")):
        for f in cages:
            yield f"chase {colour} cage front {f}", f"echo {one_led(f, bit)} > {L}/pattern"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dwell", type=float, default=0.5)
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--only", choices=("logo", "ports", "chase"))
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    ssh = subprocess.Popen(["ssh", *OPTS, f"root@{_ipreg.ip('us48-boot')}", "sh"], stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, text=True)
    ssh.stdin.write(f"test -w {L}/pattern && echo READY\n")
    ssh.stdin.flush()
    # Board must answer before any step is timed: a step printed without a live session misleads the viewer.
    if ssh.stdout.readline().strip() != "READY":
        print("board not reachable or LED sysfs missing: nothing sent", file=sys.stderr)
        ssh.kill()
        return 1
    with LOG.open("a") as log:
        try:
            for r in range(a.rounds):
                if a.only == "chase":
                    steps = list(chase_steps())
                else:
                    steps = list(logo_steps() if a.only != "ports" else []) + \
                        list(port_steps() if a.only != "logo" else [])
                for name, cmd in steps:
                    line = f"{time.strftime('%H:%M:%S')} round {r + 1} {name}"
                    print(line, flush=True)
                    log.write(line + "\n")
                    ssh.stdin.write(cmd + "\n")
                    ssh.stdin.flush()
                    time.sleep(a.dwell)
        finally:
            ssh.stdin.write(f"echo 0 > {B}/brightness; echo 1 > {W}/brightness; echo hw > {L}/pattern; exit\n")
            ssh.stdin.close()
            # 15 s: one ssh command round trip is about 1-2 s; longer means the board is gone.
            ssh.wait(timeout=15)
            log.write("# restored white steady + pattern hw\n")
    return 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
