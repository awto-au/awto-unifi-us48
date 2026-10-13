#!/usr/bin/env python3
"""Decode the per-board table in stock ubnthal.ko (UniFi switch 7.x firmware).

Layout (from unifi_get_board / ubnthal_get_* in ubnthal.ko .text):
  table = .rodata+0x4e8, stride 0x66d8, terminator .rodata+0xb3fa0
  +0x0000 char name[64]         +0x0040 char platform[..]
  +0x0076 u8  unit_count        +0x0078 unit[unit_count], stride 0x548:
        +0x000 u8 nports;  port[i] @ +0x004 + 24*i: u8 phy, u8 log, .., u32 caps @ +0x08
        +0x547 u8 cpu_port (ubnthal_get_switch_cpu_port)
  +0x3000 oob block (u8 present, +4 u8 enabled, +9 u8 port)
  +0x6454 u16 system id         +0x6464 u8 fan count
  +0x64b8 u16 max PoE power (W) +0x64ba u16 reserved PoE power
  +0x64d1 u8 lcm count          +0x64d2 u8 led count   +0x64d6 char led_prefix[]
Usage: python3 scripts/ubnthal_board.py private/.../ubnthal.ko [--id 0xeb62] [--hexdump]
Logs to tmp/logs/ubnthal_board.log.
"""
import argparse
import logging
import pathlib
import struct
import sys

from elftools.elf.elffile import ELFFile

LOG = pathlib.Path("tmp/logs/ubnthal_board.log")
TABLE, STRIDE, END = 0x4E8, 0x66D8, 0xB3FA0
UNIT, USTRIDE, PSTRIDE = 0x78, 0x548, 24
POE_CAP_MASK = 0x17A00  # ubnthal_get_num_poe_ports


def cstr(b):
    return b.split(b"\0", 1)[0].decode("latin1")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("ko")
    ap.add_argument("--id", type=lambda x: int(x, 0), default=0xEB62)
    ap.add_argument("--hexdump", action="store_true", help="dump non-zero 16-byte rows outside the port arrays")
    ap.add_argument("--list", action="store_true", help="list all boards")
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.FileHandler(LOG, mode="w"), logging.StreamHandler(sys.stdout)])
    log = logging.info
    with open(a.ko, "rb") as f:
        ro = ELFFile(f).get_section_by_name(".rodata").data()

    ent = None
    for off in range(TABLE, END, STRIDE):
        e = ro[off:off + STRIDE]
        sid = struct.unpack_from("<H", e, 0x6454)[0]
        if a.list:
            log(f"{sid:#06x} {cstr(e[0:64]):24s} {cstr(e[0x40:0x60]):10s} units={e[0x76]} "
                f"fans={e[0x6464]} poe={struct.unpack_from('<H', e, 0x64b8)[0]}W")
        if sid == a.id:
            ent, ent_off = e, off
    if ent is None:
        log(f"board {a.id:#x} not found")
        return 1
    e = ent
    u16 = lambda o: struct.unpack_from("<H", e, o)[0]
    log(f"entry @ .rodata+{ent_off:#x}: {cstr(e[0:64])} / {cstr(e[0x40:0x60])} id={u16(0x6454):#x}")
    log(f"units={e[0x76]} fans={e[0x6464]} max_poe={u16(0x64b8)}W reserved={u16(0x64ba)}W "
        f"lcm={e[0x64d1]} leds={e[0x64d2]} led_prefix={cstr(e[0x64d6:0x64f6])!r}")
    log(f"oob: present={e[0x3000]} en={e[0x3004]} port={e[0x3009]}")
    poe = 0
    for u in range(e[0x76]):
        ub = UNIT + u * USTRIDE
        n = e[ub]
        log(f"unit {u}: nports={n} cpu_port={e[ub + 0x547]} hdr={e[ub:ub + 4].hex()}")
        for i in range(n):
            p = ub + 4 + i * PSTRIDE
            raw = e[p:p + PSTRIDE]
            caps = struct.unpack_from("<I", raw, 4)[0]
            poe += bool(caps & POE_CAP_MASK)
            log(f"  phy {raw[0]:3d} -> log {raw[1]:3d}  caps={caps:#010x}  raw={raw.hex()}")
    log(f"PoE-capable ports (caps & {POE_CAP_MASK:#x}): {poe}")
    if a.hexdump:
        skip = [(UNIT + u * USTRIDE, UNIT + (u + 1) * USTRIDE) for u in range(e[0x76])]
        for o in range(0, STRIDE, 16):
            if any(s <= o < t for s, t in skip):
                continue
            row = e[o:o + 16]
            if any(row):
                log(f"  +{o:#06x}: {row.hex(' ')}  {''.join(chr(c) if 32 <= c < 127 else '.' for c in row)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
