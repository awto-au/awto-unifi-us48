#!/usr/bin/env python3
"""Find code references (literal-pool ldr, movw/movt) to a VA or string in a section-less ARM ELF (ubntbox).

Usage: python3 scripts/poe_ubntbox_xref.py ELF --str "text" | --addr 0x166278
Logs to tmp/logs/poe_ubntbox_xref.log.
"""
import argparse
import logging
import pathlib
import struct
import sys

import capstone

LOG = pathlib.Path("tmp/logs/poe_ubntbox_xref.log")


def segs(d):
    phoff, = struct.unpack_from("<I", d, 0x1C)
    ent, num = struct.unpack_from("<HH", d, 0x2A)
    out = []
    for i in range(num):
        t, off, va, _, fsz, msz, fl, _ = struct.unpack_from("<8I", d, phoff + i * ent)
        if t == 1:
            out.append((va, off, fsz, fl))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("elf")
    ap.add_argument("--str")
    ap.add_argument("--addr", type=lambda x: int(x, 0))
    ap.add_argument("--thumb", action="store_true")
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.FileHandler(LOG), logging.StreamHandler(sys.stdout)])
    d = pathlib.Path(a.elf).read_bytes()
    sg = segs(d)
    targets = []
    if a.str:
        for va, off, fsz, fl in sg:
            i = d.find(b"\0" + a.str.encode(), off, off + fsz)
            while i >= 0:
                targets.append(va + i + 1 - off)
                i = d.find(b"\0" + a.str.encode(), i + 1, off + fsz)
    if a.addr is not None:
        targets.append(a.addr)
    logging.info("targets %s", [hex(t) for t in targets])
    from capstone import arm as A
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_ARM)
    md.detail = True
    md.skipdata = True
    tset = set(targets)

    def rd32(v):
        for v2, o2, f2, _ in sg:
            if v2 <= v < v2 + f2 - 3:
                return struct.unpack_from("<I", d, o2 + v - v2)[0]
        return None

    for va, off, fsz, fl in sg:
        if not fl & 1:
            continue
        regs = {}
        for ins in md.disasm(d[off:off + fsz], va):
            if ins.id == 0:
                continue
            mn, ops = ins.mnemonic, ins.operands
            val = dst = None
            try:
                if mn == "push" and any(o.reg == A.ARM_REG_LR for o in ops):
                    regs = {}
                elif mn == "ldr" and len(ops) == 2 and ops[1].type == A.ARM_OP_MEM and ops[1].mem.base == A.ARM_REG_PC:
                    val, dst = rd32(ins.address + 8 + ops[1].mem.disp), ops[0].reg
                elif mn == "movw" and ops[1].type == A.ARM_OP_IMM:
                    dst, val = ops[0].reg, ops[1].imm & 0xFFFF
                elif mn == "movt" and ops[0].reg in regs:
                    dst, val = ops[0].reg, (regs[ops[0].reg] & 0xFFFF) | ((ops[1].imm & 0xFFFF) << 16)
                elif mn in ("add", "sub") and len(ops) == 3 and ops[2].type == A.ARM_OP_IMM and ops[1].reg in regs:
                    b = regs[ops[1].reg]
                    val, dst = (b + ops[2].imm if mn == "add" else b - ops[2].imm) & 0xFFFFFFFF, ops[0].reg
                elif mn == "add" and len(ops) == 3 and ops[1].type == A.ARM_OP_REG and ops[1].reg == A.ARM_REG_PC \
                        and ops[2].type == A.ARM_OP_IMM:
                    dst, val = ops[0].reg, (ins.address + 8 + ops[2].imm) & 0xFFFFFFFF
                elif mn == "add" and len(ops) == 3 and ops[2].type == A.ARM_OP_REG and ops[2].reg in regs \
                        and (ops[1].reg in regs or ops[1].reg == A.ARM_REG_PC):
                    base = ins.address + 8 if ops[1].reg == A.ARM_REG_PC else regs[ops[1].reg]
                    dst, val = ops[0].reg, (base + regs[ops[2].reg]) & 0xFFFFFFFF
                elif ops and ops[0].type == A.ARM_OP_REG and not mn.startswith(("str", "cmp", "cmn", "tst", "teq", "push", "b")):
                    regs.pop(ops[0].reg, None)
                if mn.startswith("bl"):
                    for r in (A.ARM_REG_R0, A.ARM_REG_R1, A.ARM_REG_R2, A.ARM_REG_R3, A.ARM_REG_R12):
                        regs.pop(r, None)
            except capstone.CsError:
                continue
            if dst is not None and val is not None:
                regs[dst] = val
                if val in tset:
                    logging.info("ref %#x at %#x", val, ins.address)


if __name__ == "__main__":
    main()
