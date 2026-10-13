#!/usr/bin/env python3
"""List call sites of ARM32 functions with the constant r0-r3 arguments set just before each call.

Call sites = direct BL plus `movw/movt rX, target ... blx rX` (needs the arm_xref_index.py index).
Only immediates set within --window bytes before the call are reported (best effort, no data flow).

Usage:
  python3 scripts/arm_callargs.py ELF --index tmp/re/switchdrvr.xref.pkl --addr 0x1095edc [--window 0x40]
Logs to tmp/logs/arm_callargs.log.
"""
import argparse
import bisect
import logging
import pathlib
import pickle
import struct
import sys

import capstone

LOG = pathlib.Path("tmp/logs/arm_callargs.log")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("elf")
    ap.add_argument("--index", required=True)
    ap.add_argument("--addr", type=lambda x: int(x, 0), action="append", required=True)
    ap.add_argument("--window", type=lambda x: int(x, 0), default=0x40)
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.FileHandler(LOG), logging.StreamHandler(sys.stdout)])
    from elftools.elf.elffile import ELFFile
    data = pathlib.Path(a.elf).read_bytes()
    with open(a.elf, "rb") as f:
        t = ELFFile(f).get_section_by_name(".text")
        t_va, t_off, t_sz = t["sh_addr"], t["sh_offset"], t["sh_size"]
    idx = pickle.load(open(a.index, "rb"))
    funcs = idx["funcs"]
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_ARM)
    md.skipdata = True

    def window(va, n):
        o = t_off + va - t_va
        return list(md.disasm(data[o:o + n], va))

    words = struct.unpack_from(f"<{t_sz // 4}I", data, t_off)
    for tgt in a.addr:
        sites = set()
        for i, w in enumerate(words):  # direct BL
            if (w & 0x0F000000) == 0x0B000000 and (w >> 28) != 0xF:
                imm = w & 0xFFFFFF
                imm = imm - 0x1000000 if imm & 0x800000 else imm
                pc = t_va + 4 * i
                if (pc + 8 + imm * 4) & 0xFFFFFFFF == tgt:
                    sites.add(pc)
        for r in idx["refs"].get(tgt, []):  # movw/movt materialised; find the following blx
            for ins in window(r, 0x40):
                if ins.mnemonic in ("blx", "bx") and ins.op_str.startswith("r"):
                    sites.add(ins.address)
                    break
        for s in sorted(sites):
            args = {}
            for ins in window(s - a.window, a.window):
                ops = ins.op_str.split(", ")
                if ins.mnemonic in ("mov", "movw") and ops[0] in ("r0", "r1", "r2", "r3") and ops[1].startswith("#"):
                    args[ops[0]] = ops[1]
                elif ops and ops[0] in ("r0", "r1", "r2", "r3"):
                    args.pop(ops[0], None)
            j = bisect.bisect_right(funcs, s) - 1
            logging.info(f"{tgt:#x} <- {s:#x} (func {funcs[j]:#x}) " +
                         " ".join(f"{k}={v}" for k, v in sorted(args.items())))


if __name__ == "__main__":
    main()
