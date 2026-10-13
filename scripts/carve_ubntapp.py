#!/usr/bin/env python3
"""Carve the embedded Ubiquiti app (ubnt app) out of the UniFi U-Boot image.

- U-Boot proper is linked at 0x1c000000 and relocates itself; the app is appended after
  __rel_dyn_end (link-time word at image offset 0x48) and U-Boot copies it from QSPI to 0x67030000
  at boot (function@0x1c01e33c reads 0x50000 B; header magic 87 56 45 23 checked there).
- Output: tmp/ghidra/in/ubntapp.bin (header at +0, entry/ubntaddr at +0x20); import at base 0x67030000.

Usage: python3 scripts/carve_ubntapp.py [U_BOOT_BIN]
Log: tmp/logs/carve_ubntapp.log
"""
import argparse
import logging
import pathlib
import struct
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAGIC = bytes.fromhex("87564523")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("uboot", nargs="?", default=str(ROOT / "private/firmware/unpacked/00-u-boot.bin"))
    ap.add_argument("--out", default=str(ROOT / "tmp/ghidra/in/ubntapp.bin"))
    a = ap.parse_args()
    log = ROOT / "tmp/logs/carve_ubntapp.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.FileHandler(log), logging.StreamHandler(sys.stdout)])
    d = pathlib.Path(a.uboot).read_bytes()
    start = struct.unpack_from("<I", d, 0x48)[0]
    if d[start:start + 4] != MAGIC:
        sys.exit(f"no app magic at {start:#x}")
    out = pathlib.Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(d[start:])
    logging.info("app offset %#x len %#x (%d B) hdr %s -> %s", start, len(d) - start, len(d) - start,
                 d[start:start + 8].hex(), out)


if __name__ == "__main__":
    main()
