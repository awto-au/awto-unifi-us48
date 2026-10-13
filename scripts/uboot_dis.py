#!/usr/bin/env python3
"""Annotated ARM disassembly for the position-independent UniFi U-Boot image.

U-Boot here is built -fpic: strings/data are reached as `ldr rX,[pc,#n]; add rX,pc,rX`
(literal = target - (add_addr + 8)), so plain literal scans find nothing. This tool resolves
those pairs, movw/movt pairs and pc-relative `ldr` literals, and prints the string at each target.

Usage:
  python3 scripts/uboot_dis.py IMG --base 0x1c000000 --xref-str "Hit any key"   # who uses a string
  python3 scripts/uboot_dis.py IMG --base 0x1c000000 --dis 0x1c0028d8 --len 0x100
  python3 scripts/uboot_dis.py IMG --base 0x1c000000 --xref-addr 0x1c048950      # refs to an address
  python3 scripts/uboot_dis.py IMG --base 0x1c000000 --callers 0x1c017d08        # BL/B callers
Needs capstone (system python3). Logs to tmp/logs/uboot_dis.log.
"""
import argparse
import logging
import pathlib
import re
import struct
import sys

import capstone

LOG = pathlib.Path("tmp/logs/uboot_dis.log")


class Img:
    def __init__(self, path, base):
        self.d = pathlib.Path(path).read_bytes()
        self.base = base

    def w(self, va):
        o = va - self.base
        if 0 <= o <= len(self.d) - 4:
            return struct.unpack_from("<I", self.d, o)[0]
        return None

    def cstr(self, va):
        o = va - self.base
        if not 0 <= o < len(self.d):
            return None
        e = self.d.find(b"\0", o, o + 160)
        if e <= o:
            return None
        s = self.d[o:e]
        if all(32 <= c < 127 or c in (9, 10) for c in s) and len(s) >= 2:
            return s.decode().replace("\n", "\\n")
        return None


def decode(img, start, length):
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_ARM)
    md.detail = False
    md.skipdata = True
    o = start - img.base
    return list(md.disasm(img.d[o:o + length], start))


def annotate(img, ins_list):
    """Yield (ins, note). Tracks ldr-literal values per register and resolves add rd,pc,rn."""
    regs = {}
    out = []
    for ins in ins_list:
        note = ""
        m = re.match(r"(r\d+|ip|lr|sp|fp), \[pc, #(-?0x[0-9a-f]+|-?\d+)\]$", ins.op_str)
        if ins.mnemonic.startswith("ldr") and m:
            lit = ins.address + 8 + int(m.group(2), 0)
            v = img.w(lit)
            if v is not None:
                regs[m.group(1)] = v
                note = f"; lit={v:#x}"
        m = re.match(r"(\w+), pc, (\w+)$", ins.op_str)
        if ins.mnemonic == "add" and m and m.group(2) in regs:
            t = (ins.address + 8 + regs[m.group(2)]) & 0xFFFFFFFF
            s = img.cstr(t)
            note = f"; ->{t:#x}" + (f' "{s}"' if s else "")
            regs[m.group(1)] = t
        m = re.match(r"(\w+), #(0x[0-9a-f]+|\d+)$", ins.op_str)
        if ins.mnemonic == "movw" and m:
            regs[m.group(1)] = int(m.group(2), 0)
        if ins.mnemonic == "movt" and m and m.group(1) in regs:
            regs[m.group(1)] = (regs[m.group(1)] & 0xFFFF) | (int(m.group(2), 0) << 16)
            note = f"; ={regs[m.group(1)]:#x}"
        out.append((ins, note))
    return out


def scan_all(img):
    ins = decode(img, img.base, len(img.d))
    return annotate(img, ins)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("image")
    ap.add_argument("--base", type=lambda x: int(x, 0), default=0x1C000000)
    ap.add_argument("--dis", type=lambda x: int(x, 0))
    ap.add_argument("--len", type=lambda x: int(x, 0), default=0x100)
    ap.add_argument("--xref-str")
    ap.add_argument("--xref-addr", type=lambda x: int(x, 0), action="append", default=[])
    ap.add_argument("--callers", type=lambda x: int(x, 0), action="append", default=[])
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.FileHandler(LOG), logging.StreamHandler(sys.stdout)])
    log = logging.info
    img = Img(a.image, a.base)
    if a.dis is not None:
        for ins, note in annotate(img, decode(img, a.dis, a.len)):
            log(f"{ins.address:#010x}: {ins.bytes.hex():8s} {ins.mnemonic} {ins.op_str}  {note}")
    if a.xref_str or a.xref_addr or a.callers:
        targets = set(a.xref_addr)
        if a.xref_str:
            for m in re.finditer(re.escape(a.xref_str.encode()), img.d):
                targets.add(a.base + m.start())
                log(f"string {a.xref_str!r} @ {a.base + m.start():#x}")
        allv = scan_all(img)
        for ins, note in allv:
            m = re.search(r"->(0x[0-9a-f]+)", note)
            if m and int(m.group(1), 16) in targets:
                log(f"ref {ins.address:#x}  {note}")
            m2 = re.search(r"=(0x[0-9a-f]+)", note)
            if m2 and int(m2.group(1), 16) in targets:
                log(f"ref {ins.address:#x}  {note}")
            if a.callers and ins.mnemonic in ("bl", "b", "blx") and ins.op_str.startswith("#"):
                if int(ins.op_str[1:], 0) in a.callers:
                    log(f"caller {ins.address:#x} {ins.mnemonic} {ins.op_str}")


if __name__ == "__main__":
    main()
