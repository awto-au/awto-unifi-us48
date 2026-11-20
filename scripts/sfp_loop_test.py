#!/usr/bin/env python3
"""sfp_loop_test: two cages of this board cabled to each other by fibre, no switch in the loop (#78).

Usage: scripts/sfp_loop_test.py [--a 50] [--b 52] [--apply] [--sweep]
- Dry run unless --apply. Ports are xe<front> netdevs; lane: fronts 49/51 = TSC0 lane 0, 50/52 = lane 2.
- Baseline: both ports up (lasers on), RX restart on both, then three PCS_LIVE reads + link event counts over the hold.
  Stable = carrier 1 on both and no 'Link is Down' in the last window.
- --sweep (only if the baseline is not stable): tries every TX/RX polarity combination of both lanes
  (c011 bit 5 = TX, c021 bit 15 = RX; docs/driver-internals.md TSC, tmp/xe51-tx-analysis.md), stops at the first stable one.
- Always: ports back to their original admin state and polarity bits restored (also on error). ge1/xe49 never touched.
- Timing: derived constants DARK_S / SETTLE_S / WINDOW_S below (about 5 s per attempt).
Log: tmp/logs/sfp_loop_test.log. Board address from _ipreg (US48_IP_US48_BOOT override).
"""
import argparse
import itertools
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _ipreg  # noqa: E402
import _sfp_state  # noqa: E402
from _cli import help_guard  # noqa: E402
from lx import OPTS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/sfp_loop_test.log"
XE = "/sys/module/us48_cmic_xe/parameters"
NEVER = {49}
LANE = {49: 0, 50: 2, 51: 0, 52: 2}


# TX analog experiments on port b (tmp/xe51-tx-analysis.md, OM SFI profile): name, reg, mask, value; reverts to the read-back value
TUNE_SPECS = [
    ("control", 0xc017, 0x0000, 0x0000),
    ("SFI 2/2/2 (c017 7ff0=2220)", 0xc017, 0x7ff0, 0x2220),
    ("IDRIVER 3", 0xc017, 0x0f00, 0x0300),
    ("IDRIVER 6", 0xc017, 0x0f00, 0x0600),
    ("IDRIVER 9", 0xc017, 0x0f00, 0x0900),
    ("IDRIVER f", 0xc017, 0x0f00, 0x0f00),
    ("FIR cad0", 0xc252, 0xffff, 0xcad0),
]
TUNES = []


def sh(cmd):
    # 30 s: ssh round trip is 1-2 s; longer = board path gone.
    r = subprocess.run(["ssh", *OPTS, f"root@{_ipreg.ip('us48-boot')}", cmd], capture_output=True, text=True, timeout=30)
    if r.returncode:
        raise RuntimeError(f"ssh rc={r.returncode}: {r.stderr.strip()}")
    return r.stdout


def rd(front, reg):
    # No `dmesg -c` here: it would wipe the link-event counts; the newest 'range' line is the one just requested.
    out = sh(f'echo "{front} {reg:x} {reg:x}" > {XE}/tscreg; dmesg | grep "xe{front} range" | tail -n 1; true')
    m = re.search(rf"{reg:x}=([0-9a-f]{{4}})", out)
    return int(m.group(1), 16) if m else -1


def wr(front, reg, mask, val):
    sh(f'echo "{front} {LANE[front]} {reg:x} {mask:x} {val:x}" > {XE}/tscwr')


# Waits, each derived (nothing inherited from the sw1 runs, where the far SerDes needed ~6 s to calibrate):
DARK_S = 0.2    # laser off: SFF-8431 TX_DISABLE assert <= 10 us; far end is our own SerDes (LOS in us): 0.2 s = 20000x
SETTLE_S = 0.3  # laser on <= 1 ms (SFF-8431) + our RX lock measured 23-175 ms (#78): 175 ms x 1.25 = 0.22 s, rounded up
WINDOW_S = 2.0  # phylink polls PCS state once a second (HZ): 2 polls; flaps seen were ~1/s on our side
DARK_MW = 0.05  # module rx low-alarm threshold is 0.0646 mW (FS SFP-10GSR-85 DDM); below it nothing usable arrives


def rxpow(front):
    """Receiver optical power in mW from the module DDM (ethtool -m); -1 if unreadable."""
    out = sh(f"ethtool -m xe{front} 2>&1 | grep -E '^\\s*Receiver signal average optical power\\s*:'; true")
    m = re.search(r":\s*([0-9.]+) mW", out)
    return float(m.group(1)) if m else -1.0


def events(front):
    return int(sh(f'dmesg | grep -c "xe{front}: Link is"; true').strip() or 0)  # grep -c exits 1 on zero matches


def state(fronts):
    return {f: sh(f"cat /sys/class/net/xe{f}/carrier 2>/dev/null; true").strip() for f in fronts}


def attempt(a, b, label, log):
    """Laser cycle both ports, restart RX, return (stable, summary)."""
    for f in (a, b):
        sh(f"ip link set xe{f} down")
    time.sleep(DARK_S)
    sh("dmesg -c >/dev/null")
    for f in (a, b):
        sh(f"ip link set xe{f} up")
    time.sleep(SETTLE_S)
    for f in (a, b):  # rejected module / ID fault: no light or carrier will follow, say why
        st, info = _sfp_state.read(sh, f)
        log.write(_sfp_state.fmt(f, st, info) + "\n")
        if _sfp_state.bad(st):
            line = f"{time.strftime('%H:%M:%S')} {label}: STOP " + _sfp_state.fmt(f, st, info)
            print(line, flush=True)
            log.write(line + "\n")
            return None
    # No light at a receiver = fibre not connected or wired TX->TX (nothing swaps it automatically): stop with a hint.
    dark = [f for f in (a, b) if rxpow(f) < DARK_MW]
    if dark:
        line = (f"{time.strftime('%H:%M:%S')} {label}: NO LIGHT at xe{dark}: rx < {DARK_MW} mW. "
                "Fibre missing or TX->TX: swap the LC duplex connector at one end.")
        print(line, flush=True)
        log.write(line + "\n")
        return None
    lock = {}
    for f in (a, b):  # the restart blocks until lock or its own limit and logs the result: no extra wait needed
        out = sh(f'echo {f} > {XE}/rx_restart; dmesg | grep "xe{f}: rx restart" | tail -n 1; true')
        m = re.search(r"PMD_LIVE ([0-9a-f]+) PCS_LIVE ([0-9a-f]+) after (\d+) ms", out)
        lock[f] = f"lock pmd={m.group(1)} pcs={m.group(2)} {m.group(3)}ms" if m else "lock ?"
    ev0 = {f: events(f) for f in (a, b)}
    time.sleep(WINDOW_S)
    pcs = {f: rd(f, 0xc154) for f in (a, b)}
    ev1 = {f: events(f) for f in (a, b)}
    car = state((a, b))
    stable = all(car[f] == "1" and pcs[f] >= 0 and not pcs[f] & 0x80 and bool(pcs[f] & 2) and ev1[f] == ev0[f] for f in (a, b))
    line = (f"{time.strftime('%H:%M:%S')} {label}: " + " ".join(
        f"xe{f}: carrier={car[f]} c154={pcs[f]:04x} ({lock[f]}) link_events {ev0[f]}->{ev1[f]}" for f in (a, b)) +
        (" STABLE" if stable else " not stable"))
    print(line, flush=True)
    log.write(line + "\n")
    log.flush()
    return stable


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--a", type=int, default=50)
    ap.add_argument("--b", type=int, default=52)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--tune", default="", metavar="POL",
                    help="4 digits txA rxA txB rxB (e.g. 0011): fix that polarity, then vary xe<b> TX drive / FIR one at a time")
    a = ap.parse_args()
    if {a.a, a.b} & NEVER or a.a == a.b:
        print("refusing: xe49 is the management path", file=sys.stderr)
        return 2
    print(f"loop xe{a.a} (lane {LANE[a.a]}) <-> xe{a.b} (lane {LANE[a.b]}); sweep={a.sweep}")
    if not a.apply:
        print("dry run (use --apply)")
        return 0
    LOG.parent.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # a stop request must still run the restore below
    fr = (a.a, a.b)
    flags = {f: int(sh(f"cat /sys/class/net/xe{f}/flags").strip(), 16) & 1 for f in fr}
    orig = {f: (rd(f, 0xc011), rd(f, 0xc021), rd(f, 0xc017), rd(f, 0xc252)) for f in fr}
    TUNES[:] = [(n, r, m, v, {0xc017: orig[a.b][2], 0xc252: orig[a.b][3]}[r] & m)
                for n, r, m, v in TUNE_SPECS]
    with LOG.open("a") as log:
        log.write(f"# start loop xe{a.a} xe{a.b}; original c011/c021 {orig}\n")
        try:
            ok = attempt(a.a, a.b, "baseline (driver polarity)", log)
            if ok is False and a.sweep:  # None = no light: a sweep cannot help
                for bits in itertools.product((0, 1), repeat=4):  # txA rxA txB rxB
                    for f, (tx, rx) in zip(fr, (bits[0:2], bits[2:4])):
                        wr(f, 0xc011, 0x20, 0x20 if tx else 0)
                        wr(f, 0xc021, 0x8000, 0x8000 if rx else 0)
                    if attempt(a.a, a.b, f"sweep txA={bits[0]} rxA={bits[1]} txB={bits[2]} rxB={bits[3]}", log):
                        break
            if a.tune:
                bits = [int(c) for c in a.tune]
                for f, (tx, rx) in zip(fr, (bits[0:2], bits[2:4])):
                    wr(f, 0xc011, 0x20, 0x20 if tx else 0)
                    wr(f, 0xc021, 0x8000, 0x8000 if rx else 0)
                for name, reg, mask, val, back in TUNES:
                    wr(a.b, reg, mask, val)
                    attempt(a.a, a.b, f"tune xe{a.b} {name}", log)
                    sh(f"ip link set xe{a.b} down")
                    wr(a.b, reg, mask, back)
        finally:
            for f in fr:
                sh(f"ip link set xe{f} down")
                wr(f, 0xc011, 0x20, orig[f][0] & 0x20)
                wr(f, 0xc021, 0x8000, orig[f][1] & 0x8000)
                wr(f, 0xc017, 0xffff, orig[f][2])
                wr(f, 0xc252, 0xffff, orig[f][3])
                if flags[f]:
                    sh(f"ip link set xe{f} up")
            log.write("# restored admin state and polarity bits\n")
    return 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
