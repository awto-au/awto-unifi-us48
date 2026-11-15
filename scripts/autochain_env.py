#!/usr/bin/env python3
"""autochain_env — build the uboot-1 env image for the awto-uboot auto-chain (#63, docs/autochain.md).

  scripts/autochain_env.py --base private/backups/<ts>/mtd1-u-boot-env.bin --size 842736 [--hz 50000000] [--xip]
  scripts/autochain_env.py --restore --base <backup mtd1>     (image = base unchanged, for recovery)

- Layout (C, docs/uboot.md): u32 LE CRC32 over bytes 4..65535, then NUL-separated vars, 0xff fill. No redundant flag.
- Changes only `bootdelay` and `bootcmd`; every other var and the tail bytes stay as in the base.
- Output: tmp/autochain/env.bin (64 KiB) + diff on stdout. Log: tmp/logs/autochain_env.log
"""
import argparse
import struct
import sys
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOG = REPO / "tmp/logs/autochain_env.log"
ENV_SIZE = 0x10000
FLASH_OFF = 0xC0000
KERNEL0_HEAD = 0xE0000
XIP_BASE = 0x1C000000  # BSPI window, docs/uboot.md addresses


def log(msg: str) -> None:
    print(msg)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(msg + "\n")


def parse(img: bytes) -> tuple[list[tuple[str, str]], int]:
    """Vars in order + offset of the last var's NUL terminator. Raises on bad size or CRC."""
    if len(img) != ENV_SIZE:
        raise ValueError(f"env image {len(img)} B, want {ENV_SIZE}")
    want = struct.unpack("<I", img[:4])[0]
    got = zlib.crc32(img[4:]) & 0xFFFFFFFF
    if want != got:
        raise ValueError(f"bad CRC: stored {want:08x}, computed {got:08x}")
    out, pos = [], 4
    while img[pos] != 0:
        end = img.index(b"\0", pos)
        k, _, v = img[pos:end].decode().partition("=")
        out.append((k, v))
        pos = end + 1
    return out, pos


def build(base: bytes, bootcmd: str, bootdelay: str) -> bytes:
    kv, tail_at = parse(base)
    new = {"bootcmd": bootcmd, "bootdelay": bootdelay}
    body = b"".join(f"{k}={new.get(k, v)}".encode() + b"\0" for k, v in kv)
    assert {k for k, _ in kv} >= set(new), "base lacks bootcmd/bootdelay"
    if dict(kv).get("ubntappinit", "") == "":
        raise ValueError("ubntappinit empty in base: would boot-loop (docs/uboot.md)")
    data = body + base[tail_at:]  # tail = final NUL(s) + 0xff fill, as in the base
    data = data[: ENV_SIZE - 4].ljust(ENV_SIZE - 4, b"\xff")
    return struct.pack("<I", zlib.crc32(data) & 0xFFFFFFFF) + data


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True, type=Path)
    ap.add_argument("--size", type=lambda x: int(x, 0), help="exact uImage size incl. 64 B header")
    ap.add_argument("--hz", type=int, default=0, help="sf probe 0 <hz>; 0 = no hz argument")
    ap.add_argument("--xip", action="store_true", help="bootm straight from the BSPI window instead of sf read")
    ap.add_argument("--restore", action="store_true", help="output the base unchanged")
    ap.add_argument("--out", type=Path, default=REPO / "tmp/autochain/env.bin")
    a = ap.parse_args()
    base = a.base.read_bytes()
    kv, _ = parse(base)
    if a.restore:
        img = base
    else:
        probe = f"sf probe 0 {a.hz}" if a.hz else "sf probe 0"
        if a.xip:
            cmd = f"bootm 0x{XIP_BASE + KERNEL0_HEAD:x}"
        else:
            if a.size is None:
                ap.error("--size required unless --xip/--restore")
            cmd = f"{probe}; sf read 0x01000000 0x{KERNEL0_HEAD:x} 0x{a.size:x}; bootm 0x01000000"
        img = build(base, cmd, "0")
    nkv, _ = parse(img)
    log(f"base {a.base}  crc {struct.unpack('<I', base[:4])[0]:08x}  vars {len(kv)}")
    old, new = dict(kv), dict(nkv)
    for k in list(old) + [k for k in new if k not in old]:
        if old.get(k) != new.get(k):
            log(f"  - {k}={old.get(k)}\n  + {k}={new.get(k)}")
    same = [k for k in old if old[k] == new.get(k)]
    log(f"unchanged vars: {len(same)}; var order same: {[k for k, _ in kv] == [k for k, _ in nkv]}")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_bytes(img)
    log(f"wrote {a.out}  crc32(file) {zlib.crc32(img) & 0xFFFFFFFF:08x}  new env crc {struct.unpack('<I', img[:4])[0]:08x}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
