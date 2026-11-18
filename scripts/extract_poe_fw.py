#!/usr/bin/env python3
"""Cut the BCM59111 PSE firmware (16 KiB) out of the stock switchdrvr into hwdb/firmware/bcm59111-v21.bin.

Usage: extract_poe_fw.py [SWITCHDRVR] [--out PATH]
  SWITCHDRVR default tmp/initramfs-full/bin/switchdrvr (stock 7.5.15). Blob VA 0x1e7a394, u32 length word at 0x1e7a390 (docs/poe.md 4).
  Checks length 0x4000 and sha256 31496a90...bdd889 (docs/board.md); refuses to write otherwise.
Logs to tmp/logs/extract_poe_fw.log.
"""
import hashlib
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LEN_VA, BLOB_VA, SIZE = 0x1E7A390, 0x1E7A394, 0x4000
SHA = "31496a90bead4c7feb87994ad30d626b70d07e112d79d60b587bf6b35ebdd889"


def va_to_off(d: bytes, va: int) -> int:
    phoff, = struct.unpack_from("<I", d, 0x1C)
    entsz, num = struct.unpack_from("<HH", d, 0x2A)
    for i in range(num):
        t, off, pva, _pa, fsz, _msz, _fl, _al = struct.unpack_from("<8I", d, phoff + i * entsz)
        if t == 1 and pva <= va < pva + fsz:
            return off + va - pva
    raise SystemExit(f"VA {va:#x} not in a file-backed segment")


def main() -> int:
    help_guard(__doc__, strict=True, known=("--out",))
    args = sys.argv[1:]
    out = ROOT / "hwdb/firmware/bcm59111-v21.bin"
    if "--out" in args:
        i = args.index("--out")
        out = Path(args[i + 1])
        del args[i:i + 2]
    src = Path(args[0]) if args else ROOT / "tmp/initramfs-full/bin/switchdrvr"
    d = src.read_bytes()
    n, = struct.unpack_from("<I", d, va_to_off(d, LEN_VA))
    blob = d[va_to_off(d, BLOB_VA):][:SIZE]
    sha = hashlib.sha256(blob).hexdigest()
    msg = f"{src}: length word {n:#x}, sha256 {sha}"
    (ROOT / "tmp/logs").mkdir(parents=True, exist_ok=True)
    with (ROOT / "tmp/logs/extract_poe_fw.log").open("a") as f:
        f.write(msg + "\n")
    print(msg)
    if n != SIZE or len(blob) != SIZE or sha != SHA:
        print("mismatch: nothing written", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(blob)
    print(f"wrote {out} ({SIZE} B)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
