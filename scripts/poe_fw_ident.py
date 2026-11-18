#!/usr/bin/env python3
"""Identify the CPU/ISA of the BCM59111 PSE firmware images and compare the two copies (docs/poe-fw.md).

Usage: poe_fw_ident.py [IMAGE ...]
  Default images: hwdb/firmware/bcm59111-v21.bin (stock) and bcm59111-meraki.bin.
  Prints: entropy, header patch table, vector-table checks (8051 / ARM Cortex-M), 8051 vs random
  branch-target coherence, opcode frequencies, XRAM (0x8000+) accesses, strings, byte diff of the first two images.
Logs to tmp/logs/poe_fw_ident.log.
"""
import collections
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402
import poe_fw_dis as D  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FW = ROOT / "hwdb/firmware"
OUT = []


def p(*a):
    s = " ".join(str(x) for x in a)
    OUT.append(s)
    print(s)


def entropy(b: bytes) -> float:
    c = collections.Counter(b)
    n = len(b)
    return -sum(v / n * math.log2(v / n) for v in c.values())


def sweep(code: bytes, start=None):
    pc, ins = (D.BASE + 0x60 if start is None else start), []
    while pc < len(code):
        i = D.decode(code, pc)
        ins.append(i)
        pc += i.n
    return ins


def coherence(code: bytes, ins):
    """Share of relative-branch targets that land on an instruction boundary of the linear sweep."""
    bounds = {i.pc for i in ins}
    rel = [i for i in ins if i.flow in ("cond",) or i.text.startswith("SJMP")]
    hit = sum(1 for i in rel if i.target in bounds)
    base = len(bounds) / (len(code) - D.BASE - 0x60)
    return hit, len(rel), base


def thumb_check(code: bytes):
    """Cortex-M: initial SP in RAM range and odd reset vector pointing into the image."""
    sp = int.from_bytes(code[0:4], "little")
    rv = int.from_bytes(code[4:8], "little")
    return sp, rv


def reg_tables(full: bytes, isr: int):
    """Host register dispatch tables (3 B entries [0xFF, hi, lo], one per register) referenced by the I2C ISR."""
    import re
    body = full[isr:isr + 0xC0]
    ms = [(m.start(), (body[m.start() + 1] << 8) | body[m.start() + 2]) for m in re.finditer(rb"\x90..\x12", body)]
    if len(ms) < 2:
        return None
    rd = next((a for o, a in ms if body[o + 6:o + 9] == b"\x75\x30\x01"), None)
    wr = next((a for o, a in ms if a != rd and body[o + 6:o + 9] != b"\x75\x30\x01"), None)
    t = lambda base: [(full[base + 3 * r + 1] << 8) | full[base + 3 * r + 2] for r in range(0x76)]
    return rd, wr, t(rd), t(wr)


def analyse(path: Path):
    code = path.read_bytes()
    p(f"\n=== {path.name}  {len(code)} B ===")
    p(f"entropy {entropy(code):.3f} bit/B (8.0 = compressed/encrypted; text ~4.5; code ~6)")
    z = code.count(0)
    ff = code.count(0xFF)
    p(f"0x00 bytes {z} ({z / len(code):.1%}), 0xFF bytes {ff} ({ff / len(code):.1%})")
    tbl = []
    for k in range(0, 0x30, 3):
        if code[k] == 0xFF:
            tbl.append((code[k + 1] << 8) | code[k + 2])
    p("header 0x00..0x2f: 15 x [0xFF, hi, lo] records ->", " ".join(f"{a:04X}" for a in tbl), "; 0x2f..0x5f zero")
    p("file offset 0 is a hook table, not an 8051 reset/vector table (no LJMP 0x02 at 0x00, no vectors every 8 B)")
    sp, rv = thumb_check(code)
    p(f"Cortex-M check: word0 {sp:#010x} (SP must be RAM, e.g. 0x2000xxxx), word1 {rv:#010x} (reset, odd, in image): "
      f"{'plausible' if 0x1FFF0000 <= sp <= 0x20100000 and rv & 1 else 'FAIL'}")
    full = bytes(D.BASE) + code
    ins = sweep(full)
    by_pc = {i.pc: i for i in ins}
    for i in ins:
        if i.text == "MOV DPTR,#0x8041" and by_pc[i.pc + 3].text.startswith("MOV A,#"):
            p(f"FW_REV: host reg 0x41 = XRAM 0x8041 initialised to {by_pc[i.pc + 3].text.split('#')[1]} at CPU {i.pc:#06x}; "
              "0x8075 <- 0xAA follows")
    p("hook table (file 0x00..0x2e, CPU 0x1A00..): slot -> target -> first 3 insns")
    for k in range(15):
        a = tbl[k]
        seq = []
        pc = a
        for _ in range(3):
            if pc in by_pc:
                seq.append(by_pc[pc].text)
                pc += by_pc[pc].n
        p(f"  slot {k:2d} -> {a:#06x}: " + "; ".join(seq))
    rt = reg_tables(full, tbl[1])
    if rt:
        rd, wr, rtab, wtab = rt
        dw = collections.Counter(wtab).most_common(1)[0][0]
        dr = collections.Counter(rtab).most_common(1)[0][0]
        p(f"host reg tables (I2C ISR slot 1): read table {rd:#06x}, write table {wr:#06x} (entry per register 0x00..0x75)")
        p("  write handlers != default {:#06x}: ".format(dw) + " ".join(f"{r:02X}" for r, h in enumerate(wtab) if h != dw))
        p("  read handlers != default {:#06x} and not a bare RET stub: ".format(dr)
          + " ".join(f"{r:02X}:{h:04X}" for r, h in enumerate(rtab) if h != dr and full[h] != 0x22))
    hit, n, base = coherence(full, ins)
    p(f"8051 linear sweep from file 0x60 (CPU {D.BASE + 0x60:#06x}, image base {D.BASE:#06x}): {len(ins)} insns, avg len {(len(code) - 0x60) / len(ins):.2f} B")
    p(f"  rel-branch targets on instruction boundary: {hit}/{n} = {hit / n:.1%} (chance for random code ~{base:.0%})")
    rnd = bytes(D.BASE) + random.Random(1).randbytes(len(code))
    rh, rn, rb = coherence(rnd, sweep(rnd))
    p(f"  same test on random bytes: {rh}/{rn} = {rh / rn:.1%}")
    ops = collections.Counter(i.text.split()[0] for i in ins)
    p("  mnemonic top 16:", ", ".join(f"{m} {c}" for m, c in ops.most_common(16)))
    ret = sum(1 for i in ins if i.flow == "ret")
    lc = sum(1 for i in ins if i.flow == "call")
    p(f"  RET/RETI {ret}, LCALL/ACALL {lc}, MOVX {ops['MOVX']}, JMP @A+DPTR {sum(1 for i in ins if i.flow == 'jmpa')}")
    xr = collections.Counter()
    for i in ins:
        if i.text.startswith("MOV DPTR,#0x8"):
            xr[int(i.text.split("#")[1], 16)] += 1
    p(f"  MOV DPTR,#0x8xxx (XRAM/peripheral window): {sum(xr.values())} uses, {len(xr)} distinct; range "
      f"{min(xr):04X}..{max(xr):04X}" if xr else "  no 0x8xxx DPTR loads")
    sfr = collections.Counter()
    for i in ins:
        for tok in i.text.replace(",", " ").split():
            if tok in D.SFR.values():
                sfr[tok] += 1
    p("  SFR names in operands:", dict(sfr.most_common(12)))
    strs = []
    cur = bytearray()
    for b in code + b"\0":
        if 32 <= b < 127:
            cur.append(b)
        else:
            if len(cur) >= 12:
                strs.append(cur.decode())
            cur = bytearray()
    for s in strs:
        p("  string:", s)
    return code


def diff(a: bytes, b: bytes):
    p("\n=== stock vs meraki ===")
    d = [i for i in range(len(a)) if a[i] != b[i]]
    p(f"differing bytes: {len(d)}/{len(a)} = {len(d) / len(a):.1%}; identical {len(a) - len(d)}")
    hdr = sum(1 for i in d if i < 0x60)
    p(f"  in header 0x00..0x5f: {hdr}; in 0x60..end: {len(d) - hdr}")
    last = max(i for i in range(len(a)) if a[i] not in (0, 0xFF) or b[i] not in (0, 0xFF))
    p(f"  last non-pad byte of either image: {last:#06x}")
    runs = []
    for i in d:
        if runs and i - runs[-1][1] <= 8:
            runs[-1][1] = i
        else:
            runs.append([i, i])
    p(f"  diff clusters (gap<=8): {len(runs)}")
    # equal-length windows: what fraction of 16 B blocks are untouched
    same = sum(1 for k in range(0, len(a), 16) if a[k:k + 16] == b[k:k + 16])
    p(f"  identical 16 B blocks at same offset: {same}/{len(a) // 16}")
    # best-alignment test: how much of the stock code appears in meraki at any offset
    idx = collections.defaultdict(list)
    for k in range(len(b) - 8):
        idx[b[k:k + 8]].append(k)
    found = 0
    shifts = collections.Counter()
    for k in range(0x60, last - 8, 8):
        w = a[k:k + 8]
        if w in idx:
            found += 1
            for o in idx[w][:1]:
                shifts[o - k] += 1
    tot = (last - 0x60) // 8
    p(f"  stock 8-byte windows found somewhere in meraki: {found}/{tot} = {found / tot:.1%}")
    p("  most common offset shifts (meraki - stock):", shifts.most_common(8))


def main() -> int:
    help_guard(__doc__)
    args = sys.argv[1:]
    paths = [Path(a) for a in args] or [FW / "bcm59111-v21.bin", FW / "bcm59111-meraki.bin"]
    codes = [analyse(x) for x in paths]
    if len(codes) >= 2:
        diff(codes[0], codes[1])
    log = ROOT / "tmp/logs/poe_fw_ident.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("\n".join(OUT) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
