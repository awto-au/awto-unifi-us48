#!/usr/bin/env python3
"""uboot1_emu_regcheck: every MMIO address in a uboot1_emu.py trace vs hwdb (hand + SDK regdb) (#69, #72).

Usage: scripts/uboot1_emu_regcheck.py [tmp/uboot1_emu.tsv]
- Classes per distinct address: reg (named register), block (inside a hand block, offset not named),
  region (inside a memory-map region only), none (undocumented).
- Writes tmp/uboot1_emu_regcheck.tsv (one row per address) and logs a summary to tmp/logs/uboot1_emu_regcheck.log.
"""
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hwdb  # noqa: E402
from _cli import help_guard  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/uboot1_emu_regcheck.log"
OUT = ROOT / "tmp/uboot1_emu_regcheck.tsv"
# DRAM, SRAM, flash XIP and boot ROM are memory, not registers
MEMORY = [(0x00000000, 0x08000000), (0x60000000, 0x70000000), (0x1b000000, 0x1b010000),
          (0x1c000000, 0x1e000000), (0xffff0000, 0x100000000)]


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "tmp/uboot1_emu.tsv"
    db = hwdb.load()
    acc = {}
    with src.open() as f:
        next(f)
        for ln in f:
            p = ln.rstrip("\n").split("\t")
            a = int(p[4], 16)
            if any(lo <= a < hi for lo, hi in MEMORY):
                continue
            e = acc.setdefault(a, {"R": 0, "W": 0, "pc": p[1], "func": p[2]})
            e[p[3]] = e.get(p[3], 0) + 1
    rows, cls = [], collections.Counter()
    for a in sorted(acc):
        hits = db.find(a)
        regs = [n for k, n, o in hits if k == "reg"]
        blocks = [f"{n}+0x{o:x}" for k, n, o in hits if k == "block"]
        regions = [n for k, n, o in hits if k == "region"]
        c = "reg" if regs else "block" if blocks else "region" if regions else "none"
        cls[c] += 1
        e = acc[a]
        rows.append(f"0x{a:08x}\t{c}\t{e['R']}\t{e['W']}\t{e['pc']}\t{e['func']}\t"
                    f"{','.join(regs) or ','.join(blocks) or ','.join(regions)}")
    OUT.write_text("addr\tclass\treads\twrites\tfirst_pc\tfunc\tname\n" + "\n".join(rows) + "\n")
    msg = [f"{src}: {len(acc)} distinct MMIO addresses: " + ", ".join(f"{k} {v}" for k, v in cls.most_common())]
    msg += [r for r in rows if r.split("\t")[1] in ("none", "region", "block")]
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("\n".join(msg) + "\n")
    print("\n".join(msg))
    return 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
