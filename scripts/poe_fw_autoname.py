#!/usr/bin/env python3
"""Name/describe every function of a BCM59111 image that has no hand name (docs/poe-fw.md section 14, #26).

Usage: poe_fw_autoname.py [IMAGE] [--names TSV]
  Default IMAGE hwdb/firmware/bcm59111-v21.bin, TSV hwdb/firmware/bcm59111-v21.names.tsv (hand rows kept, source != poe-fw-auto).
  Rewrites the poe-fw-auto rows: pattern names for helpers (channel-block / array pointer calculators, accessors) and
  subsystem names for the rest, from the SFR / XRAM / callee profile (poe_fw_re.py). All rows confidence I.
  Log: tmp/logs/poe_fw_autoname.log.
"""
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402
import poe_fw_dis as D  # noqa: E402
import poe_fw_re as R  # noqa: E402
import poe_fw_sfr as S  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
AUTO = "poe-fw-auto"


def prefix_ops(code, insns, pc, n=14):
    out = []
    for _ in range(n):
        i = insns.get(pc)
        if i is None:
            break
        out.append(i)
        if i.flow in ("ret", "jmp", "jmpa"):
            break
        pc += i.n
    return out


def addr_calc(ops):
    """Recognise DPTR = base + idx*stride calculators. Returns (kind, stride, base) or None."""
    b = bytes(x for i in ops for x in i.raw)
    # MOV B,#s; MUL AB; ADD A,#k; MOV DPL,A; CLR A; ADDC A,#hi; MOV DPH,A
    for pos in range(len(b) - 12):
        if b[pos] == 0x75 and b[pos + 1] == 0xF0 and b[pos + 3] == 0xA4 and b[pos + 4] == 0x24 and \
                b[pos + 6:pos + 8] == b"\xf5\x82" and b[pos + 8] == 0xE4 and b[pos + 9] == 0x34:
            return "mul", b[pos + 2], (b[pos + 10] << 8) | b[pos + 5]
    # ADD A,ACC; ADD A,#k; MOV DPL,A; CLR A; ADDC A,#hi; MOV DPH,A  (word array)
    for pos in range(len(b) - 8):
        if b[pos:pos + 2] == b"\x25\xe0" and b[pos + 2] == 0x24 and b[pos + 4:pos + 6] == b"\xf5\x82" and b[pos + 6] == 0xE4 \
                and b[pos + 7] == 0x34:
            return "word", 2, (b[pos + 8] << 8) | b[pos + 3]
    return None


def main() -> int:
    help_guard(__doc__, strict=True, known=("--names",))
    a = sys.argv[1:]
    img = ROOT / "hwdb/firmware/bcm59111-v21.bin"
    names_p = ROOT / "hwdb/firmware/bcm59111-v21.names.tsv"
    pos = [x for x in a if not x.startswith("--")]
    if "--names" in a:
        names_p = Path(a[a.index("--names") + 1])
        pos = [x for x in pos if x != str(names_p)]
    if pos:
        img = Path(pos[0])
    code = bytes(D.BASE) + img.read_bytes()
    insns, L, ents, host, states, tables = R.analyse(code)
    lines = names_p.read_text().splitlines()
    keep = [ln for ln in lines if ln.startswith("#") or not ln.strip() or ln.split("\t")[4:5] != [AUTO]]
    hand = {ln.split("\t")[0] for ln in keep if ln.startswith("CODE:")}
    # per-function profile
    prof = {e: {"sfr": set(), "xr": set(), "callees": set(), "callers": set()} for e in ents}
    import bisect
    for i in L:
        f = ents[bisect.bisect_right(ents, i.pc) - 1] if i.pc >= ents[0] else None
        if f is None:
            continue
        if i.flow == "call" and i.target in prof:
            prof[f]["callees"].add(i.target)
            prof[i.target]["callers"].add(f)
        for d, k in S.direct_ops(i):
            if d >= 0x80:
                prof[f]["sfr"].add(d if k == "d" else d & 0xF8)
        if i.raw[0] == 0x90:
            v = (i.raw[1] << 8) | i.raw[2]
            if 0x8000 <= v < 0x8200:
                prof[f]["xr"].add(v)
    rows, cnt = [], defaultdict(int)
    for e in ents:
        key = f"CODE:{e:04X}"
        if key in hand or e >= 0x5220 or e < D.BASE + 0x60:
            continue
        p = prof[e]
        ops = prefix_ops(code, insns, e)
        ac = addr_calc(ops)
        sub = None
        sfr, xr = p["sfr"], p["xr"]
        if ac:
            kind, stride, base = ac
            nm = f"ptr_s{stride:02X}_{base:04X}_{e:04X}"
            cm = (f"DPTR = 0x{base:04X} + idx*{stride} (idx in A/R7; channel block 0x8078 stride 0x12 when stride=0x12; "
                  f"+{base - 0x8078:#x} in the block)") if stride == 0x12 and base >= 0x8078 else \
                f"DPTR = 0x{base:04X} + idx*{stride}"
            sub = "ptr"
        elif sfr & {0xE9, 0xEA, 0xEB, 0xEC, 0xED, 0xEE, 0xEF}:
            nm, cm, sub = f"math_{e:04X}", "uses the E9..EF multiply/shift unit", "math"
        elif sfr & {0xAB, 0xAC, 0xAD, 0xB5, 0xB6, 0xB7, 0xD9, 0xDA, 0xDB}:
            nm, cm, sub = f"meas_{e:04X}", "analog front end (indirect regs AB/AC/AD, ADC B5..B7)", "meas"
        elif sfr & {0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7}:
            nm, cm, sub = f"detcls_{e:04X}", "detection/classification hardware (D1..D7)", "detcls"
        elif sfr & {0xB1, 0xB2, 0xB3, 0xB4, 0xA1, 0xA2, 0xA3, 0xA4, 0xC9, 0xF1, 0xF2, 0xA5, 0xA7}:
            nm, cm, sub = f"chcfg_{e:04X}", "per-channel config / power SFRs (A1..A7, B1..B4, C9, F1, F2)", "chcfg"
        elif any(0x8000 <= x < 0x8076 for x in xr) and any(0x8076 <= x for x in xr):
            nm, cm, sub = f"host_{e:04X}", "host register(s) 0x8000..0x8075 + private state", "host"
        elif any(0x8000 <= x < 0x8076 for x in xr):
            nm, cm, sub = f"hostreg_{e:04X}", "host register accessor / event latch", "hostreg"
        elif xr:
            nm, cm, sub = f"xstate_{e:04X}", "private XRAM state (0x8076..0x8140)", "xstate"
        elif p["callers"] and not p["callees"]:
            nm, cm, sub = f"leaf_{e:04X}", "register/IRAM leaf helper", "leaf"
        else:
            nm, cm, sub = f"fn_{e:04X}", "glue", "fn"
        info = (f" callers {','.join(f'{c:04X}' for c in sorted(p['callers'])[:6])}" if p["callers"] else " no direct caller (shared tail / state / table target)")
        if xr:
            info += "; XRAM " + ",".join(f"{x:04X}" for x in sorted(xr)[:8])
        if sfr:
            info += "; SFR " + ",".join(f"{x:02X}" for x in sorted(sfr)[:8])
        rows.append("\t".join((key, nm, "func", cm + "." + info, AUTO, "I")))
        cnt[sub] += 1
    out = keep + rows
    names_p.write_text("\n".join(out) + "\n")
    msg = f"{img.name}: {len(rows)} auto names {dict(cnt)} -> {names_p}"
    print(msg)
    log = ROOT / "tmp/logs/poe_fw_autoname.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as f:
        f.write(msg + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
