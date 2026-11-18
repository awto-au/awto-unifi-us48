#!/usr/bin/env python3
"""higig_dump — read-only HiGig/TSC1 + stacking dump of both chips, ours vs stock (#57, #78).

Usage:
  higig_dump.py ours  [--out tmp/higig/ours.txt]                    our Linux (tmp/tsc_read via lx.py)
  higig_dump.py stock [--out tmp/higig/stock.txt] [--host IP]       stock Linux (ssh ubnt, tmp/tsc_read pushed)
  higig_dump.py diff A B                                            keys whose values differ
- Reads (no writes except the TSC AER select): per unit, TSC1 (MIIM bus 1 C45 addr 5 = phys 30) lanes 0-3 over the
  tsc_lane_dump.py ranges; TSC0 (addr 1, lanes 0/2: unit 0 SFP 1/2, unit 1 SFP+ 1/2); XLPORT0/1 core + per-lane regs, XLMAC lanes 0/2;
  IPIPE/EPIPE/MMU stacking regs; PORT_TAB/EGR_ENABLE/MODPORT_MAP_* entries. Build tsc_read: build_bde_trace.py --tscread.
- Every MIIM register is read twice; a mismatch is kept as "a/b" (live status, or a race with the owner of the MIIM bus:
  neither stock nor our driver locks against /dev/mem).
- Output lines "<unit> <kind> <key> <value>". Log: tmp/logs/higig_dump.log. Each ssh call is bounded (60 s; one
  call reads ~6000 MIIM regs at ~30 us each = ~0.4 s plus shell start).
"""
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hwdb  # noqa: E402
from _cli import help_guard  # noqa: E402
import tsc_lane_dump as tld  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/higig_dump.log"
BASE = {0: "0x%08x" % hwdb.cmic_base(0), 1: "0x%08x" % hwdb.cmic_base(1)}
IPIPE, EPIPE, MMU, XLP0, XLP1 = 10, 11, 12, 5, 6
# (unit, MIIM addr, lanes): TSC1 = HiGig phys 30/32 on both units; unit 0 TSC0 = SFP 1/2 (phys 26/28).
TSCS = [(0, 5, (0, 1, 2, 3)), (1, 5, (0, 1, 2, 3)), (0, 1, (0, 2)), (1, 1, (0, 2))]
REGS = [  # (blk, name, addr, words)
    (IPIPE, "EPC_LINK_BMAP_64", 0x46002700, 1), (IPIPE, "LINK_STATUS_64", 0x46002c00, 1),
    (IPIPE, "HIGIG_TRUNK_CONTROL", 0x46002600, 1), (IPIPE, "HG_TRUNK_FAILOVER_ENABLE", 0x46002b00, 1),
    (IPIPE, "HIGIG_BITMAP", 0x46003300, 1), (IPIPE, "HG_TRUNK_BITMAP0", 0x46006900, 1),
    (IPIPE, "HG_TRUNK_BITMAP1", 0x46006a00, 1), (IPIPE, "HG_TRUNK_GROUP0", 0x46006b00, 1),
    (IPIPE, "HG_TRUNK_GROUP1", 0x46006c00, 1), (IPIPE, "ING_CONFIG_64", 0x6000000, 2),
    (IPIPE, "HG_LOOKUP_DESTINATION", 0xa000200, 1), (EPIPE, "EGR_CONFIG", 0x6000400, 1),
    (EPIPE, "EGR_CONFIG_1", 0x6000500, 1), (MMU, "E2EFC_IBP_EN", 0x2030e00, 1), (MMU, "E2EFC_TX_RMODID", 0x2032200, 1),
    (MMU, "E2EFC_RX_RMODID", 0x2032300, 1), (MMU, "E2EFC_IBP_HG_RMOD", 0x2030f00, 1), (MMU, "E2EFC_CONFIG", 0x2032000, 1),
] + [(IPIPE, f"IHG_LOOKUP[{p}]", 0x8000100 | p, 1) for p in (26, 27, 28, 29)] \
  + [(IPIPE, f"MODPORT_MAP_SEL[{p}]", 0x44001e00 | p, 1) for p in (0, 2, 26, 27, 28, 29)] \
  + [(EPIPE, f"EGR_PORT[{p}]", 0x4000900 | p, 1) for p in (0, 2, 26, 27, 28, 29)]
MEMS = [(IPIPE, f"PORT_TAB[{p}]", 0x04000000 + p, 8) for p in (0, 2, 26, 27, 28, 29)] \
     + [(EPIPE, f"EGR_ENABLE[phys {p}]", 0x345c0000 + p, 1) for p in range(26, 34)] \
     + [(IPIPE, f"{n}[{m}]", b + m, 1) for n, b in (("MODPORT_MAP_SW", 0x44180000), ("MODPORT_MAP_IM", 0x441c0000),
                                                    ("MODPORT_MAP_EM", 0x44200000)) for m in range(8)] \
     + [(IPIPE, "VLAN_TAB[1]", 0x14000001, 4), (EPIPE, "EGR_VLAN[1]", 0x100c0001, 3)] \
     + [(IPIPE, f"SRC_MODID_BLOCK[{m}]", 0x44340000 + m, 1) for m in range(4)]


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    print(msg, flush=True)
    with LOG.open("a") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%S%z ") + msg + "\n")


def requests(unit: int) -> tuple[list[str], list[tuple]]:
    req, keys = [], []
    for u, a, lanes in TSCS:
        if u != unit:
            continue
        for lane in lanes:
            for lo, hi, aer in tld.RANGES:
                if aer == 0 and lane != lanes[0]:
                    continue  # core regs once
                al = lane if aer == "lane" else 0
                for reg in range(lo, hi + 1):
                    for _ in range(2):
                        req.append(f"{a} {al} {reg:x}")
                    keys.append(("tsc", f"a{a}/l{al}/{reg:x}", 1))
    for blk in (XLP0, XLP1):
        for i in range(0x40):
            req.append(f"B {blk} 1 {0x2020000 | i << 8:x}")
            keys.append(("xlp", f"b{blk}/core/{0x2020000 | i << 8:x}", 1))
        for lane in (0, 1, 2, 3):
            for i in range(0x40):
                req.append(f"B {blk} 1 {0x20000 | i << 8 | lane:x}")
                keys.append(("xlp", f"b{blk}/l{lane}/{0x20000 | i << 8 | lane:x}", 1))
        for lane in (0, 2):
            for i in range(0x30):
                req.append(f"B {blk} 2 {0x60000 | i << 8 | lane:x}")
                keys.append(("xlmac", f"b{blk}/l{lane}/{0x60000 | i << 8 | lane:x}", 2))
    for blk, name, addr, words in REGS:
        req.append(f"B {blk} {min(words, 2)} {addr:x}")
        keys.append(("reg", name, min(words, 2)))
    for blk, name, addr, words in MEMS:
        req.append(f"M {blk} {words} {addr:x}")
        keys.append(("mem", name, words))
    return req, keys


def parse(unit: int, out: str, keys: list[tuple]) -> list[str]:
    got = [ln.split() for ln in out.splitlines() if ln.strip()]
    miim = [g for g in got if g[0] not in ("B", "M")]
    sch = [g for g in got if g[0] in ("B", "M")]
    lines, mi, si = [], 0, 0
    for kind, key, words in keys:
        if kind == "tsc":
            a, b = miim[mi][3], miim[mi + 1][3]
            mi += 2
            v = "timeout" if a == "ffffffff" else f"{int(a, 16):04x}" if a == b else f"{int(a, 16):04x}/{int(b, 16):04x}"
        else:
            g = sch[si]
            si += 1
            if g[3] == "ffffffff" or "timeout" in g:
                v = "-"
            else:
                v = " ".join(f"{int(x, 16):08x}" for x in g[4:4 + words])
        lines.append(f"{unit} {kind} {key} {v}")
    if mi != len(miim) or si != len(sch):
        raise SystemExit(f"unit {unit}: parsed {mi}/{len(miim)} MIIM, {si}/{len(sch)} S-channel lines")
    return lines


def run_ours(unit: int, req: list[str]) -> str:
    scratch = ROOT / "tmp/higig/req.txt"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_text("\n".join(req) + "\n")
    lx = [sys.executable, str(ROOT / "scripts/lx.py")]
    subprocess.run(lx + ["--put", str(ROOT / "tmp/tsc_read"), "/tmp/tsc_read"], check=True, capture_output=True, timeout=60)
    subprocess.run(lx + ["--put", str(scratch), "/tmp/hgreq.txt"], check=True, capture_output=True, timeout=60)
    r = subprocess.run(lx + [f"/tmp/tsc_read {BASE[unit]} < /tmp/hgreq.txt"], capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise SystemExit(f"lx rc={r.returncode}: {r.stderr[-300:]}")
    return r.stdout


def run_stock(unit: int, req: list[str], host: str) -> str:
    with open(ROOT / "tmp/tsc_read", "rb") as f:
        r = tld.ssh(host, "cat > /tmp/tsc_read && chmod +x /tmp/tsc_read", stdin=f.read(), timeout=60)
    if r.returncode:
        raise SystemExit(f"push rc={r.returncode} {r.stderr!r}")
    r = tld.ssh(host, f"/tmp/tsc_read {BASE[unit]}", stdin=("\n".join(req) + "\n").encode(), timeout=60)
    if r.returncode:
        raise SystemExit(f"tsc_read rc={r.returncode} {r.stderr[-300:]!r}")
    return r.stdout.decode()


def dump(kind: str, out: Path, host: str) -> None:
    lines = []
    for unit in (0, 1):
        req, keys = requests(unit)
        txt = run_ours(unit, req) if kind == "ours" else run_stock(unit, req, host)
        lines += parse(unit, txt, keys)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    log(f"{kind}: {len(lines)} lines -> {out}")


def diff(a: Path, b: Path) -> None:
    load = lambda p: {" ".join(ln.split()[:3]): " ".join(ln.split()[3:]) for ln in p.read_text().splitlines()}  # noqa: E731
    da, db = load(a), load(b)
    n = 0
    for k in da:
        if k in db and da[k] != db[k]:
            n += 1
            print(f"{k}: {a.stem}={da[k]} {b.stem}={db[k]}")
    log(f"diff {a.name} vs {b.name}: {n} differ of {len(set(da) & set(db))} common")


def main() -> int:
    a = sys.argv[1:]
    opt = lambda n, d: a[a.index(n) + 1] if n in a else d  # noqa: E731
    if a[:1] in (["ours"], ["stock"]):
        dump(a[0], ROOT / opt("--out", f"tmp/higig/{a[0]}.txt"), opt("--host", os.environ.get("US48_SWITCH_IP", "192.168.25.206")))
    elif a[:1] == ["diff"] and len(a) == 3:
        diff(Path(a[1]), Path(a[2]))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
