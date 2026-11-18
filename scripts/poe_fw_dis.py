#!/usr/bin/env python3
"""8051 disassembler for the BCM59111 PSE firmware images (docs/poe-fw.md).

Usage: poe_fw_dis.py IMAGE [--out PATH] [--seed ADDR ...] [--descent]
  IMAGE: hwdb/firmware/bcm59111-v21.bin or bcm59111-meraki.bin (code RAM base 0x1A00, see BASE).
  Addresses are CPU code addresses (file offset + 0x1A00); calls below 0x1A00 go to on-chip ROM.
  Default: linear sweep BASE+0x60..last non-pad byte (99 % of relative branches land on a sweep boundary, so it
  stays in sync); LCALL/ACALL targets get func_ labels, other targets L_ labels. Trailing pad/strings as db.
  --descent = recursive descent from BASE+0x60 + patch-table entries instead (covers ~35 %: most code is reached from ROM).
  Default --out tmp/poe-fw/<image-stem>.asm. Logs to tmp/logs/poe_fw_dis.log.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BASE = 0x1A00  # image load address: MOVC table refs 0x4C23/0x4C63 hit file offsets 0x3223/0x3263 in v21/meraki

SFR = {
    0x80: "P0", 0x81: "SP", 0x82: "DPL", 0x83: "DPH", 0x87: "PCON", 0x88: "TCON", 0x89: "TMOD",
    0x8A: "TL0", 0x8B: "TL1", 0x8C: "TH0", 0x8D: "TH1", 0x90: "P1", 0x98: "SCON", 0x99: "SBUF",
    0xA0: "P2", 0xA8: "IE", 0xB0: "P3", 0xB8: "IP", 0xD0: "PSW", 0xE0: "ACC", 0xF0: "B",
}
BITNAMES = {0xAF: "EA", 0xD7: "CY", 0xD6: "AC", 0xD2: "OV", 0xD0: "P"}


def dname(d: int) -> str:
    return SFR.get(d, f"0x{d:02X}")


def bname(b: int) -> str:
    if b in BITNAMES:
        return BITNAMES[b]
    if b < 0x80:
        return f"0x{0x20 + (b >> 3):02X}.{b & 7}"
    base = b & 0xF8
    return f"{SFR.get(base, '0x%02X' % base)}.{b & 7}"


# op -> (length, mnemonic format). Fields: {d}=direct, {i}=imm, {b}=bit, {r}=rel, {a}=addr16, {s}=second direct
def build_table():
    t = {}
    arith = {0x20: "ADD", 0x30: "ADDC", 0x40: "ORL", 0x50: "ANL", 0x60: "XRL", 0x90: "SUBB"}
    for hi, nm in arith.items():
        t[hi | 4] = (2, f"{nm} A,#{{i}}")
        t[hi | 5] = (2, f"{nm} A,{{d}}")
        for k in (6, 7):
            t[hi | k] = (1, f"{nm} A,@R{k - 6}")
        for n in range(8):
            t[hi | 8 | n] = (1, f"{nm} A,R{n}")
    for n in range(8):
        t[0x08 | n] = (1, f"INC R{n}")
        t[0x18 | n] = (1, f"DEC R{n}")
        t[0x78 | n] = (2, f"MOV R{n},#{{i}}")
        t[0x88 | n] = (2, f"MOV {{d}},R{n}")
        t[0xA8 | n] = (2, f"MOV R{n},{{d}}")
        t[0xB8 | n] = (3, f"CJNE R{n},#{{i}},{{r}}")
        t[0xC8 | n] = (1, f"XCH A,R{n}")
        t[0xD8 | n] = (2, f"DJNZ R{n},{{r}}")
        t[0xE8 | n] = (1, f"MOV A,R{n}")
        t[0xF8 | n] = (1, f"MOV R{n},A")
    for k in (0, 1):
        t[0x06 | k] = (1, f"INC @R{k}")
        t[0x16 | k] = (1, f"DEC @R{k}")
        t[0x76 | k] = (2, f"MOV @R{k},#{{i}}")
        t[0x86 | k] = (2, f"MOV {{d}},@R{k}")
        t[0xA6 | k] = (2, f"MOV @R{k},{{d}}")
        t[0xB6 | k] = (3, f"CJNE @R{k},#{{i}},{{r}}")
        t[0xC6 | k] = (1, f"XCH A,@R{k}")
        t[0xD6 | k] = (1, f"XCHD A,@R{k}")
        t[0xE6 | k] = (1, f"MOV A,@R{k}")
        t[0xF6 | k] = (1, f"MOV @R{k},A")
        t[0xE2 | k] = (1, f"MOVX A,@R{k}")
        t[0xF2 | k] = (1, f"MOVX @R{k},A")
    t.update({
        0x00: (1, "NOP"), 0x02: (3, "LJMP {a}"), 0x12: (3, "LCALL {a}"), 0x22: (1, "RET"), 0x32: (1, "RETI"),
        0x03: (1, "RR A"), 0x13: (1, "RRC A"), 0x23: (1, "RL A"), 0x33: (1, "RLC A"),
        0x04: (1, "INC A"), 0x14: (1, "DEC A"), 0x05: (2, "INC {d}"), 0x15: (2, "DEC {d}"),
        0x10: (3, "JBC {b},{r}"), 0x20: (3, "JB {b},{r}"), 0x30: (3, "JNB {b},{r}"),
        0x40: (2, "JC {r}"), 0x50: (2, "JNC {r}"), 0x60: (2, "JZ {r}"), 0x70: (2, "JNZ {r}"), 0x80: (2, "SJMP {r}"),
        0x42: (2, "ORL {d},A"), 0x43: (3, "ORL {d},#{i}"), 0x52: (2, "ANL {d},A"), 0x53: (3, "ANL {d},#{i}"),
        0x62: (2, "XRL {d},A"), 0x63: (3, "XRL {d},#{i}"),
        0x72: (2, "ORL C,{b}"), 0x82: (2, "ANL C,{b}"), 0x92: (2, "MOV {b},C"), 0xA2: (2, "MOV C,{b}"),
        0xB2: (2, "CPL {b}"), 0xC2: (2, "CLR {b}"), 0xD2: (2, "SETB {b}"),
        0x73: (1, "JMP @A+DPTR"), 0x83: (1, "MOVC A,@A+PC"), 0x93: (1, "MOVC A,@A+DPTR"), 0xA3: (1, "INC DPTR"),
        0xB3: (1, "CPL C"), 0xC3: (1, "CLR C"), 0xD3: (1, "SETB C"),
        0x74: (2, "MOV A,#{i}"), 0x75: (3, "MOV {d},#{i}"), 0x85: (3, "MOV {s},{d}"), 0x84: (1, "DIV AB"),
        0xA4: (1, "MUL AB"), 0xA5: (1, "DB 0xA5"), 0x90: (3, "MOV DPTR,#{a}"), 0xA0: (2, "ORL C,/{b}"), 0xB0: (2, "ANL C,/{b}"),
        0xB4: (3, "CJNE A,#{i},{r}"), 0xB5: (3, "CJNE A,{d},{r}"), 0xC0: (2, "PUSH {d}"), 0xD0: (2, "POP {d}"),
        0xC4: (1, "SWAP A"), 0xD4: (1, "DA A"), 0xE4: (1, "CLR A"), 0xF4: (1, "CPL A"),
        0xC5: (2, "XCH A,{d}"), 0xD5: (3, "DJNZ {d},{r}"), 0xE5: (2, "MOV A,{d}"), 0xF5: (2, "MOV {d},A"),
        0xE0: (1, "MOVX A,@DPTR"), 0xF0: (1, "MOVX @DPTR,A"),
    })
    for hi in range(8):
        t[(hi << 5) | 0x01] = (2, "AJMP {a}")
        t[(hi << 5) | 0x11] = (2, "ACALL {a}")
    return t


TABLE = build_table()
assert len(TABLE) == 256, len(TABLE)


class Insn:
    __slots__ = ("pc", "n", "text", "flow", "target", "raw")


def decode(code: bytes, pc: int):
    """Return Insn; flow in {'seq','jmp','call','cond','ret','jmpa'}, target = branch target or None."""
    op = code[pc]
    n, fmt = TABLE[op]
    ins = Insn()
    ins.pc, ins.n = pc, n
    raw = code[pc:pc + n]
    ins.raw = raw
    if len(raw) < n:
        ins.n, ins.text, ins.flow, ins.target = 1, f"DB 0x{op:02X}", "seq", None
        ins.raw = raw[:1]
        return ins
    tgt = None
    kw = {}
    if "{a}" in fmt:
        if op & 0x0F == 0x01:
            tgt = ((pc + 2) & 0xF800) | ((op >> 5) << 8) | raw[1]
        else:
            tgt = (raw[1] << 8) | raw[2]
        kw["a"] = f"0x{tgt:04X}"
    if "{r}" in fmt:
        rel = raw[-1] - 256 if raw[-1] >= 128 else raw[-1]
        tgt = (pc + n + rel) & 0xFFFF
        kw["r"] = f"0x{tgt:04X}"
    if "{d}" in fmt or "{s}" in fmt:
        if op == 0x85:
            kw["s"], kw["d"] = dname(raw[2]), dname(raw[1])  # encoding: op src dst; text: dst,src
        else:
            kw["d"] = dname(raw[1])
    if "{i}" in fmt:
        kw["i"] = f"0x{raw[-1] if '{r}' not in fmt else raw[-2]:02X}"
        if op == 0x90:
            kw["a"] = f"0x{(raw[1] << 8) | raw[2]:04X}"
    if "{b}" in fmt:
        kw["b"] = bname(raw[1])
    if op == 0x90:  # DPTR immediate is data, not a branch target
        tgt = None
        kw["a"] = f"0x{(raw[1] << 8) | raw[2]:04X}"
    ins.text = fmt.format(**kw)
    ins.target = tgt
    if op in (0x02, 0x80) or (op & 0x1F) == 0x01:
        ins.flow = "jmp"
    elif op in (0x12,) or (op & 0x1F) == 0x11:
        ins.flow = "call"
    elif op in (0x22, 0x32):
        ins.flow = "ret"
    elif op == 0x73:
        ins.flow = "jmpa"
    elif tgt is not None:
        ins.flow = "cond"
    else:
        ins.flow = "seq"
    return ins


def descend(code: bytes, seeds):
    """Recursive descent over the code. Returns (insns{pc:Insn}, entry_set)."""
    size = len(code)
    insns, entries = {}, set()
    work = list(seeds)
    entries.update(seeds)
    while work:
        pc = work.pop()
        while 0 <= pc < size and pc not in insns:
            i = decode(code, pc)
            if any(p in insns for p in range(pc + 1, pc + i.n)):
                break
            insns[pc] = i
            if i.flow == "call" and i.target is not None and BASE <= i.target < size:
                if i.target not in entries:
                    entries.add(i.target)
                    work.append(i.target)
            if i.flow in ("jmp", "cond") and i.target is not None and BASE <= i.target < size:
                work.append(i.target)
            if i.flow in ("jmp", "ret", "jmpa"):
                break
            pc += i.n
    return insns, entries


def listing(code: bytes, insns, entries, labels=None):
    lines = []
    size = len(code)
    pc = BASE
    targets = {i.target for i in insns.values() if i.target is not None and BASE <= i.target < size}
    while pc < size:
        if pc in insns:
            i = insns[pc]
            if pc in entries:
                lines.append(f"\n; ---- func_{pc:04X} ----")
            elif pc in targets:
                lines.append(f"L_{pc:04X}:")
            lines.append(f"{pc:04X}:  {i.raw.hex(' '):<9}  {i.text}")
            pc += i.n
        else:
            e = pc
            while e < size and e not in insns:
                e += 1
            run = code[pc:e]
            if len(set(run)) == 1 and len(run) > 16:
                lines.append(f"{pc:04X}:  ; {len(run)} x 0x{run[0]:02X}")
            else:
                for k in range(0, len(run), 16):
                    chunk = run[k:k + 16]
                    asc = "".join(chr(c) if 32 <= c < 127 else "." for c in chunk)
                    lines.append(f"{pc + k:04X}:  db {chunk.hex(' ')}  ; {asc}")
            pc = e
    return "\n".join(lines) + "\n"


def sweep(code: bytes):
    """Linear sweep from 0x60 to the end of code (last byte that is not 0x00/0xFF pad, string region excluded)."""
    end = len(code)
    while end > BASE + 0x60 and code[end - 1] in (0x00, 0xFF):
        end -= 1
    s = code.find(b"POETEC_IMAGE")
    if s > 0:
        end = s
        while end > BASE + 0x60 and code[end - 1] in (0x00, 0xFF):
            end -= 1
    insns, entries = {}, {BASE + 0x60}
    pc = BASE + 0x60
    while pc < end:
        i = decode(code, pc)
        insns[pc] = i
        if i.flow == "call" and i.target is not None and BASE <= i.target < len(code):
            entries.add(i.target)
        pc += i.n
    return insns, entries


def seeds_for(code: bytes):
    s = [BASE + 0x60]
    for k in range(BASE, BASE + 0x30, 3):
        if code[k] == 0xFF:
            a = (code[k + 1] << 8) | code[k + 2]
            if BASE <= a < len(code):
                s.append(a)
    return s


def main() -> int:
    help_guard(__doc__, strict=True, known=("--out", "--seed", "--descent"))
    args = sys.argv[1:]
    out = None
    extra = []
    descent = False
    pos = []
    k = 0
    while k < len(args):
        if args[k] == "--out":
            out = Path(args[k + 1])
            k += 2
        elif args[k] == "--seed":
            extra.append(int(args[k + 1], 0))
            k += 2
        elif args[k] == "--descent":
            descent = True
            k += 1
        else:
            pos.append(args[k])
            k += 1
    if not pos:
        raise SystemExit("IMAGE required")
    img = Path(pos[0])
    code = bytes(BASE) + img.read_bytes()
    out = out or ROOT / "tmp/poe-fw" / (img.stem + ".asm")
    out.parent.mkdir(parents=True, exist_ok=True)
    if descent:
        insns, entries = descend(code, seeds_for(code) + extra)
    else:
        insns, entries = sweep(code)
    out.write_text(listing(code, insns, entries))
    log = ROOT / "tmp/logs/poe_fw_dis.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    msg = f"{img.name}: {len(insns)} insns, {len(entries)} entries, {sum(i.n for i in insns.values())}/{len(code) - BASE} B decoded -> {out}"
    print(msg)
    with log.open("a") as f:
        f.write(msg + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
