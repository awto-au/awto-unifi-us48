#!/usr/bin/env python3
"""Dump the SDK soc_reg_info entries for the SMBus register blocks from stripped switchdrvr.

soc_reg_info_t layout (ELF32 LE): +0 block ptr, +4 regtype, +8 numels, +0xc offset, +0x10 flags,
+0x14 nFields, +0x18 fields ptr (12-byte records: field id, len|bp<<16, flags).
Reg-name table VA 0x01f1098c, field-name table VA 0x0200ad70 (switchdrvr 7.5.15).
The per-chip pointer array (index = soc_reg_t) is located by scoring: every reg whose name ends in
BIT_BANG_CONTROL must map to an info with an SMBCLK_OUT_EN field.

Usage: python3 scripts/smbus_regdb.py BIN [--prefix CMIC_I2CM_SMBUS_ --prefix CHIPCOMMONB_SMBUS]
Needs pyelftools. Logs to tmp/logs/smbus_regdb.log.
"""
import argparse
import array
import logging
import pathlib
import struct

from elftools.elf.elffile import ELFFile

NAMES, FNAMES, NREGS = 0x01F1098C, 0x0200AD70, 0xCF16
REGTYPES = {1: "gen", 2: "port", 4: "cos", 9: "cpureg(CMIC BAR)", 0xD: "mcsreg", 0xE: "iprocreg(AXI)"}
LOG = pathlib.Path("tmp/logs/smbus_regdb.log")


class Elf:
    def __init__(self, path):
        self.d = pathlib.Path(path).read_bytes()
        self.segs = [(s["p_vaddr"], s["p_offset"], s["p_filesz"])
                     for s in ELFFile(open(path, "rb")).iter_segments() if s["p_type"] == "PT_LOAD"]

    def off(self, va):
        for v, o, n in self.segs:
            if v <= va < v + n:
                return o + va - v
        return None

    def u32(self, va):
        o = self.off(va)
        return None if o is None else struct.unpack_from("<I", self.d, o)[0]

    def cstr(self, va):
        o = self.off(va)
        return None if o is None else self.d[o:self.d.index(b"\0", o)].decode("latin1")


def fields(e, info):
    fp, n = e.u32(info + 0x18), e.u32(info + 0x14)
    return [e.cstr(e.u32(FNAMES + 4 * e.u32(fp + 12 * j))) for j in range(n)]


def find_base(e, name_of):
    bb = [r for r in range(NREGS) if (name_of(r) or "").endswith("BIT_BANG_CONTROL")]
    w = array.array("I")
    w.frombytes(e.d[: len(e.d) // 4 * 4])
    best = (0, None)
    for v, o, n in e.segs:
        for k in range(o // 4, (o + n) // 4):
            slot = v + (k * 4 - o)
            info = w[k]
            if not (0x1d51000 <= info < 0x22c9db8) or e.off(info + 0x1c) is None:
                continue
            nf = e.u32(info + 0x14)
            if not 0 < nf < 40 or e.off(e.u32(info + 0x18) or 0) is None:
                continue
            try:
                if "SMBCLK_OUT_EN" not in fields(e, info):
                    continue
            except (TypeError, ValueError):
                continue
            for r in bb:
                base = slot - 4 * r
                score = 0
                for r2 in bb:
                    i2 = e.u32(base + 4 * r2)
                    try:
                        score += bool(i2) and "SMBCLK_OUT_EN" in fields(e, i2)
                    except (TypeError, ValueError):
                        pass
                if score > best[0]:
                    best = (score, base)
    return best


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("bin")
    ap.add_argument("--prefix", action="append", default=None)
    a = ap.parse_args()
    prefixes = a.prefix or ["CMIC_I2CM_SMBUS_", "CHIPCOMMONB_SMBUS_", "CHIPCOMMONB_SMBUS1_SMBUS_"]
    LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.FileHandler(LOG, "w"), logging.StreamHandler()])
    e = Elf(a.bin)
    name_of = lambda r: e.cstr(e.u32(NAMES + 4 * r))
    score, base = find_base(e, name_of)
    logging.info("reg_info pointer array base %#x (score %d)", base, score)
    for r in range(NREGS):
        n = name_of(r) or ""
        if not any(n.startswith(p) for p in prefixes):
            continue
        i = e.u32(base + 4 * r)
        if not i:
            continue
        logging.info("%#06x %-48s info=%#x regtype=%s offset=%#x flags=%#x fields=%s", r, n, i,
                     REGTYPES.get(e.u32(i + 4), e.u32(i + 4)), e.u32(i + 0xC), e.u32(i + 0x10), ",".join(fields(e, i)))


if __name__ == "__main__":
    main()
