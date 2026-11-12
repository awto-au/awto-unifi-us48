#!/usr/bin/env python3
"""Clause-22 MDIO scan on unit 1 (BCM53346): ID1/ID2 of PHY 0-31 on internal buses 0-3 and external buses 0-2.

Usage (board at uboot-2, US48_HOST=lizard): python3 scripts/mdio_scan_u1.py
Unit-1 CMIC = BAR2 at 0x08040000 (stock-enumerated). Sequence as common/bcm-cmic/bcm_cmic_miim.c; U-Boot runs the
reads in one batched command line per 4 PHYs (a C22 frame is ~26 us; the console round trip dominates).
Log: tmp/logs/mdio_scan_u1.log
"""
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import console  # noqa: E402

B = 0x08040000
PARAM, RDATA, ADDR, CTRL = B + 0x31080, B + 0x31084, B + 0x31088, B + 0x3108C
LOG = pathlib.Path(__file__).resolve().parent.parent / "tmp/logs/mdio_scan_u1.log"


def one(internal, bus, phy, reg):
    p = int(internal) << 25 | bus << 22 | phy << 16
    return f"mw.l {PARAM:x} {p:x};mw.l {ADDR:x} {reg:x};mw.l {CTRL:x} 0;mw.l {CTRL:x} 2;md.l {RDATA:x} 1;"


def main():
    LOG.parent.mkdir(parents=True, exist_ok=True)
    found = []
    with console.Session() as s:
        s.write(b"\x03\r\n")
        s.read_idle(1.0, 3.0)
        for internal, buses in ((True, range(4)), (False, range(3))):
            for bus in buses:
                ids = []
                for first in range(0, 32, 4):
                    line = "".join(one(internal, bus, p, r) for p in range(first, first + 4) for r in (2, 3))
                    s.send_line(line)
                    out = s.read_idle(0.5, 4.0).decode(errors="replace")
                    ids += re.findall(rf"{RDATA:08x}: ([0-9a-f]{{8}})", out)
                hits = [(p, ids[2 * p], ids[2 * p + 1]) for p in range(32) if len(ids) >= 2 * p + 2
                        and (int(ids[2 * p], 16) & 0xFFFF) not in (0, 0xFFFF)]
                msg = f"{'int' if internal else 'ext'} bus {bus}: {len(ids)} reads, devices {[(p, a[-4:], b[-4:]) for p, a, b in hits]}"
                print(msg, flush=True)
                with LOG.open("a") as f:
                    f.write(time.strftime("%Y-%m-%dT%H:%M:%S%z ") + msg + "\n")
                found += hits
    print("total devices:", len(found))


if __name__ == "__main__":
    main()
