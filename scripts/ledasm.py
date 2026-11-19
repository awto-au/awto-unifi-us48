#!/usr/bin/env python3
"""ledasm: assembler + reference simulator for the CMIC LEDUP processor (docs/leds.md, #14).

Usage: ledasm.py [--asm FILE] [--out HEADER] [--check] [--verify ASM HEX] [--sim] [--list]
       ledasm.py --disasm FILE [--len N] | --find FILE DIR
- default: assemble kernel/us48-cmic/us48_ledup.asm -> kernel/us48-cmic/us48_ledup_prog.h (generated, committed).
- --check: exit 1 if the committed header differs from a fresh assembly (no write).
- --verify ASM HEX: assemble a reference program and compare with its published hex bytes (space separated, file),
  e.g. OpenBCM tools/led/example/greyhound/gh_sdk53402.asm vs rc/gh_ledup.soc. Proves the encoder, nothing is copied.
- --disasm FILE: disassemble a raw .bin or a text file of hex bytes (up to --len bytes, default all non-zero tail trimmed).
- --find FILE DIR: assemble every *.asm under DIR, report byte identity / common prefix with FILE (published-program match).
- --sim: run our program in the simulator for the pattern cases below; exit 1 on a mismatch.
- --list: print the listing (address, bytes, source).
- ISA: opcode table and operand forms of OpenBCM sdk-6.5.16 tools/led/tools/ledasm.h (Broadcom licence, Legal/LICENSE:
  derivative works allowed). Our own encoder; no Broadcom source in this file.
- Simulator semantics are ours (I): cmp/sub/dec set C on borrow, add/inc on carry, Z on zero; tst d,s: C = bit s of d;
  pop: C = top bit; tor/tand/txor pop two and push the result, tinv inverts the top; jt/jnt test without popping; pushst reads the simulated status; send n emits the first n packed bits.
- Log: tmp/logs/ledasm.log
"""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ASM = ROOT / "kernel/us48-cmic/us48_ledup.asm"
HDR = ROOT / "kernel/us48-cmic/us48_ledup_prog.h"
LOG = ROOT / "tmp/logs/ledasm.log"

FIXED = {"clc": 0x07, "stc": 0x17, "cmc": 0x37, "ret": 0x57, "pack": 0x87, "pop": 0x97,
         "txor": 0xA7, "tor": 0xB7, "tand": 0xC7, "tinv": 0xD7}
JUMPS = {"call": 0x67, "jmp": 0x77, "jz": 0x70, "jc": 0x71, "jt": 0x72, "jnz": 0x74, "jnc": 0x75, "jnt": 0x76}
SRC1 = {"push": 0x20, "port": 0x28, "send": 0x38}          # | sss field (push also allows cy)
DST1 = {"inc": 0x80, "dec": 0x90, "rol": 0x88, "ror": 0x98}  # | ddd field
ALU = {"xor": 0xA0, "or": 0xB0, "and": 0xC0, "cmp": 0xD0, "sub": 0xE0, "add": 0xF0}  # | d<<3 | sss
TSTS = {"tst": 0x08, "bit": 0x0C}                          # | ddd<<4 | ss
# operand field codes: a 0, b 1, # 2, (a) 4, (b) 5, (#) 6, cy 7
SSSX, SSS, SS, DDD = {0, 1, 2, 4, 5, 6, 7}, {0, 1, 2, 4, 5, 6}, {0, 1, 2}, {0, 1, 4, 5, 6}


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}\n")
    print(msg)


class AsmError(Exception):
    pass


def evaluate(expr: str, sym: dict) -> int:
    toks = re.findall(r"0x[0-9a-fA-F]+|\d+|[A-Za-z_]\w*|[-+*/()]", expr)
    if "".join(toks) != expr.replace(" ", ""):
        raise AsmError(f"bad expression {expr!r}")
    py = []
    for t in toks:
        if re.fullmatch(r"[A-Za-z_]\w*", t):
            if t not in sym:
                raise AsmError(f"undefined symbol {t}")
            py.append(str(sym[t]))
        else:
            py.append(str(int(t, 0)) if t[0].isdigit() else t)
    return eval("".join(py).replace("/", "//"), {"__builtins__": {}}) & 0xFFFF  # noqa: S307 (tokens are numbers and + - * ( ) only)


def operand(text: str, sym: dict) -> tuple[int, int | None]:
    t = text.strip()
    low = t.lower().replace(" ", "")
    simple = {"a": 0, "b": 1, "(a)": 4, "(b)": 5, "cy": 7}
    if low in simple:
        return simple[low], None
    if low.startswith("(") and low.endswith(")"):
        return 6, evaluate(t.strip()[1:-1], sym) & 0xFF
    return 2, evaluate(t, sym) & 0xFF


def split_line(line: str) -> tuple[str | None, str | None, list[str]]:
    line = line.split(";", 1)[0].rstrip()
    if not line.strip():
        return None, None, []
    label = None
    m = re.match(r"^\s*([A-Za-z_]\w*):(.*)$", line)
    if m:
        label, line = m.group(1), m.group(2)
    m = re.match(r"^\s*([A-Za-z_]\w*)\s+equ\s+(.+)$", line, re.I)
    if m:
        return m.group(1), "equ", [m.group(2).strip()]
    parts = line.split(None, 1)
    if not parts:
        return label, None, []
    ops = [o.strip() for o in parts[1].split(",")] if len(parts) > 1 else []
    return label, parts[0].lower(), ops


def encode(mn: str, ops: list[str], sym: dict, where: str) -> list[int]:
    def need(n):
        if len(ops) != n:
            raise AsmError(f"{where}: {mn} takes {n} operand(s)")

    def one(code: int, field: int, allowed: set, imm):
        if field not in allowed:
            raise AsmError(f"{where}: operand form not allowed for {mn}")
        return [code | field] + ([imm] if imm is not None else [])

    if mn in FIXED:
        need(0)
        return [FIXED[mn]]
    if mn in JUMPS:
        need(1)
        return [JUMPS[mn], evaluate(ops[0], sym) & 0xFF]
    if mn in SRC1:
        need(1)
        f, imm = operand(ops[0], sym)
        return one(SRC1[mn], f, SSSX if mn == "push" else SSS, imm)
    if mn == "pushst":
        need(1)
        f, imm = operand(ops[0], sym)
        return one(0x30, f, SS, imm)
    if mn in DST1:
        need(1)
        f, imm = operand(ops[0], sym)
        return one(DST1[mn], f, DDD, imm)
    if mn in ALU:
        need(2)
        d, _ = operand(ops[0], sym)
        s, imm = operand(ops[1], sym)
        if d not in (0, 1) or s not in SSS:
            raise AsmError(f"{where}: operand form not allowed for {mn}")
        return [ALU[mn] | d << 3 | s] + ([imm] if imm is not None else [])
    if mn in TSTS or mn == "ld":
        need(2)
        d, dimm = operand(ops[0], sym)
        s, simm = operand(ops[1], sym)
        if d not in DDD or s not in (SS if mn != "ld" else SSS) or (dimm is not None and simm is not None):
            raise AsmError(f"{where}: operand form not allowed for {mn}")
        base = TSTS.get(mn, 0x00)
        imm = dimm if dimm is not None else simm
        return [base | d << 4 | s] + ([imm] if imm is not None else [])
    raise AsmError(f"{where}: unknown mnemonic {mn}")


def assemble(text: str, name: str) -> tuple[bytes, list[tuple[int, list[int], str]], dict]:
    lines = text.splitlines()
    sym: dict = {}
    for _ in range(2):  # pass 1 sizes (forward labels as 0), pass 2 final
        pc, out, listing = 0, [], []
        for n, raw in enumerate(lines, 1):
            label, mn, ops = split_line(raw)
            if mn == "equ":
                sym[label] = evaluate(ops[0], sym)
                continue
            if label:
                sym[label] = pc
            if not mn:
                continue
            tmp = dict(sym)
            for o in ops:  # unknown forward symbols in pass 1
                for t in re.findall(r"[A-Za-z_]\w*", o):
                    tmp.setdefault(t, 0)
            code = encode(mn, ops, tmp, f"{name}:{n}")
            listing.append((pc, code, raw.strip()))
            out += code
            pc += len(code)
        if pc > 256:
            raise AsmError(f"{name}: program is {pc} bytes, LEDUP program RAM holds 256")
    return bytes(out), listing, sym


def header(prog: bytes, src: Path, sym: dict) -> str:
    rows = [", ".join(f"0x{b:02x}" for b in prog[i:i + 12]) for i in range(0, len(prog), 12)]
    syms = [k for k in ("CTL", "SPORT", "TICK", "NSLOT", "NBITS", "POL", "TOTAL", "PH") if k in sym]
    defs = "".join(f"#define US48_LEDUP_{k}\t0x{sym[k]:02x}\n" for k in syms)
    body = ",\n\t".join(rows)
    return (f"/* SPDX-License-Identifier: GPL-2.0 */\n/* generated by scripts/ledasm.py from {src.relative_to(ROOT)}, do not edit */\n"
            f"#ifndef US48_LEDUP_PROG_H\n#define US48_LEDUP_PROG_H\n\n{defs}\n"
            f"static const u8 us48_ledup_prog[{len(prog)}] = {{\n\t{body}\n}};\n\n#endif\n")


class Sim:
    """Reference model of our program (semantics in the docstring are assumptions, I)."""

    def __init__(self, prog: bytes, ram: dict, status):
        self.p = list(prog) + [0] * (256 - len(prog))
        self.ram = [0] * 256
        for k, v in ram.items():
            self.ram[k] = v & 0xFF
        self.status = status  # status(port, bit) -> 0/1

    def run(self, limit: int = 20000) -> list[int]:
        a = b = c = z = pc = 0
        st, out, port, ret = [], [], 0, []
        for _ in range(limit):
            op = self.p[pc]
            imm = self.p[(pc + 1) & 0xFF]

            def src(f, a=a, b=b):
                return {0: a, 1: b, 2: imm, 4: self.ram[a], 5: self.ram[b], 6: self.ram[imm], 7: c}[f]

            ln = 2 if (op & 7) in (2, 6) else 1
            if op in FIXED.values():
                ln = 1
                if op == 0x87:
                    out.append(st.pop())
                elif op == 0x97:
                    c = st.pop()
                elif op == 0x57:
                    pc = ret.pop()
                    continue
                elif op in (0x07, 0x17, 0x37):
                    c = {0x07: 0, 0x17: 1, 0x37: c ^ 1}[op]
                elif op in (0xA7, 0xB7, 0xC7):  # txor / tor / tand: pop two, push the result
                    x, y = st.pop(), st.pop()
                    st.append({0xA7: x ^ y, 0xB7: x | y, 0xC7: x & y}[op])
                elif op == 0xD7:
                    st.append(st.pop() ^ 1)
                else:
                    raise RuntimeError(f"sim: op {op:02x} not modelled")
            elif op in JUMPS.values():
                take = {0x77: 1, 0x67: 1, 0x70: z, 0x71: c, 0x74: not z, 0x75: not c,
                        0x72: st[-1] if st else 0, 0x76: not (st[-1] if st else 0)}[op]
                if op == 0x67:
                    ret.append(pc + 2)
                pc = imm if take else pc + 2
                continue
            elif op & 0xF8 == 0x20:
                st.append(src(op & 7) & 1)
            elif op & 0xF8 == 0x28:
                port = src(op & 7)
            elif op & 0xFC == 0x30:
                st.append(self.status(port, src(op & 3)) & 1)
                ln = 2 if (op & 3) == 2 else 1
            elif op & 0xF8 == 0x38:
                return out[:src(op & 7)]
            elif op & 0xE0 == 0x80 and op & 0xF0 in (0x80, 0x90) and (op & 0x08) == 0:
                f = op & 7
                v = src(f)
                r = (v + (1 if op & 0xF0 == 0x80 else -1))
                c, z, r = int(r > 255 or r < 0), int((r & 0xFF) == 0), r & 0xFF
                self._store(f, r, a, b, imm)
                a, b = (r if f == 0 else a), (r if f == 1 else b)
            elif op & 0xF0 in ALU.values():
                d, s = (op >> 3) & 1, src(op & 7)
                v = b if d else a
                kind = op & 0xF0
                r = {0xA0: v ^ s, 0xB0: v | s, 0xC0: v & s, 0xD0: v - s, 0xE0: v - s, 0xF0: v + s}[kind]
                if kind in (0xD0, 0xE0, 0xF0):
                    c = int(r < 0 or r > 255)
                z = int((r & 0xFF) == 0)
                if kind != 0xD0:
                    a, b = (r & 0xFF if not d else a), (r & 0xFF if d else b)
            elif op & 0x8C == 0x08:
                d, s = (op >> 4) & 7, op & 3
                dv = {0: a, 1: b, 4: self.ram[a], 5: self.ram[b], 6: self.ram[imm]}[d]
                c = (dv >> ({0: a, 1: b, 2: imm}[s] & 7)) & 1
                ln = 2 if d == 6 or s == 2 else 1
            elif op & 0x88 == 0x00:
                d, s = (op >> 4) & 7, op & 7
                v = src(s)
                ln = 2 if d == 6 or s in (2, 6) else 1
                self._store(d, v, a, b, imm)
                a, b = (v if d == 0 else a), (v if d == 1 else b)
            else:
                raise RuntimeError(f"sim: op {op:02x} at {pc:02x} not modelled")
            pc = (pc + ln) & 0xFF
        raise RuntimeError("sim: no send within the step limit")

    def _store(self, f, v, a, b, imm):
        if f == 4:
            self.ram[a] = v
        elif f == 5:
            self.ram[b] = v
        elif f == 6:
            self.ram[imm] = v


def ref_bits(ctl: list[int], nbits: int, pol: int, tick: int, ph: int, act: list[bool]) -> list[int]:
    """Intended output; ph = activity phase after this refresh's step (0..4), blank while < 2 (stock)."""
    out = []
    for s, v in enumerate(ctl):
        vv = v & 3
        speed = (v >> 4) & 3
        if speed and (tick >> (5 - speed)) & 1:
            vv &= ~((v >> 2) & 3)
        if v & 0x40 and act[s] and ph < 2:
            vv = 0
        vv ^= pol
        out += [(vv >> i) & 1 for i in range(nbits)]
    return out


def simulate(prog: bytes, sym: dict) -> int:
    bad = 0
    cases = [
        ("on/off", [3, 1, 2, 0], 2, 0, [False] * 4),
        ("slow blink", [0x1F, 0x1F, 0x17], 2, 0, [False] * 3),
        ("fast blink + act", [0x3F, 0x41, 0x41, 0x7B], 2, 3, [True, True, False, True]),
        ("1 bit, inverted", [1, 0, 1], 1, 1, [False] * 3),
        ("4 bits", [2, 1], 4, 0, [False] * 2),
        ("hw idle ports: steady", [0x41, 0x42, 0x41], 2, 0, [False] * 3),
        ("hw busy ports: 2/5 blank", [0x41, 0x42, 0x41, 0x40], 2, 0, [True, True, False, True]),
        ("busy, RX only / TX only", [0x41, 0x41], 2, 0, ["rx", "tx"]),
    ]
    n = 0
    for name, ctl, nbits, pol, act in cases:
        for tick0, ph0 in ((0, 0), (1, 1), (7, 2), (15, 3), (31, 4), (2, 5)):
            ram = {sym["TICK"]: tick0, sym["PH"]: ph0, sym["NSLOT"]: len(ctl), sym["NBITS"]: nbits, sym["POL"]: pol,
                   sym["TOTAL"]: len(ctl) * nbits}
            for s, v in enumerate(ctl):
                ram[sym["CTL"] + s] = v
                ram[sym["SPORT"] + s] = 2 + s

            def status(port, bitno, act=act):  # RX = 0, TX = 1; "rx"/"tx" = only that bit set
                a = act[port - 2]
                return int(a is True and bitno in (0, 1) or a == "rx" and bitno == 0 or a == "tx" and bitno == 1)
            got = Sim(prog, ram, status).run()
            ph = ph0 + 1 if ph0 + 1 < 5 else 0
            want = ref_bits(ctl, nbits, pol, (tick0 + 1) & 0xFF, ph, [bool(a) for a in act])
            n += 1
            if got != want:
                bad += 1
                log(f"sim FAIL {name} tick {tick0} ph {ph0}: got {got} want {want}")
    log(f"sim: {n - bad}/{n} cases match the reference model")
    return 1 if bad else 0


RN = {0: "a", 1: "b", 4: "(a)", 5: "(b)", 7: "cy"}


def disasm(prog: bytes) -> list[tuple[int, list[int], str]]:
    rows, pc = [], 0
    fixed = {v: k for k, v in FIXED.items()}
    jumps = {v: k for k, v in JUMPS.items()}

    def opd(f, imm):
        return f"({imm:#04x})" if f == 6 else f"{imm:#04x}" if f == 2 else RN[f]

    while pc < len(prog):
        op = prog[pc]
        imm = prog[pc + 1] if pc + 1 < len(prog) else 0
        if op in fixed:
            ln, txt = 1, fixed[op]
        elif op in jumps:
            ln, txt = 2, f"{jumps[op]:7} {imm:#04x}"
        elif op & 0xF8 == 0x20 and op & 7 != 3:
            ln = 2 if op & 7 in (2, 6) else 1
            txt = f"push    {opd(op & 7, imm)}"
        elif op & 0xF8 == 0x28 and op & 7 != 3:
            ln = 2 if op & 7 in (2, 6) else 1
            txt = f"port    {opd(op & 7, imm)}"
        elif op & 0xFC == 0x30:
            ln = 2 if op & 3 == 2 else 1
            txt = f"pushst  {opd(op & 3, imm)}"
        elif op & 0xF8 == 0x38 and op & 7 != 3:
            ln = 2 if op & 7 in (2, 6) else 1
            txt = f"send    {opd(op & 7, imm)}"
        elif op & 0xE0 == 0x80 and op & 0xF0 in (0x80, 0x90) and op & 7 != 3:
            f = op & 7
            ln = 2 if f == 6 else 1
            txt = f"{('inc', 'rol', 'dec', 'ror')[((op >> 3) & 1) | ((op >> 3) & 2)]:7} {opd(f, imm)}"
        elif op >= 0xA0:
            d, f = (op >> 3) & 1, op & 7
            ln = 2 if f in (2, 6) else 1
            nm = {0xA0: "xor", 0xB0: "or", 0xC0: "and", 0xD0: "cmp", 0xE0: "sub", 0xF0: "add"}[op & 0xF0]
            txt = f"{nm:7} {RN[d]}, {opd(f, imm)}"
        elif op & 0x8C in (0x08, 0x0C):
            d, f = (op >> 4) & 7, op & 3
            ln = 2 if d == 6 or f == 2 else 1
            nm = "tst" if op & 0x04 == 0 else "bit"
            di = imm if d == 6 else 0
            txt = f"{nm:7} {opd(d, di)}, {opd(f, imm)}"
        elif op & 0x88 == 0:
            d, f = (op >> 4) & 7, op & 7
            ln = 2 if d == 6 or f in (2, 6) else 1
            txt = f"ld      {opd(d, imm)}, {opd(f, imm)}"
        else:
            ln, txt = 1, f".byte   {op:#04x}"
        rows.append((pc, list(prog[pc:pc + ln]), txt))
        pc += ln
    return rows


def load_bytes(path: Path) -> bytes:
    raw = path.read_bytes()
    try:
        return bytes(int(t, 16) for t in raw.decode().split())
    except ValueError:
        return raw


def main() -> int:
    help_guard(__doc__, strict=True, known=("--asm", "--out", "--check", "--verify", "--sim", "--list", "--disasm",
                                            "--find", "--len"))
    args = sys.argv[1:]
    if "--disasm" in args:
        data = load_bytes(Path(args[args.index("--disasm") + 1]))
        n = int(args[args.index("--len") + 1], 0) if "--len" in args else len(data.rstrip(b"\0"))
        for pc, code, txt in disasm(data[:n]):
            log(f"{pc:02x}  {' '.join(f'{b:02x}' for b in code):6}  {txt}")
        return 0
    if "--find" in args:
        i = args.index("--find")
        ref = load_bytes(Path(args[i + 1])).rstrip(b"\0")
        n = 0
        for f in sorted(Path(args[i + 2]).rglob("*.asm")):
            try:
                prog, _, _ = assemble(f.read_text(errors="replace"), f.name)
            except Exception as e:  # noqa: BLE001 (unsupported syntax in a reference file: report and go on)
                log(f"skip {f.name}: {str(e)[:60]}")
                continue
            n += 1
            prog = prog.rstrip(b"\0")
            common = next((k for k in range(min(len(prog), len(ref))) if prog[k] != ref[k]), min(len(prog), len(ref)))
            log(f"{'IDENTICAL' if prog == ref else 'prefix %3d' % common}  {len(prog):3} B vs {len(ref)} B  {f.name}")
        log(f"find: {n} reference programs assembled")
        return 0
    src = Path(args[args.index("--asm") + 1]) if "--asm" in args else ASM
    out = Path(args[args.index("--out") + 1]) if "--out" in args else HDR
    if "--verify" in args:
        i = args.index("--verify")
        asm, hexf = Path(args[i + 1]), Path(args[i + 2])
        prog, _, _ = assemble(asm.read_text(), asm.name)
        txt = hexf.read_text()
        blocks = re.findall(r"ledcode '([^']*)'", txt.replace("\\\n", " "))  # .soc: any ledcode block may match
        refs = [bytes(int(t, 16) for t in re.findall(r"\b[0-9A-Fa-f]{2}\b", b)) for b in blocks] or \
               [bytes(int(t, 16) for t in txt.split())]
        ref = next((r for r in refs if r[:len(prog)] == prog and not any(r[len(prog):])), refs[0])
        ok = ref[:len(prog)] == prog and not any(ref[len(prog):])
        log(f"verify {asm.name}: {len(prog)} B, {'MATCH' if ok else 'DIFFER'} vs {hexf.name}")
        if not ok:
            log(f"  ours {prog.hex(' ')}\n  ref  {ref.hex(' ')}")
        return 0 if ok else 1
    prog, listing, sym = assemble(src.read_text(), src.name)
    if "--list" in args:
        for pc, code, txt in listing:
            print(f"{pc:02x}  {' '.join(f'{b:02x}' for b in code):6}  {txt}")
    if "--sim" in args:
        return simulate(prog, sym)
    text = header(prog, src, sym)
    if "--check" in args:
        same = out.exists() and out.read_text() == text
        log(f"check {out.relative_to(ROOT)}: {'up to date' if same else 'STALE'} ({len(prog)} B)")
        return 0 if same else 1
    out.write_text(text)
    log(f"wrote {out.relative_to(ROOT)}: {len(prog)} B program")
    return 0


if __name__ == "__main__":
    sys.exit(main())
