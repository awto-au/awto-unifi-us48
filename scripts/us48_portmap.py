#!/usr/bin/env python3
"""Print the US-48-500W front-port map: front port -> (unit, logical, physical, octal PHY MDIO addr).

Sources: FastPath 52-port table (switchdrvr VA 0x1d8ecb4, 4 words/entry: slot, port-1, unit, logical)
and OpenBCM HR2 tables (bcm5615x_config=10 -> p2l_mapping_24_2_2; _soc_phy_addr_bcm56150 default).
Usage: python3 scripts/us48_portmap.py private/firmware/rootfs-selected/bin/switchdrvr
Logs to tmp/logs/us48_portmap.log.
"""
import argparse
import logging
import pathlib
import struct
import sys

LOG = pathlib.Path("tmp/logs/us48_portmap.log")
TABLE_VA, TABLE_N = 0x1D8ECB4, 52
# p2l_mapping_24_2_2: index = physical port, value = logical port
P2L = {0: 0, **{2 + i: 9 - i for i in range(8)}, **{p: p for p in range(10, 26)},
       26: 26, 28: 27, 30: 28, 32: 29}
L2P = {v: k for k, v in P2L.items()}
PHY_BASE = lambda p: p if p <= 9 else (p + 1 if p <= 17 else p + 2)  # octal PHY MDIO addr, bus 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("elf")
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.FileHandler(LOG), logging.StreamHandler(sys.stdout)])
    d = pathlib.Path(a.elf).read_bytes()
    phoff, = struct.unpack_from("<I", d, 0x1C)
    es, n = struct.unpack_from("<HH", d, 0x2A)
    raw = None
    for i in range(n):
        t, off, va, _, fsz, _, _, _ = struct.unpack_from("<8I", d, phoff + i * es)
        if t == 1 and va <= TABLE_VA < va + fsz:
            raw = d[off + TABLE_VA - va: off + TABLE_VA - va + 16 * TABLE_N]
    assert raw, "table not mapped"
    logging.info("front unit logical phys  mdio(bus0)  note")
    for k in range(TABLE_N):
        slot, port0, unit, logical = struct.unpack_from("<4I", raw, 16 * k)
        assert port0 == k and slot == 0
        phys = L2P[logical]
        if logical <= 25:
            note = f"octal PHY {'ABC'[(phys - 2) // 8]} ch{(phys - 2) % 8}"
            mdio = f"0x{PHY_BASE(phys):02x}"
        else:
            note = "SFP uplink " + ("1G (BCM53344 ge)" if unit == 0 else "10G (BCM53346 xe)")
            mdio = "-"
        logging.info("%5d %4d %7d %4d  %-10s  %s", k + 1, unit, logical, phys, mdio, note)


if __name__ == "__main__":
    main()
