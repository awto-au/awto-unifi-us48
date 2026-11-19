#!/usr/bin/env python3
"""led_cycle: step the logo (system) LED and all port LEDs through every colour combination, fixed dwell each.

Usage: scripts/led_cycle.py [--dwell 0.5] [--rounds 2] [--only logo|ports|chase]
- --only chase: running light, one LED at a time: green odd 1-47 then even 48-2, yellow the same, then the cages
  SFP+2, SFP+1, SFP1, SFP2 in green then yellow (panel labels; front 50, 49, 51, 52), then repeat.
  PoE LED of the same front port lights with it: field 1 in the green pass, 2 in the yellow pass; cages: PoE off.
- PoE LEDs (us48_boardio poe_pattern, docs/leds.md "PoE LEDs"): skipped with a note when the driver is absent.
  Fields 0-2 only (3 is refused by the driver); no port power is involved.
- Logo LED: white:status / blue:status brightness 00, 10, 01, 11 (only the two cited pins, docs/leds.md).
- Port LEDs: pattern for all 52 columns with bit0 / bit1 = 00, 01 (green), 10 (amber), 11; all 48 PoE LEDs with
  them: 0, 1, 2, 1.
- One ssh session; commands are piped with host-side timing. Restores white steady + pattern hw + PoE LEDs off.
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
POE = "/sys/devices/platform/us48-board-io/poe_pattern"
NCOL = 52
NPOE = 48


def logo_steps():
    yield "logo trigger none", f"echo none > {W}/trigger"
    for w, b in ((0, 0), (1, 0), (0, 1), (1, 1)):
        yield f"logo white={w} blue={b}", f"echo {w} > {W}/brightness; echo {b} > {B}/brightness"


def poe_cmd(poe, fields):
    """`; echo <48 digits> > poe_pattern`, or nothing without the PoE LED driver."""
    return f"; echo {fields} > {POE}" if poe else ""


def one_poe(front, val):
    """PoE fields with only `front` set to `val` (1 or 2); front 0 = all off."""
    return "".join(str(val) if f == front else "0" for f in range(1, NPOE + 1))


def port_steps(poe):
    for b0, b1, name, pv in ((0, 0, "off", 0), (1, 0, "bit0 green", 1), (0, 1, "bit1 amber", 2), (1, 1, "both", 1)):
        pat = ("7" if b0 else "0") * NCOL + ("7" if b1 else "0") * NCOL
        yield f"ports {name}" + (f", PoE all {pv}" if poe else ""), \
            f"echo {pat} > {L}/pattern" + poe_cmd(poe, str(pv) * NPOE)


def one_led(front, bit):
    """Pattern with only front port `front` lit: bit 0 = green, bit 1 = amber (column = front - 1)."""
    pat = ["0"] * (2 * NCOL)
    pat[bit * NCOL + front - 1] = "7"
    return "".join(pat)


def chase_steps(poe):
    """Running light: odd 1..47 up, even 48..2 down, then cages SFP+2, SFP+1, SFP1, SFP2 (front 50, 49, 51, 52).

    PoE LED of the same port: field 1 with green, 2 with yellow; one poe_pattern write = at most 2 frames (~60 us each).
    """
    copper = list(range(1, 48, 2)) + list(range(48, 1, -2))
    cages = (50, 49, 51, 52)
    for bit, colour in ((0, "green"), (1, "yellow")):
        for f in copper:
            yield f"chase {colour} port {f}" + (f", PoE {bit + 1}" if poe else ""), \
                f"echo {one_led(f, bit)} > {L}/pattern" + poe_cmd(poe, one_poe(f, bit + 1))
    for bit, colour in ((0, "green"), (1, "yellow")):
        for f in cages:
            yield f"chase {colour} cage front {f}", f"echo {one_led(f, bit)} > {L}/pattern" + poe_cmd(poe, one_poe(0, 0))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dwell", type=float, default=0.5)
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--only", choices=("logo", "ports", "chase"))
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    ssh = subprocess.Popen(["ssh", *OPTS, f"root@{_ipreg.ip('us48-boot')}", "sh"], stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, text=True)
    ssh.stdin.write(f"test -w {L}/pattern && echo READY; test -w {POE} && echo POE || echo NOPOE\n")
    ssh.stdin.flush()
    # Board must answer before any step is timed: a step printed without a live session misleads the viewer.
    if ssh.stdout.readline().strip() != "READY":
        print("board not reachable or LED sysfs missing: nothing sent", file=sys.stderr)
        ssh.kill()
        return 1
    poe = ssh.stdout.readline().strip() == "POE"
    if not poe:
        print(f"note: {POE} absent (us48_boardio not loaded): PoE LEDs skipped", flush=True)
    with LOG.open("a") as log:
        try:
            for r in range(a.rounds):
                if a.only == "chase":
                    steps = list(chase_steps(poe))
                else:
                    steps = list(logo_steps() if a.only != "ports" else []) + \
                        list(port_steps(poe) if a.only != "logo" else [])
                for name, cmd in steps:
                    line = f"{time.strftime('%H:%M:%S')} round {r + 1} {name}"
                    print(line, flush=True)
                    log.write(line + "\n")
                    ssh.stdin.write(cmd + "\n")
                    ssh.stdin.flush()
                    time.sleep(a.dwell)
        finally:
            ssh.stdin.write(f"echo 0 > {B}/brightness; echo 1 > {W}/brightness; echo hw > {L}/pattern"
                            f"{poe_cmd(poe, 'off')}; exit\n")
            ssh.stdin.close()
            # 15 s: one ssh command round trip is about 1-2 s; longer means the board is gone.
            ssh.wait(timeout=15)
            log.write("# restored white steady + pattern hw" + (" + PoE LEDs off\n" if poe else "\n"))
    return 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
