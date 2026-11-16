#!/usr/bin/env python3
"""a2_dmesg — a2 kernel log without the DCC: read the printk ring out of the a2 RAM through the us48_a2 sysfs `a2_ram` (#103).

- a1 reads 16 MiB of the a2 pool (host DDR, valid while a2 is started, also after an oops; zeroed at every start).
- The ring is found in the dump, not by System.map: struct printk_ringbuffer = {count_bits, descs, infos, head_id, tail_id, last_seq, size_bits, data, head_lpos, tail_lpos}
  with size_bits = count_bits + 5 (PRB_AVGBITS) and kernel virtual pointers; a2 RAM offset = virt - 0xc0000000 (a2 AXI base 0x08000000, kernel at +0x8000).
- Text only; text_len from the info array (stride = descs - infos / count, text_len at +16). Run while a2 is started; offline = ENODEV.
Usage: a2_dmesg.py [--tail N]   Log: tmp/logs/a2_dmesg.log
"""
import argparse
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import lx  # noqa: E402

RAM = "/sys/bus/auxiliary/devices/us48_pci.a2.0/a2_ram"
DUMP_MIB = 16
VBASE = 0xC0000000


def fetch() -> bytes:
    cmd = ["ssh", *lx.OPTS, f"root@{lx.HOST}", f"dd if={RAM} bs=4096 count={DUMP_MIB * 256} 2>/dev/null"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode or len(r.stdout) < DUMP_MIB << 20:
        sys.exit(f"a2_ram read failed rc={r.returncode} got {len(r.stdout)} B: {r.stderr[-200:]!r}")
    return r.stdout


def find_ring(m: bytes) -> int:
    n = len(m)
    for off in range(0, n - 40, 4):
        cb, dp, ip, hid, tid, ls, sb, dat, hl, tl = struct.unpack_from("<10I", m, off)
        if not (8 <= cb <= 16 and sb == cb + 5):
            continue
        if not all(VBASE <= p < VBASE + n and p % 4 == 0 for p in (dp, ip, dat)):
            continue
        return off
    sys.exit("printk ring not found in the dump (a2 never reached printk, or not started)")


def records(m: bytes) -> list[str]:
    off = find_ring(m)
    cb, dp, ip, hid, tid, ls, sb, dat, hl, tl = struct.unpack_from("<10I", m, off)
    count, size = 1 << cb, 1 << sb
    dpo, dato = dp - VBASE, dat - VBASE
    stride = (dp - ip) // count  # printk_info size (infos precede descs in the static ring); text_len = u16 at +16
    if stride < 24 or ip + stride * count > dp:
        sys.exit(f"info array layout unexpected: infos {ip:#x} descs {dp:#x} count {count}")
    ipo = ip - VBASE
    out = []
    i = tid
    while True:
        sv, begin, nxt = struct.unpack_from("<3I", m, dpo + 12 * (i & (count - 1)))
        state = sv >> 30
        if state in (1, 2) and (sv & 0x3FFFFFFF) == (i & 0x3FFFFFFF) and begin != nxt:
            b, e = begin & (size - 1), nxt & (size - 1)
            blk = m[dato + b + 4: dato + e] if e > b else m[dato + b + 4: dato + size] + m[dato: dato + e]
            tl_ = struct.unpack_from("<H", m, ipo + stride * (i & (count - 1)) + 16)[0]
            out.append(blk[:tl_].decode("utf-8", "replace"))
        if i == hid:
            break
        i = (i + 1) & 0xFFFFFFFF
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tail", type=int, default=0)
    a = ap.parse_args()
    recs = records(fetch())
    if a.tail:
        recs = recs[-a.tail:]
    txt = "\n".join(recs)
    print(txt)
    log = ROOT / "tmp/logs/a2_dmesg.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {len(recs)} records\n{txt}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
