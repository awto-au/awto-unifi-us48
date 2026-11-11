#!/usr/bin/python3
"""flash_regions — map used vs erased (0xff) regions of a flash dump, per partition.

Usage: scripts/flash_regions.py <full-flash.bin> [--gran 0x1000]
- Granule = 4 KiB (QSPI erase sector): a granule is 'used' if any byte is not 0xff.
- Partitions = stock mtdparts (docs/board.md / the US-48 DTS). Log: tmp/logs/flash_regions.log
"""
import argparse
import sys
import time
from pathlib import Path

LOG = Path(__file__).resolve().parent.parent / "tmp/logs/flash_regions.log"
PARTS = [("u-boot", 0x0, 0xC0000), ("u-boot-env", 0xC0000, 0x10000), ("shmoo", 0xD0000, 0x10000),
         ("kernel0", 0xE0000, 0xF00000), ("kernel1", 0xFE0000, 0xF10000), ("cfg", 0x1EF0000, 0x100000),
         ("EEPROM", 0x1FF0000, 0x10000)]


def runs(data: bytes, base: int, gran: int):
    """Yield (start, end) of contiguous used granules."""
    start = None
    for off in range(0, len(data), gran):
        used = data[off:off + gran].count(0xFF) != len(data[off:off + gran])
        if used and start is None:
            start = off
        elif not used and start is not None:
            yield base + start, base + off
            start = None
    if start is not None:
        yield base + start, base + len(data)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("image", type=Path)
    ap.add_argument("--gran", type=lambda x: int(x, 0), default=0x1000)
    a = ap.parse_args()
    img = a.image.read_bytes()
    out = [f"{a.image.name}: {len(img)} B, granule 0x{a.gran:x}"]
    for name, off, size in PARTS:
        part = img[off:off + size]
        used = sum(e - s for s, e in runs(part, off, a.gran))
        out.append(f"{name:10s} 0x{off:07x}+0x{size:06x}: used {used:#x} ({100 * used // size}%)")
        for s, e in runs(part, off, a.gran):
            out.append(f"    0x{s:07x}..0x{e:07x} ({e - s:#x})")
    text = "\n".join(out)
    print(text)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%S%z ") + text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
