#!/usr/bin/env python3
"""asic_fwd_test — cross-chip ASIC forwarding test over the HiGig links (#57, #4; ssh + devmem S-channel).

Usage: asic_fwd_test.py survey
       asic_fwd_test.py setup [--learn] [--apply]
       asic_fwd_test.py send --dir fwd|rev [--count N] [--rate R]
       asic_fwd_test.py count
       asic_fwd_test.py restore [--apply]
- survey: read-only HiGig readiness (XLPORT/XLMAC, trunk, stacking regs). Exit 0 = both chips ready, 2 = not.
- setup: stacking as stock (docs/driver-internals.md 15.2/15.4) + test VLAN 200 = {ge17, HiGig 28/29} on unit 0 and
  {HiGig 28/29, ge41} on unit 1, untagged on ge17/ge41, HiGig tagged. ge8/ge32 stay in their private VLANs (loop rule,
  docs/switch-model.md stage 3); never ge1, never the CPU port. --learn: CML 8 (hardware learn + forward) on ge17/ge41.
  Dry-run prints every write (value before -> after, source); --apply writes and records the restore list
  (tmp/fwd_test/restore.json, one entry per write, saved before the next write).
- send: fwd = unit 0 CPU -> ge8 -> cable -> ge17 -> HiGig -> ge41 -> cable -> ge32 -> unit 1 CPU; rev = the opposite.
  tools/fwd_pkt (build_bde_trace.py --fwdpkt) sends N frames (EtherType 0x88b5, locally administered MACs, <= 100/s)
  and counts them on the four netdevs; per-hop MIB deltas. A storm signature (any hop or ge1 > 10x N) runs restore.
- count: per-hop MIB snapshot + L2 learning check (TABLE_LOOKUP of both test MACs, read only).
- restore: writes every recorded value back (reverse order), verifies by read-back, then survey.
- S-channel via devmem is not locked against the driver: run with no link change / ifup / reload in flight.
- Log: tmp/logs/asic_fwd_test.log.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402
import _board  # noqa: E402
import hwdb  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/asic_fwd_test.log"
RESTORE = ROOT / "tmp/fwd_test/restore.json"
IPIPE, EPIPE, MMU, XLP1 = 10, 11, 12, 6
HG_LANE = {28: 0, 29: 2}	# logical 28/29 = phys 30/32 = XLPORT1 lanes 0/2 (docs/port-map.md)
HG_PHYS = {28: 30, 29: 32}
TEST_VID = 200
# Per unit: test port in VLAN 200 (octal end of the loop cable) and the cable's other end (stays private).
# lport/phys/GPORT block+lane: hwdb/board/ports.toml (front 17/41 = lport 18 phys 18 GPORT2 lane 0; front 8/32 = lport 9 phys 2 GPORT0 lane 0).
TEST = {0: {"name": "ge17", "lp": 18, "phys": 18, "blk": 4, "lane": 0}, 1: {"name": "ge41", "lp": 18, "phys": 18, "blk": 4, "lane": 0}}
PEER = {0: {"name": "ge8", "lp": 9, "phys": 2, "blk": 2, "lane": 0}, 1: {"name": "ge32", "lp": 9, "phys": 2, "blk": 2, "lane": 0}}
GE1 = {"blk": 2, "lane": 7}	# unit 0 front 1 = GPORT0 lane 7 (management)
MAC_A, MAC_B = "02:b5:00:00:00:01", "02:b5:00:00:00:02"	# locally administered (U/L bit set), fixed
STOCK = "docs/driver-internals.md 15.2 (stock-warm capture tmp/higig/stock.txt)"
# Unit 1 stock-warm PORT_TAB[28/29] (8 words) and EGR_PORT[28/29]: same capture.
U1_PORT_TAB_HG = {28: [0x01c00001, 0x02980110, 0x04200010, 0x106e4000, 0x05f58d11, 0xc0000000, 0x008001ce, 0],
                  29: [0x01c00001, 0x02980110, 0x04200010, 0x106e4000, 0x05f58d11, 0x00000000, 0x008001df, 0]}
U1_EGR_PORT_HG = {28: 0x9c00030d, 29: 0x9d00030d}
DB = hwdb.default()


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def field(w: list[int], lo: int, n: int) -> int:
    v = 0
    for i in range(n):
        b = lo + i
        v |= ((w[b // 32] >> (b % 32)) & 1) << i
    return v


def setf(w: list[int], lo: int, n: int, v: int) -> None:
    for i in range(n):
        b = lo + i
        w[b // 32] = (w[b // 32] & ~(1 << (b % 32))) | (((v >> i) & 1) << (b % 32))


def even_parity(w: list[int], bit: int) -> None:
    setf(w, bit, 1, 0)
    setf(w, bit, 1, sum(bin(x).count("1") for x in w) & 1)


def mem(name: str) -> tuple[int, int, dict]:
    """(base, words, {field: (lo, len)}) from the SDK regdb merged into hwdb."""
    m = DB.sdk_sbus_mems[name]
    words = (max(f.hi for f in m.fields) + 32) // 32
    return m.offset, words, {f.name: (f.lo, f.hi - f.lo + 1) for f in m.fields}


def reg(name: str) -> tuple[int, dict]:
    r = DB.sdk_sbus_regs[name]
    return r.offset, {f.name: (f.lo, f.hi - f.lo + 1) for f in r.fields}


def rd(ops: list[tuple]) -> list[list[int]]:
    """ops: (unit, 'reg'|'mem', blk, addr, words) -> data words."""
    req = [(u, "r" + k, b, a, n, None) for u, k, b, a, n in ops]
    try:
        res = _board.schan_batch(req)
    except RuntimeError as e:	# devmem S-channel is not locked against the driver: one logged retry
        log(f"  read retry after: {e}")
        res = _board.schan_batch(req)
    return [r[1:] for r in res]


def rd_stable(u: int, kind: str, blk: int, addr: int, words: int) -> list[int]:
    """Two equal consecutive reads (up to 4): a value recorded for restore must not be a race glitch."""
    prev = rd([(u, kind, blk, addr, words)])[0][:words]
    for _ in range(3):
        cur = rd([(u, kind, blk, addr, words)])[0][:words]
        if cur == prev:
            return cur
        log(f"  u{u} {addr:#010x}: unstable read {' '.join('%08x' % x for x in prev)} vs {' '.join('%08x' % x for x in cur)}")
        prev = cur
    raise RuntimeError(f"u{u} {addr:#x}: no two equal reads in 4")


def wr(u: int, kind: str, blk: int, addr: int, data: list[int]) -> None:
    _board.schan_batch([(u, "w" + kind, blk, addr, len(data), data)])


def wr_verified(u: int, kind: str, blk: int, addr: int, data: list[int]) -> bool:
    """Write + read-back; one logged rewrite on mismatch (a devmem op can be lost to the driver's S-channel use)."""
    for attempt in (1, 2, 3):
        try:
            wr(u, kind, blk, addr, data)
            got = rd([(u, kind, blk, addr, len(data))])[0][:len(data)]
        except RuntimeError as e:
            log(f"  u{u} {addr:#010x}: attempt {attempt}: {e}")
            continue
        if got == data:
            return True
        log(f"  u{u} {addr:#010x}: read back {' '.join('%08x' % x for x in got)} after write {attempt}")
    return False


# ---------------------------------------------------------------- survey (read-only)
def survey() -> int:
    ready = True
    for u in (0, 1):
        got = {}
        ops = [(u, "reg", IPIPE, 0x46002700, 1), (u, "reg", IPIPE, 0x46002c00, 1), (u, "reg", IPIPE, 0x46002600, 1),
               (u, "reg", IPIPE, 0x46002b00, 1), (u, "reg", IPIPE, 0x46006900, 1), (u, "reg", IPIPE, 0x46006a00, 1),
               (u, "reg", IPIPE, 0x46006b00, 1), (u, "reg", IPIPE, 0x46006c00, 1),
               (u, "reg", XLP1, 0x2020b00, 1), (u, "reg", XLP1, 0x2020a00, 1)]
        names = ["EPC_LINK_BMAP_64", "LINK_STATUS_64", "HIGIG_TRUNK_CONTROL", "HG_TRUNK_FAILOVER_ENABLE",
                 "HG_TRUNK_BITMAP[0]", "HG_TRUNK_BITMAP[1]", "HG_TRUNK_GROUP[0]", "HG_TRUNK_GROUP[1]",
                 "XLPORT1 ENABLE_REG", "XLPORT1 MODE_REG"]
        for lp, ln in HG_LANE.items():
            ops += [(u, "reg", XLP1, 0x20000 | ln, 1), (u, "reg", XLP1, 0x60000 | ln, 2), (u, "reg", XLP1, 0x60100 | ln, 2),
                    (u, "reg", XLP1, 0x60b00 | ln, 2), (u, "reg", XLP1, 0xb00 | ln, 2), (u, "reg", XLP1, 0x4c00 | ln, 2),
                    (u, "reg", EPIPE, 0x4000900 | lp, 1), (u, "reg", IPIPE, 0x8000100 | lp, 1),
                    (u, "reg", IPIPE, 0x44001e00 | lp, 1), (u, "mem", IPIPE, 0x04000000 + lp, 8)]
            names += [f"HG{lp} XLPORT_CONFIG", f"HG{lp} XLMAC_CTRL", f"HG{lp} XLMAC_MODE", f"HG{lp} RX_LSS_STATUS",
                      f"HG{lp} RPKT", f"HG{lp} TPKT", f"HG{lp} EGR_PORT", f"HG{lp} IHG_LOOKUP", f"HG{lp} MODPORT_MAP_SEL",
                      f"HG{lp} PORT_TAB"]
        ops += [(u, "mem", IPIPE, 0x14000001, 4), (u, "mem", EPIPE, 0x100c0001, 3),
                (u, "mem", IPIPE, 0x14000000 + TEST_VID, 4), (u, "mem", EPIPE, 0x100c0000 + TEST_VID, 3)]
        names += ["VLAN_TAB[1]", "EGR_VLAN[1]", f"VLAN_TAB[{TEST_VID}]", f"EGR_VLAN[{TEST_VID}]"]
        for p in (TEST[u], PEER[u]):
            ops += [(u, "mem", IPIPE, 0x04000000 + p["lp"], 8), (u, "reg", EPIPE, 0x4000900 | p["lp"], 1)]
            names += [f"{p['name']} PORT_TAB", f"{p['name']} EGR_PORT"]
        ops += [(u, "mem", EPIPE, 0x345c0000 + p, 1) for p in (2, 18, 30, 31, 32, 33)]
        names += [f"EGR_ENABLE[phys {p}]" for p in (2, 18, 30, 31, 32, 33)]
        ops += [(u, "mem", IPIPE, b + m, 1) for b in (0x44180000, 0x441c0000, 0x44200000) for m in range(2)]
        names += [f"MODPORT_MAP_{t}[{m}]" for t in ("SW", "IM", "EM") for m in range(2)]
        ops += [(u, "reg", MMU, 0x2032200, 1), (u, "reg", MMU, 0x2032300, 1)]
        names += ["E2EFC_TX_RMODID", "E2EFC_RX_RMODID"]
        for n, v in zip(names, rd(ops)):
            extra = ""
            if n.endswith("PORT_TAB"):
                extra = (f"  PORT_VID {field(v, 24, 12)} MY_MODID {field(v, 40, 7)} PORT_TYPE {field(v, 36, 2)} "
                         f"HIGIG2 {field(v, 55, 1)} HIGIG_TRUNK {field(v, 51, 1)} CML {field(v, 121, 4)}/{field(v, 125, 4)}")
            elif n.endswith("EGR_PORT"):
                extra = f"  PORT_TYPE {v[0] & 3} HIGIG2 {v[0] >> 2 & 1} MY_MODID {v[0] >> 9 & 0x7f}"
            elif n.startswith("VLAN_TAB"):
                extra = (f"  PORT_BITMAP {field(v, 0, 30):#x} VALID {field(v, 38, 1)} STG {field(v, 39, 9)} "
                         f"FID {field(v, 48, 12)} PROFILE {field(v, 60, 4)}")
            elif n.startswith("EGR_VLAN"):
                extra = f"  UT {field(v, 0, 30):#x} PORT_BITMAP {field(v, 30, 30):#x} STG {field(v, 60, 8)} VALID {field(v, 68, 1)}"
            elif n.endswith("XLPORT_CONFIG"):
                extra = f"  HIGIG_MODE {v[0] >> 10 & 1} HIGIG2_MODE {v[0] >> 11 & 1} MY_MODID {v[0] >> 1 & 0xff}"
            log(f"u{u} {n}: {' '.join('%08x' % x for x in v)}{extra}")
            got[n] = v
        en = got["XLPORT1 ENABLE_REG"][0]
        for lp, ln in HG_LANE.items():
            ctrl, cfg = got[f"HG{lp} XLMAC_CTRL"][0], got[f"HG{lp} XLPORT_CONFIG"][0]
            why = [w for w, bad in (("XLPORT_ENABLE off", not en >> ln & 1), ("XLMAC TX/RX off", ctrl & 3 != 3),
                                    ("XLMAC SOFT_RESET", ctrl >> 6 & 1), ("HIGIG2_MODE 0", not cfg >> 11 & 1)) if bad]
            log(f"u{u} HG{lp}: {'ready' if not why else 'NOT ready: ' + ', '.join(why)}")
            ready &= not why
    log(f"=== HiGig {'ready on both chips' if ready else 'NOT ready: no cross-chip forwarding test possible (#57)'}")
    return 0 if ready else 2


# ---------------------------------------------------------------- setup plan
class Plan:
    def __init__(self) -> None:
        self.writes: list[dict] = []
        self.planned: dict[tuple, list[int]] = {}	# entry -> value after the writes planned so far

    def rmw(self, u: int, kind: str, blk: int, addr: int, words: int, fn, what: str, src: str) -> None:
        key = (u, kind, blk, addr)
        before = list(self.planned.get(key) or rd_stable(u, kind, blk, addr, words))
        after = list(before)
        fn(after)
        self.planned[key] = after
        if after != before:
            self.writes.append({"unit": u, "kind": kind, "blk": blk, "addr": addr, "before": before, "after": after,
                                "what": what, "src": src})
        else:
            log(f"  u{u} {what}: already {' '.join('%08x' % x for x in before)} (no write)")


def build_plan(learn: bool) -> Plan:
    pl = Plan()
    pt_base, _, ptf = mem("PORT_TAB")
    vt_base, _, vtf = mem("VLAN_TAB")
    ev_base, _, evf = mem("EGR_VLAN")
    stg_base, _, _ = mem("STG_TAB")
    estg_base, _, _ = mem("EGR_VLAN_STG")
    ee_base, _, _ = mem("EGR_ENABLE")
    ep_off, epf = reg("EGR_PORT")
    epc_off, _ = reg("EPC_LINK_BMAP_64")
    members = [TEST[0]["lp"], 28, 29]	# same logical numbers on both units

    def const(vals):
        def f(w):
            w[:] = vals
        return f

    # Unit 1 stacking (unit 1 holds none of it after our cold init, driver-internals 15.2 last bullet).
    for lp in (28, 29):
        pl.rmw(1, "mem", IPIPE, pt_base + lp, 8, const(U1_PORT_TAB_HG[lp]), f"PORT_TAB[{lp}] (HiGig2, trunk 1, MY_MODID 1, CML 8)", STOCK)
        pl.rmw(1, "reg", EPIPE, ep_off | lp, 1, const([U1_EGR_PORT_HG[lp]]), f"EGR_PORT[{lp}]", STOCK)
        pl.rmw(1, "mem", EPIPE, ee_base + HG_PHYS[lp], 1, const([1]), f"EGR_ENABLE[phys {HG_PHYS[lp]}]", STOCK + " (1 on phys 30/32)")
    for p in (TEST[1], PEER[1]):
        lo, n = ptf["MY_MODID"]
        pl.rmw(1, "mem", IPIPE, pt_base + p["lp"], 8, lambda w, lo=lo, n=n: setf(w, lo, n, 1),
               f"PORT_TAB[{p['lp']}] {p['name']} MY_MODID 1", STOCK + " (unit 1 front PORT_TAB word 1 0x02000100)")
        lo, n = epf["MY_MODID"]
        pl.rmw(1, "reg", EPIPE, ep_off | p["lp"], 1, lambda w, lo=lo, n=n: setf(w, lo, n, 1),
               f"EGR_PORT[{p['lp']}] {p['name']} MY_MODID 1", STOCK + " (unit 1 EGR_PORT[2] 0x82000308)")
    hgb_off, _ = reg("HG_TRUNK_BITMAP")
    hgg_off, _ = reg("HG_TRUNK_GROUP")
    pl.rmw(1, "reg", IPIPE, hgb_off + (1 << 8), 1, const([0xc]), "HG_TRUNK_BITMAP[1] = {28, 29}", STOCK)
    pl.rmw(1, "reg", IPIPE, hgg_off + (1 << 8), 1, const([0x779de3]), "HG_TRUNK_GROUP[1] = {28, 29, 28, 29} RTAG 3", STOCK)
    for r in ("E2EFC_TX_RMODID", "E2EFC_RX_RMODID"):
        pl.rmw(1, "reg", MMU, reg(r)[0], 1, const([0x80]), f"{r} = remote modid 0, valid", STOCK)
    # MODPORT_MAP_*[remote modid] = 0x88 (HIGIG_PORT_BITMAP 8 = port 29 + parity), both units.
    for u, rmod in ((0, 1), (1, 0)):
        for t in ("SW", "IM", "EM"):
            pl.rmw(u, "mem", IPIPE, mem(f"MODPORT_MAP_{t}")[0] + rmod, 1, const([0x88]), f"MODPORT_MAP_{t}[{rmod}]", STOCK)
    # Per unit: HiGig in the EP link bitmap, VLAN 200, STG, test port.
    for u in (0, 1):
        tp = TEST[u]
        pl.rmw(u, "reg", IPIPE, epc_off, 1, lambda w: w.__setitem__(0, w[0] | 1 << 28 | 1 << 29),
               "EPC_LINK_BMAP_64 |= 28, 29", "SDK soc/common/link.c soc_link_fwd_set (bit per linked port); stock 28/29 set")
        for p in (tp, PEER[u]):	# the driver writes EGR_ENABLE[logical]: phys 2 (ge8/ge32) stays 0 on unit 1 (#57)
            pl.rmw(u, "mem", EPIPE, ee_base + p["phys"], 1, const([1]), f"EGR_ENABLE[phys {p['phys']}] {p['name']}",
                   "common/bcm-cmic ge_vlan_init (EGR_ENABLE 0 drops all egress); index = phys (stock 26-33 pattern)")
        v1, e1, v200 = rd([(u, "mem", IPIPE, vt_base + 1, 4), (u, "mem", EPIPE, ev_base + 1, 3),
                           (u, "mem", IPIPE, vt_base + TEST_VID, 4)])
        if field(v200, *vtf["VALID"]):
            raise SystemExit(f"u{u} VLAN_TAB[{TEST_VID}] already valid: refusing (restore pending or VLAN in use)")
        stg, estg = field(v1, *vtf["STG"]), field(e1, *evf["STG"])

        def vlan(w, v1=v1):
            w[:] = v1
            setf(w, *vtf["PORT_BITMAP"], sum(1 << p for p in members))
            even_parity(w, vtf["EVEN_PARITY"][0])

        def egr_vlan(w, e1=e1, tp=tp):
            w[:] = e1
            setf(w, *evf["UT_BITMAP"], 1 << tp["lp"])
            setf(w, *evf["PORT_BITMAP"], sum(1 << p for p in members))
            even_parity(w, evf["EVEN_PARITY"][0])

        for base, blk, name in ((stg_base, IPIPE, "STG_TAB"), (estg_base, EPIPE, "EGR_VLAN_STG")):
            idx = stg if name == "STG_TAB" else estg

            def fwd(w):
                for p in members:
                    setf(w, 2 * p, 2, 3)
                even_parity(w, 60)
            pl.rmw(u, "mem", blk, base + idx, 2, fwd, f"{name}[{idx}] SP_TREE {members} = 3 (forward)",
                   "common/bcm-cmic ge_vlan_init (SP_TREE 3 = forwarding, STG of VLAN 1)")
        pl.rmw(u, "mem", IPIPE, vt_base + TEST_VID, 4, vlan, f"VLAN_TAB[{TEST_VID}] = VLAN 1 clone, PORT_BITMAP {members}",
               "common/bcm-cmic ge_vlan_init (clone VLAN 1, bitmaps replaced); docs/switch-model.md stage 3")
        pl.rmw(u, "mem", EPIPE, ev_base + TEST_VID, 3, egr_vlan,
               f"EGR_VLAN[{TEST_VID}] = VLAN 1 clone, PORT_BITMAP {members}, UT {tp['lp']} (HiGig tagged)",
               "common/bcm-cmic ge_vlan_init; HiGig2 header carries the VLAN")

        def port_vid(w, tp=tp):
            setf(w, *ptf["PORT_VID"], TEST_VID)
            if learn:
                setf(w, *ptf["CML_FLAGS_NEW"], 8)
                setf(w, *ptf["CML_FLAGS_MOVE"], 8)
        pl.rmw(u, "mem", IPIPE, pt_base + tp["lp"], 8, port_vid,
               f"PORT_TAB[{tp['lp']}] {tp['name']} PORT_VID {TEST_VID}" + (" CML 8/8 (learn)" if learn else ""),
               "common/bcm-cmic ge_port_tab (PORT_VID 24:12)" + ("; CML 8 = ARL|FWD, SDK bcm/esw/trx/port.c "
                                                                 "_bcm_trx_port_cml_flags2hw; stock HiGig PORT_TAB CML 8" if learn else ""))
    return pl


def show(w: dict) -> str:
    h = lambda x: " ".join("%08x" % v for v in x)  # noqa: E731
    return f"u{w['unit']} {w['kind']} blk {w['blk']} {w['addr']:#010x} {w['what']}: {h(w['before'])} -> {h(w['after'])}  [{w['src']}]"


def setup(learn: bool, apply: bool) -> int:
    if RESTORE.exists():
        log(f"STOP: {RESTORE} exists (restore pending): run restore first")
        return 1
    pl = build_plan(learn)
    log(f"setup: {len(pl.writes)} writes ({'APPLY' if apply else 'dry run'})")
    for w in pl.writes:
        log("  " + show(w))
    if not apply:
        return 0
    RESTORE.parent.mkdir(parents=True, exist_ok=True)
    done: list[dict] = []
    for w in pl.writes:
        RESTORE.write_text(json.dumps(done + [w], indent=1))	# recorded before the write lands
        try:
            ok = wr_verified(w["unit"], w["kind"], w["blk"], w["addr"], w["after"])
        except RuntimeError as e:
            ok = False
            log(f"  {e}")
        if not ok:
            log(f"STOP: write not confirmed ({show(w)}): restoring")
            restore(True)
            return 1
        done.append(w)
    log(f"setup applied: {len(done)} writes, each read back; restore list {RESTORE}")
    return 0


def restore(apply: bool) -> int:
    if not RESTORE.exists():
        log("restore: no restore list (nothing applied)")
        return 0
    ws = json.loads(RESTORE.read_text())
    log(f"restore: {len(ws)} entries ({'APPLY' if apply else 'dry run'})")
    bad = 0
    for w in reversed(ws):
        n = len(w["before"])
        cur = rd([(w["unit"], w["kind"], w["blk"], w["addr"], n)])[0][:n]
        note = "" if cur == w["after"] else f" (current {' '.join('%08x' % x for x in cur)} != written value)"
        log(f"  u{w['unit']} {w['what']}: -> {' '.join('%08x' % x for x in w['before'])}{note}")
        if apply and not wr_verified(w["unit"], w["kind"], w["blk"], w["addr"], w["before"]):
            log(f"  READBACK MISMATCH u{w['unit']} {w['what']}")
            bad += 1
    if apply:
        l2_cleanup()
        RESTORE.rename(RESTORE.with_name(f"restored-{time.strftime('%Y%m%d-%H%M%S')}.json"))
        log(f"restore done: {len(ws)} writes back, {bad} mismatches")
    return 1 if bad else 0


# ---------------------------------------------------------------- counters, traffic, learning
def counter_ops() -> tuple[list[str], list[tuple]]:
    names, ops = [], []
    for u in (0, 1):
        for p in (PEER[u], TEST[u]):
            for c, off in (("GRPKT", 0x1b00), ("GTPKT", 0x3800)):
                names.append(f"u{u} {p['name']} {c}")
                ops.append((u, "reg", p["blk"], off | p["lane"], 1))
        for lp, ln in HG_LANE.items():
            for c, off in (("RPKT", 0xb00), ("TPKT", 0x4c00)):
                names.append(f"u{u} HG{lp} {c}")
                ops.append((u, "reg", XLP1, off | ln, 2))
        for lp in (TEST[u]["lp"], 28, 29):
            names.append(f"u{u} RDISC[{lp}]")
            ops.append((u, "reg", IPIPE, 0x44003700 | lp, 1))
    names += ["u0 ge1 GRPKT", "u0 ge1 GTPKT"]
    ops += [(0, "reg", GE1["blk"], 0x1b00 | GE1["lane"], 1), (0, "reg", GE1["blk"], 0x3800 | GE1["lane"], 1)]
    return names, ops


def counters() -> dict[str, int]:
    """Two snapshots; a value that moved by > 1000 between them (devmem race glitch) is read a third time, median kept."""
    names, ops = counter_ops()
    snap = lambda: [v[0] | (v[1] << 32 if len(v) > 1 and "HG" in n else 0) for n, v in zip(names, rd(ops))]  # noqa: E731
    a, b = snap(), snap()
    out = {}
    for i, n in enumerate(names):
        if abs(a[i] - b[i]) > 1000:
            c = rd([ops[i]])[0]
            c = c[0] | (c[1] << 32 if len(c) > 1 and "HG" in n else 0)
            log(f"  counter {n}: {a[i]} / {b[i]} / {c} (glitch, median kept)")
            b[i] = sorted((a[i], b[i], c))[1]
        out[n] = b[i]
    return out


HOPS = {"fwd": ["u0 ge8 GTPKT", "u0 ge17 GRPKT", "u0 HG28 TPKT", "u0 HG29 TPKT", "u1 HG28 RPKT", "u1 HG29 RPKT",
                "u1 ge41 GTPKT", "u1 ge32 GRPKT"],
        "rev": ["u1 ge32 GTPKT", "u1 ge41 GRPKT", "u1 HG28 TPKT", "u1 HG29 TPKT", "u0 HG28 RPKT", "u0 HG29 RPKT",
                "u0 ge17 GTPKT", "u0 ge8 GRPKT"]}


def send(direction: str, count: int, rate: int) -> int:
    t = _board.too_hot(_board.temps())
    if t:
        log(f"STOP: {t}")
        return 1
    tx, src, dst = ("ge8", MAC_A, MAC_B) if direction == "fwd" else ("ge32", MAC_B, MAC_A)
    rc, out = _board.ssh("test -x /tmp/fwd_pkt && echo ok")
    if "ok" not in out:
        import subprocess
        subprocess.run([sys.executable, str(ROOT / "scripts/lx.py"), "--put", str(ROOT / "tmp/fwd_pkt"), "/tmp/fwd_pkt"], check=True)
    ge1_0 = int(_board.ssh("cat /sys/class/net/ge1/statistics/rx_packets")[1].split()[0])
    c0 = counters()
    # TAIL 200 ms: RX DMA + netif delivery take < 1 ms; covers a scheduling gap on the host after the last frame.
    rc, out = _board.ssh(f"/tmp/fwd_pkt {tx} {count} {rate} {src} {dst} 200 ge8 ge17 ge41 ge32; cat /proc/loadavg",
                         call_s=count / rate * 1.25 + 10)
    c1 = counters()
    ge1_1 = int(_board.ssh("cat /sys/class/net/ge1/statistics/rx_packets")[1].split()[0])
    log(f"send {direction}: {tx} {count} frames at {rate}/s, SA {src} DA {dst}, EtherType 0x88b5")
    for ln in out.splitlines():
        log(f"  board: {ln}")
    d = {n: c1[n] - c0[n] for n in c0}
    for n in HOPS[direction]:
        log(f"  hop {n:16s} +{d[n]}")
    log("  other: " + ", ".join(f"{n} +{v}" for n, v in d.items() if n not in HOPS[direction] and v))
    log(f"  ge1 netdev rx +{ge1_1 - ge1_0}")
    storm = [n for n, v in d.items() if v > 10 * count] + (["ge1 rx"] if ge1_1 - ge1_0 > 10 * count else [])
    if storm:
        log(f"STORM signature on {storm}: restoring now")
        restore(True)
        return 1
    return 0


def l2_lookup(u: int, vid: int, mac: str, op: int = 0x28) -> str:
    """TABLE_LOOKUP (op 0x28, driver-internals 1) on L2X by key {KEY_TYPE 0, VLAN_ID, MAC_ADDR}: read only.
    op 0x26 = TABLE_DELETE by the same key (restore: test-learned entries; common/bcm-cmic l2 delete path)."""
    base, words, f = mem("L2X")
    key = [0] * words
    setf(key, *f["VLAN_ID"], vid)
    setf(key, *f["MAC_ADDR"], int(mac.replace(":", ""), 16))
    b = _board.CMIC[u] + _board.SCHAN
    hdr = op << 26 | IPIPE << 20 | (4 * words) << 7
    s = f"devmem {b:#x} 32 0; devmem {b + 12:#x} 32 {hdr:#x}; devmem {b + 16:#x} 32 {base:#x}; "
    s += "".join(f"devmem {b + 20 + 4 * i:#x} 32 {v:#x}; " for i, v in enumerate(key))
    s += (f"devmem {b:#x} 32 1; n=0; while [ $(( $(devmem {b:#x} 32) & 2 )) = 0 ] && [ $n -lt 50 ]; do n=$((n+1)); done; "
          f"echo S $(devmem {b:#x} 32)" + "".join(f" $(devmem {b + 12 + 4 * i:#x} 32)" for i in range(words + 2))
          + f"; devmem {b:#x} 32 0")
    for attempt in (1, 2, 3):	# a reply whose ack opcode is not LOOKUP_ACK 0x29 was clobbered by another S-channel op
        rc, out = _board.ssh(s)
        ln = [x for x in out.splitlines() if x.startswith("S ")]
        if rc or not ln:
            return f"ssh rc {rc}: {out[-200:]}"
        v = [int(x, 16) for x in ln[0].split()[1:]]
        ctrl, resp = v[0], v[1:]
        if resp[0] >> 26 == op + 1:
            break
        log(f"  lookup u{u} attempt {attempt}: ack {resp[0]:#010x} ctrl {ctrl:#x} (not a lookup ack)")
    else:
        return "no valid lookup reply in 3 attempts"
    if ctrl & 0x200000:
        return f"not found (NACK, ctrl {ctrl:#x}, ack {resp[0]:#010x})"
    if op != 0x28:
        return f"done: ctrl {ctrl:#x} ack {resp[0]:#010x}"
    e = resp[2:2 + words]	# ack header, response word (index), entry
    return (f"found: ctrl {ctrl:#x} ack {resp[0]:#010x} resp {resp[1]:#010x} entry {' '.join('%08x' % x for x in e)} "
            f"VALID {field(e, *f['VALID'])} STATIC {field(e, *f['STATIC_BIT'])} MODULE_ID {field(e, *f['MODULE_ID'])} "
            f"PORT_NUM {field(e, *f['PORT_NUM'])} T {field(e, *f['T'])} MAC {field(e, *f['MAC_ADDR']):012x} "
            f"VLAN {field(e, *f['VLAN_ID'])}")


def l2_cleanup() -> None:
    """Delete dynamic L2 entries of the two test MACs (keys: VLAN 200 and FID 0 = SVL key of the cloned VLANs)."""
    _, _, f = mem("L2X")
    for u in (0, 1):
        for mac in (MAC_A, MAC_B):
            for k in (0, TEST_VID):
                r = l2_lookup(u, k, mac)
                if r.startswith("found") and "STATIC 0" in r:
                    log(f"  L2 u{u} key {k} {mac}: delete ({l2_lookup(u, k, mac, op=0x26)}); now {l2_lookup(u, k, mac)[:40]}")
                elif r.startswith("found"):
                    log(f"  L2 u{u} key {k} {mac}: static entry, not ours, kept")


def count() -> int:
    c = counters()
    for n, v in c.items():
        log(f"  {n}: {v}")
    vt_base, _, vtf = mem("VLAN_TAB")
    for u in (0, 1):
        v = rd([(u, "mem", IPIPE, vt_base + TEST_VID, 4)])[0]
        keys = sorted({TEST_VID, field(v, *vtf["FID_ID"])})
        for mac in (MAC_A, MAC_B):
            for k in keys:
                log(f"  L2 u{u} key {k} {mac}: {l2_lookup(u, k, mac)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["survey", "setup", "send", "count", "restore"])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--learn", action="store_true")
    ap.add_argument("--dir", choices=["fwd", "rev"], default="fwd")
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--rate", type=int, default=100)
    a = ap.parse_args()
    if not 1 <= a.rate <= 100 or not 1 <= a.count <= 1000:
        ap.error("rate 1-100 frames/s, count 1-1000")
    log(f"--- asic_fwd_test {' '.join(sys.argv[1:])}")
    if a.cmd == "survey":
        return survey()
    if a.cmd == "setup":
        return setup(a.learn, a.apply)
    if a.cmd == "send":
        return send(a.dir, a.count, a.rate)
    if a.cmd == "count":
        return count()
    return restore(a.apply)


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
