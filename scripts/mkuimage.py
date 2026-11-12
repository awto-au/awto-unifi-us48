#!/usr/bin/env python3
"""Wrap a raw binary as a legacy uImage (ARM, Linux, kernel, uncompressed). Pure Python, no mkimage.

  mkuimage.py u-boot-dtb.bin out.img --load 0x04000000 --name us48-uboot

Stock U-Boot `bootm` checks magic + header CRC + data CRC only (docs/uboot-second-stage.md).
"""

import argparse
import struct
import sys
import time
import zlib
from pathlib import Path

IH_MAGIC = 0x27051956
IH_OS_LINUX, IH_ARCH_ARM, IH_TYPE_KERNEL, IH_COMP_NONE = 5, 2, 2, 0


def pack(data: bytes, load: int, entry: int, name: str) -> bytes:
    dcrc = zlib.crc32(data) & 0xFFFFFFFF
    nm = name.encode()[:31].ljust(32, b"\0")

    def hdr(hcrc: int) -> bytes:
        return struct.pack(">IIIIIIIBBBB32s", IH_MAGIC, hcrc, int(time.time()), len(data), load, entry, dcrc,
                           IH_OS_LINUX, IH_ARCH_ARM, IH_TYPE_KERNEL, IH_COMP_NONE, nm)

    return hdr(zlib.crc32(hdr(0)) & 0xFFFFFFFF) + data


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image")
    ap.add_argument("out")
    ap.add_argument("--name", default="linux")
    ap.add_argument("--load", type=lambda x: int(x, 0), default=0x04000000)
    ap.add_argument("--entry", type=lambda x: int(x, 0), default=None)
    a = ap.parse_args()
    entry = a.load if a.entry is None else a.entry
    raw = Path(a.image).read_bytes()
    out = pack(raw, a.load, entry, a.name)
    Path(a.out).write_bytes(out)
    print(f"{a.out}: {len(out)} bytes (raw {len(raw)}), load=0x{a.load:08x} entry=0x{entry:08x}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
