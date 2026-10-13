#!/usr/bin/env python3
"""Recover the PoE message-id -> BCM59111 register map from stock switchdrvr.

Interprets the leaf function that translates (msg_id, port channel 1..4) into
(register, length) (switchdrvr 7.5.15: 0x612290; out-params r2 = u8 *reg, r3 = u32 *len).
Supports only the instructions that function uses (sub/cmp/mov/b<cond>/ldrls pc jump table/strb/str/bx).

Usage: python3 scripts/poe_msgmap.py private/.../bin/switchdrvr [--func 0x612290]
Logs to tmp/logs/poe_msgmap.log.
"""
import argparse
import logging
import pathlib
import struct
import sys

import capstone

LOG = pathlib.Path("tmp/logs/poe_msgmap.log")
TEXT_DELTA = 0x10000  # file offset = VA - 0x10000 for .text/.rodata in this build
COND = {"eq": lambda f: f == 0, "ne": lambda f: f != 0, "hi": lambda f: f > 0, "ls": lambda f: f <= 0,
        "lo": lambda f: f < 0, "hs": lambda f: f >= 0, "": lambda f: True}


def run(data, func, r0, r1, limit=200):
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_ARM)
    regs = {"r0": r0, "r1": r1, "r2": "REG", "r3": "LEN"}
    out = {}
    flag = 0  # sign of last cmp (unsigned compare)
    pc = func
    for _ in range(limit):
        o = pc - TEXT_DELTA
        ins = next(md.disasm(data[o:o + 4], pc), None)
        if ins is None:
            return None
        mn, ops = ins.mnemonic, [x.strip() for x in ins.op_str.split(",")]
        base, cc = mn, ""
        for c in COND:
            if c and mn.endswith(c) and mn[:-2] in ("b", "mov", "ldr", "str", "strb", "bx"):
                base, cc = mn[:-2], c
        val = lambda s: int(s[1:], 0) if s.startswith("#") else regs[s]
        nxt = pc + 4
        if not COND[cc](flag):
            pc = nxt
            continue
        if base == "sub":
            regs[ops[0]] = (val(ops[1]) - val(ops[2])) & 0xFFFFFFFF
        elif base == "cmp":
            a, b = val(ops[0]), val(ops[1])
            flag = (a > b) - (a < b)
        elif base in ("mov", "movw"):
            regs[ops[0]] = val(ops[1])
        elif base == "ldr" and ops[1] == "[pc" and "lsl #2]" in ins.op_str:
            idx = regs[ops[2]]
            nxt = struct.unpack_from("<I", data, pc + 8 + 4 * idx - TEXT_DELTA)[0]
        elif base == "b":
            nxt = int(ops[0][1:], 0)
        elif base in ("strb", "str"):
            out[regs[ops[1].strip("[]")]] = regs[ops[0]]
        elif base == "bx":
            return regs["r0"], out
        else:
            return None
        pc = nxt
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("elf")
    ap.add_argument("--func", type=lambda x: int(x, 0), default=0x612290)
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.FileHandler(LOG, mode="w"), logging.StreamHandler(sys.stdout)])
    data = pathlib.Path(a.elf).read_bytes()
    for msg in range(0x09, 0x1F):
        row = []
        for ch in range(1, 5):
            r = run(data, a.func, msg, ch)
            if r is None:
                row.append("?")
            elif r[0] != 0:
                row.append(f"err{r[0]}")
            else:
                row.append(f"reg {r[1].get('REG', 0):#04x} len {r[1].get('LEN', 0)}")
        logging.info(f"msg {msg:#04x}: " + " | ".join(row))


if __name__ == "__main__":
    main()
