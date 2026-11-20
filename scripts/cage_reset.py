#!/usr/bin/env python3
"""cage_reset: runtime-only reset of one SFP cage (xe50/xe51/xe52): link down, 0.3 s, up, rx_restart, then read c150/c154/carrier (#78).

Usage: scripts/cage_reset.py <50|51|52> [--leave-down]
- Refuses xe49 (management path). No module reload, no reboot.
- 0.3 s down time: one 1G/10G laser-off/LOS cycle at the far end is ~100 ms; x3 margin.
- Reads at +0.5 s, +1.5 s, +3 s after rx_restart (sync/AN settle) in one board-side script.
- --leave-down: admin down at the end (xe51/xe52 must end down).
Log: tmp/logs/cage_reset.log. Board address from _ipreg.
"""
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _ipreg  # noqa: E402
from lx import OPTS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/cage_reset.log"
XE = "/sys/module/us48_cmic_xe/parameters"


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("50", "51", "52"):
        sys.exit(__doc__)
    f = sys.argv[1]
    leave_down = "--leave-down" in sys.argv
    rd = (f'echo "{f} c150 c154" > {XE}/tscreg; echo "$(date +%T.%N | cut -c1-12) xe{f} carrier=$(cat /sys/class/net/xe{f}/carrier 2>&1) $(cat {XE}/tscreg)"')
    s = (f"echo \"$(date +%T.%N | cut -c1-12) down\"; ip link set xe{f} down; usleep 300000; ip link set xe{f} up; "
         f"echo \"$(date +%T.%N | cut -c1-12) up\"; echo {f} > {XE}/rx_restart; echo \"$(date +%T.%N | cut -c1-12) rx_restart\"; "
         f"usleep 500000; {rd}; usleep 1000000; {rd}; usleep 1500000; {rd}; "
         + (f"ip link set xe{f} down; echo admin down; " if leave_down else "")
         + "dmesg | tail -8")
    r = subprocess.run(["ssh", *OPTS, f"root@{_ipreg.ip('us48-boot')}", s], capture_output=True, text=True, timeout=30)
    out = f"# {time.strftime('%Y-%m-%dT%H:%M:%S%z')} cage_reset xe{f}\n{r.stdout}{r.stderr}"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(out)
    print(out)


if __name__ == "__main__":
    main()
