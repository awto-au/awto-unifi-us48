#!/usr/bin/env python3
"""xlport_cmp: read XLPORT/XLMAC registers of xe49/xe50 (unit 1) and xe51/xe52 (unit 0) and print them side by side.

Usage: scripts/xlport_cmp.py   (read-only: schan rreg through /sys/module/us48_cmic/parameters/schan, #125)
Block 5 = XLPORT0 on both units (BLK_XLPORT, kernel/us48-cmic/us48_cmic.h); lane l adds l to the XLPORT/XLMAC addresses.
Log: tmp/logs/xlport_cmp.log. Board address from _ipreg (US48_IP_US48_BOOT override).
"""
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _ipreg  # noqa: E402
from _cli import help_guard  # noqa: E402
from lx import OPTS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/xlport_cmp.log"
SCHAN = "/sys/module/us48_cmic/parameters/schan"
REGS = [("XLPORT_CONFIG", 0x20000), ("XLPORT_TXFIFO_CTRL", 0x20300), ("XLMAC_CTRL", 0x60000), ("XLMAC_MODE", 0x60100),
        ("XLMAC_TX_CTRL", 0x60400), ("XLMAC_RX_CTRL", 0x60600), ("XLMAC_RX_MAX_SIZE", 0x60800), ("XLMAC_RX_LSS_CTRL", 0x60a00)]
CORE = [("XLPORT_MODE_REG", 0x2020a00), ("XLPORT_ENABLE_REG", 0x2020b00), ("XLPORT_POWER_SAVE", 0x2020d00)]
PORTS = [("xe49", 1, 0), ("xe50", 1, 2), ("xe51", 0, 0), ("xe52", 0, 2)]  # name, unit, lane


def sh(cmd):
    # 30 s: ssh round trip 1-2 s; longer = board path gone.
    r = subprocess.run(["ssh", *OPTS, f"root@{_ipreg.ip('us48-boot')}", cmd], capture_output=True, text=True, timeout=30)
    if r.returncode:
        raise RuntimeError(f"ssh rc={r.returncode}: {r.stderr.strip()}")
    return r.stdout


def rreg(unit, addr):
    out = sh(f'echo "{unit} rreg 5 {addr:x} 2" > {SCHAN}; cat {SCHAN}')
    m = re.search(r":\s*([0-9a-f]{8}) ([0-9a-f]{8})", out)
    return f"{m.group(1)}" if m else "?"


def main():
    rows = ["| register | " + " | ".join(p[0] for p in PORTS) + " |", "|---|" + "---|" * len(PORTS)]
    for name, addr in CORE:
        rows.append(f"| {name} (core) | " + " | ".join(rreg(u, addr) for _, u, _ in PORTS) + " |")
    for name, base in REGS:
        rows.append(f"| {name} | " + " | ".join(rreg(u, base | lane) for _, u, lane in PORTS) + " |")
    out = "\n".join(rows)
    print(out)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text(out + "\n")
    return 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
