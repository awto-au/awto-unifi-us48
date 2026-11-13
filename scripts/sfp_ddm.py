#!/usr/bin/env python3
"""Read-only SFP identity + DDM of a cage over ssh (mainline/OpenWrt, i2c-tools on the board).

Usage: sfp_ddm.py [BUS ...]    Linux i2c bus numbers of the mux channels, default 4 (SFP+ 2); 3 = SFP+ 1 (DAC: no 0x51)
Host: $US48_SSH (default root@192.168.25.207). Reads A0h vendor/PN/SN (bytes 20-35, 40-55, 68-83) and A2h 96-105 (SFF-8472, external calibration).
Log: tmp/logs/sfp_ddm.log. One ssh round trip per cage, ~30 i2cget calls; ssh BatchMode, 20 s connect+run cap (stall guard, not a measurement).
"""
import math
import os
import pathlib
import subprocess
import sys
import time

LOG = pathlib.Path(__file__).resolve().parent.parent / "tmp/logs/sfp_ddm.log"
HOST = os.environ.get("US48_SSH", "root@192.168.25.207")


def log(msg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    print(msg, flush=True)
    with LOG.open("a") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%S%z ") + msg + "\n")


def read_bytes(bus, addr, regs):
    sh = f"for r in {' '.join(map(str, regs))}; do i2cget -y {bus} {addr} $r b 2>/dev/null || echo NAK; done"
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", HOST, sh],
                       capture_output=True, text=True, timeout=20)
    out = r.stdout.split()
    if len(out) != len(regs):
        raise RuntimeError(f"ssh/i2cget: got {len(out)} of {len(regs)} bytes: {r.stderr.strip()[:200]}")
    return [None if o == "NAK" else int(o, 16) for o in out]


def text(b):
    return "".join(chr(x) if x and 32 <= x < 127 else "" for x in b).strip() if None not in b else "NAK"


def dbm(uw_x10):
    return "-inf" if uw_x10 == 0 else f"{10 * math.log10(uw_x10 / 10000):.2f} dBm"  # unit 0.1 uW -> mW


def main():
    for bus in [int(x) for x in sys.argv[1:]] or [4]:
        regs = list(range(20, 36)) + list(range(40, 56)) + list(range(68, 84)) + [92]
        d = read_bytes(bus, "0x50", regs)
        log(f"== i2c-{bus}: vendor '{text(d[0:16])}' PN '{text(d[16:32])}' SN '{text(d[32:48])}' DIAG_TYPE {d[48] if d[48] is not None else 'NAK'}")
        a2 = read_bytes(bus, "0x51", list(range(96, 106)))
        if None in a2:
            log(f"   A2h (0x51) NAK: passive DAC / no DDM")
            continue
        w = [a2[i] << 8 | a2[i + 1] for i in range(0, 10, 2)]
        temp = (w[0] - 65536 if w[0] & 0x8000 else w[0]) / 256
        log(f"   temp {temp:.1f} C, Vcc {w[1] / 10000:.3f} V, bias {w[2] * 2 / 1000:.2f} mA, "
            f"TX {dbm(w[3])} ({w[3] / 10:.1f} uW), RX {dbm(w[4])} ({w[4] / 10:.1f} uW)  (external-cal modules need slope/offset; raw approximation)")


if __name__ == "__main__":
    main()
