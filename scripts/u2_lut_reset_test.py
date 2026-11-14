#!/usr/bin/env python3
"""#63 E1-E3: warm-reboot-into-uboot-2 experiments, run from the uboot-2 prompt.

Loads an SRAM stub at 0x1b000000 (prints '!' on UART1, optionally writes a DDR/SRAM
marker, chains to 0x1c000000), sets the ROM LUT word 0xffff0400, triggers a reset.
Pass = '!' appears before the stock banner. Log: tmp/logs/u2_lut_reset_test.log
"""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/u2_lut_reset_test.log"
STUB = [
    0xE59F0024, 0xE5D01005, 0xE3110020, 0x0AFFFFFC,  # wait THRE
    0xE3A01021, 0xE5C01000,  # '!'
    0xE5D01005, 0xE3110040, 0x0AFFFFFC,  # wait TEMT
    0xE59FF004, 0xE1A00000,  # ldr pc,=0x1c000000
    0x18000400, 0x1C000000,
]
SRAM = 0x1B000000
LUT = "0xffff0400"

RESETS = {
    "dmu": ["mw.l 0x1803f200 0x1"],  # IPROC_RESET_N cleared (what `reset` does)
    "swrst": ["mw.l 0x1803f200 0x2"],  # SW_RESET_N cleared
    "core": ["mw.l 0x19000f00 0xa5a501", "mw.l 0x19000f08 0x2"],  # A9 core0 reset
}


def send(cmd, cap=3):
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts/dev.py"), "send", cmd, str(cap)],
        capture_output=True, text=True, cwd=ROOT,
    )
    out = r.stdout + r.stderr
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"### {cmd}\n{out}\n")
    print(out)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["probe", "arm"])
    ap.add_argument("--reset", choices=list(RESETS), default="dmu")
    ap.add_argument("--marker", action="store_true", help="E3: DDR marker at 0x01000000")
    a = ap.parse_args()
    if a.action == "probe":
        send(f"md.l {LUT} 2; md.l 0x1803f200 1; md.l 0x1803f020 1; md.l 0x19000f08 1")
        return
    send(f"mw.l {LUT} 0x1c000000")
    for i, w in enumerate(STUB):
        send(f"mw.l 0x{SRAM + 4 * i:x} 0x{w:08x}", 2)
    if a.marker:
        send("mw.l 0x01000000 0xdeadbeef")
    send(f"md.l 0x{SRAM:x} {len(STUB)}")
    send(f"mw.l {LUT} 0x{SRAM:x}; md.l {LUT} 1")
    send("; ".join(RESETS[a.reset]), 6)


if __name__ == "__main__":
    main()
