#!/usr/bin/env python3
"""ARM32 string/literal cross-reference helper for stock binaries (raw image or ELF).

Finds a string, the literal-pool words that point at it, and disassembles around each ref.

Usage:
  python3 scripts/arm_xref.py IMG --base 0xc0018000 --str pse_master_int
  python3 scripts/arm_xref.py IMG --base 0xc0018000 --addr 0xc02e4c32 --dis 0xc0020000 --len 0x200
  python3 scripts/arm_xref.py private/.../bin/switchdrvr --str "pd69" --refs
ELF input: segments are mapped by their vaddr (base ignored).
Needs capstone (any venv that has it). Logs to tmp/logs/arm_xref.log.
"""
import argparse
import logging
import pathlib
import re
import struct
import sys

import capstone

LOG = pathlib.Path("tmp/logs/arm_xref.log")


class Image:
    def __init__(self, path, base):
        self.data = pathlib.Path(path).read_bytes()
        self.segs = []  # (vaddr, file_off, size)
        if self.data[:4] == b"\x7fELF":
            from elftools.elf.elffile import ELFFile
            with open(path, "rb") as f:
                e = ELFFile(f)
                for s in e.iter_segments():
                    if s["p_type"] == "PT_LOAD":
                        self.segs.append((s["p_vaddr"], s["p_offset"], s["p_filesz"]))
        else:
            self.segs.append((base, 0, len(self.data)))

    def off2va(self, off):
        for va, fo, sz in self.segs:
            if fo <= off < fo + sz:
                return va + off - fo
        return None

    def va2off(self, va):
        for v, fo, sz in self.segs:
            if v <= va < v + sz:
                return fo + va - v
        return None

    def read(self, va, n):
        o = self.va2off(va)
        return self.data[o:o + n] if o is not None else b""

    def cstr(self, va):
        o = self.va2off(va)
        if o is None:
            return None
        e = self.data.find(b"\0", o, o + 256)
        return self.data[o:e].decode("latin1", "replace") if e > 0 else None


def find_strings(img, s):
    out = []
    for m in re.finditer(re.escape(s.encode()), img.data):
        va = img.off2va(m.start())
        if va is not None:
            out.append(va)
    return out


def find_words(img, value):
    pat = struct.pack("<I", value)
    res, i = [], img.data.find(pat)
    while i >= 0:
        if i % 4 == 0:
            va = img.off2va(i)
            if va is not None:
                res.append(va)
        i = img.data.find(pat, i + 1)
    return res


def disasm(img, va, length):
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_ARM)
    md.skipdata = True  # literal pools / data words
    lines = []
    for ins in md.disasm(img.read(va, length), va):
        note = ""
        m = re.match(r"\[pc, #(-?0x[0-9a-f]+|-?\d+)\]", ins.op_str.split(", ", 1)[-1])
        if ins.mnemonic.startswith("ldr") and m:
            lit = ins.address + 8 + int(m.group(1), 0)
            w = struct.unpack("<I", img.read(lit, 4) or b"\0\0\0\0")[0]
            s = img.cstr(w)
            note = f"  ; ={w:#x}" + (f' "{s}"' if s and len(s) >= 3 and s.isprintable() else "")
        lines.append(f"{ins.address:#010x}: {ins.bytes.hex():8s} {ins.mnemonic} {ins.op_str}{note}")
    return lines


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("image")
    ap.add_argument("--base", type=lambda x: int(x, 0), default=0)
    ap.add_argument("--str", action="append", default=[], help="string to locate")
    ap.add_argument("--addr", type=lambda x: int(x, 0), action="append", default=[], help="find literal refs to VA")
    ap.add_argument("--dis", type=lambda x: int(x, 0), help="disassemble from VA")
    ap.add_argument("--len", type=lambda x: int(x, 0), default=0x100)
    ap.add_argument("--ctx", type=lambda x: int(x, 0), default=0x80, help="bytes before/after each ref to show")
    ap.add_argument("--refs", action="store_true", help="show disassembly around each literal-pool ref")
    ap.add_argument("--words", type=lambda x: int(x, 0), help="dump 32-bit words from VA (strings annotated)")
    ap.add_argument("--count", type=lambda x: int(x, 0), default=64)
    a = ap.parse_args()

    LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.FileHandler(LOG), logging.StreamHandler(sys.stdout)])
    log = logging.info
    img = Image(a.image, a.base)
    targets = list(a.addr)
    for s in a.str:
        for va in find_strings(img, s):
            log(f"str {s!r} @ {va:#x}")
            targets.append(va)
    for t in targets:
        for r in find_words(img, t):
            log(f"  ref to {t:#x} from literal @ {r:#x}")
            if a.refs:
                for ln in disasm(img, (r - a.ctx) & ~3, a.ctx + 4):
                    log("    " + ln)
    if a.words is not None:
        for i in range(a.count):
            va = a.words + 4 * i
            w = struct.unpack("<I", img.read(va, 4) or b"\0\0\0\0")[0]
            s = img.cstr(w) if w > 0x1000 else None
            ann = f'  "{s}"' if s and len(s) >= 2 and s.isprintable() else ""
            log(f"{va:#010x}: {w:08x}{ann}")
    if a.dis is not None:
        for ln in disasm(img, a.dis, a.len):
            log(ln)


if __name__ == "__main__":
    main()
