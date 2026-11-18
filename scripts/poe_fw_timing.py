#!/usr/bin/env python3
"""Cycle count of the host-I2C slave ISR paths (v21 0x3F10) with the awto-ddc DW8051 timing model (docs/poe-fw.md section 6).

Usage: poe_fw_timing.py
  Model: /mnt/2tb/git/awto-ddc/scripts/dw8051_model.json (DW8051 databook v3.2a table 2-4 cycles per opcode, 4 clk per cycle;
  MOVX = 2 + CKCON[2:0], CKCON never written by this firmware so reset value 1 -> 3 cycles). Override: $AWTO_DDC_REPO.
  Paths are traced from the decoded listing with fixed branch decisions per path (no simulation of data).
  Output: cycles and CPU clocks per path; time = clocks / f_cpu (clock not known: it is set by the ROM / hardware).
  Log: tmp/logs/poe_fw_timing.log.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402
import poe_fw_dis as D  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DDC = Path(os.environ.get("AWTO_DDC_REPO", "/mnt/2tb/git/awto-ddc"))
CKCON = 1


def trace(code, insns, start, taken, icall_target, model):
    """Follow from start until the outermost RET. taken = set of conditional-branch pcs that are taken."""
    cyc, pc, stack, n = 0, start, [], 0
    while n < 400:
        n += 1
        i = insns[pc]
        op = i.raw[0]
        cyc += model["cycles"][op] + (CKCON if op in (0xE0, 0xF0, 0xE2, 0xE3, 0xF2, 0xF3) else 0)
        if i.flow == "ret":
            if not stack:
                return cyc, n
            pc = stack.pop()
        elif i.flow == "call":
            stack.append(pc + i.n)
            pc = i.target
        elif i.flow == "jmpa":
            pc = icall_target
        elif i.flow == "jmp":
            pc = i.target
        elif i.flow == "cond":
            pc = i.target if pc in taken else pc + i.n
        else:
            pc += i.n
    raise SystemExit("trace too long")


def main() -> int:
    help_guard(__doc__, strict=True, known=())
    model = json.load((DDC / "scripts/dw8051_model.json").open())
    code = bytes(D.BASE) + (ROOT / "hwdb/firmware/bcm59111-v21.bin").read_bytes()
    insns = {}
    pc = 0x3F10
    for a, b in ((0x3F10, 0x3FBE), (0x47AB, 0x47BC), (0x43E7, 0x43F3), (0x43F3, 0x43FF), (0x4425, 0x442B),
                 (0x50FC, 0x5105), (0x5223, 0x5224), (0x5260, 0x5261), (0x4F57, 0x4F6F)):
        pc = a
        while pc < b:
            i = D.decode(code, pc)
            insns[pc] = i
            pc += i.n
    rd_taken = {0x3F27}
    wr_taken = {0x3F2D, 0x3F5A}  # 0x3F2D: not A8/B8; 0x3F5A: JZ phase flag = 0 -> dispatch write
    ptr_taken = set()            # first byte after SLA+W: pointer phase (0x30 != 0): fallthrough 3F5C..3F62, SJMP 3F6A
    paths = [("SLA+R / read byte (0xA8/0xB8), stub read handler", 0x3F10, rd_taken, 0x5223),
             ("write data byte, default write handler (no-op)", 0x3F10, wr_taken, 0x5260),
             ("write data byte, reg 0x13 handler 0x50FC", 0x3F10, wr_taken, 0x50FC),
             ("register-pointer byte (0x80, phase flag set)", 0x3F10, {0x3F2D}, 0x5260)]
    lines = [f"DW8051 model: {model['source']}; clk/cycle {model['clk_per_cycle']}, MOVX = 2 + CKCON({CKCON}) cycles"]
    for name, start, tk, ic in paths:
        c, n = trace(code, insns, start, tk, ic, model)
        lines.append(f"{name}: {n} insns, {c} cycles, {c * model['clk_per_cycle']} CPU clk "
                     f"(+ {model['irq_cycles']} cycles interrupt entry)")
    out = "\n".join(lines)
    print(out)
    log = ROOT / "tmp/logs/poe_fw_timing.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(out + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
