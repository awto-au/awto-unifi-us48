#!/usr/bin/env python3
"""ghidra_func_regs: which functions of a Ghidra decompile export touch device registers.

- Input: tmp/ghidra/out/<name>/<name>.bin.c from scripts/ghidra_export.py / ExportAll.java (decompiled text, not committed).
- Finds `DAT_/Ram_<8 hex>` constants in 0x18000000..0x1bffffff and 0x48010000..0x4801ffff, plus Ghidra labels made by
  hwdb/gen/ghidra_import_hwdb.py (BLOCK_REG, STRUCT.FIELD), and maps them to hwdb register names.
- Output TSV tmp/ghidra/out/<name>/regs.tsv: addr, name, regs ("block.REG", or the block name when the address is not a known register, comma list).
- Usage: ghidra_func_regs.py <name> [--summary]
- Log: tmp/logs/ghidra_func_regs.log
"""
import logging
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import hwdb  # noqa: E402

HEAD = re.compile(r"^// ==== (\S+) @ ([0-9a-f]+) ====$", re.M)
RAW = re.compile(r"(?:Ram|DAT|_DAT|UNK)_([0-9a-f]{8})\b")
LBL = re.compile(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)(?:\.([A-Z][A-Z0-9_]*))?")


def dev(a):
    return 0x18000000 <= a < 0x1c000000 and not 0x1b000000 <= a < 0x1c000000 or 0x48010000 <= a < 0x48020000


def build_labels(db):
    lab, first = {}, {}
    for b in db.hand_blocks():
        if b.space != "axi0":
            continue
        regs = sorted(b.regs.values(), key=lambda g: g.offset)
        for g in regs:
            lab["%s_%s" % (b.name.upper(), g.name)] = (b, g.name)
        if regs:
            first["%s_%s" % (b.name.upper(), regs[0].name)] = b
    return lab, first


def regname(db, a):
    hits = [(k, n) for k, n, _ in db.find(a) if k == "reg" and not n.startswith("sdk_")]
    if hits:
        return hits[0][1]
    for k, n, _off in db.find(a):
        if k == "block" and not n.startswith("sdk_"):
            return n  # block only: hwdb func regs accept block.REG or a block name
    return None


def scan(text, db, lab, first):
    out = set()
    for m in RAW.finditer(text):
        a = int(m.group(1), 16)
        if dev(a):
            r = regname(db, a)
            if r:
                out.add(r)
    for m in LBL.finditer(text):
        l, fld = m.group(1), m.group(2)
        if fld and l in first and fld in first[l].regs:
            out.add("%s.%s" % (first[l].name, fld))
        elif l in lab:
            out.add("%s.%s" % (lab[l][0].name, lab[l][1]))
    return sorted(out)


def main():
    name = sys.argv[1]
    d = ROOT / "tmp/ghidra/out" / name
    text = (d / (name + ".bin.c")).read_text()
    log = ROOT / "tmp/logs/ghidra_func_regs.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=[logging.FileHandler(log), logging.StreamHandler()])
    db = hwdb.default()
    lab, first = build_labels(db)
    parts = HEAD.split(text)
    rows = []
    for i in range(1, len(parts), 3):
        fn, addr, body = parts[i], parts[i + 1], parts[i + 2]
        r = scan(body, db, lab, first)
        if r:
            rows.append((addr, fn, r))
    with open(d / "regs.tsv", "w") as f:
        for a, n, r in rows:
            f.write("%s\t%s\t%s\n" % (a, n, ",".join(r)))
    logging.info("%s: %d functions touch registers (of %d) -> %s", name, len(rows), (len(parts) - 1) // 3, d / "regs.tsv")


if __name__ == "__main__":
    main()
