#!/usr/bin/env python3
"""sfp_eeprom_fix: repair the A0h EEPROM of the space-padded 'CISCO-FINISAR GLC-SX-MM' clones in a US-48 SFP cage (#143).

Usage: scripts/sfp_eeprom_fix.py --front 51 [--apply]
- Dry run unless --apply (Dan 2026-11-21: these clones may be written). Only for CISCO-FINISAR / GLC-SX-MM with the known padded layout.
- Bus: the sfp-<front> DT node's i2c-bus phandle matched against the adapters' of_node phandle (no bus scan), address 0x50.
- Fixes: connector 0x03 -> 0x07 (LC); extended compliance byte 36 0x01 -> 0x00; 0x20 padding in the numeric bytes 3-5, 7-10, 13-19, 36-39, 64-67 -> 0x00; encoding byte 11 = 0x01 (8B/10B),
  BR nominal byte 12 = 0x0d (1.3 GBd); CC_BASE (63) recomputed. Byte 6 (1000BASE-SX), ASCII fields, CC_EXT/bytes 64-95 serial/date untouched
  except 64-65 (CC_EXT recomputed too). Link lengths stay 0 (unspecified).
- Safety: refuses if the module is not the expected one or checksums are wrong before; full 256 B backup to tmp/sfp-eeprom/<SN>-before.txt;
  first change = connector byte, read back before anything else; every byte read back; CC_BASE/CC_EXT last; on a failed read-back it
  restores the backup bytes and stops. The sfp layer re-reads at insert only: reseat or reload to see the result.
Log: tmp/logs/sfp_eeprom_fix.log. Board address from _ipreg (US48_IP_US48_BOOT override).
"""
import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _ipreg  # noqa: E402
from _cli import help_guard  # noqa: E402
from lx import OPTS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/sfp_eeprom_fix.log"
BACKUP = ROOT / "tmp/sfp-eeprom"
ADDR = "0x50"
PAD_RANGES = [(3, 6), (7, 11), (13, 20), (36, 40), (64, 68)]  # [lo, hi) numeric fields (64-65 options, 66-67 BR max/min) padded with 0x20 (byte 6 is real)


def sh(cmd):
    # 30 s: ssh round trip is 1-2 s; a stuck i2c transfer = board path gone.
    r = subprocess.run(["ssh", *OPTS, f"root@{_ipreg.ip('us48-boot')}", cmd], capture_output=True, text=True, timeout=30)
    return r.stdout + r.stderr


def bus_of(front):
    ph = sh(f"od -An -tx1 /proc/device-tree/sfp-{front}/i2c-bus").split()
    out = sh("for d in /sys/class/i2c-dev/i2c-*; do echo ${d##*/} $(od -An -tx1 $d/device/of_node/phandle 2>/dev/null); done")
    hits = [l.split()[0] for l in out.splitlines() if l.split()[1:] == ph]
    if len(hits) != 1:
        raise SystemExit(f"cannot map sfp-{front} i2c-bus {ph} to one adapter: {hits}")
    return hits[0].split("-")[1]


def dump(bus):
    out = sh(f"i2cdump -y -r 0-255 {bus} {ADDR} b 2>&1")
    rows = re.findall(r"^([0-9a-f]{2}): ((?:[0-9a-f]{2} ){16})", out, re.M)
    data = bytearray()
    for _, hexs in rows:
        data += bytes.fromhex(hexs.replace(" ", ""))
    if len(data) != 256:
        raise SystemExit(f"short dump {len(data)} B: {out[:200]}")
    return bytes(data), out


def cc(buf, lo, hi):
    return sum(buf[lo:hi]) & 0xff


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--front", type=int, required=True, choices=(49, 50, 51, 52))
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if a.front == 49:
        raise SystemExit("refusing xe49 (management path)")
    LOG.parent.mkdir(parents=True, exist_ok=True)
    BACKUP.mkdir(parents=True, exist_ok=True)
    bus = bus_of(a.front)
    data, raw = dump(bus)
    vendor, pn, sn = (data[20:36].decode("ascii", "replace"), data[40:56].decode("ascii", "replace"),
                      data[68:84].decode("ascii", "replace").strip())
    print(f"sfp-{a.front} -> i2c-{bus}: {vendor.strip()} {pn.strip()} sn {sn}")
    if vendor.strip() != "CISCO-FINISAR" or pn.strip() != "GLC-SX-MM":
        raise SystemExit("not the expected module: refusing")
    if cc(data, 0, 63) != data[63] or cc(data, 64, 95) != data[95]:
        raise SystemExit(f"checksums wrong before the change (base {cc(data, 0, 63):02x}/{data[63]:02x} ext {cc(data, 64, 95):02x}/{data[95]:02x}): refusing")
    new = bytearray(data)
    new[2] = 0x07
    for lo, hi in PAD_RANGES:
        for i in range(lo, hi):
            if new[i] == 0x20:
                new[i] = 0x00
    new[11], new[12] = 0x01, 0x0d
    new[36] = 0x00  # extended compliance 0x01 = '100G AOC' is padding junk on these clones
    new[63], new[95] = cc(new, 0, 63), cc(new, 64, 95)
    changes = [(i, data[i], new[i]) for i in range(256) if data[i] != new[i]]
    order = [c for c in changes if c[0] not in (63, 95)] + [c for c in changes if c[0] in (63, 95)]
    order.sort(key=lambda c: (c[0] in (63, 95), c[0] != 2, c[0]))  # connector first, checksums last
    for i, o, n in order:
        print(f"  byte {i:3d} (0x{i:02x}): {o:02x} -> {n:02x}")
    (BACKUP / f"{sn}-before.txt").write_text(raw)
    if not a.apply:
        print(f"dry run: {len(order)} bytes would change; backup saved to tmp/sfp-eeprom/{sn}-before.txt (use --apply)")
        return 0
    with LOG.open("a") as log:
        log.write(f"# {time.strftime('%Y-%m-%dT%H:%M:%S%z')} fix sfp-{a.front} sn {sn} bus {bus}: {order}\n")
        done = []
        for i, o, n in order:
            for attempt in range(3):  # 24Cxx NACKs during its internal write cycle (5 ms max): retry, then verify by read-back
                sh(f"i2cset -y {bus} {ADDR} 0x{i:02x} 0x{n:02x} b 2>&1")
                got = sh(f"i2cget -y {bus} {ADDR} 0x{i:02x} b 2>&1").strip()
                if got.lower() == f"0x{n:02x}":
                    break
            else:
                log.write(f"FAIL byte {i}: wrote {n:02x}, read {got}\n")
                print(f"FAIL byte {i}: wrote {n:02x}, read back {got!r}: restoring {len(done)} earlier bytes", file=sys.stderr)
                for j, oo, _ in reversed(done + [(i, o, n)]):
                    sh(f"i2cset -y {bus} {ADDR} 0x{j:02x} 0x{oo:02x} b 2>&1")
                return 1
            done.append((i, o, n))
            log.write(f"ok byte {i}: {o:02x} -> {n:02x}\n")
        after, _ = dump(bus)
        ok = after == bytes(new)
        log.write(f"verify full dump: {'OK' if ok else 'MISMATCH'}\n")
        print("verify full dump:", "OK" if ok else "MISMATCH")
        return 0 if ok else 1


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
