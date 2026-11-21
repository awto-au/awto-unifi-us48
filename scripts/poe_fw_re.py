#!/usr/bin/env python3
"""Function inventory / call graph / variable xref for the BCM59111 v21 PSE firmware (docs/poe-fw.md, #26).

Usage: poe_fw_re.py [IMAGE] [--names TSV] [--out DIR]
  IMAGE default hwdb/firmware/bcm59111-v21.bin; names default hwdb/firmware/bcm59111-v21.names.tsv (hand names, committed).
  Writes to --out (default tmp/poe-fw/re/): functions.tsv (every function: extent, callers, callees, XRAM, SFR, IRAM, bits),
  callgraph.dot, xram.tsv / iram.tsv (variable -> functions reading/writing), rom_calls.txt (targets < 0x1A00),
  annotated.asm (listing with names + comments). Log: tmp/logs/poe_fw_re.log.
  Function entries = LCALL/ACALL targets, hook table, host dispatch tables, state-handler pointers (MOV R4,#hi / MOV R5,#lo).
"""
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402
import poe_fw_dis as D  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BASE = D.BASE


def load_names(p: Path):
    names = {}
    if p.exists():
        for ln in p.read_text().splitlines():
            if not ln.strip() or ln.startswith("#"):
                continue
            f = ln.split("\t")
            names[f[0]] = f
    return names


def analyse(code: bytes):
    insns, entries = D.sweep(code)
    L = sorted(insns.values(), key=lambda i: i.pc)
    ents = set(entries)
    tables = D.data_regions(code)
    # host tables: write table / read table are consecutive 0x180-byte blocks (128 records) in the data region
    host = {}
    for a, b in tables:
        for k in range(a, b, 3):
            t = (code[k + 1] << 8) | code[k + 2]
            host.setdefault(t, []).append(k)
            ents.add(t)
    for k in range(BASE, BASE + 0x2F, 3):
        if code[k] == 0xFF:
            ents.add((code[k + 1] << 8) | code[k + 2])
    # state-handler pointers: MOV R4,#hi ; MOV R5,#lo (adjacent in either order, within 2 insns)
    states = {}
    for n, i in enumerate(L):
        if i.raw[0] == 0x7C:
            for j in L[n + 1:n + 3]:
                if j.raw[0] == 0x7D:
                    t = (i.raw[1] << 8) | j.raw[1]
                    if BASE <= t < len(code) and t in insns:
                        states.setdefault(t, []).append(i.pc)
        if i.raw[0] == 0x7D:
            for j in L[n + 1:n + 3]:
                if j.raw[0] == 0x7C:
                    t = (j.raw[1] << 8) | i.raw[1]
                    if BASE <= t < len(code) and t in insns:
                        states.setdefault(t, []).append(i.pc)
    ents.update(states)
    ents = sorted(e for e in ents if e in insns)
    return insns, L, ents, host, states, tables


def main() -> int:
    help_guard(__doc__, strict=True, known=("--names", "--out"))
    a = sys.argv[1:]
    img = ROOT / "hwdb/firmware/bcm59111-v21.bin"
    names_p = ROOT / "hwdb/firmware/bcm59111-v21.names.tsv"
    out = ROOT / "tmp/poe-fw/re"
    pos = []
    k = 0
    while k < len(a):
        if a[k] == "--names":
            names_p, k = Path(a[k + 1]), k + 2
        elif a[k] == "--out":
            out, k = Path(a[k + 1]), k + 2
        else:
            pos.append(a[k])
            k += 1
    if pos:
        img = Path(pos[0])
    code = bytes(BASE) + img.read_bytes()
    names = load_names(names_p)
    insns, L, ents, host, states, tables = analyse(code)
    out.mkdir(parents=True, exist_ok=True)
    import bisect
    idx = {e: n for n, e in enumerate(ents)}

    def func_of(pc):
        n = bisect.bisect_right(ents, pc) - 1
        return ents[n] if n >= 0 else None

    info = {e: {"callees": set(), "callers": set(), "xr": defaultdict(set), "sfr": set(), "iram": defaultdict(set),
                "bits": set(), "size": 0, "jmps": set()} for e in ents}
    rom = defaultdict(list)
    lastdptr = {}
    for i in L:
        f = func_of(i.pc)
        if f is None:
            continue
        inf = info[f]
        inf["size"] += i.n
        op = i.raw[0]
        if i.flow in ("call", "jmp") and i.target is not None:
            if i.target < BASE:
                rom[i.target].append(i.pc)
            else:
                if i.flow == "call" and i.target in info:
                    inf["callees"].add(i.target)
                    info[i.target]["callers"].add(f)
                elif i.flow == "jmp" and i.target in info and i.target != f:
                    inf["jmps"].add(i.target)
        if op == 0x90:
            v = (i.raw[1] << 8) | i.raw[2]
            lastdptr[f] = (i.pc, v)
        for d, kind in ((x for x in _dops(i))):
            if kind == "b" and d < 0x80:
                inf["bits"].add(d)
            elif kind == "d" and d >= 0x80:
                inf["sfr"].add(d)
            elif kind == "d":
                inf["iram"][d].add(i.pc)
            elif kind == "b" and d >= 0x80:
                inf["sfr"].add(d & 0xF8)
    # XRAM: MOV DPTR,#imm followed by MOVX read/write within 6 insns
    pcs = [i.pc for i in L]
    for n, i in enumerate(L):
        if i.raw[0] != 0x90:
            continue
        v = (i.raw[1] << 8) | i.raw[2]
        if not 0x8000 <= v < 0x8200:
            continue
        f = func_of(i.pc)
        for j in L[n + 1:n + 7]:
            if j.raw[0] == 0xE0:
                info[f]["xr"][v].add("R")
                break
            if j.raw[0] == 0xF0:
                info[f]["xr"][v].add("W")
                break
            if j.flow in ("ret", "jmp") or j.raw[0] in (0x90, 0x12):
                break
    # outputs
    with (out / "functions.tsv").open("w") as fo:
        fo.write("addr\tname\tsize\tcallers\tcallees\txram\tsfr\tiram\tcomment\n")
        for e in ents:
            inf = info[e]
            key = f"CODE:{e:04X}"
            nm = names.get(key, [None, f"FUN_{e:04X}"])[1]
            cm = names.get(key, [None, None, None, ""])[3] if key in names else ""
            xr = ",".join(f"{v:04X}{''.join(sorted(m))}" for v, m in sorted(inf["xr"].items()))
            fo.write(f"{e:04X}\t{nm}\t{inf['size']}\t{','.join(f'{c:04X}' for c in sorted(inf['callers']))}\t"
                     f"{','.join(f'{c:04X}' for c in sorted(inf['callees']))}\t{xr}\t"
                     f"{','.join(f'{s:02X}' for s in sorted(inf['sfr']))}\t"
                     f"{','.join(f'{d:02X}' for d in sorted(inf['iram']))}\t{cm}\n")
    with (out / "callgraph.dot").open("w") as fo:
        fo.write("digraph poefw {\n")
        for e in ents:
            nm = names.get(f"CODE:{e:04X}", [None, f"FUN_{e:04X}"])[1]
            for c in sorted(info[e]["callees"]):
                nc = names.get(f"CODE:{c:04X}", [None, f"FUN_{c:04X}"])[1]
                fo.write(f'  "{nm}" -> "{nc}";\n')
        fo.write("}\n")
    xr = defaultdict(lambda: defaultdict(set))
    ir = defaultdict(lambda: defaultdict(set))
    for e in ents:
        for v, m in info[e]["xr"].items():
            for x in m:
                xr[v][x].add(e)
        for d, s in info[e]["iram"].items():
            ir[d]["use"].add(e)
    with (out / "xram.tsv").open("w") as fo:
        fo.write("xram\treaders\twriters\n")
        for v in sorted(xr):
            fo.write(f"{v:04X}\t{','.join(f'{x:04X}' for x in sorted(xr[v]['R']))}\t{','.join(f'{x:04X}' for x in sorted(xr[v]['W']))}\n")
    with (out / "iram.tsv").open("w") as fo:
        fo.write("iram\tfunctions\n")
        for d in sorted(ir):
            fo.write(f"{d:02X}\t{','.join(f'{x:04X}' for x in sorted(ir[d]['use']))}\n")
    (out / "rom_calls.txt").write_text("".join(f"{t:04X}\t{','.join(f'{p:04X}' for p in v)}\n" for t, v in sorted(rom.items())))
    # annotated listing
    lst = (ROOT / "tmp/poe-fw" / (img.stem + ".asm"))
    msg = (f"{img.name}: {len(ents)} functions, {len(states)} state-handler pointers, {len(host)} distinct host-table targets, "
           f"{sum(len(v) for v in rom.values())} ROM call sites, {len(tables)} table region(s) -> {out}")
    print(msg)
    log = ROOT / "tmp/logs/poe_fw_re.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as f:
        f.write(msg + "\n")
    # names-annotated listing
    lines = []
    for ln in lst.read_text().splitlines() if lst.exists() else []:
        m = re.match(r"^([0-9A-F]{4}):", ln)
        if m:
            a_ = int(m.group(1), 16)
            key = f"CODE:{a_:04X}"
            if key in names and a_ in info:
                lines.append(f"\n; ==== {names[key][1]}  {names[key][3]} [{names[key][5] if len(names[key]) > 5 else ''}] ====")
        lines.append(ln)
    (out / "annotated.asm").write_text("\n".join(lines) + "\n")
    return 0


def _dops(i):
    import poe_fw_sfr as S
    return S.direct_ops(i)


if __name__ == "__main__":
    sys.exit(main())
