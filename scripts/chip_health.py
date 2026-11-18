#!/usr/bin/env python3
"""chip_health — over ssh on the running Linux board: unit 0 L2X readable over the whole range, parity regs 0 (#34).

Usage: scripts/chip_health.py [--host IP]    (log tmp/logs/chip_health.log; exit 0 = healthy)
- Read-only S-channel ops via busybox devmem on the CMIC (same sequence as dev.py schan), one ssh call.
- Checks: L2X entries 0, 0x1fff, 0x2000, 0x3fff, 0x7fff read with ctrl == 2 (DONE, no NAK/error bits);
  L2_ENTRY_PARITY_STATUS_0/1 == 0; ING_HW_RESET_CONTROL_2 == 0.
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hwdb  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/chip_health.log"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
       "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR"]
SSH_CAP_S = 20.0  # ~14 ops x ~50 devmem forks on a 1 GHz A9; 20 s backstop, expiry = FATAL
CMIC0 = hwdb.cmic_base(0) + 0x31000
L2X = 0x1C000000
CHECKS = [("L2X[%#x]" % i, 0x07, 10, L2X | i, 4) for i in (0, 0x1FFF, 0x2000, 0x3FFF, 0x7FFF)] + [
    ("L2_ENTRY_PARITY_STATUS_0", 0x0B, 10, 0x1E010D00, 1), ("L2_ENTRY_PARITY_STATUS_1", 0x0B, 10, 0x1E010E00, 1),
    ("ING_HW_RESET_CONTROL_2", 0x0B, 10, 0x02000300, 1)]


def op_cmd(opc: int, blk: int, addr: int, words: int) -> str:
    b = CMIC0
    hdr = opc << 26 | blk << 20 | (4 * words) << 7
    return (f"devmem {b:#x} 32 0; devmem {b + 12:#x} 32 {hdr:#x}; devmem {b + 16:#x} 32 {addr:#x}; devmem {b:#x} 32 1; n=0; "
            f"while [ $(( $(devmem {b:#x} 32) & 2 )) = 0 ] && [ $n -lt 50 ]; do n=$((n+1)); done; "
            f"echo ctrl=$(devmem {b:#x} 32) $(for i in $(seq 1 {words}); do devmem $(({b + 12}+4*i)) 32; done); devmem {b:#x} 32 0")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.25.207")
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    script = "; ".join(f"echo '== {n}'; " + op_cmd(o, b, ad, w) for n, o, b, ad, w in CHECKS)
    t0 = time.monotonic()
    try:
        r = subprocess.run(SSH + [f"root@{a.host}", script], capture_output=True, text=True, timeout=SSH_CAP_S)
    except subprocess.TimeoutExpired:
        print(f"FATAL ssh gave no result within {SSH_CAP_S} s")
        return 2
    out = r.stdout
    bad = 0
    lines = out.splitlines()
    for i, (name, *_rest) in enumerate(CHECKS):
        try:
            res = lines[lines.index(f"== {name}") + 1]
        except (ValueError, IndexError):
            res = "missing"
        toks = res.replace("ctrl=", "").split()
        ctrl = int(toks[0], 16) if toks and toks[0].startswith("0x") else -1
        vals = [int(t, 16) for t in toks[1:]]
        ok = ctrl == 2
        if "PARITY" in name or "RESET" in name:
            ok = ok and vals == [0]
        bad += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name}: {res}")
    msg = f"{'HEALTHY' if not bad else 'UNHEALTHY'} ({bad} failing) in {time.monotonic() - t0:.1f} s"
    print(msg)
    with LOG.open("a") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%S%z ") + msg + "\n" + out + r.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
