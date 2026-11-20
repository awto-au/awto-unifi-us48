#!/usr/bin/env python3
"""port_scan: bring every front port up at once, read link/speed/module/neighbour hints, put the ports back.

Usage: scripts/port_scan.py [--apply] [--keep ge8,ge17]
- Dry run unless --apply. ge1 and xe49 (management paths) are never touched. Ports that were up before stay up.
- Every port is in its own private ASIC VLAN (docs/switchdev.md), so no frames cross between ports; no bridge, no IPv4.
- Steps: snapshot admin state -> `ip link set up` on all other ports -> poll carrier until every port settled or
  the autoneg limit -> read carrier/speed/duplex, copper partner modes, SFP module ID + DDM (ethtool -m), IPv6 neighbours
  per port, rx/tx counters -> set the ports we raised back down (always, also on error).
- Autoneg limit 4.4 s = 1.25 x 3.49 s (slowest relink measured, #133 copper_admin_test); polled every 0.5 s.
- Output: markdown table on stdout + tmp/logs/port_scan.log. Board address from _ipreg (override US48_IP_US48_BOOT).
"""
import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _ipreg  # noqa: E402
import _sfp_state  # noqa: E402
from _cli import help_guard  # noqa: E402
from lx import OPTS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/port_scan.log"
NEVER = {"ge1", "xe49"}  # management paths: never touched
AUTONEG_LIMIT_S = 4.4
POLL_S = 0.5


def sh(cmd):
    # 30 s: a few ssh round trips of 1-2 s each; longer means the board path is gone.
    r = subprocess.run(["ssh", *OPTS, f"root@{_ipreg.ip('us48-boot')}", cmd], capture_output=True, text=True, timeout=30)
    if r.returncode:
        raise RuntimeError(f"ssh rc={r.returncode}: {r.stderr.strip()}")
    return r.stdout


def ports():
    out = sh("ls /sys/class/net")
    names = [n for n in out.split() if re.fullmatch(r"(ge|xe)\d+", n)]
    return sorted(names, key=lambda n: (n[:2] != "ge", int(n[2:])))


def snapshot(names):
    """name -> (admin 'up'/'down' from the IFF_UP flag, carrier); operstate is not admin state (NO-CARRIER shows down)."""
    cmd = "; ".join(f"echo {n} $(cat /sys/class/net/{n}/flags) $(cat /sys/class/net/{n}/carrier 2>/dev/null)" for n in names)
    state = {}
    for ln in sh(cmd).splitlines():
        f = ln.split()
        state[f[0]] = ("up" if int(f[1], 16) & 1 else "down", f[2] if len(f) > 2 else "")
    return state


def read_carriers(names):
    cmd = "; ".join(f"echo {n} $(cat /sys/class/net/{n}/carrier 2>/dev/null)" for n in names)
    return {ln.split()[0]: (ln.split() + [""])[1] for ln in sh(cmd).splitlines()}


def details(names):
    parts = []
    for n in names:
        parts.append(f"echo '=={n}'; ethtool {n} 2>&1 | grep -E 'Speed|Duplex|Link detected|link partner|Supported ports'; "
                     f"cat /sys/class/net/{n}/statistics/rx_packets /sys/class/net/{n}/statistics/tx_packets; "
                     f"ip -6 neigh show dev {n} 2>&1 | head -3")
        if n.startswith("xe"):
            parts.append(f"echo SFPSTATE $(cat /sys/class/net/{n}/sfp_state 2>/dev/null || echo n/a); "
                         f"echo SFPINFO $(cat /sys/class/net/{n}/sfp_info 2>/dev/null)")
            parts.append(f"ethtool -m {n} 2>&1 | grep -E 'Vendor name|Vendor PN|Vendor SN|Identifier|Laser (rx|output) power|Module temp|Length'")
    return sh("; ".join(parts))


def parse(text):
    res, cur = {}, None
    for ln in text.splitlines():
        if ln.startswith("=="):
            cur = ln[2:]
            res[cur] = []
        elif cur:
            res[cur].append(ln.strip())
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--keep", default="", help="comma list of ports to leave alone as well")
    ap.add_argument("--only", default="", help="comma list: scan just these ports")
    ap.add_argument("--wait", type=float, default=AUTONEG_LIMIT_S,
                    help="seconds to wait for link (optics: 10G lock took ~5 s on xe49 + RX restart; try 15)")
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    skip = NEVER | set(filter(None, a.keep.split(",")))
    names = ports()
    before = snapshot(names)
    only = set(filter(None, a.only.split(",")))
    raise_set = [n for n in names if n not in skip and before[n][0] == "down" and (not only or n in only)]
    print(f"{len(names)} ports; raising {len(raise_set)}: {' '.join(raise_set)}", flush=True)
    if not a.apply:
        print("dry run (use --apply)")
        return 0
    t0 = time.monotonic()
    try:
        sh("; ".join(f"ip link set {n} up" for n in raise_set))
        while True:
            time.sleep(POLL_S)
            car = read_carriers(raise_set)
            if time.monotonic() - t0 >= a.wait:
                break
        scan = [n for n in names if n in raise_set or n in NEVER or n in only]
        info = parse("".join(details([n]) for n in scan))  # one ssh per port: a single huge command was reset once
    finally:
        sh("; ".join(f"ip link set {n} down" for n in raise_set))
    rows = ["| port | before | carrier | speed/duplex | notes |", "|---|---|---|---|---|"]
    for n in (scan if only else names):
        lines = info.get(n, [])
        speed = next((x.split(":", 1)[1].strip() for x in lines if x.startswith("Speed")), "")
        dup = next((x.split(":", 1)[1].strip() for x in lines if x.startswith("Duplex")), "")
        c = car.get(n, before[n][1]) if n in raise_set else before[n][1]
        note = " ".join(x for x in lines if x.startswith(("Vendor", "Laser", "Module temp", "Identifier", "fe80")))
        sfp = next((x.split(None, 1)[1] for x in lines if x.startswith("SFPSTATE ")), "")
        sinfo = next((x.split(None, 1)[1] for x in lines if x.startswith("SFPINFO ") and " " in x), "")
        if sfp:
            note = f"SFP {sfp}" + (f" ({sinfo})" if _sfp_state.bad(sfp) and sinfo else "") + " " + note
        if c == "1" or n.startswith("xe"):
            rows.append(f"| {n} | {before[n][0]} | {c} | {speed} {dup} | {note} |")
    out = "\n".join(rows)
    print(out)
    LOG.write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z") + f" raised={raise_set}\n{out}\n\nraw:\n{info}\n")
    return 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
