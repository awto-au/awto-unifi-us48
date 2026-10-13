#!/usr/bin/python3
"""fan_frame — send one 7-byte MSPI board-IO fan frame from the U-Boot prompt (mw/md, RAM only).

Usage: scripts/fan_frame.py BYTE0 [--chain A B] [--ports 0x00]
- BYTE0: bits 5:4 = stock preset (0x00/0x10/0x20/0x30 = 50/70/90/100 %), bits 3:0 = unknown (test).
- Chain select: A = CMIC unit 0 GP0 (0x48002004), B = unit 1 GP0 (left at reset default here) → (1,0) = unit 0 fans.
- MSPI config replayed from stock capture (docs/fan.md): SPCR0 0x8d/0x83, SPCR1 1/1, CDRAM 0x80…0x00.
Board must be at `u-boot>` (scripts/dev.py watch shows state). Log: tmp/logs/fan_frame.log
"""
import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import console  # noqa: E402

LOG = Path(__file__).resolve().parent.parent / "tmp/logs/fan_frame.log"
MSPI, GP0 = 0x48001500, 0x48002000
# MSPI frame at 1 MHz-ish SPBR: 7 bytes ≈ 60 µs; U-Boot md round trip dominates. 2 s = done-flag poll cap.
DONE_BUDGET_S = 2.0


def mw(s, addr: int, val: int) -> None:
    s.send_line(f"mw.l {addr:x} {val:x}")
    s.read_idle(0.15, 2.0)


def md(s, addr: int) -> int | None:
    s.buf = b""
    s.send_line(f"md.l {addr:x} 1")
    out = s.read_idle(0.2, 2.0).decode(errors="replace")
    m = re.search(rf"{addr:08x}:\s+([0-9a-f]{{8}})", out)
    return int(m.group(1), 16) if m else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("byte0", type=lambda x: int(x, 0))
    ap.add_argument("--ports", type=lambda x: int(x, 0), default=0x00, help="bytes 1-6 value (stock 0xaa)")
    a = ap.parse_args()
    frame = [a.byte0] + [a.ports] * 6
    with console.Session() as s:
        mw(s, GP0 + 0x4, 0x1)          # CMIC_GP_DATA_OUT: A = 1
        mw(s, GP0 + 0x8, 0x1)          # CMIC_GP_OUT_EN: GP0 output
        mw(s, MSPI + 0x180, 0x1)       # CMICM_BSPI_MAST_N_BOOT = 1: MSPI owns the bus
        for off, v in ((0x0, 0x8D), (0x4, 0x83), (0x8, 0x1), (0xC, 0x1)):
            mw(s, MSPI + off, v)       # SPCR0 LSB/MSB, SPCR1 LSB/MSB (stock capture)
        for i, b in enumerate(frame):
            mw(s, MSPI + 0x40 + 8 * i, b)                                   # TXRAM (stride 8)
            mw(s, MSPI + 0x140 + 4 * i, 0x80 if i < len(frame) - 1 else 0)  # CDRAM: CONT except last
        mw(s, MSPI + 0x10, 0)                  # NEWQP
        mw(s, MSPI + 0x14, len(frame) - 1)     # ENDQP
        mw(s, MSPI + 0x20, 0)                  # MSPI_STATUS clear
        mw(s, MSPI + 0x18, 0x40)               # SPCR2 = SPE: go
        t0, st = time.monotonic(), None
        while time.monotonic() - t0 < DONE_BUDGET_S:
            st = md(s, MSPI + 0x20)
            if st is not None and st & 1:
                break
    msg = f"frame {' '.join(f'{b:02x}' for b in frame)} status={st}"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%S%z ") + msg + "\n")
    print(msg, "DONE" if st and st & 1 else "NOT DONE")
    return 0 if st and st & 1 else 1


if __name__ == "__main__":
    sys.exit(main())
