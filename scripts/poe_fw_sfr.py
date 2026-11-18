#!/usr/bin/env python3
"""SFR / memory-space fingerprint of the BCM59111 PSE firmware images (docs/poe-fw.md section 3, #26).

Usage: poe_fw_sfr.py [IMAGE ...] [--detail]
  Default images: hwdb/firmware/bcm59111-v21.bin bcm59111-meraki.bin.
  Linear-sweep decode (scripts/poe_fw_dis.py), then per SFR 0x80..0xFF: direct and bit-addressed access counts, classified
  std8051 / std8052 / DW8051 ext / free external-SFR-bus address (DW8051 databook v3.2a tables 2-6, 4-8). Also MOVX @Ri vs
  @DPTR, MOVC, MPAGE / SPC_FNC / DPS writes, MOV SP, IRAM 0x80..0xFF indirect use.
  --detail = list every instruction per SFR. Log: tmp/logs/poe_fw_sfr.log (stdout copy).
"""
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402
import poe_fw_dis as D  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STD51 = {0x80: "P0", 0x81: "SP", 0x82: "DPL", 0x83: "DPH", 0x87: "PCON", 0x88: "TCON", 0x89: "TMOD", 0x8A: "TL0",
         0x8B: "TL1", 0x8C: "TH0", 0x8D: "TH1", 0x90: "P1", 0x98: "SCON", 0x99: "SBUF", 0xA0: "P2", 0xA8: "IE",
         0xB0: "P3", 0xB8: "IP", 0xD0: "PSW", 0xE0: "ACC", 0xF0: "B"}
STD52 = {0xC8: "T2CON", 0xCA: "RCAP2L", 0xCB: "RCAP2H", 0xCC: "TL2", 0xCD: "TH2"}
DW_EXT = {0x84: "DPL1", 0x85: "DPH1", 0x86: "DPS", 0x8E: "CKCON", 0x8F: "SPC_FNC", 0x91: "EXIF", 0x92: "MPAGE",
          0xC0: "SCON1", 0xC1: "SBUF1", 0xD8: "EICON", 0xE8: "EIE", 0xF8: "EIP"}
# 8052-family (not DW8051) names for the same addresses where they differ: SCON1/EIE/EIP = DW8051 or Dallas
SFR_NAMES = {**STD51, **STD52, **DW_EXT}


def cls(a: int) -> str:
    if a in STD51:
        return "std8051"
    if a in STD52:
        return "std8052/T2"
    if a in DW_EXT:
        return "DW8051-ext"
    return "bus/unknown"


def direct_ops(ins):
    """Direct-address operands of one instruction: list of (addr, kind) kind 'd' direct / 'b' bit."""
    op = ins.raw[0]
    t = D.TABLE[op][1]
    out = []
    if len(ins.raw) < D.TABLE[op][0]:
        return out
    if "{s}" in t:  # MOV dir,dir
        out += [(ins.raw[1], "d"), (ins.raw[2], "d")]
    elif "{d}" in t:
        out.append((ins.raw[1], "d"))
    if "{b}" in t:
        out.append((ins.raw[1], "b"))
    return out


def analyse(path: Path, detail: bool, w) -> None:
    code = bytes(D.BASE) + path.read_bytes()
    insns, _ = D.sweep(code)
    L = sorted(insns.values(), key=lambda i: i.pc)
    sfr = defaultdict(lambda: {"d": Counter(), "b": Counter(), "bits": set()})
    movx_ri = movx_dptr = movc_dptr = movc_pc = 0
    sp_init, sp_all, iram_hi = [], [], []
    wr = defaultdict(list)
    for k, i in enumerate(L):
        op = i.raw[0]
        if op in (0xE2, 0xE3, 0xF2, 0xF3):
            movx_ri += 1
        if op in (0xE0, 0xF0):
            movx_dptr += 1
        if op == 0x93:
            movc_dptr += 1
        if op == 0x83:
            movc_pc += 1
        for a, kind in direct_ops(i):
            if kind == "d" and a >= 0x80:
                sfr[a]["d"][i.text] += 1
            elif kind == "b" and a >= 0x80:
                base = a & 0xF8
                sfr[base]["b"][i.text] += 1
                sfr[base]["bits"].add(a & 7)
        if op == 0x75 and i.raw[1] == 0x81:
            sp_init.append((i.pc, i.raw[2]))
        if op in (0x78, 0x79) and i.raw[1] >= 0x80:  # MOV R0/R1,#>=0x80 then @Ri within 4 insns
            r = op & 1
            for j in L[k + 1:k + 5]:
                if j.raw[0] & 0xFE in (0x06, 0x16, 0x76, 0x86, 0xA6, 0xC6, 0xD6, 0xE6, 0xF6) and (j.raw[0] & 1) == r or \
                        j.raw[0] in (0x26 | r, 0x36 | r, 0x46 | r, 0x56 | r, 0x66 | r, 0x96 | r, 0xB6 | r):
                    iram_hi.append((i.pc, i.raw[1], j.text))
                    break
    w(f"\n== {path.name}: {len(L)} insns, MOVX @DPTR {movx_dptr}, MOVX @Ri {movx_ri}, MOVC @A+DPTR {movc_dptr}, "
      f"MOVC @A+PC {movc_pc}")
    w(f"MOV SP,#imm: {[(hex(p), hex(v)) for p, v in sp_init]}")
    w(f"MOV R0/R1,#>=0x80 followed by @Ri (IRAM upper half candidates): {len(iram_hi)}: "
      f"{[(hex(p), hex(v), t) for p, v, t in iram_hi[:12]]}")
    w(f"{'SFR':<5}{'name':<8}{'class':<12}{'dir':>5}{'bit':>5}  bits  top instructions")
    for a in sorted(sfr):
        e = sfr[a]
        top = [f"{t}x{n}" for t, n in (e["d"] + e["b"]).most_common(3 if not detail else 999)]
        w(f"0x{a:02X}  {SFR_NAMES.get(a, ''):<8}{cls(a):<12}{sum(e['d'].values()):>5}{sum(e['b'].values()):>5}  "
          f"{''.join(map(str, sorted(e['bits']))) or '-':<5} {'; '.join(top)}")


def main() -> int:
    help_guard(__doc__, strict=True, known=("--detail",))
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    detail = "--detail" in sys.argv
    imgs = [Path(a) for a in args] or [ROOT / "hwdb/firmware/bcm59111-v21.bin", ROOT / "hwdb/firmware/bcm59111-meraki.bin"]
    log = ROOT / "tmp/logs/poe_fw_sfr.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w") as f:
        def w(s):
            print(s)
            f.write(s + "\n")
        for p in imgs:
            analyse(p, detail, w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
