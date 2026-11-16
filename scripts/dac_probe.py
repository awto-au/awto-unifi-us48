#!/usr/bin/env python3
"""dac_probe: one-change-at-a-time TX/RX experiments on xe50 (DAC) against sw1 lan3, sw1 read correctly (#78).

Usage: scripts/dac_probe.py --exp A,B1,B2,... [--xe xe50] [--lan lan3] [--hold-s 40]
- sw1 lan3 = SerDes 4 (even): RX_IDLE = WDIG reg 0x14 bit 0 (even lane), valid while WDIG reg 2 == 0x35 (the sw1 driver
  writes 0x35 itself on every rxcal poll, pcs-rtl-otto.c l.1562-1590; this script does NOT write sw1 SerDes registers).
- Per experiment: lan3 up, t+3 s apply the change (tscwr, SDK values cited in docs/driver-internals.md 13.8), sample every 5 s:
  sw1 WDIG2/WDIG14/RX_IDLE, TGR_STD_1[0]/[1], carrier, xe50 PCS_LIVE (0xc154, tscreg); then revert the TSC regs, lan3 down.
- Hold bound: sw1 rxcal verdict every ~30.3 s (#78): 40 s = verdict + margin. Admin state of lan3/lan6/lan7 restored in finally.
- Thermal stop: die >= 108 C or any LM75 >= 78 C (checked per experiment).
- Log: tmp/logs/dac_probe.log
"""
import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import xe49_traffic as t  # noqa: E402

LOG = t.ROOT / "tmp/logs/dac_probe.log"
RESTORE = ("lan3", "lan6", "lan7")
PARAM = "/sys/module/us48_cmic_xe/parameters"
# name -> list of (reg, mask, val) applied in order; "pulse" entries: ("pulse", reg, mask, set, clear, ...) handled below
EXPS = {
    "A": [],
    "B1": [(0xc017, 0x0f00, 0x0900)], "B2": [(0xc017, 0x0f00, 0x0400)], "B3": [(0xc017, 0x0f00, 0x0f00)],
    "B4": [(0xc017, 0xffff, 0x2220)],
    "C": [(0xc252, 0xffff, 0xcad0), (0xc017, 0xffff, 0x2220)],
    "D1": [("pulse", 0xc017, 0x8000)],
    "D2": [("seq", 0xc113, 0xc1c8, 0xc1cb)],
    "E": [("seq", 0xc113, 0xc1c8, 0xc1cb), ("rxrestart",)],
}
REVERT_REGS = (0xc017, 0xc252, 0xc113)


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def tscrd(front: int, reg: int, aer: int) -> int:
    out = t.ssh(t.BOARD, f"dmesg -c >/dev/null; echo '{front} {reg:x} {reg:x} {aer:x}' > {PARAM}/tscreg; dmesg | grep -o '{reg:x}=[0-9a-f]*' | tail -1")
    m = re.search(r"=([0-9a-f]+)", out)
    return int(m.group(1), 16) if m else -1


def tscwr(front: int, aer: int, reg: int, mask: int, val: int) -> str:
    return t.ssh(t.BOARD, f"echo '{front} {aer:x} {reg:x} {mask:x} {val:x}' > {PARAM}/tscwr; echo rc=$?")


def sw1_state(lan: str) -> str:
    idx = int(lan[3:]) + 1
    assert idx % 2 == 0, 'odd SerDes: RX_IDLE is bit 1 of the even partner page, not implemented'
    out = t.ssh(t.SW1, f"grep -E '^(WDIG|TGR_STD_1) ' /sys/kernel/debug/realtek_otto_serdes/serdes.{idx}/registers; cat /sys/class/net/{lan}/carrier")
    row = {}
    for ln in out.splitlines():
        k, _, v = ln.partition(":")
        row[k.strip()] = v.split()
    try:
        w2 = int(row["WDIG"][2], 16)
        w14 = int(row["WDIG"][0x14], 16)
        bit = w14 & 1  # even SerDes only (lan3 = 4, lan7 = 8)
        valid = "valid" if w2 == 0x35 else f"WDIG2={w2:04x} NOT 0x35, RX_IDLE unreliable"
        return (f"serdes.{idx} WDIG14={w14:04x} RX_IDLE={bit} ({valid}) TGR_STD_1={row['TGR_STD_1'][0]}/{row['TGR_STD_1'][1]} "
                f"carrier={out.splitlines()[-1]}")
    except (KeyError, IndexError, ValueError):
        return f"sw1 read failed: {out[:100]}"


def thermal_ok() -> bool:
    out = t.ssh(t.BOARD, "cat /sys/class/hwmon/hwmon*/temp1_input")
    try:
        v = [int(x) for x in out.split()]
    except ValueError:
        log(f"THERMAL read failed: {out[:80]}")
        return False
    die = max(v[4:6]) / 1000 if len(v) >= 6 else 0
    lm = max(v[:4]) / 1000 if len(v) >= 4 else 0
    log(f"thermal die {die} C, lm75 max {lm} C")
    return die < 108 and lm < 78


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--exp", required=True)
    ap.add_argument("--xe", default="xe50")
    ap.add_argument("--lan", default="lan3")
    ap.add_argument("--hold-s", type=float, default=40.0)
    a = ap.parse_args()
    front = int(a.xe[2:])
    aer = 2 if front == 50 else 0
    orig_admin = {l: t.ssh(t.SW1, f"ip -o link show {l}") for l in RESTORE}
    orig_up = {l: "UP" in v.split("<")[1].split(">")[0].split(",") for l, v in orig_admin.items()}
    log(f"original admin state {orig_up}")
    orig = {r: tscrd(front, r, aer) for r in REVERT_REGS}
    log("orig TSC " + " ".join(f"{r:04x}={v:04x}" for r, v in orig.items()))
    try:
        for name in a.exp.split(","):
            if not thermal_ok():
                log("THERMAL STOP")
                return 3
            log(f"=== {name} {EXPS[name]}")
            t.ssh(t.SW1, f"dmesg | tail -1 >/dev/null; ip link set {a.lan} up")
            base = t.ssh(t.SW1, "cut -d. -f1 /proc/uptime").strip()
            t0 = time.monotonic()
            applied = False
            nxt = 0.0
            while time.monotonic() - t0 < a.hold_s:
                el = time.monotonic() - t0
                if not applied and el >= 3.0:
                    for st in EXPS[name]:
                        if st[0] == "pulse":
                            cur = tscrd(front, st[1], aer)
                            log(tscwr(front, aer, st[1], 0xffff, cur | st[2]) + " set")
                            log(tscwr(front, aer, st[1], 0xffff, cur & ~st[2]) + " clear")
                        elif st[0] == "seq":
                            log(tscwr(front, aer, st[1], 0xffff, st[2]) + f" {st[1]:04x}={st[2]:04x}")
                            log(tscwr(front, aer, st[1], 0xffff, st[3]) + f" {st[1]:04x}={st[3]:04x}")
                        elif st[0] == "rxrestart":
                            log(t.ssh(t.BOARD, f"echo {front} > {PARAM}/rx_restart; echo rc=$?"))
                        else:
                            log(tscwr(front, aer, st[0], st[1], st[2]) + f" {st[0]:04x} m{st[1]:04x}={st[2]:04x}")
                    applied = True
                if el >= nxt:
                    log(f"[{name}] t+{el:4.1f} s {sw1_state(a.lan)} | {a.xe} c154={tscrd(front, 0xc154, aer):04x} carrier={t.ssh(t.BOARD, f'cat /sys/class/net/{a.xe}/carrier').strip()}")
                    nxt += 5.0
                time.sleep(0.5)
            log(f"[{name}] sw1 log:\n" + t.ssh(t.SW1, f"dmesg | awk -F'[][]' '$2+0 >= {int(base)}' | grep -E 'SerDes 4|{a.lan}' | tail -5"))
            for r in REVERT_REGS:
                tscwr(front, aer, r, 0xffff, orig[r])
            log(f"[{name}] reverted: " + " ".join(f"{r:04x}={tscrd(front, r, aer):04x}" for r in REVERT_REGS))
            t.ssh(t.SW1, f"ip link set {a.lan} down")
    finally:
        for l, up in orig_up.items():
            t.ssh(t.SW1, f"ip link set {l} {'up' if up else 'down'}")
        log("restored admin state: " + str({l: "UP" in t.ssh(t.SW1, f"ip -o link show {l}").split("<")[1].split(">")[0].split(",") for l in RESTORE}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
