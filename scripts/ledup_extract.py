#!/usr/bin/env python3
"""ledup_extract: dump the LEDUP programs stock switchdrvr loads (docs/ledup-stock.md, #14).

Usage: ledup_extract.py [--elf PATH] [--va 0x15a38ac] [--len 256] [--out FILE]
- Maps VA to file offset via the ELF PT_LOAD headers (read-only; the binary is never modified).
- Prints the bytes as hex, the six 256-byte program slots of the board descriptors, and the used length.
- --out writes the raw bytes. Log: tmp/logs/ledup_extract.log
"""
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/ledup_extract.log"
ELF = Path("/mnt/2tb/git/awto-unifi-us48/tmp/initramfs-full/bin/switchdrvr")
SLOTS = (0x15A38AC, 0x15A39AC, 0x15A3AAC, 0x15A3BAC, 0x15A3CAC, 0x15A3DAC)


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}\n")
    print(msg)


def load_segments(data: bytes) -> list[tuple[int, int, int]]:
    phoff, = struct.unpack_from("<I", data, 0x1C)
    phentsize, phnum = struct.unpack_from("<HH", data, 0x2A)
    segs = []
    for i in range(phnum):
        p_type, off, va, _pa, filesz, _memsz, _fl, _al = struct.unpack_from("<8I", data, phoff + i * phentsize)
        if p_type == 1:
            segs.append((va, off, filesz))
    return segs


def va_to_off(segs, va: int) -> int:
    for sva, off, sz in segs:
        if sva <= va < sva + sz:
            return va - sva + off
    raise SystemExit(f"VA {va:#x} not in any PT_LOAD")


def used_len(b: bytes) -> int:
    n = len(b)
    while n and b[n - 1] == 0:
        n -= 1
    return n


def main() -> int:
    help_guard(__doc__, strict=True, known=("--elf", "--va", "--len", "--out"))
    a = sys.argv[1:]
    elf = Path(a[a.index("--elf") + 1]) if "--elf" in a else ELF
    va = int(a[a.index("--va") + 1], 0) if "--va" in a else SLOTS[0]
    n = int(a[a.index("--len") + 1], 0) if "--len" in a else 256
    data = elf.read_bytes()
    segs = load_segments(data)
    for s in SLOTS:
        off = va_to_off(segs, s)
        blob = data[off:off + 256]
        log(f"slot VA {s:#x} file offset {off:#x} used {used_len(blob)} B: {blob[:12].hex(' ')} ...")
    off = va_to_off(segs, va)
    blob = data[off:off + n]
    log(f"program VA {va:#x} -> file offset {off:#x}, {len(blob)} B, used {used_len(blob)} B")
    for i in range(0, len(blob), 16):
        log(f"{i:02x}: {blob[i:i + 16].hex(' ')}")
    if "--out" in a:
        Path(a[a.index("--out") + 1]).write_bytes(blob)
    return 0


if __name__ == "__main__":
    sys.exit(main())
