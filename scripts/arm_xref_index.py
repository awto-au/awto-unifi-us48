#!/usr/bin/env python3
"""Build a constant/address cross-reference index for a stripped ARM32 ELF (e.g. switchdrvr).

Linear-sweeps .text with capstone and tracks per-register constants from
literal loads, movw/movt and add/sub-immediate (section-anchor addressing),
then records every computed value that lands in a mapped segment.
Function starts come from .ARM.exidx.

Usage:
  python3 scripts/arm_xref_index.py build ELF -o tmp/re/switchdrvr.xref.pkl
  python3 scripts/arm_xref_index.py query tmp/re/switchdrvr.xref.pkl --addr 0x15a7a14 [--str ELF "text"]
Logs to tmp/logs/arm_xref_index.log.
"""
import argparse
import bisect
import logging
import pathlib
import pickle
import re
import struct
import sys

LOG = pathlib.Path("tmp/logs/arm_xref_index.log")


def load_elf(path):
    from elftools.elf.elffile import ELFFile
    f = open(path, "rb")
    e = ELFFile(f)
    data = pathlib.Path(path).read_bytes()
    segs = [(s["p_vaddr"], s["p_offset"], s["p_filesz"], s["p_memsz"]) for s in e.iter_segments()
            if s["p_type"] == "PT_LOAD"]
    secs = {s.name: (s["sh_addr"], s["sh_offset"], s["sh_size"]) for s in e.iter_sections()}
    return data, segs, secs


def build(path, out):
    import capstone
    from capstone import arm as A
    data, segs, secs = load_elf(path)
    lo = min(s[0] for s in segs)
    hi = max(s[0] + s[3] for s in segs)

    def rd32(va):
        for v, o, fs, _ in segs:
            if v <= va < v + fs - 3:
                return struct.unpack_from("<I", data, o + va - v)[0]
        return None

    # function starts from .ARM.exidx (prel31 pairs)
    funcs = []
    ex_va, ex_off, ex_sz = secs[".ARM.exidx"]
    for i in range(0, ex_sz, 8):
        w = struct.unpack_from("<I", data, ex_off + i)[0]
        off = w & 0x7FFFFFFF
        if off & 0x40000000:
            off -= 0x80000000
        funcs.append((ex_va + i + off) & 0xFFFFFFFF)
    funcs = sorted(set(funcs))

    t_va, t_off, t_sz = secs[".text"]
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_ARM)
    md.detail = True
    md.skipdata = True
    refs = {}  # value -> list of insn addrs
    regs = {}
    fi = 0
    n = 0
    code = data[t_off:t_off + t_sz]
    for ins in md.disasm(code, t_va):
        n += 1
        if n % 1000000 == 0:
            logging.info(f"{n} insns, at {ins.address:#x}")
        while fi < len(funcs) and funcs[fi] <= ins.address:
            regs = {}
            fi += 1
        if ins.id == 0:  # skipped data
            continue
        mn = ins.mnemonic
        ops = ins.operands
        val = None
        dst = None
        try:
            if mn.startswith("ldr") and mn in ("ldr",) and len(ops) == 2 and ops[1].type == A.ARM_OP_MEM \
                    and ops[1].mem.base == A.ARM_REG_PC and ops[1].mem.index == 0:
                lit = ins.address + 8 + ops[1].mem.disp
                val = rd32(lit)
                dst = ops[0].reg
            elif mn == "movw" and ops[1].type == A.ARM_OP_IMM:
                dst, val = ops[0].reg, ops[1].imm & 0xFFFF
            elif mn == "movt" and ops[1].type == A.ARM_OP_IMM and ops[0].reg in regs:
                dst, val = ops[0].reg, (regs[ops[0].reg] & 0xFFFF) | ((ops[1].imm & 0xFFFF) << 16)
            elif mn in ("add", "sub") and len(ops) == 3 and ops[2].type == A.ARM_OP_IMM \
                    and ops[1].type == A.ARM_OP_REG and ops[1].reg in regs:
                b = regs[ops[1].reg]
                val = (b + ops[2].imm if mn == "add" else b - ops[2].imm) & 0xFFFFFFFF
                dst = ops[0].reg
            elif mn == "add" and len(ops) == 3 and ops[1].type == A.ARM_OP_REG and ops[1].reg == A.ARM_REG_PC \
                    and ops[2].type == A.ARM_OP_IMM:
                dst, val = ops[0].reg, (ins.address + 8 + ops[2].imm) & 0xFFFFFFFF
            elif ops and ops[0].type == A.ARM_OP_REG and not mn.startswith(("str", "cmp", "cmn", "tst", "teq", "push", "b", "bl")):
                regs.pop(ops[0].reg, None)
            if mn.startswith(("bl", "blx")):
                for r in (A.ARM_REG_R0, A.ARM_REG_R1, A.ARM_REG_R2, A.ARM_REG_R3, A.ARM_REG_R12):
                    regs.pop(r, None)
        except capstone.CsError:
            continue
        if dst is not None and val is not None:
            regs[dst] = val
            if lo <= val < hi:
                refs.setdefault(val, []).append(ins.address)
        # memory operands with known base register: [rX, #imm]
        for op in ops:
            if op.type == A.ARM_OP_MEM and op.mem.base in regs and op.mem.base != A.ARM_REG_PC and op.mem.index == 0:
                v = (regs[op.mem.base] + op.mem.disp) & 0xFFFFFFFF
                if lo <= v < hi:
                    refs.setdefault(v, []).append(ins.address)
    logging.info(f"done: {n} insns, {len(refs)} referenced values, {len(funcs)} funcs")
    with open(out, "wb") as f:
        pickle.dump({"refs": refs, "funcs": funcs}, f)


def callers(elf, targets, funcs_idx):
    """Raw scan of .text for ARM BL/B (any cond) to targets; no disassembly needed."""
    data, segs, secs = load_elf(elf)
    t_va, t_off, t_sz = secs[".text"]
    funcs = pickle.load(open(funcs_idx, "rb"))["funcs"] if funcs_idx else []
    tset = set(targets)
    words = struct.unpack_from(f"<{t_sz // 4}I", data, t_off)
    for i, w in enumerate(words):
        if (w & 0x0E000000) != 0x0A000000 or (w >> 28) == 0xF:
            continue
        imm = w & 0xFFFFFF
        if imm & 0x800000:
            imm -= 0x1000000
        pc = t_va + 4 * i
        tgt = (pc + 8 + imm * 4) & 0xFFFFFFFF
        if tgt in tset:
            j = bisect.bisect_right(funcs, pc) - 1
            fn = funcs[j] if j >= 0 else 0
            kind = "bl" if w & 0x01000000 else "b"
            logging.info(f"  {tgt:#x} <- {kind} @ {pc:#x} (func {fn:#x})")


def query(idx, addrs, elf, strs):
    d = pickle.load(open(idx, "rb"))
    refs, funcs = d["refs"], d["funcs"]
    if strs:
        data, segs, _ = load_elf(elf)
        for s in strs:
            for m in re.finditer(re.escape(s.encode()), data):
                for v, o, fs, _ in segs:
                    if o <= m.start() < o + fs:
                        # walk back to the start of the containing C string
                        st = m.start()
                        while st > o and data[st - 1] != 0:
                            st -= 1
                        logging.info(f"str {s!r} @ {v + m.start() - o:#x} (cstring start {v + st - o:#x})")
                        addrs += [v + m.start() - o, v + st - o]
    for a in dict.fromkeys(addrs):
        for r in refs.get(a, []):
            i = bisect.bisect_right(funcs, r) - 1
            fn = funcs[i] if i >= 0 else 0
            logging.info(f"  {a:#x} <- {r:#x} (func {fn:#x})")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("elf")
    b.add_argument("-o", "--out", required=True)
    q = sub.add_parser("query")
    q.add_argument("index")
    q.add_argument("--addr", type=lambda x: int(x, 0), action="append", default=[])
    q.add_argument("--elf")
    q.add_argument("--str", action="append", default=[])
    c = sub.add_parser("calls", help="find direct BL/B callers of addresses")
    c.add_argument("elf")
    c.add_argument("--index", help="xref index (for function starts)")
    c.add_argument("--addr", type=lambda x: int(x, 0), action="append", default=[])
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.FileHandler(LOG), logging.StreamHandler(sys.stdout)])
    if a.cmd == "build":
        build(a.elf, a.out)
    elif a.cmd == "calls":
        callers(a.elf, a.addr, a.index)
    else:
        query(a.index, a.addr, a.elf, a.str)


if __name__ == "__main__":
    main()
