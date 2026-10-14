#!/usr/bin/env python3
"""Dump the full HR2 (BCM5334x / BCM56150) SDK register and memory DB from stripped switchdrvr.

soc_reg_info_t (ELF32 LE, 0x40 bytes): +0 block-type list ptr (-1 terminated), +4 regtype, +8 numels,
+0xc offset, +0x10 flags, +0x14 nFields, +0x18 fields ptr, +0x1c/+0x20 reset value lo/hi,
+0x24/+0x28 reset mask lo/hi, +0x2c counter idx (older layout than OpenBCM 6.5 register.h: no first_array_index/flags1).
Field record (12 bytes): field id, len | bp << 16, flags.
soc_driver_t: found by the word that equals the reg_info array; fields at +0x18 reg_info,
+0x28 mem_info ptr array (index = soc_mem_t), +0x38 block_info (type, number, schan, cmic; -1 end).
soc_mem_info_t (0x40 bytes): flags, cmp_fn, null_entry, index_min, index_max, minblk | maxblk << 16,
block-row mask, w7, base, gran, bytes | nFields << 16, fields ptr. Mem block bits index block_info ROWS.
Mem names: 3 consecutive pointer tables of equal length ending at the first mem_info struct, all indexed by
soc_mem_t: [0] short alias (sparse), [1] user-facing name, [2] full SDK name (VLAN_TAB, MMU_XQ0, ...).

Usage: python3 scripts/hr2_regdb.py [BIN] [--out tmp/regdb]
Writes hr2_regs.tsv, hr2_mems.tsv, hr2_blocks.tsv. Needs pyelftools. Log: tmp/logs/hr2_regdb.log.
"""
import argparse
import logging
import os
import pathlib
import subprocess
import sys

from smbus_regdb import FNAMES, NAMES, NREGS, Elf, find_base

REGTYPES = {1: "gen", 2: "port", 4: "cos", 9: "cpureg", 0xD: "mcsreg", 0xE: "iprocreg"}
# Inferred from the reg names inside each block type (see docs/REGISTERS.md).
BLOCK_LABELS = {6: "CMIC", 9: "MMU", 14: "GPORT", 16: "IPIPE", 18: "EPIPE", 35: "OTPC",
                38: "XLPORT", 47: "TOP", 48: "SER", 100: "IPROC"}
DEFAULT_BASE = 0x01FB6628


def repo_root():
    env = os.environ.get("US48_REPO")
    if env:
        return pathlib.Path(env)
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                             cwd=pathlib.Path(__file__).resolve().parent, check=True)
        return pathlib.Path(out.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return pathlib.Path(__file__).resolve().parent.parent


class Db:
    def __init__(self, path):
        self.e = Elf(path)
        e = self.e
        self.name_of = lambda r: e.cstr(e.u32(NAMES + 4 * r))
        self.fname = lambda i: e.cstr(e.u32(FNAMES + 4 * i))
        self.base = self.locate_base()
        self.drv = self.locate_driver()
        self.blocks = self.read_blocks()
        self.mem_arr = e.u32(self.drv + 0x28)
        self.mem_n = (self.drv - self.mem_arr) // 4

    def locate_base(self):
        e = self.e
        r = next(r for r in range(NREGS) if self.name_of(r) == "CMIC_I2CM_SMBUS_BIT_BANG_CONTROL")
        i = e.u32(DEFAULT_BASE + 4 * r)
        if i and "SMBCLK_OUT_EN" in self.fields(i):
            return DEFAULT_BASE
        score, base = find_base(e, self.name_of)
        logging.info("reg_info base found by scan: %#x (score %d)", base, score)
        return base

    def locate_driver(self):
        e = self.e
        needle = self.base.to_bytes(4, "little")
        i = e.d.find(needle)
        while i >= 0:
            va = next(v + i - o for v, o, n in e.segs if o <= i < o + n)
            drv = va - 0x18
            if (e.cstr(e.u32(drv + 4) or 0) or "").startswith("hurricane"):
                logging.info("soc_driver_t at %#x chip=%s", drv, e.cstr(e.u32(drv + 4)))
                return drv
            i = e.d.find(needle, i + 1)
        raise SystemExit("soc_driver_t not found")

    def read_blocks(self):
        e = self.e
        a, rows = e.u32(self.drv + 0x38), []
        while e.u32(a) != 0xFFFFFFFF:
            rows.append(tuple(e.u32(a + 4 * j) for j in range(4)))
            a += 16
        return rows

    def fields(self, info, n=None):
        e = self.e
        fp = e.u32(info + 0x18)
        n = e.u32(info + 0x14) if n is None else n
        return [self.fname(e.u32(fp + 12 * j)) for j in range(n)]

    def field_spec(self, fp, n):
        e = self.e
        out = []
        for j in range(n):
            w = e.u32(fp + 12 * j + 4)
            out.append(f"{self.fname(e.u32(fp + 12 * j))}:{w >> 16}:{w & 0xFFFF}")
        return ",".join(out)

    def block_list(self, p):
        out = []
        while self.e.u32(p) != 0xFFFFFFFF:
            out.append(self.e.u32(p))
            p += 4
        return out

    def regs(self):
        e = self.e
        for r in range(NREGS):
            i = e.u32(self.base + 4 * r)
            if not i:
                continue
            blks = self.block_list(e.u32(i))
            n = e.u32(i + 0x14)
            yield dict(idx=r, name=self.name_of(r), regtype=e.u32(i + 4), blocks=blks, offset=e.u32(i + 0xC),
                       numels=e.u32(i + 8), flags=e.u32(i + 0x10), rst_lo=e.u32(i + 0x1C), rst_hi=e.u32(i + 0x20),
                       rmask_lo=e.u32(i + 0x24), rmask_hi=e.u32(i + 0x28), ctr_idx=e.u32(i + 0x2C), nfields=n, fields=self.field_spec(e.u32(i + 0x18), n))

    def mem_names(self):
        e = self.e
        ptrs = [e.u32(self.mem_arr + 4 * k) for k in range(self.mem_n)]
        first = min(p for p in ptrs if p)
        a = first
        while (p := e.u32(a - 4)) and 0x100000 < p < 0x1D50000 and e.off(p) is not None:
            a -= 4
        total = (first - a) // 4
        logging.info("mem name tables %#x..%#x (%d slots = 3 x %d), mem_info array %#x (%d slots)", a, first,
                     total, total // 3, self.mem_arr, self.mem_n)
        return a, total // 3

    def mems(self):
        e = self.e
        nbase, span = self.mem_names()
        nm = lambda t, k: e.cstr(e.u32(nbase + 4 * (t * span + k)))
        for k in range(self.mem_n):
            p = e.u32(self.mem_arr + 4 * k)
            if not p:
                continue
            w = [e.u32(p + 4 * j) for j in range(12)]
            nf, f = w[10] >> 16, w[11]
            rows = [b for b in range(32) if w[6] >> b & 1]
            maxbit = max(((e.u32(f + 12 * j + 4) >> 16) + (e.u32(f + 12 * j + 4) & 0xFFFF) for j in range(nf)),
                         default=0)
            yield dict(idx=k, name=nm(2, k) or f"MEM_{k}", ufname=nm(1, k), shortname=nm(0, k), flags=w[0], index_min=w[3],
                       index_max=w[4], minblk=w[5] & 0xFFFF, maxblk=w[5] >> 16, blkmask=w[6], rows=rows, w7=w[7],
                       base=w[8], gran=w[9], nbytes=w[10] & 0xFFFF, nbits=maxbit, nfields=nf,
                       fields=self.field_spec(f, nf), named=bool(nm(2, k)))

    def label(self, t):
        return BLOCK_LABELS.get(t, f"T{t}")


def tsv(path, header, rows):
    with open(path, "w") as f:
        f.write("\t".join(header) + "\n")
        for r in rows:
            f.write("\t".join(str(c) for c in r) + "\n")


def main():
    root = repo_root()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("bin", nargs="?", default=str(root / "private/firmware/rootfs-selected/bin/switchdrvr"))
    ap.add_argument("--out", default=str(root / "tmp/regdb"))
    a = ap.parse_args()
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    log = root / "tmp/logs/hr2_regdb.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.FileHandler(log, "w"), logging.StreamHandler(sys.stdout)])
    db = Db(a.bin)
    regs = list(db.regs())
    mems = list(db.mems())
    sch = {}
    for row, (t, n, s, c) in enumerate(db.blocks):
        sch.setdefault(t, []).append(s)

    tsv(out / "hr2_regs.tsv",
        ["idx", "name", "regtype", "regtype_name", "blocks", "block_labels", "schan", "offset", "numels", "flags",
         "rst_lo", "rst_hi", "rmask_lo", "rmask_hi", "ctr_idx", "nfields", "fields"],
        [(r["idx"], r["name"], r["regtype"], REGTYPES.get(r["regtype"], "?"),
          ",".join(map(str, r["blocks"])), ",".join(db.label(b) for b in r["blocks"]),
          ",".join(f"{s:#x}" for b in r["blocks"] for s in sch.get(b, [])),
          f"{r['offset']:#x}", r["numels"], f"{r['flags']:#x}", f"{r['rst_lo']:#x}", f"{r['rst_hi']:#x}",
          f"{r['rmask_lo']:#x}", f"{r['rmask_hi']:#x}", f"{r['ctr_idx']:#x}", r["nfields"], r["fields"]) for r in regs])
    tsv(out / "hr2_mems.tsv",
        ["idx", "name", "ufname", "shortname", "named", "flags", "index_min", "index_max", "bytes", "nbits", "base", "gran", "blk_rows",
         "block_labels", "schan", "nfields", "fields"],
        [(m["idx"], m["name"], m["ufname"], m["shortname"], int(m["named"]), f"{m['flags']:#x}", m["index_min"], m["index_max"], m["nbytes"],
          m["nbits"], f"{m['base']:#x}", m["gran"], ",".join(map(str, m["rows"])),
          ",".join(db.label(db.blocks[b][0]) for b in m["rows"]),
          ",".join(f"{db.blocks[b][2]:#x}" for b in m["rows"]), m["nfields"], m["fields"]) for m in mems])
    nregs_by = {}
    for r in regs:
        for b in r["blocks"]:
            nregs_by[b] = nregs_by.get(b, 0) + 1
    tsv(out / "hr2_blocks.tsv", ["row", "type", "number", "schan", "cmic", "label", "regs_of_type", "mems_on_row"],
        [(i, t, n, s, c, db.label(t), nregs_by.get(t, 0), sum(1 for m in mems if i in m["rows"]))
         for i, (t, n, s, c) in enumerate(db.blocks)])
    by_type = {}
    for r in regs:
        by_type[r["regtype"]] = by_type.get(r["regtype"], 0) + 1
    logging.info("regs %d of %d slots; by regtype %s", len(regs), NREGS, {REGTYPES.get(k, k): v for k, v in by_type.items()})
    logging.info("mems %d (%d unnamed) of %d slots; blocks %d rows", len(mems), sum(not m["named"] for m in mems),
                 db.mem_n, len(db.blocks))
    logging.info("wrote %s", out)


if __name__ == "__main__":
    main()
