#!/usr/bin/env python3
"""sensors_test: every temperature / SFP sensor of the mainline image through the standard Linux interfaces (#21).

Usage: sensors_test.py [inventory|hotplug] [--apply] [--cage 49..52] [--window-s 120]
- Dry-run by default: prints the board commands, runs none. --apply = ssh to the board (root@192.168.25.207, as lx.py).
- Read-only on the board: sysfs/debugfs reads, `ethtool`, `ethtool -m`, i2cget -f of BCM59111 reg 0x12 (op mode, plain
  register) to show PoE is off. Never PoE event registers (0x00/0x03/0x07), never a write.
- inventory: every hwmon device (name, label, *_input + *_label), every thermal zone, the sfp socket state (debugfs: debug profile image),
  `ethtool` + `ethtool -m` on xe49/xe50 (optic + DAC as cabled), PoE chip temperatures with the op-mode byte (PoE off =
  0x00). Checks: LM75 labels TEMP-1..4, die0/die1, 12 x bcm59111 labelled PoE-01..12, sfp sockets sfp-49..52 present,
  `ethtool -m` answers on each xe port whose cage holds a module.
- hotplug: plan for a module swap by hand (Dan): prints the steps, then (with --apply) polls the cage's sfp state, the xe
  carrier and the kernel log once per ssh round trip (~1-2 s) for --window-s; logs every change. The window is the time
  given to a person to pull/insert a module (120 s default); expiry = "no change seen", exit 1.
- Exit: 0 all checks passed, 1 a check failed, 2 usage. Log: tmp/logs/sensors_test.log
"""
import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/sensors_test.log"
BOARD = "root@192.168.25.207"  # ip-registry us48-boot
SSH = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
       "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=8"]
CALL_S = 20.0  # one board call: ~40 sysfs reads + 24 i2c transfers at 100 kHz + ethtool; < 2 s expected, 20 s = board gone
CAGES = (49, 50, 51, 52)
XE = ("xe49", "xe50")

HWMON = ('for h in /sys/class/hwmon/hwmon*; do echo "HW $h $(cat $h/name) [$(cat $h/label 2>/dev/null)]"; '
         'for f in $h/*_input; do [ -e "$f" ] || continue; b=${f%_input}; '
         'echo "  ${f##*/} $(cat $f 2>&1) [$(cat ${b}_label 2>/dev/null)]"; done; done')
THERMAL = 'for z in /sys/class/thermal/thermal_zone*; do echo "TZ $(cat $z/type) $(cat $z/temp 2>&1)"; done'
SFPSTATE = ('for d in /sys/kernel/debug/sfp-*; do [ -e "$d/state" ] && { echo "SFP ${d##*/}"; '
            'sed "s/^/  /" "$d/state"; }; done')
# PoE op mode (reg 0x12, 00 = shutdown per port) of the 12 chips: channel 4/5 bus found by name, as us48-poe-off
POE = ('for ch in 4 5; do bus=; for n in /sys/bus/i2c/devices/i2c-*/name; do grep -q "chan_id $ch)" $n && '
       '{ d=${n%/name}; bus=${d##*i2c-}; }; done; for a in 0x28 0x29 0x2a 0x2b 0x2c 0x2d; do '
       'echo "POE ch$ch $a opmode $(i2cget -f -y $bus $a 0x12 2>&1)"; done; done')


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def board(cmd: str, apply: bool) -> str:
    if not apply:
        log(f"dry-run: {cmd}")
        return ""
    t0 = time.monotonic()
    try:
        r = subprocess.run(SSH + [BOARD, cmd], capture_output=True, text=True, timeout=CALL_S)
    except subprocess.TimeoutExpired:
        raise SystemExit(f"board call over {CALL_S:.0f} s ({time.monotonic() - t0:.1f} s): {cmd[:120]}")
    out = r.stdout + r.stderr
    with LOG.open("a") as f:
        f.write(f"$ {cmd}\n{out}")
    return out


def check(ok: bool, what: str, fails: list) -> None:
    log(("PASS " if ok else "FAIL ") + what)
    if not ok:
        fails.append(what)


def inventory(apply: bool) -> int:
    fails: list = []
    hw = board(HWMON, apply)
    tz = board(THERMAL, apply)
    sfp = board(SFPSTATE, apply)
    eth = {x: board(f"ethtool {x}; echo ---; ethtool -m {x} 2>&1", apply) for x in XE}
    poe = board(POE, apply)
    if not apply:
        return 0
    print(hw + tz + sfp + poe)
    names = re.findall(r"^HW \S+ (\S+) \[(.*)\]$", hw, re.M)
    labels = {lab for _n, lab in names}
    for i in range(1, 5):
        check(f"TEMP-{i}" in labels, f"LM75 hwmon label TEMP-{i}", fails)
    for z in ("die0", "die1"):
        check(any(n == z for n, _l in names), f"die sensor hwmon {z}", fails)
    poe_hw = [lab for n, lab in names if n == "bcm59111"]
    check(sorted(poe_hw) == [f"PoE-{i:02d}" for i in range(1, 13)], f"12 bcm59111 hwmon PoE-01..12 (got {len(poe_hw)})",
          fails)
    for c in CAGES:
        check(f"SFP sfp-{c}" in sfp, f"sfp socket sfp-{c} (debugfs state)", fails)
    for x, out in eth.items():
        cage = x[2:]
        st = re.search(rf"SFP sfp-{cage}\n((?:  .*\n)+)", sfp)
        present = bool(st) and re.search(r"Module state: (?!empty)", st.group(1)) is not None
        if present:
            check("Identifier" in out, f"{x}: ethtool -m decodes the module", fails)
        else:
            log(f"INFO {x}: cage empty (or state unreadable): ethtool -m not checked")
        check("Speed: 10000Mb/s" in out or "Link detected: no" in out, f"{x}: ethtool link settings via phylink", fails)
    log("PoE temperatures with op mode (0x00 = PoE off on that chip):")
    for line in poe.splitlines():
        log("  " + line)
    log(f"inventory: {len(fails)} check(s) failed" + (": " + "; ".join(fails) if fails else ""))
    return 1 if fails else 0


def hotplug(apply: bool, cage: int, window_s: float) -> int:
    xe = f"xe{cage}" if cage in (49, 50) else None
    log(f"hot-plug plan, cage {cage} ({'SFP+' if cage < 51 else 'SFP'} {1 + (cage - 49) % 2}, netdev {xe or 'none'}):")
    for s in ("1. note the module in the cage (inventory): optic or DAC",
              "2. pull it: sfp state -> empty, carrier 0, its sfp hwmon device goes away",
              "3. insert the other type (DAC <-> optic): state -> present/link, 'FIRMWARE_MODE x -> y' in dmesg (xe only)",
              "4. ethtool -m on the netdev decodes the new module; DDM hwmon back (optics only; DACs have no A2h)",
              "5. link comes up if the far end is cabled (xe49: sw1 lan7 optic; xe50: DAC peer)"):
        log("  " + s)
    cmd = ("sed -n 's/^Module state: /mod=/p;s/^Device state: /dev=/p;s/^Main state: /main=/p;s/^moddef0: /moddef0=/p;"
           f"s/^tx_disable: /txdis=/p' /sys/kernel/debug/sfp-{cage}/state "
           f"| tr '\\n' ' '; " + (f"echo carrier $(cat /sys/class/net/{xe}/carrier 2>/dev/null); " if xe else "echo; ") +
           f"dmesg | grep -E 'sfp-{cage}|{xe or 'sfp-'}' | tail -n 3")
    if not apply:
        board(cmd, apply)
        return 0
    last, end, changes = None, time.monotonic() + window_s, 0
    while time.monotonic() < end:
        out = board(cmd, apply).strip()
        if out != last:
            log(f"state: {out}")
            changes += last is not None
            last = out
        time.sleep(0.5)  # ssh round trip ~1-2 s already paces this; 0.5 s keeps it from spinning on a fast error
    log(f"hot-plug window {window_s:.0f} s over: {changes} change(s) seen")
    return 0 if changes else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("what", nargs="?", default="inventory", choices=("inventory", "hotplug"))
    ap.add_argument("--apply", action="store_true", help="run on the board (default: print the commands)")
    ap.add_argument("--cage", type=int, default=50, choices=CAGES)
    ap.add_argument("--window-s", type=float, default=120.0, help="time for the person to swap the module")
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    log(f"sensors_test {a.what} apply={a.apply}")
    if a.what == "hotplug":
        return hotplug(a.apply, a.cage, a.window_s)
    return inventory(a.apply)


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
