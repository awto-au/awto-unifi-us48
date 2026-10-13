#!/usr/bin/env python3
"""Compare BCM59111/BCM59121 PSE firmware blobs across stock binaries and C headers (hashes only, no copy).

Usage:
  python3 scripts/poe_fw_compare.py --elf UNIFI_SWITCHDRVR:0x1e7a390 [--elf ESWH_SWITCHDRVR] [--hdr FILE.h ...]
  --elf PATH[:VA[@LENVA],...]  VA of the u32 length word before the blob, or blob VA@VA of its length word;
                   always also searches PATH for the --hdr blobs
  --hdr PATH       C array header (BIN2C / "ARRAY file" style); hex bytes after the first '{'
Logs to tmp/logs/poe_fw_compare.log.
"""
import argparse
import hashlib
import logging
import pathlib
import re
import struct
import sys

LOG = pathlib.Path("tmp/logs/poe_fw_compare.log")


def elf_segs(d):
    phoff, = struct.unpack_from("<I", d, 0x1C)
    phentsize, phnum = struct.unpack_from("<HH", d, 0x2A)
    segs = []
    for i in range(phnum):
        t, off, va, _pa, fsz, _msz, _fl, _al = struct.unpack_from("<8I", d, phoff + i * phentsize)
        if t == 1:
            segs.append((va, off, fsz))
    return segs


def va2off(segs, va):
    for sva, off, fsz in segs:
        if sva <= va < sva + fsz:
            return off + va - sva
    return None


def off2va(segs, o):
    for sva, off, fsz in segs:
        if off <= o < off + fsz:
            return sva + o - off
    return None


def hdr_blob(path):
    txt = pathlib.Path(path).read_text(errors="replace")
    body = txt[txt.index("{") + 1:]
    body = re.sub(r"//[^\n]*", "", body)
    return bytes(int(x, 16) for x in re.findall(r"0x([0-9A-Fa-f]{2})\b", body.split("}")[0]))


def info(name, b):
    nz = len(b.rstrip(b"\xff").rstrip(b"\x00"))
    logging.info("%-40s len %5d (0x%04x) used ~%5d sha256 %s", name, len(b), len(b), nz, hashlib.sha256(b).hexdigest())


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--elf", action="append", default=[])
    ap.add_argument("--hdr", action="append", default=[])
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.FileHandler(LOG, mode="w"), logging.StreamHandler(sys.stdout)])
    hdrs = {}
    for h in a.hdr:
        b = hdr_blob(h)
        hdrs[pathlib.Path(h).name] = b
        info(pathlib.Path(h).name, b)
    for spec in a.elf:
        path, _, va = spec.partition(":")
        d = pathlib.Path(path).read_bytes()
        segs = elf_segs(d)
        name = pathlib.Path(path).parent.parent.name + "/" + pathlib.Path(path).name
        for one in filter(None, va.split(",")):
            blob_va, _, len_va = one.partition("@")
            o = va2off(segs, int(blob_va, 0))
            if len_va:  # blob VA with separate length-word VA
                n, = struct.unpack_from("<I", d, va2off(segs, int(len_va, 0)))
                b, at = d[o:o + n], int(blob_va, 0)
            else:  # length word directly before the blob
                n, = struct.unpack_from("<I", d, o)
                b, at = d[o + 4:o + 4 + n], int(blob_va, 0) + 4
            info(f"{name}@{at:#x}", b)
            logging.info("  feature string: %s", re.findall(rb"POETEC_IMAGE[A-Z_+]*", b))
            for hn, hb in hdrs.items():
                logging.info("  == %s: %s; first diff at %s", hn, b == hb,
                             next((i for i in range(min(len(b), len(hb))) if b[i] != hb[i]), None))
        for hn, hb in hdrs.items():
            o = d.find(hb[:64])
            while o >= 0:
                b = d[o:o + len(hb)]
                logging.info("  %s: %s prefix at off %#x VA %#x, full match %s, len word before %#x", name, hn, o,
                             off2va(segs, o) or 0, b == hb, struct.unpack_from("<I", d, o - 4)[0])
                o = d.find(hb[:64], o + 1)


main()
