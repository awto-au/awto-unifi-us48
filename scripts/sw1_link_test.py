#!/usr/bin/env python3
"""sw1_link_test: bring one sw1 port up against one US-48 xe port, watch both carriers + sw1 RX state, restore sw1 (#78, #87).

Usage: scripts/sw1_link_test.py --sw1 lan6 --xe xe49 [--wait-s 40] [--ping] [--no-board-dump]
- sw1 = root@192.168.25.48 (ssh key). lan5 is refused (Selwyn's route). Admin state of lan3 and lan6 is recorded first and
  restored in a finally block (up stays up, down goes down); only the --sw1 port is changed.
- SerDes index on sw1: lanN = SerDes N+1 (pcs phandle map lan1..8 = SerDes 2..9, driver-internals 13.6); RX_IDLE = WDIG reg 0x14 bit 0.
- Wait bound: sw1 runs RX calibration 3x per cycle, verdict every ~30.3 s (#78); 40 s covers one verdict + margin. Expiry = "no".
- Carrier = sw1 /sys/class/net/<lan>/carrier (polled 0.5 s). Board side: carrier + PCS_LIVE (tsc param dump) at the end.
- --ping: private /30 10.48.1.0/30 (US-48 .1 on the xe port, sw1 .2 on the lan port; no house LAN), 3 pings from the board
  (the ARP/ICMP source MAC is what sw1 learns), then `bridge fdb show` lines for the board's xe MAC. Addresses removed in finally.
- Log: tmp/logs/sw1_link_test.log
"""
import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/sw1_link_test.log"
SW1 = "root@192.168.25.48"
BOARD = "root@192.168.25.207"
POLL_S = 0.5
RESTORE = ("lan3", "lan6")
MAC = {"xe49": "02:75:73:34:38:31", "xe50": "02:75:73:34:38:32"}
SSH = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", "-o", "StrictHostKeyChecking=no",
       "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR"]


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def ssh(host: str, cmd: str, call_s: float = 10.0) -> str:
    try:
        r = subprocess.run(SSH + [host, cmd], capture_output=True, text=True, timeout=call_s)
    except subprocess.TimeoutExpired:
        return f"ssh {host} exceeded {call_s} s"
    return (r.stdout + r.stderr).strip()


def admin_up(lan: str) -> bool:
    out = ssh(SW1, f"ip -o link show {lan}")
    m = re.search(r"<([^>]*)>", out)
    if not m:
        raise RuntimeError(f"cannot read admin state of {lan}: {out[:100]}")
    return "UP" in m.group(1).split(",")


def sds(lan: str) -> str:
    """sw1 SerDes debugfs of this lan: WDIG reg 0x14 bit 0 = RX_IDLE (valid while WDIG reg 0x02 selects RX_STATUS), 10GBASE-R PCS status."""
    idx = int(lan[3:]) + 1
    out = ssh(SW1, f"grep -E '^(WDIG|TGR_STD_1) ' /sys/kernel/debug/realtek_otto_serdes/serdes.{idx}/registers")
    row = {}
    for ln in out.splitlines():
        k, _, v = ln.partition(":")
        row[k.strip()] = v.split()
    try:
        w = row["WDIG"]
        return f"serdes.{idx} WDIG02={w[2]} WDIG14={w[0x14]} (RX_IDLE bit0={int(w[0x14], 16) & 1}) TGR_STD_1[0]={row['TGR_STD_1'][0]} [1]={row['TGR_STD_1'][1]}"
    except (KeyError, IndexError, ValueError):
        return f"serdes.{idx} read failed: {out[:120]}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sw1", required=True, help="sw1 port, e.g. lan6")
    ap.add_argument("--xe", required=True, help="US-48 port, e.g. xe49")
    ap.add_argument("--wait-s", type=float, default=40.0)
    ap.add_argument("--ping", action="store_true")
    ap.add_argument("--no-board-dump", action="store_true")
    a = ap.parse_args()
    if not re.fullmatch(r"lan[1-8]", a.sw1) or a.sw1 == "lan5":
        sys.exit(f"refusing sw1 port {a.sw1!r}: lan1..lan8 except lan5")
    if a.xe not in MAC:
        sys.exit(f"unknown xe port {a.xe!r}")
    orig = {l: admin_up(l) for l in RESTORE}
    log(f"original admin state: {orig}")
    carrier = False
    try:
        base = ssh(SW1, "cut -d. -f1 /proc/uptime").strip()
        log(f"sw1 {a.sw1} up: " + ssh(SW1, f"ip link set {a.sw1} up; echo ok"))
        t0 = time.monotonic()
        nxt = 3.0
        while time.monotonic() - t0 < a.wait_s:
            if ssh(SW1, f"cat /sys/class/net/{a.sw1}/carrier").strip() == "1":
                carrier = True
                break
            if time.monotonic() - t0 >= nxt:
                log(f"t+{time.monotonic() - t0:4.1f} s sw1 {sds(a.sw1)}")
                nxt += 8.0
            time.sleep(POLL_S)
        log(f"SW1 CARRIER {'yes' if carrier else 'no'} after {time.monotonic() - t0:.1f} s (limit {a.wait_s} s)")
        log(f"sw1 {sds(a.sw1)}")
        log("sw1 new log lines:\n" + ssh(SW1, f"dmesg | awk -F'[][]' '$2+0 >= {int(base)}' | tail -8"))
        if not a.no_board_dump:
            bc = ssh(BOARD, f"cat /sys/class/net/{a.xe}/carrier")
            ssh(BOARD, "dmesg -c >/dev/null; echo 1 > /sys/module/us48_cmic_xe/parameters/tsc")
            log(f"board {a.xe} carrier={bc}\n" + ssh(BOARD, f"dmesg | grep '{a.xe}' | grep -E 'PCS_LIVE|link' | tail -4"))
        if a.ping and carrier:
            ssh(BOARD, f"ip addr add 10.48.1.1/30 dev {a.xe}; ip link set {a.xe} up")
            ssh(SW1, f"ip addr add 10.48.1.2/30 dev {a.sw1}")
            log("ping: " + ssh(BOARD, "ping -c 3 -W 1 10.48.1.2 | tail -3", 15))
            log(f"sw1 fdb for {MAC[a.xe]}:\n" + ssh(SW1, f"bridge fdb show | grep -i {MAC[a.xe]}; echo rc=$?"))
    finally:
        if a.ping:
            ssh(SW1, f"ip addr del 10.48.1.2/30 dev {a.sw1}")
            ssh(BOARD, f"ip addr del 10.48.1.1/30 dev {a.xe}")
        for l, up in orig.items():
            ssh(SW1, f"ip link set {l} {'up' if up else 'down'}")
        log("restored admin state: " + str({l: admin_up(l) for l in RESTORE}) + f" (want {orig})")
    return 0 if carrier else 1


if __name__ == "__main__":
    sys.exit(main())
