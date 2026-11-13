#!/usr/bin/env python3
"""Clause-22 PHY access through a chip's own CMIC MIIM, from uboot-2: first chip (u0, BCM53344, AXI 0x48000000) or second chip (u1, BCM53346, PCIe BAR2 0x08040000).

Usage (board at uboot-2, US48_HOST=lizard):
  phy.py u0|u1 read  int|ext BUS PHY REG
  phy.py u0|u1 write int|ext BUS PHY REG VALUE
  phy.py u0|u1 up    int|ext BUS PHY        minimal bring-up: BMCR = AN + restart + FD + 1000, then poll BMSR for link
Frame ~26 us; the console round trip dominates.
Link poll: 1 s steps, 8 s budget (autoneg takes ~1.5-3 s for 10/100, longer for 1000BASE-T). Log: tmp/logs/phy.log
"""
import pathlib
import re
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import console  # noqa: E402

BASES = {"u0": 0x48000000, "u1": 0x08040000}
B = BASES["u1"]
PARAM = RDATA = ADDR = CTRL = 0
LOG = pathlib.Path(__file__).resolve().parent.parent / "tmp/logs/phy.log"
POLL_S, POLL_BUDGET_S = 1.0, 8.0


def log(msg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    print(msg, flush=True)
    with LOG.open("a") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%S%z ") + msg + "\n")


def cmd(s, c, w=0.3):
    s.send_line(c)
    return s.read_idle(w, 3.0).decode(errors="replace")


def mread(s, internal, bus, phy, reg):
    p = int(internal) << 25 | bus << 22 | phy << 16
    cmd(s, f"mw.l {PARAM:x} {p:x};mw.l {ADDR:x} {reg:x};mw.l {CTRL:x} 0;mw.l {CTRL:x} 2", 0.2)
    m = re.search(rf"{RDATA:08x}: ([0-9a-f]{{8}})", cmd(s, f"md.l {RDATA:x} 1"))
    return int(m.group(1), 16) & 0xFFFF if m else None


def mwrite(s, internal, bus, phy, reg, val):
    p = int(internal) << 25 | bus << 22 | phy << 16 | (val & 0xFFFF)
    cmd(s, f"mw.l {PARAM:x} {p:x};mw.l {ADDR:x} {reg:x};mw.l {CTRL:x} 0;mw.l {CTRL:x} 1", 0.2)
    cmd(s, f"mw.l {CTRL:x} 0", 0.1)


def main():
    global B, PARAM, RDATA, ADDR, CTRL
    a = sys.argv[1:]
    if len(a) < 5 or a[0] not in BASES or a[1] not in ("read", "write", "up") or a[2] not in ("int", "ext"):
        raise SystemExit(__doc__)
    B = BASES[a.pop(0)]
    PARAM, RDATA, ADDR, CTRL = B + 0x31080, B + 0x31084, B + 0x31088, B + 0x3108C
    op, internal, bus, phy = a[0], a[1] == "int", int(a[2], 0), int(a[3], 0)
    with console.Session() as s:
        s.write(b"\x03\r\n")
        s.read_idle(1.0, 3.0)
        if op == "read":
            log(f"{a[1]} bus {bus} phy {phy} reg {int(a[4], 0)} = {mread(s, internal, bus, phy, int(a[4], 0)):#06x}")
        elif op == "write":
            mwrite(s, internal, bus, phy, int(a[4], 0), int(a[5], 0))
            log(f"wrote {int(a[5], 0):#06x} to reg {int(a[4], 0)}")
        else:
            log(f"before: BMCR {mread(s, internal, bus, phy, 0):#06x} BMSR {mread(s, internal, bus, phy, 1):#06x}")
            mwrite(s, internal, bus, phy, 0, 0x1340)  # AN enable | restart AN | full duplex | speed 1000
            t0 = time.monotonic()
            while time.monotonic() - t0 < POLL_BUDGET_S:
                threading.Event().wait(POLL_S)
                mread(s, internal, bus, phy, 1)  # BMSR link bit is latched low: first read clears it
                st = mread(s, internal, bus, phy, 1)
                log(f"+{time.monotonic() - t0:.0f}s BMCR {mread(s, internal, bus, phy, 0):#06x} BMSR {st:#06x} "
                    f"LINK {'UP' if st & 4 else 'down'} partner-ability {mread(s, internal, bus, phy, 5):#06x}")
                if st & 4:
                    return


if __name__ == "__main__":
    main()
