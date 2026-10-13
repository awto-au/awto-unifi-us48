#!/usr/bin/env python3
"""mspi_capture — read-only dump of CMIC MSPI + GP registers on stock (fan/board-IO frame replay source).

Stock running, after `scripts/dev.py fan-level N`. Uses `/sys/bcm/mem` R (prints to dmesg) over SSH.
Regions (CMIC unit 0 @ AXI 0x48000000, docs/board.md MSPI section):
  SPCR/STATUS 0x1500-0x152c, TXRAM 0x1540-0x15bc, CDRAM 0x1640-0x167c, GP 0x2000-0x200c.
Log: tmp/logs/mspi_capture.log
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/mspi_capture.log"
BASE = 0x48000000
UNIT1 = 0x0A000000  # BCM53346 CMIC = PCIe 01:00.0 BAR 2 (stock /proc/iomem)
RANGES = [(0x1500, 0x1530), (0x1540, 0x15C0), (0x1640, 0x1684), (0x2000, 0x2010)]
# + MSPI bus claim (CMICM_BSPI_MAST_N_BOOT 0x1680, CMIC_OVERRIDE_STRAP 0x10234) and unit 1 GP (select line B)
EXTRA = [BASE + 0x10234, UNIT1 + 0x2000, UNIT1 + 0x2004, UNIT1 + 0x2008]


def main() -> int:
    addrs = [BASE + a for lo, hi in RANGES for a in range(lo, hi, 4)] + EXTRA
    script = "".join(f"echo R {a:#x} 1 > /sys/bcm/mem; " for a in addrs) + f"dmesg | tail -n {3 * len(addrs)}"
    r = subprocess.run([sys.executable, str(ROOT / "scripts/us48_ssh.py"), script], capture_output=True, text=True)
    vals = dict(re.findall(r"\[(0x[0-9a-f]{8})\] = (0x[0-9a-f]+)", r.stdout))
    lines = [f"{a:#010x} {vals.get(f'{a:#010x}', '?')}" for a in addrs]
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0 if vals else 1


if __name__ == "__main__":
    sys.exit(main())
