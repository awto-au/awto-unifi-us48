#!/usr/bin/env python3
"""find_dtb — find and extract flattened device trees (FDT magic d00dfeed) in firmware blobs.

- Validates header: totalsize 0x40..4 MiB within file, version 16/17, struct/strings offsets inside blob.
- Extracts valid ones to tmp/dtb/<file>@<offset>.dtb and decompiles with dtc if present.
- Log: tmp/logs/find_dtb.log

Usage: find_dtb.py <file|dir> [...]
"""

import argparse
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/find_dtb.log"
OUT = ROOT / "tmp/dtb"
MAGIC = b"\xd0\x0d\xfe\xed"
MAX_SIZE = 4 * 2**20


def scan(path: Path, lines: list[str]) -> None:
    data = path.read_bytes()
    i = data.find(MAGIC)
    while i != -1:
        if i + 40 <= len(data):
            total, off_struct, off_strings, _, version = struct.unpack_from(">5I", data, i + 4)
            ok = (0x40 <= total <= MAX_SIZE and i + total <= len(data) and version in (16, 17)
                  and off_struct < total and off_strings < total)
            if ok:
                OUT.mkdir(parents=True, exist_ok=True)
                dtb = OUT / f"{path.name}@{i:x}.dtb"
                dtb.write_bytes(data[i:i + total])
                lines.append(f"DTB {path}@0x{i:x} size {total} v{version} -> {dtb.relative_to(ROOT)}")
                if shutil.which("dtc"):
                    dts = dtb.with_suffix(".dts")
                    subprocess.run(["dtc", "-I", "dtb", "-O", "dts", "-o", str(dts), str(dtb)],
                                   capture_output=True)
            else:
                lines.append(f"magic only {path}@0x{i:x} (total={total} version={version})")
        i = data.find(MAGIC, i + 1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="+", type=Path)
    a = ap.parse_args()
    lines: list[str] = []
    n = 0
    for p in a.paths:
        for f in ([p] if p.is_file() else sorted(x for x in p.rglob("*") if x.is_file() and not x.is_symlink())):
            n += 1
            scan(f, lines)
    lines.append(f"scanned {n} files")
    report = "\n".join(lines)
    print(report)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n{report}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
