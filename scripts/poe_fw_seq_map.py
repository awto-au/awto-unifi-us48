#!/usr/bin/env python3
"""Map v21 functions of the power/limit/state cluster to the Meraki v18 image (docs/poe-fw-seq/03-powergood-limits.md).

Usage: poe_fw_seq_map.py
  For each v21 entry address in ADDRS: best Meraki function by normalised-byte similarity (normalisation of poe_fw_diff).
  Output tmp/poe-fw/seq_map.tsv (v21 addr, v18 addr, size21, size18, ratio, identical); log tmp/logs/poe_fw_seq_map.log.
"""
import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import poe_fw_diff as DF  # noqa: E402

ROOT = DF.ROOT
ADDRS = [0x2EE0, 0x2F8C, 0x3B3C, 0x3FBE, 0x41D9, 0x448D, 0x44ED, 0x454A, 0x4601, 0x46AB, 0x48BE, 0x4A31, 0x4AD7, 0x4D5C,
         0x4DF5, 0x4E60, 0x4E80, 0x4E9D, 0x4EEF, 0x4F09, 0x4F23, 0x4F3D, 0x4F9F, 0x4FB7, 0x5091, 0x509E, 0x4BA2, 0x2C79,
         0x47E4, 0x1A60, 0x49BD, 0x3334, 0x4B71, 0x46FC, 0x4BD3, 0x45A7, 0x4A69, 0x4DD0, 0x4AA1, 0x497E, 0x493F, 0x4FF6,
         0x5074, 0x5055, 0x5065, 0x4ED5, 0x4C62, 0x4C33, 0x49FF, 0x5024, 0x3C1E, 0x4900, 0x4658, 0x2143, 0x4DAA, 0x5216,
         0x482F, 0x3DA6, 0x29EE]


def main():
    c1, f1, e1, h1 = DF.funcs(ROOT / "hwdb/firmware/bcm59111-v21.bin")
    c2, f2, e2, h2 = DF.funcs(ROOT / "hwdb/firmware/bcm59111-meraki.bin")
    out = ["v21\tv18\tsize21\tsize18\tratio\tidentical"]
    for a in ADDRS:
        if a not in f1:
            out.append(f"{a:04X}\t-\t-\t-\t-\tnot a v21 entry")
            continue
        n1 = f1[a][0]
        best, br = None, -1.0
        for b, v in f2.items():
            r = difflib.SequenceMatcher(None, n1, v[0], autojunk=False).quick_ratio()
            if r > br - 0.0001:
                r2 = difflib.SequenceMatcher(None, n1, v[0], autojunk=False).ratio()
                if r2 > br:
                    best, br = b, r2
        out.append(f"{a:04X}\t{best:04X}\t{len(n1)}\t{len(f2[best][0])}\t{br:.3f}\t{int(f2[best][0] == n1)}")
    txt = "\n".join(out)
    (ROOT / "tmp/poe-fw/seq_map.tsv").write_text(txt + "\n")
    Path(ROOT / "tmp/logs").mkdir(parents=True, exist_ok=True)
    (ROOT / "tmp/logs/poe_fw_seq_map.log").write_text(txt + "\n")
    print(txt)


if __name__ == "__main__":
    sys.exit(main())
