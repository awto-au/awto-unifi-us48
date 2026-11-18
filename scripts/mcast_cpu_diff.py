#!/usr/bin/env python3
"""mcast_cpu_diff — read-only: unit 0 vs unit 1 state that decides unknown/IPv6 multicast to the CPU (#127).

Usage: mcast_cpu_diff.py [--all]
- Per unit, loop-cable ports (u0 ge8/ge17 = lport 9/18, u1 ge32/ge41 = lport 9/18) and their private VLANs (100 + front):
  VLAN_TAB fields, VLAN_PROFILE_TAB / VLAN_PROFILE_2 at VLAN_PROFILE_PTR, L2MC at BC/UMC/UUC_IDX, PORT_TAB fields,
  per-port IGMP_MLD_PKT_CONTROL / PROTOCOL_PKT_CONTROL / (I)UNKNOWN_MCAST/UCAST_BLOCK_MASK_64, global CPU_CONTROL_*,
  ING_CONFIG_64, ING_MISC_CONFIG(2).
- Prints only rows whose decoded value differs between the units (--all: every row).
- S-channel reads through the us48_cmic `schan` parameter (_board.schan_batch, kernel-locked, #125). No writes.
- Log: tmp/logs/mcast_cpu_diff.log.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard

help_guard(__doc__, strict=True, known=("--all",))

import _board

import hwdb

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/mcast_cpu_diff.log"
IPIPE = 10
DB = hwdb.default()
# role -> (lport, front) per unit; hwdb/board/ports.toml (front 8/32 = lport 9, front 17/41 = lport 18)
PORTS = {"peer": 9, "test": 18}
FRONT = {0: {"peer": 8, "test": 17}, 1: {"peer": 32, "test": 41}}
PORT_REGS = ("IGMP_MLD_PKT_CONTROL", "PROTOCOL_PKT_CONTROL", "UNKNOWN_MCAST_BLOCK_MASK_64",
             "IUNKNOWN_MCAST_BLOCK_MASK_64", "UNKNOWN_UCAST_BLOCK_MASK_64")
GLOBAL_REGS = ("CPU_CONTROL_0", "CPU_CONTROL_1", "CPU_CONTROL_M", "ING_CONFIG_64", "ING_MISC_CONFIG",
               "ING_MISC_CONFIG2")


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def field(w: list[int], lo: int, n: int) -> int:
    return sum(((w[(lo + i) // 32] >> ((lo + i) % 32)) & 1) << i for i in range(n))


def mem(name: str) -> tuple[int, int, dict]:
    m = DB.sdk_sbus_mems[name]
    return m.offset, (max(f.hi for f in m.fields) + 32) // 32, {f.name: (f.lo, f.hi - f.lo + 1) for f in m.fields}


def reg(name: str) -> tuple[int, int, dict]:
    r = DB.sdk_sbus_regs[name]
    fl = {f.name: (f.lo, f.hi - f.lo + 1) for f in r.fields}
    return r.offset, 2 if max(f.hi for f in r.fields) >= 32 else 1, fl


def read(ops: list[tuple]) -> list[list[int]]:
    res = _board.schan_batch([(u, "r" + k, IPIPE, a, n, None) for u, k, a, n in ops])
    for r in res:
        if r["status"] != "ok":
            raise RuntimeError(r["line"])
    return [r["data"] for r in res]


def decode(w: list[int], fl: dict) -> dict:
    return {k: field(w, lo, n) for k, (lo, n) in sorted(fl.items()) if not k.startswith(("DATA_", "KEY", "MASK"))}


def unit_state(u: int) -> dict:
    """{row label: {field: value}} for one unit; labels are unit-neutral (role, not VID)."""
    st: dict = {}
    vt, vtw, vtf = mem("VLAN_TAB")
    pt, ptw, ptf = mem("PORT_TAB")
    vids = {"vlan1": 1} | {f"vlan_{r}": 100 + FRONT[u][r] for r in PORTS}
    rows = read([(u, "mem", vt + v, vtw) for v in vids.values()])
    for (lbl, v), w in zip(vids.items(), rows):
        st[lbl] = decode(w, vtf) | {"raw": " ".join(f"{x:08x}" for x in w[:vtw])}
    pp, ppw, ppf = mem("VLAN_PROFILE_TAB")
    p2, p2w, p2f = mem("VLAN_PROFILE_2")
    l2, l2w, l2f = mem("L2MC")
    for lbl in list(vids):
        d = st[lbl]
        ptr = d["VLAN_PROFILE_PTR"]
        a, b = read([(u, "mem", pp + ptr, ppw), (u, "mem", p2 + ptr, p2w)])
        st[f"{lbl}.profile"] = decode(a, ppf) | {"ptr": ptr}
        st[f"{lbl}.profile2"] = decode(b, p2f) | {"ptr": ptr}
        for idx in ("BC_IDX", "UMC_IDX", "UUC_IDX"):
            (m,) = read([(u, "mem", l2 + d[idx], l2w)])
            st[f"{lbl}.L2MC[{idx}]"] = decode(m, l2f) | {"index": d[idx]}
    for r, lp in PORTS.items():
        (w,) = read([(u, "mem", pt + lp, ptw)])
        st[f"PORT_TAB.{r}"] = decode(w, ptf)
        for name in PORT_REGS:
            a, n, fl = reg(name)
            (w,) = read([(u, "reg", a + lp, n)])
            st[f"{name}.{r}"] = decode(w, fl) | {"raw": f"{w[0]:08x}"}
        ppi = st[f"PORT_TAB.{r}"]["PROTOCOL_PKT_INDEX"]
        a, n, fl = reg("PROTOCOL_PKT_CONTROL")
        (w,) = read([(u, "reg", a + ppi, n)])
        st[f"PROTOCOL_PKT_CONTROL[ppi].{r}"] = decode(w, fl) | {"ppi": ppi}
    for name in GLOBAL_REGS:
        a, n, fl = reg(name)
        (w,) = read([(u, "reg", a, n)])
        st[name] = decode(w, fl) | {"raw": " ".join(f"{x:08x}" for x in w[:n])}
    return st


def main() -> int:
    show_all = "--all" in sys.argv[1:]
    s = {u: unit_state(u) for u in (0, 1)}
    ndiff = 0
    for lbl in s[0]:
        a, b = s[0][lbl], s[1].get(lbl, {})
        for k in a:
            if k == "raw" or k.startswith("RESERVED") or k == "EVEN_PARITY":
                continue
            diff = a[k] != b.get(k)
            if diff and lbl.startswith(("vlan_", "vlan1")) and k in ("PORT_BITMAP", "PORT_BITMAP_LO", "L3_IIF"):
                diff = False  # expected per-VID / per-port differences
            if diff or show_all:
                ndiff += diff
                log(f"{'DIFF' if diff else '    '} {lbl:34s} {k:36s} u0 {a[k]:#x}  u1 {b.get(k, -1):#x}")
        if "raw" in a:
            log(f"     {lbl:34s} raw u0 {a['raw']} | u1 {b.get('raw')}")
    log(f"{ndiff} differing fields")
    return 0


if __name__ == "__main__":
    sys.exit(main())
