#!/usr/bin/env python3
"""asic_fwd_test — Part B (#57, #4): ASIC forwarding readiness over the HiGig links, read-only (ssh + devmem S-channel).

Usage: asic_fwd_test.py survey
- Reads on both units: EPC_LINK_BMAP_64, LINK_STATUS_64, HiGig trunk regs, XLPORT1 (HiGig, logical 28/29 = XLPORT1
  lanes 0/2) ENABLE/MODE/CONFIG, XLMAC_CTRL/MODE/RX_LSS, MIB RPKT/TPKT, EGR_PORT, IHG_LOOKUP, MODPORT_MAP_SEL,
  PORT_TAB[28/29], VLAN_TAB[1], EGR_VLAN[1], EGR_ENABLE[phys 30-33], MODPORT_MAP[0-3]. No writes.
- Verdict per HiGig port: ready = XLPORT_ENABLE bit, XLMAC TX_EN+RX_EN, SOFT_RESET clear, XLPORT_CONFIG HIGIG2_MODE.
  Exit 0 = both chips ready (a forwarding test may follow), 2 = not ready (stop: the HiGig bring-up is missing).
- Planned test once ready (switch-model.md stage 3): VLAN 200 = {ge17, one HiGig port per chip, ge41}, untagged on ge17/ge41;
  ge8/ge32 stay in private VLANs; never both 28 and 29 without a HiGig trunk; never ge1 or the CPU port.
- S-channel via devmem is not locked against the driver: run with no link change / ifup / reload in flight.
- Log: tmp/logs/asic_fwd_test.log.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402
import _board  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/asic_fwd_test.log"
IPIPE, EPIPE, XLP1 = 10, 11, 6
HG_LANE = {28: 0, 29: 2}	# logical 28/29 = phys 30/32 = XLPORT1 lanes 0/2 (board.md, port-map)


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


def rd(ops: list[tuple]) -> list[list[int]]:
    """ops: (unit, 'reg'|'mem', blk, addr, words) -> data words."""
    res = _board.schan_batch([(u, "r" + k, b, a, n, None) for u, k, b, a, n in ops])
    return [r[1:] for r in res]


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
        ops += [(u, "mem", IPIPE, 0x14000001, 4), (u, "mem", EPIPE, 0x100c0001, 3)]
        names += ["VLAN_TAB[1]", "EGR_VLAN[1]"]
        ops += [(u, "mem", EPIPE, 0x345c0000 + p, 1) for p in (30, 31, 32, 33)]
        names += [f"EGR_ENABLE[phys {p}]" for p in (30, 31, 32, 33)]
        ops += [(u, "mem", IPIPE, 0x44140000 + m, 1) for m in range(4)]
        names += [f"MODPORT_MAP[{m}]" for m in range(4)]
        for n, v in zip(names, rd(ops)):
            extra = ""
            if n.endswith("PORT_TAB"):
                extra = (f"  PORT_VID {field(v, 24, 12)} MY_MODID {field(v, 40, 7)} PORT_TYPE {field(v, 36, 2)} "
                         f"HIGIG2 {field(v, 55, 1)} HIGIG_TRUNK {field(v, 51, 1)} FILTER {field(v, 0, 1)} EN_IFILTER {field(v, 5, 1)}")
            elif n.endswith("EGR_PORT"):
                extra = f"  PORT_TYPE {v[0] & 3} HIGIG2 {v[0] >> 2 & 1} MY_MODID {v[0] >> 9 & 0x7f}"
            elif n.startswith("VLAN_TAB"):
                extra = f"  PORT_BITMAP {field(v, 0, 30):#x} VALID {field(v, 38, 1)} STG {field(v, 39, 9)} PROFILE {field(v, 60, 4)}"
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["survey"])
    a = ap.parse_args()
    log(f"--- asic_fwd_test {a.cmd}")
    return {"survey": survey}[a.cmd]()


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
