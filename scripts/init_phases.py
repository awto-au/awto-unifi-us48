#!/usr/bin/env python3
"""init_phases: stock S-channel write trace -> named, phased init description (#68).

Usage: python3 scripts/init_phases.py [--unit 0|1] [--dump]
- Input: tmp/bde_stock_init.log (u0 + u1, line: <ms> <tid> SCH u<unit> <hdr> <addr> <data...>), tmp/mmio_stock_init.log (u0 CMIC MMIO).
- Names/fields: hwdb.py (SDK regdb merged). Phases = marker registers resolved per unit (ordinals differ slightly).
- Output: docs/init-phases-u<unit>.md. Log: tmp/logs/init_phases.log
- ord = per-unit ordinal of write ops (reg 0x0d, mem 0x09, insert 0x24, delete 0x26); ms = tracer clock.
"""
import argparse
import collections
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hwdb  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/init_phases.log"
BLK = {2: "GPORT0", 3: "GPORT1", 4: "GPORT2", 5: "XLPORT0", 6: "XLPORT1", 10: "IPIPE", 11: "EPIPE", 12: "MMU",
       13: "OTPC", 14: "CMIC", 16: "TOP", 19: "SER"}
LABEL = {2: "GPORT", 3: "GPORT", 4: "GPORT", 5: "XLPORT", 6: "XLPORT", 10: "IPIPE", 11: "EPIPE", 12: "MMU",
         13: "OTPC", 16: "TOP", 19: "SER"}
WR = {0x0d: "REG", 0x09: "MEM", 0x24: "INS", 0x26: "DEL"}

# (id, title, marker name, purpose). Marker = first write with that name at ordinal > previous marker.
PHASES = [
    ("A1", "TOP: PLL bypass/refclk, TS/BS PLLs, soft reset", "TOP_PLL_BYP_AND_REFCLK_CONTROL",
     "release PLL resets, then IP/EP/MMU/GPORT/XLPORT/QGPHY out of reset, load PVTMON/XG PLL"),
    ("A2", "XLPORT XGXS + MAC reset", "XLPORT_XGXS0_CTRL_REG", "TSC/XGXS PLL control and MAC_CONTROL for both XLPORT blocks"),
    ("A3", "IPIPE/EPIPE table reset", "ING_HW_RESET_CONTROL_1", "hardware memory clear of every IPIPE/EPIPE table (RESET_ALL, then per-stage ranges)"),
    ("A4", "MMU IPMC table init", "MMU_IPMC_VLAN_TBL", "write zero IPMC VLAN/GROUP tables with MISCCONFIG.IPMC_VLAN_TBL_RD_SEL toggled per entry"),
    ("A5", "Port number maps, MIB reset", "XLPORT_MIB_RESET", "phys->logical (IPIPE), logical->phys (EPIPE, MMU), MIB counter reset"),
    ("A6", "Parity/ECC/interrupt enable, SER ranges", "MEMFAILINTMASK", "memory error detection; SER protection ranges and enable"),
    ("A7", "TOP misc + core PLL, GPORT EEE", "TOP_CORE_PLL_CTRL4", "core PLL load; per-lane EEE config"),
    ("A8", "Port enable, EPC link, XLPORT mode, pipe config", "L2_AUX_HASH_CONTROL", "enable ports in EP, GPORT/XLPORT enable, ING/EGR config, per-port EGR_PORT/VLAN control"),
    ("A9", "TDM", "IARB_TDM_CONTROL", "IPIPE+MMU arbitration TDM tables, CMIC credit transfer"),
    ("A10", "MMU thresholds (HOL, dyn, IBP, E2EFC limits)", "HOLCOSPKTSETLIMIT", "per-port/COS buffer limits, MMU port enable"),
    ("A11", "IPIPE/EPIPE table defaults", "LPORT_TAB", "LPORT/PORT_TAB, priority maps, VLAN tag profiles, SOURCE_TRUNK_MAP clear"),
    ("B1", "Port MAC init (GPORT/XLMAC)", "XLPORT_MAC_CONTROL", "MAC frame length, pause, IPG, flush, XLMAC ctrl for each port (after the untraced PHY/MIIM gap)"),
    ("B2", "SYSTEM_CONFIG_TABLE, VLAN_CTRL", "SYSTEM_CONFIG_TABLE", "per-port VLAN control, system config table"),
    ("B3", "VLAN/STG/trunk defaults", "ING_1588_INGRESS_CTRL", "VLAN 1, STG, TRUNK tables, L2 age timer, MAC limit"),
    ("B4", "MMU bucket/COS arbitration", "HIGIG_TRUNK_CONTROL", "metering buckets, XQCOSARBSEL, WRRWEIGHT_COS, CNG limits"),
    ("B5", "CPU_COS_MAP, debug counter select", "CPU_COS_MAP", "CPU COS map, RDBGC/TDBGC selects, GPORT max size, stat counter clear"),
    ("B6", "Storm control, mirror, L3, IPMC", "ICONTROL_OPCODE_BITMAP", "storm meters, FP helper tables, L3 defaults, IPMC tables, OAM, per-port repl/age"),
    ("C1", "Run-time: XLMAC/E2EFC/MODPORT/block masks", "XLMAC_TIMESTAMP_ADJUST", "stock app configuration after both chips are up (not chip init)"),
    ("C2", "Run-time: FP, VLAN, L2, DOS, DSCP, per-port", "FP_UDF_OFFSET", "FP slices/TCAM, CPU control, hash, MAC address, VLAN churn"),
]

# phase id -> list of (name regex, why). Shown as key registers.
KEY = {
    "A1": [(r"^TOP_PLL_BYP_AND_REFCLK_CONTROL$", "0 = no PLL bypass, use refclk"),
           (r"^TOP_TS_PLL_CTRL_", "time-sync PLL divider setup"),
           (r"^TOP_BS_PLL\d_CTRL|^TOP_BROAD_SYNC", "broadsync PLLs: set up only; unused by the switch path"),
           (r"^TOP_SOFT_RESET_REG_2$", "PLL reset/post-reset bits release order"),
           (r"^TOP_SOFT_RESET_REG$", "block resets: IP,EP,MMU,XLP,GP,TS,QGPHY released in steps with 10 ms gaps"),
           (r"^TOP_MISC_CONTROL_1$", "PLL SW override/power-down bits"),
           (r"^TOP_XG_PLL\d_CTRL_3|^TOP_PVTMON", "XG PLL post-divider; temperature monitor")],
    "A2": [(r"^XLPORT_XGXS0_CTRL_REG$", "TSC/XGXS reset sequence (power, PLL, reset release); 6 writes per XLPORT"),
           (r"^XLPORT_MAC_CONTROL$", "XMAC soft reset low/high")],
    "A3": [(r"^ING_HW_RESET_CONTROL_[12]$", "stage/range clear; _2 written 0 between stages (pulse), RESET_ALL|VALID|COUNT"),
           (r"^EGR_HW_RESET_CONTROL_[01]$", "EPIPE table clear, same scheme")],
    "A4": [(r"^MMU_IPMC_VLAN_TBL$", "zero entry (3 words) 0..255"), (r"^MMU_IPMC_GROUP_TBL", "zero group tables"),
           (r"^MISCCONFIG$", "bit14 IPMC_VLAN_TBL_RD_SEL toggled around every VLAN_TBL write; final 0x1d41 (METERING_CLK_EN, PARITY_CHECK_EN, dyn mem/xq, CNG drop)")],
    "A5": [(r"^XLPORT_MIB_RESET$", "0xf then 0: pulse clearing XLMAC MIB"),
           (r"PORT_NUMBER_MAPPING|^LOG_TO_PHY_PORT_MAPPING|ING_PHYSICAL", "port maps; unused ports = 0x1f/0x20 (invalid)")],
    "A6": [(r"^MEMFAILINTMASK$", "MMU mem-fail interrupt mask"), (r"^SER_RING_ERR_CTRL|^SER_RANGE_ENABLE|^SER_CONFIG_REG", "SER enable, last in this phase"),
           (r"^SER_RANGE_\d_CONFIG$", "per-range protection config")],
    "A7": [(r"^TOP_CORE_PLL_CTRL", "core PLL divider load"), (r"^TOP_MISC_CONTROL_1$", "0x5001e880: CMIC_TO_*_PLL_LOAD pulses"),
           (r"^GE0_EEE_CONFIG$", "EEE per lane; harmless for us")],
    "A8": [(r"^L2_AUX_HASH_CONTROL|^L3_AUX_HASH_CONTROL", "aux hash select"),
           (r"^EGR_ENABLE$", "EGR_ENABLE[port]=1: EP port enable (needed for TX on that port)"),
           (r"^EPC_LINK_BMAP_64$", "bit per logical port: link up for EP; written again at run time"),
           (r"^GPORT_CONFIG$", "GPORT_EN with CLR_CNT pulse (0 -> 2 -> 0 -> 3 -> 1)"),
           (r"^XLPORT_CONFIG$|^XLPORT_MODE_REG$|^XLPORT_ENABLE_REG$|^XLPORT_SOFT_RESET$", "XLPORT core mode, lane enable, soft reset pulse"),
           (r"^ING_CONFIG_64$|^EGR_CONFIG_1$", "IPIPE/EPIPE config"),
           (r"^EGR_VLAN_CONTROL_1$|^EGR_PORT$|^IEGR_PORT$|^MC_CONTROL_", "per-port EP defaults, multicast control")],
    "A9": [(r"^IARB_TDM_CONTROL$", "0xa7 -> table write -> 0x94: DISABLE during table load? (see ordering)"),
           (r"^IARB_TDM_TABLE$|^MMU_ARB_TDM_TABLE$", "TDM slot -> physical port; 75 slots each"),
           (r"^IP_TO_CMICM_CREDIT_TRANSFER$", "0x60: TRANSFER_ENABLE + 32 credits IPIPE->CMIC (CPU path)")],
    "A10": [(r"^HOLCOSPKTSETLIMIT|^HOLCOSCELLMAXLIMIT|^LWMCOSCELLSETLIMIT|^DYNCELLLIMIT|^IBP(PKT|CELL)SETLIMIT|^IBPDISCARDSETLIMIT|^DYNXQCNTPORT|^TOTALDYNCELL", "buffer limits, rewritten at the end of pass B"),
            (r"^MMUPORTENABLE$", "0x3ffffffd: all ports except 1 (logical 1 unused)"),
            (r"^E2EFC_CNT_", "E2E flow control counter limits")],
    "A11": [(r"^LPORT_TAB$", "port 0 LPORT entry"), (r"^PORT_TAB$", "per-port ingress defaults (PORT_VID etc.), rewritten later"),
            (r"^EGR_PRI_CNG_MAP$|^ING_VLAN_TAG_ACTION_PROFILE$|^EGR_VLAN_TAG_ACTION_PROFILE$|^ING_VLAN_RANGE$", "priority/tag defaults"),
            (r"^SOURCE_TRUNK_MAP_TABLE$", "4096 entries cleared then set: per-(module,port) SVP/VLAN"),
            (r"^EGR_VLAN_CONTROL_[23]$|^IPORT_TABLE$", "EP VLAN control, iport table")],
    "B1": [(r"^COMMAND_CONFIG$|^FRM_LENGTH$|^GPORT_RSV_MASK$|^PAUSE_CONTROL$|^TX_IPG_LENGTH$", "UMAC per GE port: COMMAND_CONFIG 0x1180015b = TX/RX enable, 1G, PAD/CRC, pause"),
           (r"^FLUSH_CONTROL$|^MMUFLUSHCONTROL$|^EGR_PORT_CREDIT_RESET$", "flush/credit reset pulses per port; edge-acting"),
           (r"^XLPORT_TXFIFO_CTRL$|^XLMAC_(CTRL|RX_CTRL|TX_CTRL|MODE|RX_MAX_SIZE|RX_LSS_CTRL|PAUSE_CTRL)$|^XLPORT_MAC_RSV_MASK$", "XLMAC bring-up: SOFT_RESET toggle, TX/RX enable, max size, LSS")],
    "B2": [(r"^SYSTEM_CONFIG_TABLE$", "per-(module,port) system config, 4096 entries"), (r"^VLAN_CTRL$", "per-port VLAN control (0x10200000 final)")],
    "B3": [(r"^STG_TAB$|^EGR_VLAN_STG$|^VLAN_TAB$|^EGR_VLAN$|^VLAN_PROFILE_TAB$", "default VLAN 1 + STG 1 forwarding"),
           (r"^TRUNK_|^L2_AGE_TIMER$|^SYS_MAC_LIMIT_CONTROL$", "trunk clear, age timer, MAC limit")],
    "B4": [(r"^MAXBUCKETCONFIG$|^MINBUCKETCONFIG$|^MAXBUCKET$|^MINBUCKET$", "egress metering buckets; CPU port 0 has a cap (questionable)"),
           (r"^XQCOSARBSEL$|^WRRWEIGHT_COS$|^CNGCOSPKTLIMIT", "COS arbitration, weights, CNG limits")],
    "B5": [(r"^CPU_COS_MAP$", "CPU COS map: rule -> COS queue (CPU path)"), (r"^RDBGC\d_SELECT$|^TDBGC\d_SELECT$", "debug counter select: harmless"),
           (r"^GPORT_CNTMAXSIZE$", "0x5ee max counted size")],
    "B6": [(r"^STORM_CONTROL_METER_", "storm meters"), (r"^ICONTROL_OPCODE_BITMAP$|^MIRROR_CONTROL$|^EMIRROR_CONTROL_64$", "mirror/opcode defaults"),
           (r"^PER_PORT_REPL_CONTROL$|^PER_PORT_AGE_CONTROL$", "replication/ageing per port"),
           (r"^IARB_OAM_REFRESH_CONTROL$|^OAM_TX_CONTROL$|^LMEP_COMMON$", "OAM: harmless")],
    "C1": [(r"^XLMAC_TIMESTAMP_ADJUST$|^XLMAC_E2E", "XMAC timestamp/E2E: HiGig side"), (r"^MODPORT_MAP_SEL$|_BLOCK_MASK_64$|^SRC_MODID_BLOCK$", "module/port maps, block masks"),
           (r"^E2EFC_", "E2EFC (HiGig flow control) config")],
    "C2": [(r"^CPU_CONTROL_1$|^HASH_CONTROL$|^EGR_SHAPING_CONTROL$|^ING_EN_EFILTER_BITMAP_64$", "CPU control (opcode->CPU), hash, shaping, egress filter"),
           (r"^MAC_[01]$|^XLMAC_(TX|RX)_MAC_SA$", "port MAC address"), (r"^DOS_CONTROL", "DoS controls: harmless"),
           (r"^L2X$", "static L2 entries insert/delete")],
}

# (regex, tag, text) for questionable/known-bad registers (evidence in #68).
QUESTION = [
    (r"^MAXBUCKETCONFIG$|^MINBUCKETCONFIG$", "Q", "CPU port (0) egress bucket config non-zero (COS0 0x4808d, COS1.. 0x1802f in phase C) caps CPU frames (~130/s per #68); we zero"),
    (r"^EPC_LINK_BMAP_64$", "Q", "written several times with changing bitmaps; later value reflects link state, not init"),
    (r"^MISCCONFIG$", "Q", "RD_SEL toggled 256x then final 0x1fed differs from first-pass 0x1d6d (bits 5..: later pass sets more); ordering-sensitive"),
    (r"^SER_RANGE_\d_CONFIG$", "H", "SER protection: not needed for forwarding"),
    (r"PARITY_CONTROL|_ECC_CONTROL|_INTR_ENABLE|MEMFAILINTMASK", "H", "error detection; interrupts unused by the bootloader"),
    (r"EEE", "H", "EEE off-path for 1G/10G bring-up"),
    (r"^TOP_BS_PLL|^TOP_BROAD_SYNC|^TOP_TS_PLL|^CMIC_BS|DBGC|^OAM_|^LMEP|^DOS_CONTROL", "H", "unused feature"),
]


class Op:
    __slots__ = ("o", "ms", "kind", "blk", "addr", "data", "name", "n")

    def __init__(self, o, ms, kind, blk, addr, data, name):
        self.o, self.ms, self.kind, self.blk, self.addr, self.data, self.name = o, ms, kind, blk, addr, data, name


class Names:
    def __init__(self, db):
        self.db = db
        self.regs = collections.defaultdict(list)
        for r in db.sdk_sbus_regs.values():
            for lab in r.blocks.split(","):
                self.regs[(lab, r.offset)].append(r)
        self.mems = sorted((m.offset, m.numels, m.name, set(m.blocks.split(","))) for m in db.sdk_sbus_mems.values())
        self.arr = collections.defaultdict(list)    # label -> arrays with stride 0x100 (index in addr bits 8+)
        for r in db.sdk_sbus_regs.values():
            if r.numels > 1:
                for lab in r.blocks.split(","):
                    self.arr[lab].append(r)
        self.cache = {}

    def reg(self, blk, addr):
        lab = LABEL.get(blk, "")
        for key in (addr, addr & ~0xFF):
            for r in self.regs.get((lab, key), []):
                return r
        ck = (lab, addr)
        if ck not in self.cache:
            hit = None
            for r in self.arr.get(lab, []):
                d = (addr & ~0xFF) - r.offset
                if d > 0 and d % 0x100 == 0 and d // 0x100 < r.numels:
                    hit = r
                    break
            self.cache[ck] = hit
        return self.cache[ck]

    def mem(self, blk, addr):
        lab = LABEL.get(blk, "")
        for base, n, name, labs in reversed(self.mems):
            if base <= addr < base + max(n, 1) and lab in labs:
                return name
        return ""


def load(unit, names):
    ops, reads = [], collections.Counter()
    n = 0
    for ln in (ROOT / "tmp/bde_stock_init.log").read_text().splitlines():
        p = ln.split()
        if len(p) < 6 or p[2] != "SCH" or p[3] != "u%d" % unit:
            continue
        h, addr = int(p[4], 16), int(p[5], 16)
        op, blk = h >> 26, h >> 20 & 0x3f
        if op in (0x0b, 0x07):
            r = names.reg(blk, addr) if op == 0x0b else None
            reads[(blk, r.name if r else names.mem(blk, addr))] += 1
        if op not in WR:
            continue
        n += 1
        if op == 0x0d:
            r = names.reg(blk, addr)
            nm = r.name if r else ""
        else:
            nm = names.mem(blk, addr)
        ops.append(Op(n, int(p[0]), WR[op], blk, addr, [int(x, 16) for x in p[6:]], nm))
    return ops, reads


def resolve_phases(ops):
    out, start = [], 0
    pos = 0
    for pid, title, marker, why in PHASES:
        k = next((i for i in range(pos, len(ops)) if ops[i].name == marker and (pid != "A1" or True)), None)
        if k is None:
            continue
        out.append([pid, title, k, why])
        pos = k + 1
    res = []
    for i, (pid, title, k, why) in enumerate(out):
        end = out[i + 1][2] if i + 1 < len(out) else len(ops)
        res.append((pid, title, k, end, why))
    return res


def fields_of(names, r, val):
    if not r:
        return ""
    parts = []
    for f in r.fields:
        v = (val >> f.lo) & ((1 << (f.hi - f.lo + 1)) - 1)
        if v and not f.name.startswith("RSVD") and not f.name.startswith("RESERVED"):
            parts.append(f.name if f.hi == f.lo else f"{f.name}={v:#x}")
    return " ".join(parts[:7]) + (" ..." if len(parts) > 7 else "")


def tag_of(name):
    for rx, t, why in QUESTION:
        if re.search(rx, name):
            return t, why
    return "", ""


def fmt(d):
    return " ".join(f"{x:08x}" for x in d[:3]) + (" .." if len(d) > 3 else "")


def phase_section(pid, title, a, b, why, ops, names, later):
    seg = ops[a:b]
    kinds = collections.Counter(o.kind for o in seg)
    L = [f"### {pid} {title}", f"- ord {seg[0].o}-{seg[-1].o}, {seg[0].ms}-{seg[-1].ms} ms, {len(seg)} ops ({', '.join(f'{k} {v}' for k, v in kinds.items())}); blocks " +
         ", ".join(f"{BLK.get(k, k)} {v}" for k, v in collections.Counter(o.blk for o in seg).most_common(5)),
         f"- purpose: {why}"]
    rows = []
    for rx, w in KEY.get(pid, []):
        by = collections.OrderedDict()
        for o in seg:
            if o.name and re.search(rx, o.name):
                by.setdefault((o.kind, o.blk, o.name), []).append(o)
        for (kind, blk, nm), lst in by.items():
            addrs = sorted({o.addr for o in lst})
            last = lst[-1]
            r = names.reg(blk, last.addr) if kind == "REG" else None
            vals = collections.Counter(tuple(o.data) for o in lst)
            ad = f"{addrs[0]:#x}" if len(addrs) == 1 else f"{len(addrs)} addrs {addrs[0]:#x}..{addrs[-1]:#x}"
            lv = later.get((kind, blk, last.addr))
            ov = ""
            if lv and lv[0] > seg[-1].o and lv[1] != last.data:
                ov = f"; overwritten ord {lv[0]} ({lv[2]} ms) -> {fmt(lv[1])}"
            dec = fields_of(names, r, last.data[0]) if r and last.data else ""
            t, tw = tag_of(nm)
            rows.append(f"| {BLK.get(blk, blk)} | {nm} | {ad} | {len(lst)} | {fmt(last.data)}{ov} | {dec} | {w}{' [' + t + ': ' + tw + ']' if t else ''} |")
    if rows:
        L += ["", "| blk | reg/mem | addr | writes | last value in phase | fields set | why |", "|---|---|---|---|---|---|---|"] + rows
    return L


def gaps(seg, thr=5):
    out = []
    for x, y in zip(seg, seg[1:]):
        if y.ms - x.ms >= thr:
            out.append(f"ord {x.o}->{y.o}: {y.ms - x.ms} ms after {x.name} ({fmt(x.data)})")
    return out


def pulse_seq(ops, nm, limit=10):
    sel = [o for o in ops if o.name == nm]
    if not sel:
        return ""
    a0 = (sel[0].blk, sel[0].addr)
    return " ".join(f"{o.data[0]:x}@{o.ms}" for o in sel if (o.blk, o.addr) == a0)[:limit * 14]


def overwrite_table(ops, phases, names):
    """Regs whose value written in phase P is replaced by a different value in a later phase."""
    pidx = {}
    for pid, _t, a, b, _w in phases:
        for o in ops[a:b]:
            pidx[o.o] = pid
    last = {}
    first = {}
    for o in ops:
        if o.kind != "REG" or not o.name:
            continue
        k = (o.blk, o.addr)
        first.setdefault(k, o)
        last[k] = o
    agg = collections.OrderedDict()
    for k, f in first.items():
        l = last[k]
        if pidx.get(f.o) != pidx.get(l.o) and f.data != l.data:
            key = (pidx.get(f.o), pidx.get(l.o), BLK.get(f.blk, f.blk), f.name)
            e = agg.setdefault(key, [0, f, l])
            e[0] += 1
    return agg


def render(unit, ops, reads, names, phases, mmio):
    later = {}
    for o in ops:
        later[(o.kind, o.blk, o.addr)] = (o.o, o.data, o.ms)
    L = [f"# Stock init phases, unit {unit} ({'BCM53344 AXI' if unit == 0 else 'BCM53346 PCIe'})",
         "",
         "- Generated by `scripts/init_phases.py` from `tmp/bde_stock_init.log` (raw traces stay in `tmp/`); names/fields via `scripts/hwdb.py`. Issue #68.",
         f"- {len(ops)} write ops (S-channel only), {ops[0].ms}-{ops[-1].ms} ms on the tracer clock; ord = write ordinal in this unit.",
         "- Stock order seen: u0 pass A (1.1-4.6 s), u0 pass B (11.5-22.4 s), u1 pass A (22.5-27 s), u1 pass B (35-49 s), then both units run-time config (49-125 s, phase C).",
         "- Untraced: MIIM/MDIO (SerDes/PHY/TSC, CMC0 DMA page 0x31000). The 7-8 s hole between pass A and B of each unit is PHY/TSC bring-up.",
         "- Tags: [Q] questionable, [H] harmless (not needed for forwarding)."]
    if unit == 1:
        L += ["", "## Minimum cold init for unit 1 xe49/xe50 + CPU path", ""] + min_u1(ops, names, phases)
    L += ["", "## Phases", "", "| id | name | ord | ms | ops |", "|---|---|---|---|---|"]
    for pid, t, a, b, w in phases:
        L.append(f"| {pid} | {t} | {ops[a].o}-{ops[b - 1].o} | {ops[a].ms}-{ops[b - 1].ms} | {b - a} |")
    if unit == 0 and mmio:
        L += ["", "## Non-S-channel CMIC writes (tools/mmio_trace.c run, u0 only)", ""] + mmio
    L += ["", "## Ordering constraints", ""]
    pre = [o for o in ops if o.o <= 60]
    L += ["- TOP_SOFT_RESET_REG steps (ms): " + pulse_seq(ops[:60], "TOP_SOFT_RESET_REG"),
          "- TOP_SOFT_RESET_REG_2 (ms): " + pulse_seq(ops[:60], "TOP_SOFT_RESET_REG_2"),
          "- Pipe reset (A3): ING_HW_RESET_CONTROL_1/_2 and EGR_HW_RESET_CONTROL_0/_1 alternate; _2/_1 low-write between ranges = new range start; RESET_ALL first (0x38000 / 0x34000), clear, then 18 ING ranges; poll reads of DONE: "
          f"{sum(v for (b, n), v in reads.items() if n.startswith('ING_HW_RESET') or n.startswith('EGR_HW_RESET'))} reads in whole trace",
          "- Pulses (value@ms, first address): XLPORT_MIB_RESET " + pulse_seq(ops, "XLPORT_MIB_RESET", 4) + "; GPORT_CONFIG " + pulse_seq(ops, "GPORT_CONFIG", 8)
          + "; XLPORT_SOFT_RESET " + pulse_seq(ops, "XLPORT_SOFT_RESET", 6) + "; XLMAC_CLEAR_RX_LSS_STATUS " + pulse_seq(ops, "XLMAC_CLEAR_RX_LSS_STATUS", 4),
          "- MISCCONFIG.IPMC_VLAN_TBL_RD_SEL (bit 14): 0x5d41 written before and 0x1d41 after every MMU_IPMC_VLAN_TBL write (select + deselect), 256 times.",
          "- IARB_TDM_CONTROL: 0xa7 (DISABLE=1) while IARB_TDM_TABLE + MMU_ARB_TDM_TABLE are loaded (75 slots, interleaved), then 0x94 (DISABLE=0, TDM_WRAP_PTR=0x4a = last slot 74); IP_TO_CMICM_CREDIT_TRANSFER=0x60 (32 credits, enable) only after the TDM is running.",
          "- XLPORT_XGXS0_CTRL_REG (per XLPORT): e0 (TSCCLK_EN|PWRDWN|REFIN_EN) -> a0 (PWRDWN off) -> +10 ms -> a4 (RSTB_HW) -> ac (RSTB_REFCLK) -> ae (RSTB_PLL). Order = power up, wait, release HW reset, refclk, PLL.",
          "- TOP reset order: TOP_SOFT_RESET_REG_2 PLL bits (0x1400, 0x3c00) -> TOP_SOFT_RESET_REG 0xf7fd8 (+11 ms) 0xffff8 -> REG_2 0x3d00, 0x3f00 (+10 ms) -> [CMIC_CPS_RESET ~here] -> REG 0xfffff (+11 ms) -> REG_2 0x13f00. IP/EP/MMU/XLP/GP bits (0-8) last.",
          "- Port MAC bring-up (B1) ends with each XLMAC_CTRL parked (SOFT_RESET set, 0x1041); stock re-enables at link-up in phase C (0x1003). A cold start must write the enabled value itself.",
          "- CMIC_RXBUF_EPINTF_RELEASE_ALL_CREDITS is edge-acting: stock pulses 0->1 twice (before CPS_RESET, and ~100 ms later, i.e. after the EP pipe reset in A3); the pulse before EGR_HW_RESET is lost (#46).",
          "- MMU: MMUPORTENABLE written last in A10, after thresholds; thresholds re-written in pass B (final values differ).",
          "- Long gaps >= 5 ms in pass A (ms):"]
    L += ["  - " + g for g in gaps([o for o in ops if o.ms < 6000 or 22000 < o.ms < 30000][:3000])[:12]]
    L += ["", "## Phase details", ""]
    for pid, t, a, b, w in phases:
        L += phase_section(pid, t, a, b, w, ops, names, later) + [""]
    ow = overwrite_table(ops, phases, names)
    L += ["## Written, then overwritten in a later phase", "", "- Registers (non-counter) whose first write is in one phase and last value in another, grouped by name; addrs = count.", "",
          "| from | to | blk | reg | addrs | first value | final value |", "|---|---|---|---|---|---|---|"]
    for (p0, p1, blk, nm), (n, f, l) in list(ow.items())[:90]:
        L.append(f"| {p0} | {p1} | {blk} | {nm} | {n} | {fmt(f.data)} | {fmt(l.data)} |")
    L += ["", "## Questionable writes", "", "| tag | reg(s) | ops | note |", "|---|---|---|---|"]
    q, h = collections.Counter(), collections.defaultdict(lambda: [set(), 0])
    for o in ops:
        if o.name:
            t, w = tag_of(o.name)
            if t == "Q":
                q[(o.name, w)] += 1
            elif t == "H":
                h[w][0].add(o.name)
                h[w][1] += 1
    for (nm, w), n in sorted(q.items()):
        L.append(f"| Q | {nm} | {n} | {w} |")
    for w, (nms, n) in h.items():
        ex = ", ".join(sorted(nms)[:3])
        L.append(f"| H | {len(nms)} regs ({ex}, ...) | {n} | {w} |")
    L += curated(ops, names, unit)
    L += reserved_bits(ops, names)
    return L


def curated(ops, names, unit):
    L = ["", "### Specific findings", ""]
    mb = [o for o in ops if o.name == "MAXBUCKETCONFIG" and o.addr & 0xff == 0 and o.data and o.data[0]]
    ot = [o for o in ops if o.name == "MAXBUCKETCONFIG" and o.addr & 0xff == 0 and o.addr > 0x19c00 and o.data and o.data[0]]
    r = names.reg(12, mb[0].addr) if mb else None
    if mb:
        L.append(f"- [Q] CPU-port egress meter: MAXBUCKETCONFIG[0] = {mb[0].data[0]:#x} at ord {mb[0].o} ({mb[0].ms} ms, phase C), fields {fields_of(names, r, mb[0].data[0])}; "
                 + (f"CPU port COS1.. = {ot[0].data[0]:#x} (ord {ot[0].o}, {len(ot)} writes); " if ot else "") + "phase A/B leaves all buckets 0. We zero these (#68).")
    L.append("- Per-COS MMU registers (HOLCOS*, LWMCOS*, *BUCKET*, WRRWEIGHT_COS, ...): addr = base + 0x100*cos, port in the low byte (regdb numels = COS count, stride 0x100).")
    pt = [o for o in ops if o.name == "PORT_TAB"]
    L.append(f"- [Q] PORT_TAB written {len(pt)} times over {len({o.addr for o in pt})} entries: stock rewrites it per VLAN/port change in phase C; init value is the first pass.")
    ep = [o for o in ops if o.name == "EGR_PORT" and o.addr & 0xff == 0]
    L.append("- [Q] EGR_PORT[0] (CPU): " + " -> ".join(f"{o.data[0]:x} (ord {o.o})" for o in ep) + "; u0 and u1 end differently (bit 9 set on u1 later).")
    ing = [o for o in ops if o.name == "ING_HW_RESET_CONTROL_2"]
    L.append(f"- ING_HW_RESET_CONTROL_2 written {len(ing)}x (first {ing[0].data[0]:#x}, then 0 between ranges): relies on the DONE poll; no poll = ranges overlap.")
    L.append("- CMIC_RXBUF_EPINTF_RELEASE_ALL_CREDITS pulsed twice by stock (the first before CPS_RESET has no lasting effect); order-by-accident, #46.")
    mc = [o for o in ops if o.name == "MISCCONFIG" and o.data and o.data[0] not in (0x5d41, 0x1d41)]
    L.append("- MISCCONFIG: " + ", ".join(f"{o.data[0]:#x} (ord {o.o})" for o in mc[:6]) + f"; final {mc[-1].data[0]:#x} " + (fields_of(names, names.reg(12, mc[-1].addr), mc[-1].data[0]) if mc else ""))
    return L


def reserved_bits(ops, names):
    L = ["", "### Writes with bits outside any regdb field (reserved-bit set)", "", "| reg | value | stray bits | ord |", "|---|---|---|---|"]
    seen = set()
    for o in ops:
        if o.kind != "REG" or not o.name or o.name in seen or not o.data:
            continue
        r = names.reg(o.blk, o.addr)
        if not r or not r.fields:
            continue
        cov = 0
        for f in r.fields:
            cov |= ((1 << (f.hi - f.lo + 1)) - 1) << f.lo
        stray = o.data[0] & ~cov & 0xffffffff
        if stray:
            seen.add(o.name)
            L.append(f"| {o.name} | {o.data[0]:08x} | {stray:08x} | {o.o} |")
    return L[:60]


PORTS = {0, 26, 27}      # unit 1 logical: CPU, xe49 (front 49), xe50 (front 50)
SKIP_TOP = re.compile(r"^TOP_(TS_PLL|BS_PLL|BROAD_SYNC|PVTMON)")
KEEP_A8 = re.compile(r"^(EGR_ENABLE|EPC_LINK_BMAP_64|XLPORT_(SOFT_RESET|MODE_REG|ENABLE_REG)|ING_CONFIG_64|EGR_CONFIG_1|EGR_VLAN_CONTROL_1|EGR_PORT|IEGR_PORT|"
                     r"MC_CONTROL_\d|EGR_MC_CONTROL_\d|SW2_FP_DST_ACTION_CONTROL|TOP_CLOCKING_ENFORCE_PSG)$")
KEEP_A11 = re.compile(r"^(LPORT_TAB|PORT_TAB|EGR_PRI_CNG_MAP|ING_VLAN_TAG_ACTION_PROFILE|EGR_VLAN_TAG_ACTION_PROFILE|ING_VLAN_RANGE|EGR_VLAN_CONTROL_[23]|IPORT_TABLE)$")
KEEP_B = re.compile(r"^(SYSTEM_CONFIG_TABLE|VLAN_CTRL|STG_TAB|EGR_VLAN_STG|VLAN_TAB|EGR_VLAN|VLAN_PROFILE_TAB|VLAN_PROFILE_2|CPU_COS_MAP|XQCOSARBSEL|WRRWEIGHT_COS|CNGCOSPKTLIMIT[01]|"
                    r"XLMAC_\w+|XLPORT_(TXFIFO_CTRL|MAC_RSV_MASK|ENABLE_REG|SOFT_RESET|MAC_CONTROL)|EGR_PORT_CREDIT_RESET|MMUFLUSHCONTROL|ING_EN_EFILTER_BITMAP_64)$")


def select_min(ops, phases):
    """Rule-derived minimal candidate for unit 1 (xe49/xe50 + CPU). Returns [(op, phase, rule)]; untested on the board."""
    per_port = collections.Counter()
    for o in ops:
        if o.kind == "REG":
            per_port[o.name] += 0
    addrs = collections.defaultdict(set)
    for o in ops:
        addrs[(o.blk, o.name)].add(o.addr)
    pp = {k for k, v in addrs.items() if len(v) >= 20 and k[1]}      # per-port/per-index registers and tables

    def port_of(o):
        return o.addr & 0xff if o.kind == "REG" else o.addr & 0xff
    sel = []
    end = next(p[3] for p in phases if p[0] == "B6")
    for pid, _t, a, b, _w in phases:
        if a >= end:
            break
        for o in ops[a:b]:
            nm = o.name
            ok, rule = False, ""
            if pid == "A1":
                ok, rule = not SKIP_TOP.match(nm), "TOP resets/PLLs verbatim (TS/BS PLL, PVTMON dropped)"
            elif pid == "A2":
                ok, rule = o.blk == 5, "XLPORT0 (xe49/xe50 TSC) only"
            elif pid in ("A3", "A5", "A9"):
                ok, rule = True, "verbatim"
            elif pid == "A4":
                ok, rule = nm == "MISCCONFIG" and o.data[0] == 0x1d41, "only MISCCONFIG 0x1d41; IPMC zeroing skipped (untested)"
            elif pid == "A7":
                ok, rule = nm.startswith("TOP_"), "core PLL + MISC_CONTROL_1 load"
            elif pid == "A8":
                ok = bool(KEEP_A8.match(nm)) and (o.blk != 5 or True) and ((o.blk, nm) not in pp or port_of(o) in PORTS) and o.blk != 6
                rule = "port enable/EP config for CPU, 26, 27"
            elif pid == "A10":
                ok = (nm.startswith(("HOL", "LWM", "DYN", "IBP", "TOTALDYN", "E2EFC_CNT", "MMUPORTENABLE")) or nm == "") and ((o.blk, nm) not in pp or port_of(o) in PORTS) and (nm != "" or port_of(o) in PORTS)
                rule = "MMU limits for CPU, 26, 27 (+ globals)"
            elif pid == "A11":
                ok = bool(KEEP_A11.match(nm)) and (nm not in ("PORT_TAB",) or port_of(o) in PORTS)
                rule = "pipe defaults; PORT_TAB for CPU, 26, 27"
            elif pid in ("B1", "B2", "B3", "B4", "B5"):
                ok = bool(KEEP_B.match(nm)) and (o.blk in (5, 6) and o.blk == 5 or o.blk not in (2, 3, 4, 5, 6)) and ((o.blk, nm) not in pp or port_of(o) in PORTS or o.blk == 5 and port_of(o) in (0, 2) or nm in ("CPU_COS_MAP", "SYSTEM_CONFIG_TABLE", "VLAN_TAB", "EGR_VLAN", "STG_TAB", "EGR_VLAN_STG"))
                rule = "MAC for XLPORT0 lanes 0/2; CPU COS map, VLAN/STG defaults"
            if ok:
                sel.append((o, pid, rule))
    # later writes to the same location win for table/config writes outside the verbatim phases
    last = {}
    for i, (o, pid, _r) in enumerate(sel):
        if pid not in ("A1", "A2", "A3", "A5", "A9") and not re.search(r"RESET|SOFT|CREDIT|MAC_CONTROL|XLMAC_CTRL|TXFIFO|TDM", o.name):
            last[(o.kind, o.blk, o.addr)] = i
    return [t for i, t in enumerate(sel) if (t[0].kind, t[0].blk, t[0].addr) not in last or last[(t[0].kind, t[0].blk, t[0].addr)] == i]


def min_u1(ops, names, phases):
    sel = select_min(ops, phases)
    cnt = collections.Counter(p for _o, p, _r in sel)
    tot = {p[0]: p[3] - p[2] for p in phases}
    L = [
        "- Status: derived by rule from the u1 trace + register semantics (`select_min()` in `scripts/init_phases.py`). NOT yet run on the board; each step needs a read-back check.",
        "- Emit the exact op list: `python3 scripts/init_phases.py --emit-min tmp/u1_min_init.txt` (ord, ms, op, blk, addr, data, name, phase).",
        "- Stock runs u1 with the u0 script: u0 and u1 pass A+B are identical write-for-write except XLMAC_MODE/RX_CTRL of XLPORT0 (u1 xe lanes = 10G: MODE 0x40, RX_CTRL 0x408; u0 same lanes 1G: 0x20/0x400). So `bcm_cmic_hr2_init1_data.h` (u1) and `..._init_data.h` (u0) are the same list except those values.",
        "",
        "### #78 finding (host side first)",
        "- `us48_eth_start()` (uboot-port/drivers/net/us48_cmic_eth.c) calls `bcm_hr2_u0_cold_init()` only when `p->ge`; the xe1 path calls `bcm_hr2_xe1_bringup()` directly. `bcm_hr2_u1_cold_init()` exists but nothing calls it (grep over repo).",
        "- Failing op `0b blk10 addr 46002700` (read EPC_LINK_BMAP_64), ctrl `0x00400002` = DONE | bit 22 (SCHAN_ERRS bit 22, timeout): IPIPE unreachable on unit 1 = no SBUS ring map / TOP release on the cold chip. Consistent with steps 1-3 below never having run. Cold TSC state is a separate, later problem.",
        "",
        "### Steps, in order (trace ords are unit-1 write ordinals; ms = tracer clock)",
        "",
        "| # | step | phase / ord | ops kept of phase | needed because |",
        "|---|---|---|---|---|",
        "| 0 | host: PCIe up, BAR0 mapped, CMIC PIO endianness regs 0x101ec/0x101f0 = 0 | n/a (MMIO, u1 untraced) | n/a | S-channel word order (u0 trace; u1 assumed equal) |",
        "| 1 | CMIC: SBUS_TIMEOUT 0x10094 = 0x7d0; RING_MAP 0x10098.. = 11122200 00430000 00005004 0 | n/a (MMIO; u0 order: after TOP steps 1-33, before first XLPORT op ord 39) | 5 writes | blocks IPIPE/EPIPE/MMU/XLPORT/GPORT unreachable without it (TOP answers on the reset-default map, which is why ops 1-33 work first) |",
        f"| 2 | TOP PLL/soft reset, verbatim incl. the 10-11 ms gaps (ords 24->25, 27->28, 28->29) | A1 ord 1-38 | {cnt['A1']} of {tot['A1']} | releases IP/EP/MMU/XLP/GP from reset; TS/BS PLL + PVTMON writes dropped (unused feature) |",
        "| 3 | CMIC_CPS_RESET pulse between ords 27 and 28 (u0 MMIO ~1181 ms) | n/a | 1 write | optional on a cold chip with a fresh ring map; if used, the ring map and RELEASE_ALL_CREDITS must come after it |",
        f"| 4 | XLPORT0 XGXS power-up: e0, a0, +10 ms, a4, ac, ae; MAC_CONTROL 1,0 | A2 ord 39-54 (blk5 half) | {cnt['A2']} of {tot['A2']} | TSC PLL/clock for xe49/xe50; the SerDes microcode load is NOT here (MIIM, untraced) |",
        f"| 5 | pipe table reset, poll DONE (bit 18 of *_HW_RESET_CONTROL_2/_1) after each range | A3 ord 55-100 | {cnt['A3']} of {tot['A3']} | IPIPE/EPIPE tables undefined until cleared; stock does 12 reads |",
        "| 6 | CMIC RELEASE_ALL_CREDITS pulse 0 then 1 (edge) AFTER step 5 | n/a (MMIO) | 2 writes | CPU RX path credits to EP; before step 5 it is lost (#46) |",
        f"| 7 | MMU MISCCONFIG 0x1d41 (IPMC table zeroing skipped) | A4 ord 101-1380 | {cnt['A4']} of {tot['A4']} | MMU mode bits; IPMC tables only matter for multicast replication (skip untested) |",
        f"| 8 | port maps: ING_PHYSICAL_TO_LOGICAL, EGR_LOGICAL_TO_PHYSICAL, MMU LOG_TO_PHY; XLPORT_MIB_RESET pulse | A5 ord 1381-1477 | {cnt['A5']} of {tot['A5']} | logical 26 = phys 26 (xe49), logical 27 = phys 28 (xe50); CPU = 0 |",
        f"| 9 | core PLL (TOP_CORE_PLL_CTRL4/2) + MISC_CONTROL_1 load bit | A7 ord 2014-2016 | {cnt['A7']} of {tot['A7']} | core clock; EEE/parity/SER (A6, EEE) skipped |",
        f"| 10 | EP/ports: EGR_ENABLE[0,26,27]=1, EPC_LINK_BMAP bit 0, XLPORT0 SOFT_RESET/MODE_REG(0 = quad)/ENABLE_REG(5 = lanes 0,2), ING_CONFIG_64, EGR_CONFIG_1, EGR_VLAN_CONTROL_1, EGR_PORT/IEGR_PORT, MC_CONTROL | A8 ord 2330-2522 | {cnt['A8']} of {tot['A8']} | EP port enable; TX on a port needs EGR_ENABLE |",
        f"| 11 | TDM: IARB_TDM_CONTROL 0xa7, 75 slots to IARB_TDM_TABLE + MMU_ARB_TDM_TABLE, 0x94; IP_TO_CMICM_CREDIT_TRANSFER 0x60 | A9 ord 2523-2676 | {cnt['A9']} of {tot['A9']} | no TDM = no ingress/egress slot for any port; credit transfer = CPU RX path |",
        f"| 12 | MMU limits for ports 0/26/27 + MMUPORTENABLE 0x3ffffffd | A10 ord 2677-4111 | {cnt['A10']} of {tot['A10']} | without HOL/IBP/dyn limits the MMU drops everything; CPU port 0 has its own set (IBP 0x3fff, HOLCOSPKTSETLIMIT 0x67, ...) |",
        f"| 13 | LPORT_TAB[0], PORT_TAB[0,26,27], priority/tag profiles, VLAN control | A11 ord 4112-8972 | {cnt['A11']} of {tot['A11']} | ingress port defaults (our code rewrites PORT_TAB vid via port_vid_set) |",
        f"| 14 | XLMAC lanes 0/2: CTRL/MODE(0x40)/RX_CTRL(0x408)/TX_CTRL/RX_MAX_SIZE, TXFIFO clear, then CTRL 0x1003 (stock parks at 0x1041, soft reset) | B1 ord 8973-10325 | see B1/B2.. | 10G MAC; end state must be enabled (xe1_enable does this) |",
        f"| 15 | SYSTEM_CONFIG_TABLE, VLAN/STG defaults, CPU_COS_MAP, COS arbitration | B2-B5 | {sum(cnt[x] for x in ('B1','B2','B3','B4','B5'))} of {sum(tot[x] for x in ('B1','B2','B3','B4','B5'))} (B1-B5 together; 4096 of them are the SYSTEM_CONFIG_TABLE fill, index layout not decoded so not reduced) | VLAN 1 + STG forwarding; CPU COS map |",
        "| 16 | our code: EPC_LINK_BMAP bits 26/27 on link, private VLAN, PORT_TAB vid, static L2X to CPU | n/a (bcm_hr2_xe1_bringup) | n/a | stock does this in phase C (EPC_LINK_BMAP final 0x30060001 on u1) |",
        "",
        "- Not in either trace: TSC/PHY init over MIIM (PLL lock check, SerDes ucode, lane config), CMC0 DMA page, u1 CMIC MMIO. Cold xe1 needs the TSC part; `tsc enable` only works warm (docs/uboot-second-stage.md).",
        "- Dropped on purpose (harmless unless forwarding features are used): A6 parity/ECC/SER, EEE, TS/BS PLL, IPMC zeroing, MIB/debug counter clears, pass C (FP/VLAN/L2/DOS/DSCP churn).",
        "- Do not carry over: CPU-port MAXBUCKETCONFIG[0] (phase C, 0x4808d), see Questionable writes.",
        ""]
    return L


MMIO_KEY = {  # cmic offset -> (name, why)
    0x101ec: ("CMIC_COMMON_PCIE_PIO_ENDIANESS", "PIO endianness 0 (little); first, before any S-channel"),
    0x101f0: ("CMIC_COMMON_UC0_PIO_ENDIANESS", "uC PIO endianness 0"),
    0x1a000: ("CMIC_RXBUF_EPINTF_RELEASE_ALL_CREDITS", "EDGE-ACTING (0->1 releases credits to the EP); stock pulses it twice, before and after CPS_RESET; ours must come after EP reset (#46)"),
    0x10220: ("CMIC_CPS_RESET", "pulse: resets CMIC core (ring map back to default); written once, between the first two TOP_SOFT_RESET_REG steps"),
    0x10098: ("CMIC_SBUS_RING_MAP_0_7", "ring map; after CPS_RESET, before ING_HW_RESET"),
    0x1009c: ("CMIC_SBUS_RING_MAP_8_15", "ring map"),
    0x100a0: ("CMIC_SBUS_RING_MAP_16_23", "ring map"),
    0x100a4: ("CMIC_SBUS_RING_MAP_24_31", "ring map"),
    0x10094: ("CMIC_SBUS_TIMEOUT", "0x7d0 SBUS timeout, written just before pipe reset"),
    0x10260: ("CMIC_PCIE_USERIF_PURGE_CONTROL", "0x13 purge control"),
    0x11000: ("CMIC_RATE_ADJUST", "MIIM MDC divider 0x10028"),
    0x11004: ("CMIC_RATE_ADJUST_INT_MDIO", "internal MDIO divider 0x10028"),
    0x11008: ("CMIC_MIIM_SCAN_CTRL", "MIIM scan control (rewritten 29x)"),
    0x10234: ("CMIC_OVERRIDE_STRAP", "0x36 strap override"),
    0x02008: ("CMIC_GP_OUT_EN", "GPIO output enable"),
}


def mmio_lines():
    p = ROOT / "tmp/mmio_stock_init.log"
    if not p.exists():
        return []
    seq = collections.defaultdict(list)
    for ln in p.read_text().splitlines():
        f = ln.split()
        if len(f) >= 6 and f[2] == "MW":
            off = int(f[4], 16)
            if off in MMIO_KEY:
                seq[off].append(f"{int(f[5], 16):x}@{f[0]}")
    L = ["- Times are ms since that tracer start (a different boot than the bde trace); correlation with S-channel ops is +-25 ms.",
         "- 0x31000 page (S-channel, MIIM, CMC0 DMA) not trapped; LEDUP/I2CM/MSPI/BS/TIMESYNC writes omitted here.", "",
         "| order (first ms) | offset | name | values@ms | why |", "|---|---|---|---|---|"]
    for off in sorted(seq, key=lambda o: int(seq[o][0].split("@")[1])):
        n, w = MMIO_KEY[off]
        L.append(f"| {seq[off][0].split('@')[1]} | {off:#x} | {n} | {' '.join(seq[off][:6])} | {w} |")
    return L


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--unit", type=int, choices=(0, 1))
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--emit-min", metavar="FILE", help="write the rule-derived unit-1 minimal op list")
    a = ap.parse_args()
    db = hwdb.load()
    names = Names(db)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    msgs = []
    for unit in (0, 1) if a.unit is None else (a.unit,):
        ops, reads = load(unit, names)
        phases = resolve_phases(ops)
        msgs.append(f"u{unit}: {len(ops)} ops, phases " + ", ".join(f"{p[0]}[{ops[p[2]].o}-{ops[p[3] - 1].o}]" for p in phases))
        if a.emit_min and unit == 1:
            sel = select_min(ops, phases)
            pathlib.Path(a.emit_min).write_text("".join(
                f"{o.o} {o.ms} {o.kind} blk{o.blk} {o.addr:#010x} {' '.join(f'{x:08x}' for x in o.data)} {o.name} {pid}\n" for o, pid, _r in sel))
            msgs.append(f"u1 minimal list: {len(sel)} ops -> {a.emit_min}")
        if a.dump or a.emit_min:
            continue
        text = "\n".join(render(unit, ops, reads, names, phases, mmio_lines())) + "\n"
        (ROOT / f"docs/init-phases-u{unit}.md").write_text(text)
        msgs.append(f"wrote docs/init-phases-u{unit}.md ({len(text.splitlines())} lines)")
    LOG.write_text("\n".join(msgs) + "\n")
    print("\n".join(msgs))


if __name__ == "__main__":
    main()
