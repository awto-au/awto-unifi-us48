#!/usr/bin/env python3
"""func_names_from_strings — propose names for stripped functions from __func__-style log strings.

- Input: Ghidra export <prog>.strings.tsv (addr, string, xrefs "FUN_x@addr,...") from scripts/ghidra_export.py.
- A string that looks like a C identifier and is referenced by exactly one function names that function
  (typical `printf("%s: ...", __func__)`). A function with several such strings is marked ambiguous.
- Output: <outdir>/<prog>.names.tsv (FUN_addr, name, confidence unique|ambiguous, candidates).
- Log: tmp/logs/func_names_from_strings.log

Usage: func_names_from_strings.py tmp/ghidra/out/switchdrvr/switchdrvr.strings.tsv [--min-len 4]
"""

import argparse
import collections
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/func_names_from_strings.log"
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def looks_like_func(s: str, min_len: int) -> bool:
    if len(s) < min_len or not IDENT.fullmatch(s) or s.isupper():
        return False
    return "_" in s or any(c.isupper() for c in s[1:])  # snake_case or camelCase, not a plain word


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("strings_tsv", type=Path)
    ap.add_argument("--min-len", type=int, default=4)
    a = ap.parse_args()
    cands = collections.defaultdict(set)  # FUN -> {name}
    for line in a.strings_tsv.read_text(errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or not looks_like_func(parts[1], a.min_len):
            continue
        funcs = {r.split("@")[0] for r in parts[2].split(",") if r.startswith("FUN_")}
        if len(funcs) == 1:
            cands[funcs.pop()].add(parts[1])
    out = a.strings_tsv.with_name(a.strings_tsv.name.replace(".strings.tsv", ".names.tsv"))
    rows, uniq = [], 0
    for fun, names in sorted(cands.items()):
        conf = "unique" if len(names) == 1 else "ambiguous"
        uniq += conf == "unique"
        rows.append(f"{fun}\t{sorted(names)[0] if conf == 'unique' else ''}\t{conf}\t{','.join(sorted(names))}")
    out.write_text("\n".join(rows) + "\n")
    msg = f"{a.strings_tsv.name}: {len(cands)} functions with candidates, {uniq} unique -> {out.relative_to(ROOT)}"
    print(msg)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
