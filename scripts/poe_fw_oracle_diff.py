#!/usr/bin/env python3
"""Rewrite vs stock acceptance diff in the oracle harness (docs/poe-fw-seq/06-oracle.md, scripts/poe_fw_oracle.py).

Usage: poe_fw_oracle_diff.py [--pt N] [--ticks N] [--ch C] [--scen camera|open|timeout|nopg] [--images stock,main,nopower]
  Runs the stock v21 image and the rewrite images (hwdb/firmware/bcm59111-awto*.bin) in the same pure-Python MCS-51 harness with the
  same modelled analog front end and host setup. Compares (a) published host registers (SAT, SAT_PWR, events, limits) per tick and
  (b) the ordered key SFR write log (C9 F1 F2 A1..A4 B2 D1..D4 D9 S95 BD) against stock. Differences are listed with ticks.
  Host writes reach the rewrite through its own I2C slave ISR (hook 1), stock through its write table (oracle hostwrite).
  Log: tmp/logs/poe_fw_oracle_diff.log (stdout too). Offline only.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402
import poe_fw_oracle as O  # noqa: E402

ROOT = O.ROOT
LOGF = ROOT / "tmp/logs/poe_fw_oracle_diff.log"
IMG = {"stock": "v21", "main": ROOT / "hwdb/firmware/bcm59111-awto.bin", "nopower": ROOT / "hwdb/firmware/bcm59111-awto-nopower.bin",
       "probe2": ROOT / "hwdb/firmware/bcm59111-awto-probe2.bin",
       "probe3": ROOT / "hwdb/firmware/bcm59111-awto-probe3.bin"}
DIFF_KEYS = {0xA1, 0xA2, 0xA3, 0xA4, 0xB2, 0xC9, 0xD1, 0xD2, 0xD3, 0xD4, 0xD9, 0xF1, 0xF2, 0x95, 0xBD, 0xBC, 0xBE, 0xBF}
_out = []


def log(msg=""):
    print(msg, flush=True)
    _out.append(msg)


def map_sym(which, name):
    import re
    mp = ROOT / "tmp/poe-fw-build" / ("main" if which == "main" else which) / "out.map"
    m = re.search(r"^C:\s+([0-9A-F]{8})\s+%s\s" % name, mp.read_text(), re.M)
    return int(m.group(1), 16)


class RewriteMachine(O.Machine):
    """Oracle machine for the rewrite: main-loop top = _poll_common, host writes through the rewrite's I2C slave ISR."""

    def __init__(self, which, pt, passes):
        super().__init__(IMG[which], pt, passes)
        self.poll = self.loop = map_sym(which, "_poll_common")
        self.f7q = []
        self.cpu.sfr[0xDD] = 0x11
        prev = self.cpu.sfr_rd

        def rd(a, v):
            if a == 0xF7:
                return self.f7q.pop(0) if self.f7q else 0
            return prev(a, v)
        self.cpu.sfr_rd = rd

    def i2c(self, code, data=None):
        self.f7q = [code] if data is None else [code, data]
        self.cpu.sfr[0xDD] = 0x11
        self.inject(1)

    def hostwrite(self, r, *vals):
        self.i2c(0x60)
        self.i2c(0x80, r)
        for v in vals:
            self.i2c(0x80, v)
        self.i2c(0xA0)


def build(which, args):
    if which == "stock":
        m = O.Machine("v21", args.pt, args.passes)
    else:
        m = RewriteMachine(which, args.pt, args.passes)
    if getattr(args, "calib", None) is not None:
        m.cpu.sfr[0xD7] = 0x85 if args.calib else 0x05      # trim-valid bit (0x6A.7) on / off at calibration
    m.boot()
    O.CH = args.ch
    O.host_setup(m, 3 << (2 * args.ch))
    s = args.scen
    if getattr(args, "calib", None) is not None:
        import random
        rng = random.Random(7 + args.calib)
        m.an.adc = {k: rng.randrange(256) if rng.random() < 0.8 else 0 for k in range(0x20)}
        for i in range(0x40):
            m.an.words[i] = rng.randrange(0x100, 0x500) if i < 4 or 0x24 <= i < 0x2C else rng.randrange(0, 0x600)
        m.an.ae = 0x55 if args.ae else 0xAA
        m.an.det_code[:] = [4, 4, 4, 4]
        return m
    O.mkword(m, det={"camera": 4, "nopg": 4, "open": 6, "timeout": 4, "bad": 0}[s], cls=0x300, cm=0x30, vm=0x32E)
    if s == "timeout":
        m.an.det_latency = 10 ** 6
    if s == "nopg":
        m.an.pg_latency = 10 ** 6
    return m


TICK_CYC = 20000     # machine cycles per 10.13 ms tick: f_cpu 7.9 MHz / 4 clk per DW8051 cycle (bench-measured tick and f_cpu)
HOOK3_PER_TICK = 4.45  # bench-measured hook-3 rate


def run(which, args):
    m = build(which, args)
    pub, last = [], None
    cyc = {}
    orig = m.inject

    last_slot = [0]
    origl = m.to_loop

    def inj(slot):
        c0 = m.cpu.cycles
        orig(slot)
        cyc.setdefault(slot, []).append(m.cpu.cycles - c0)
        last_slot[0] = slot

    def tl():
        c0 = m.cpu.cycles
        origl()
        cyc.setdefault("pass3" if last_slot[0] == 3 else "idle", []).append(m.cpu.cycles - c0)
        last_slot[0] = 0
    m.inject = inj
    m.to_loop = tl
    m.cyc = cyc
    for _ in range(args.ticks):
        m.step_tick()
        cur = O.snapfmt(m)
        if cur != last:
            pub.append((m.tick, cur))
            last = cur
    wl = [(w[0], O.SFRN.get(w[2], "%02X" % w[2]), w[2], w[3]) for w in m.wlog if w[1] != "boot" and w[2] in DIFF_KEYS and w[0] >= 1]
    ded, prev = [], None
    for w in wl:
        if (w[2], w[3]) != prev:
            ded.append(w)
        prev = (w[2], w[3])
    xl = [(t, a - 0x8000, v) for t, ph, a, v in m.xlog if ph != "boot" and t >= 1 and 0x0C <= a - 0x8000 <= 0x10]
    return m, pub, ded, xl


def calib_compare(args):
    """AC/AD writes (AB index, value) of the first calls, stock vs rewrite, same random analog stimulus."""
    out = {}
    for w in ("stock", "main"):
        m = build(w, args)
        for _ in range(args.ticks):
            m.step_tick()
        out[w] = [(x[4], x[2], x[3]) for x in m.wlog if x[1] != "boot" and x[0] >= 1 and x[2] in (0xAC, 0xAD)]
    n = min(len(out["stock"]), len(out["main"]))
    bad = [(i, out["stock"][i], out["main"][i]) for i in range(n) if out["stock"][i] != out["main"][i]]
    log("calib=%d ae=%d: stock %d writes, rewrite %d writes, compared %d, mismatches %d" % (args.calib, args.ae, len(out["stock"]), len(out["main"]), n, len(bad)))
    for i, a, b in bad[:12]:
        log("  #%d stock AB=%02X %s=%02X   mine AB=%02X %s=%02X" % (i, a[0], "AC" if a[1] == 0xAC else "AD", a[2], b[0], "AC" if b[1] == 0xAC else "AD", b[2]))
    LOGF.parent.mkdir(parents=True, exist_ok=True)
    LOGF.write_text("\n".join(_out) + "\n")
    return 1 if bad else 0


def main():
    help_guard(__doc__)
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--pt", type=int, default=1)
    ap.add_argument("--passes", type=int, default=2)
    ap.add_argument("--ticks", type=int, default=70)
    ap.add_argument("--ch", type=int, default=1)
    ap.add_argument("--scen", default="camera")
    ap.add_argument("--images", default="stock,main,nopower")
    ap.add_argument("--calib", type=int, default=None, help="0 / 1: compare the AB/AC/AD calibration writes (0x1B03) with trim-valid off / on")
    ap.add_argument("--ae", type=int, default=0, help="--calib: AE status 0x55 (accumulate on) instead of 0xAA")
    args = ap.parse_args()
    if args.calib is not None:
        return calib_compare(args)
    res = {}
    for w in args.images.split(","):
        m, pub, wl, xl = run(w, args)
        res[w] = (pub, wl, xl)
        log("== %s  scen=%s ch=%d pt=%d ticks=%d" % (w, args.scen, args.ch, args.pt, args.ticks))
        log("  published registers (changes):")
        for t, s in pub:
            log("    t%-3d %s" % (t, s))
        log("  key SFR writes (tick, sfr = val; consecutive duplicates collapsed):")
        for t, n, a, v in wl:
            log("    t%-3d %-10s = %02X" % (t, n, v))
        log("  host reg writes 0x0C..0x10 by the firmware:")
        for t, r, v in xl:
            log("    t%-3d reg %02X = %02X" % (t, r, v))
        h3, h8 = m.cyc.get(3, [0])[10:] or [0], m.cyc.get(8, [0])[10:] or [0]
        a3, a8 = sum(h3) / len(h3), sum(h8) / len(h8)
        log("  CPU (steady, tick >= 10): hook 3 avg %.0f max %d cycles/call, hook 8 avg %.0f max %d; at %.2f calls/tick: ISR share %.1f %% of a tick (%d cycles)" % (
            a3, max(h3), a8, max(h8), HOOK3_PER_TICK, 100 * (HOOK3_PER_TICK * a3 + a8) / TICK_CYC, TICK_CYC))
        p3, pi = m.cyc.get("pass3", [0])[10:] or [0], m.cyc.get("idle", [0])[10:] or [0]
        log("  main loop: pass after a hook-3 call avg %.0f max %d cycles, idle pass avg %.0f" % (sum(p3) / len(p3), max(p3), sum(pi) / len(pi)))
        acw = sum(1 for x in m.wlog if x[1] != "boot" and x[0] >= 5 and x[2] in (0xAC, 0xAD))
        log("  calibration: AC/AD writes (tick >= 5): %d  (stock rewrites the indirect words 4..11 on every periodic call)" % acw)
        log("  CPU: hook3 count reg 0x22/23 = %02X%02X, hook8 0x2D/0x40 = %02X%02X" % (
            m.cpu.xram[0x8023], m.cpu.xram[0x8022], m.cpu.xram[0x8040], m.cpu.xram[0x802D]) if w != "stock" else "")
    if "stock" in res:
        ref = res["stock"][1]
        for w in args.images.split(","):
            if w == "stock":
                continue
            mine = res[w][1]
            log("== DIFF %s vs stock (key SFR writes, tick shown as stock/mine)" % w)
            a = [(n, v) for t, n, _, v in ref]
            b = [(n, v) for t, n, _, v in mine]
            import difflib
            sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
            nd = 0
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag == "equal":
                    for i, j in zip(range(i1, i2), range(j1, j2)):
                        dt = mine[j][0] - ref[i][0]
                        if dt:
                            log("    same  %-10s = %02X  tick stock %d mine %d (%+d)" % (a[i][0], a[i][1], ref[i][0], mine[j][0], dt))
                else:
                    nd += 1
                    for i in range(i1, i2):
                        log("    only stock  t%-3d %-10s = %02X" % (ref[i][0], a[i][0], a[i][1]))
                    for j in range(j1, j2):
                        log("    only mine   t%-3d %-10s = %02X" % (mine[j][0], b[j][0], b[j][1]))
            log("   %s: %d difference blocks; F1 writes: stock %d, mine %d" % (
                w, nd, sum(1 for x in ref if x[1] == "F1_PWR_SET"), sum(1 for x in mine if x[1] == "F1_PWR_SET")))
            fa, fb = res["stock"][0], res[w][0]
            log("   final published stock: %s" % (fa[-1][1] if fa else "-"))
            log("   final published mine : %s" % (fb[-1][1] if fb else "-"))
    LOGF.parent.mkdir(parents=True, exist_ok=True)
    LOGF.write_text("\n".join(_out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
