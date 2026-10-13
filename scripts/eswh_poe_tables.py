#!/usr/bin/env python3
"""Decode the hpc_ubnt_poe PoE card tables of a switchdrvr ELF (EdgeSwitch ESWH build).

Layout (stride 0x128 per card, found by inspection): board id u32, card count u32, 2 words,
then per card: mux name[0x20], mux select u32 @+0x20, {mux count, chip count, first port,
port count} u32 @+0x48, chip entries @+0x58 (name[0x20] + addr u8, packed, stride 0x21).
Usage: python3 scripts/eswh_poe_tables.py ELF [--lo 0x1c00000 --hi 0x1d00000]
Logs to tmp/logs/eswh_poe_tables.log.
"""
import argparse
import logging
import pathlib
import struct
import sys

from elftools.elf.elffile import ELFFile

LOG = pathlib.Path("tmp/logs/eswh_poe_tables.log")
CARD_STRIDE = 0x128


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("elf")
    ap.add_argument("--lo", type=lambda x: int(x, 0), default=0x1c00000)
    ap.add_argument("--hi", type=lambda x: int(x, 0), default=0x1d00000)
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.FileHandler(LOG, "w"), logging.StreamHandler(sys.stdout)])
    d = pathlib.Path(a.elf).read_bytes()
    with open(a.elf, "rb") as f:
        segs = [(s["p_vaddr"], s["p_offset"], s["p_filesz"])
                for s in ELFFile(f).iter_segments() if s["p_type"] == "PT_LOAD"]

    def rd(va, n):
        for v, o, z in segs:
            if v <= va < v + z:
                return d[o + va - v:o + va - v + n]
        return b"\0" * n

    def u32(va):
        return struct.unpack("<I", rd(va, 4))[0]

    def cs(va):
        return rd(va, 32).split(b"\0")[0].decode("latin1")

    for va in range(a.lo, a.hi, 4):
        w, n = u32(va), u32(va + 4)
        if (w >> 16) != 0xB344 or not 1 <= n <= 4 or u32(va + 0x14) != 0x6D757830 and cs(va + 0x10) != "mux0":
            continue
        logging.info("board %#x at %#x: %d card(s)", w, va, n)
        for c in range(n):
            p = va + 0x10 + c * CARD_STRIDE
            nm, nch, first, cnt = (u32(p + 0x48 + 4 * i) for i in range(4))
            chips = [(cs(p + 0x58 + 0x21 * i), rd(p + 0x58 + 0x21 * i + 0x20, 1)[0]) for i in range(nch)]
            logging.info("  card %d: mux %s sel=%#x mux_count=%d first_port=%d ports=%d chips=%s",
                         c, cs(p), u32(p + 0x20), nm, first, cnt, [f"{a_}@{b:#x}" for a_, b in chips])


main()
