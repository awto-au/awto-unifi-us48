#!/usr/bin/env python3
"""Infer Keil C51 register-ABI signatures of the BCM59111 image functions (docs/poe-fw.md section 13, #26).

Usage: poe_fw_abi.py [IMAGE] [--out TSV]
  Default IMAGE hwdb/firmware/bcm59111-v21.bin, TSV hwdb/firmware/bcm59111-v21.abi.tsv.
  Keil ABI (awto-sdcc docs/keil-abi.md): args arg1 R7 / R6:R7 / R4..R7, arg2 R5 / R4:R5, arg3 R3 / R2:R3 (generic pointer R3:R2:R1),
  returns R7 / R6:R7 / R4..R7. Arguments = registers read before written along the straight-line entry prefix; return = the
  register write run before the first RET. Heuristic (shared-tail functions are entered mid-body): confidence I.
  Output rows: addr, ret (void|byte|word|long), args (comma list of Rn or Rm:Rn). Applied by scripts/ghidra/ApplyKeilAbi.java.
  Log: tmp/logs/poe_fw_abi.log.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402
import poe_fw_dis as D  # noqa: E402
import poe_fw_re as R  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def regs_rw(i):
    """(reads, writes) of R1..R7 (bank-0 numbering) for one instruction; only the forms the compiler emits."""
    op, raw = i.raw[0], i.raw
    rd, wr = set(), set()
    n = op & 7
    hi = op & 0xF8
    if op & 0xF8 == 0xF8:        # MOV Rn,A
        wr.add(n)
    elif op & 0xF8 == 0xE8:      # MOV A,Rn
        rd.add(n)
    elif op & 0xF8 == 0x78:      # MOV Rn,#imm
        wr.add(n)
    elif op & 0xF8 == 0xA8:      # MOV Rn,direct
        wr.add(n)
        if raw[1] in range(0, 8):
            rd.add(raw[1])
    elif op & 0xF8 == 0x88:      # MOV direct,Rn
        rd.add(n)
        if raw[1] in range(0, 8):
            wr.add(raw[1])
    elif hi in (0x08, 0x18, 0xD8):  # INC/DEC Rn, DJNZ
        rd.add(n)
        wr.add(n)
    elif hi in (0x28, 0x38, 0x48, 0x58, 0x68, 0x98):  # ADD/ADDC/ORL/ANL/XRL/SUBB A,Rn
        rd.add(n)
    elif hi in (0xC8,):          # XCH A,Rn
        rd.add(n)
        wr.add(n)
    elif hi == 0xB8:             # CJNE Rn,#imm,rel
        rd.add(n)
    elif op in (0x85,):
        for d in (raw[1], raw[2]):
            if d < 8:
                rd.add(d)
    elif op in (0xE5, 0xA2) or op in (0xC5, 0x25, 0x35, 0x45, 0x55, 0x65, 0x95, 0xB5):
        if raw[1] < 8:
            rd.add(raw[1])
    elif op in (0x75,):
        if raw[1] < 8:
            wr.add(raw[1])
    elif op in (0xF5, 0x05, 0x15, 0xD5):
        if raw[1] < 8:
            wr.add(raw[1])
    rd.discard(0)
    wr.discard(0)
    return rd, wr


def main() -> int:
    help_guard(__doc__, strict=True, known=("--out",))
    a = sys.argv[1:]
    img = ROOT / "hwdb/firmware/bcm59111-v21.bin"
    out = ROOT / "hwdb/firmware/bcm59111-v21.abi.tsv"
    if "--out" in a:
        out = Path(a[a.index("--out") + 1])
        a = [x for x in a if x != str(out) and x != "--out"]
    if a:
        img = Path(a[0])
    code = bytes(D.BASE) + img.read_bytes()
    insns, L, ents, host, states, tables = R.analyse(code)
    rows = []
    for e in ents:
        if e >= 0x5220 or e < D.BASE + 0x60:
            continue
        # args: straight-line prefix to first call / jump / ret
        written, args, pc, n = set(), set(), e, 0
        while pc in insns and n < 40:
            i = insns[pc]
            if i.flow in ("call", "jmp", "ret", "jmpa"):
                if i.flow == "call" and i.target in (0x4425, 0x43FF):  # ICALL / CCASE consume R1,R2 / A
                    pass
                break
            rd, wr = regs_rw(i)
            args |= {r for r in rd if r not in written}
            written |= wr
            pc += i.n
            n += 1
        # return: writes among the last 6 insns before the first RET reachable linearly
        ret, pc, n, trail = set(), e, 0, []
        while pc in insns and n < 400:
            i = insns[pc]
            trail.append(i)
            if i.flow == "ret":
                for t in trail[-6:-1]:
                    ret |= regs_rw(t)[1]
                break
            if i.flow in ("jmp", "jmpa"):
                break
            pc += i.n
            n += 1
        a_sig = []
        if 7 in args:
            a_sig.append("R6:R7" if 6 in args else "R7")
        elif 6 in args:
            a_sig.append("R6")
        if 5 in args:
            a_sig.append("R4:R5" if 4 in args else "R5")
        if 3 in args:
            a_sig.append("R2:R3" if 2 in args else "R3")
        if 4 in args and 7 in args and 6 in args and 5 in args:
            a_sig = ["R4:R5:R6:R7"]
        r_sig = "void"
        if 7 in ret:
            r_sig = "word" if 6 in ret else "byte"
        if {4, 5, 6, 7} <= ret:
            r_sig = "long"
        if r_sig == "void" and not a_sig:
            continue
        rows.append(f"{e:04X}\t{r_sig}\t{','.join(a_sig)}")
    out.write_text("# addr\tret\targs: Keil register ABI inferred by scripts/poe_fw_abi.py (confidence I)\n" + "\n".join(rows) + "\n")
    msg = f"{img.name}: {len(rows)} signatures -> {out}"
    print(msg)
    log = ROOT / "tmp/logs/poe_fw_abi.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as f:
        f.write(msg + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
