#!/usr/bin/env python3
"""sfp_check: read-only state of the four SFP cages (xe49-xe52): module EEPROM, carrier, SerDes lock. Changes nothing.

Usage: scripts/sfp_check.py
- Per cage: ethtool -m identity (error text shown verbatim, e.g. 'No such device' = SFP layer sees no module),
  carrier, TSC c150 (PMD_LIVE: 0x000f = signal+lock) and c154 (PCS_LIVE: b0 sync, b1 link). Last sfp log lines per cage.
- No module reload, no dmesg -c, no admin-state change: ports are not brought up, so an admin-down cage shows c150 of an idle lane.
  Read light only on ports that are up (scripts/cage_watch.py brings 51/52 up for a timed watch).
Log: tmp/logs/sfp_check.log. Board address from _ipreg (US48_IP_US48_BOOT override).
"""
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
LOG = ROOT / "tmp/logs/sfp_check.log"
XE = "/sys/module/us48_cmic_xe/parameters"
FRONTS = (49, 50, 51, 52)


def sh(cmd):
    # 30 s: ssh round trip is 1-2 s; longer = board path gone.
    r = subprocess.run(["ssh", *OPTS, f"root@{_ipreg.ip('us48-boot')}", cmd], capture_output=True, text=True, timeout=30)
    return r.stdout + r.stderr


def main():
    LOG.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# {time.strftime('%Y-%m-%dT%H:%M:%S%z')} sfp_check"]
    for f in FRONTS:
        em = sh(f"ethtool -m xe{f} 2>&1 | grep -E 'netlink|Connector|Vendor (name|PN|SN)|Transceiver type|Encoding'")
        ident = {k: (re.search(rf"{k}\s*:\s*(.*)", em).group(1).strip() if re.search(rf"{k}\s*:\s*(.*)", em) else "?")
                 for k in ("Vendor name", "Vendor PN", "Vendor SN", "Connector")}
        types = re.findall(r"Transceiver type\s*:\s*(.*)", em)
        err = re.search(r"netlink error: (.*)", em)
        car = sh(f"cat /sys/class/net/xe{f}/carrier 2>&1").strip()
        up = int(sh(f"cat /sys/class/net/xe{f}/flags").strip() or "0", 16) & 1
        sh(f'echo "{f} c150 c154" > {XE}/tscreg')
        rg = sh(f"dmesg | grep 'xe{f} range' | tail -n 1")
        m = re.search(r"c150=([0-9a-f]{4}).*c154=([0-9a-f]{4})", rg)
        c150, c154 = (m.group(1), m.group(2)) if m else ("?", "?")
        cage = sh(f"dmesg | grep 'sfp-{f}' | tail -n 3").strip().replace("\n", " | ")
        mod = f"NO MODULE SEEN ({err.group(1)})" if err else f"{ident['Vendor name']} {ident['Vendor PN']} sn {ident['Vendor SN']} conn {ident['Connector']}"
        lines.append(f"xe{f}: {mod}; admin {'up' if up else 'down'}, carrier {car}; c150={c150} c154={c154}; types: {'; '.join(types[:3]) or '-'}")
        if cage:
            lines.append(f"      sfp log: {cage}")
    out = "\n".join(lines)
    print(out)
    with LOG.open("a") as fh:
        fh.write(out + "\n")
    return 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
