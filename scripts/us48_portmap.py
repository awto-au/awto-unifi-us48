#!/usr/bin/env python3
"""Check switchdrvr's FastPath 52-port table (VA 0x1d8ecb4, 4 words/entry: slot, port-1, unit, logical) against hwdb/board/ports.toml.

hwdb is the source of the port map; this script only verifies. Also checks hwdb against the OpenBCM
p2l_mapping_24_2_2 rule (phys 2..9 -> logical 9..2, 10..25 identity, 26/28/30/32 -> 26..29).
Usage: python3 scripts/us48_portmap.py [private/firmware/rootfs-selected/bin/switchdrvr]   (no ELF = hwdb self-check only)
Logs to tmp/logs/us48_portmap.log. Exit 1 on any mismatch.
"""
import argparse
import logging
import pathlib
import struct
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import hwdb  # noqa: E402

ROOT = hwdb.repo_root()
LOG = ROOT / "tmp/logs/us48_portmap.log"
TABLE_VA, TABLE_N = 0x1D8ECB4, 52
P2L = {**{2 + i: 9 - i for i in range(8)}, **{p: p for p in range(10, 26)}, 26: 26, 28: 27, 30: 28, 32: 29}


def read_table(elf):
    d = pathlib.Path(elf).read_bytes()
    phoff, = struct.unpack_from("<I", d, 0x1C)
    es, n = struct.unpack_from("<HH", d, 0x2A)
    for i in range(n):
        t, off, va, _, fsz, _, _, _ = struct.unpack_from("<8I", d, phoff + i * es)
        if t == 1 and va <= TABLE_VA < va + fsz:
            raw = d[off + TABLE_VA - va: off + TABLE_VA - va + 16 * TABLE_N]
            return [struct.unpack_from("<4I", raw, 16 * k) for k in range(TABLE_N)]
    raise SystemExit("table not mapped")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("elf", nargs="?")
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.FileHandler(LOG), logging.StreamHandler(sys.stdout)])
    db = hwdb.load(sdk=False)
    bad = 0
    for p in db.ports:
        if P2L.get(p["phys"]) != p["logical"]:
            logging.info("MISMATCH hwdb vs p2l: %s phys %d -> logical %d, p2l says %s", p["id"], p["phys"], p["logical"],
                         P2L.get(p["phys"]))
            bad += 1
    if a.elf:
        for k, (slot, port0, unit, logical) in enumerate(read_table(a.elf)):
            p = db.port(k + 1)
            ok = slot == 0 and port0 == k and p["unit"] == unit and p["logical"] == logical
            logging.info("front %2d table unit %d logical %2d | hwdb unit %d logical %2d %s", k + 1, unit, logical,
                         p["unit"], p["logical"], "ok" if ok else "MISMATCH")
            bad += not ok
    logging.info("us48_portmap: %d mismatches (%d hwdb ports%s)", bad, len(db.ports), ", switchdrvr table checked" if a.elf else "")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
