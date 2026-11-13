#!/usr/bin/env python3
"""Read-only TSC0 status of the second chip (BCM53346: SFP+ 1 = lane 0 / MIIM A 2, SFP+ 2 = lane 2 / A 4) from uboot-2.

Usage (board at uboot-2, US48_HOST=lizard):  tsc_status.py [1|2]     SFP+ port, default both
Checks = docs/driver-internals.md 13.2 (PLL, uC/firmware, signal detect, CDR, PCS, TX). Clause 45 on internal bus 1, AER rewritten before each access (13.1).
Frame ~26 us; the console round trip (~0.3 s) dominates, ~15 reads per port. Log: tmp/logs/tsc_status.log
"""
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import console  # noqa: E402

B = 0x08040000
PARAM, RDATA, ADDR, CTRL = B + 0x31080, B + 0x31084, B + 0x31088, B + 0x3108C
LOG = pathlib.Path(__file__).resolve().parent.parent / "tmp/logs/tsc_status.log"
PORTS = {1: (2, 0), 2: (4, 2)}   # SFP+ N -> (MIIM A, lane)
# (name, addr32 as in 13.2: devad<<16 | reg, copies 0 = per lane AER, copies 1 = core AER 0), bit/mask description
CHECKS = [
    ("ANAPLL_STATUS (PLLSEQPASS b11, LOSTPLLLOCK b6)", 0x9010, False),
    ("uC CRC (nonzero = ucode ran)", 0xF015, False),
    ("uC VERSION", 0xF010, False),
    ("DOWNLOAD_STATUS (INIT_DONE b15)", 0xFFC5, False),
    ("COMMAND (UC_RESET_N b4)", 0xFFC2, False),
    ("UC_CTRL lane (READY b7, ERR b6)", 0xC20E, True),
    ("RX PMA_PMD_LIVE (SIGDET b1, SIGNAL_OK b0, PMD_LOCK b3)", 0xC150, True),
    ("ANARX_STATUS1 lane (SIGDET b15)", 0xC02B, True),
    ("RX_SIGDET override lane (b2 must be 0)", 0xC138, True),
    ("RX PCS_LIVE (SYNC b0, LINK b1, LOCAL_FAULT b8)", 0xC154, True),
    ("ANATX_DRIVER lane (ELEC_IDLE b15)", 0xC017, True),
    ("TX_X4_MISC lane (RSTB b1, ENABLE b0)", 0xC113, True),
]


def log(msg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    print(msg, flush=True)
    with LOG.open("a") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%S%z ") + msg + "\n")


def cmd(s, c, w=0.2):
    s.send_line(c)
    return s.read_idle(w, 3.0).decode(errors="replace")


def c45_write(s, a, reg, val):
    cmd(s, f"mw.l {PARAM:x} {0x02600000 | a << 16 | val:x};mw.l {ADDR:x} {reg:x};mw.l {CTRL:x} 0;mw.l {CTRL:x} 1")
    cmd(s, f"mw.l {CTRL:x} 0", 0.1)


def c45_read(s, a, reg):
    cmd(s, f"mw.l {PARAM:x} {0x02600000 | a << 16:x};mw.l {ADDR:x} {reg:x};mw.l {CTRL:x} 0;mw.l {CTRL:x} 2")
    m = re.search(rf"{RDATA:08x}: ([0-9a-f]{{8}})", cmd(s, f"md.l {RDATA:x} 1"))
    return int(m.group(1), 16) & 0xFFFF if m else None


def main():
    sel = [int(x) for x in sys.argv[1:]] or [1, 2]
    with console.Session() as s:
        s.write(b"\x03\r\n")
        s.read_idle(1.0, 3.0)
        for n in sel:
            a, lane = PORTS[n]
            log(f"== SFP+ {n} (front {48 + n}), MIIM A {a}, lane {lane}")
            for name, reg, per_lane in CHECKS:
                c45_write(s, a, 0xFFDE, lane if per_lane else 0)   # AER, devad 0
                v = c45_read(s, a, reg)
                log(f"  {name:58s} reg {reg:#06x} = {'n/a' if v is None else f'{v:#06x}'}")


if __name__ == "__main__":
    main()
