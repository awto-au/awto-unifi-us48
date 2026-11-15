#!/usr/bin/env python3
"""sw1_lan3_test: bring sw1 lan3 (far end of xe49 / SFP+ 1 DAC) up, watch carrier + sw1's RX calibration log, set lan3 down (#78).

Usage: scripts/sw1_lan3_test.py [--wait-s 35] [--board-dump]
- sw1 = root@192.168.25.48 (ssh key). Never touches lan5. lan3 is set down again in a finally block.
- Wait bound: sw1 runs its RX calibration 3x per cycle, failure verdict every ~30.3 s (comment on #78; lan3wait.sh uses 35 s); carrier = pass. Expiry = verdict "no".
- --board-dump: also logs the board's xe49 TSC status (module param tsc) after the wait.
- sw1 SerDes 4 (lan3) RX_IDLE is read from sw1 debugfs every 8 s: bit 0 = 1 means sw1 sees no signal from us (calibration never starts, no log).
- Verdict line: CARRIER yes/no + new sw1 log lines. Log: tmp/logs/sw1_lan3_test.log
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/sw1_lan3_test.log"
SW1 = "root@192.168.25.48"
BOARD = "root@192.168.25.207"
POLL_S = 0.5
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


def sds4() -> str:
    """sw1 SerDes 4 (= lan3) debugfs: WDIG reg 0x14 bit 0 = RX_IDLE (valid while WDIG reg 0x02 selects RX_STATUS), 10GBASE-R PCS status."""
    out = ssh(SW1, "grep -E '^(WDIG|TGR_STD_1) ' /sys/kernel/debug/realtek_otto_serdes/serdes.4/registers")
    row = {}
    for ln in out.splitlines():
        k, _, v = ln.partition(":")
        row[k.strip()] = v.split()
    try:
        w = row["WDIG"]
        return f"WDIG02={w[2]} WDIG14={w[0x14]} (RX_IDLE bit0={int(w[0x14], 16) & 1}) TGR_STD_1[0]={row['TGR_STD_1'][0]}"
    except (KeyError, IndexError, ValueError):
        return f"sds4 read failed: {out[:120]}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--wait-s", type=float, default=35.0)
    ap.add_argument("--board-dump", action="store_true")
    a = ap.parse_args()
    carrier = False
    try:
        log("sw1 lan3 up: " + ssh(SW1, "ip link set lan3 up; echo ok"))
        t0 = time.monotonic()
        nxt = 3.0
        while time.monotonic() - t0 < a.wait_s:
            if ssh(SW1, "cat /sys/class/net/lan3/carrier").strip() == "1":
                carrier = True
                break
            if time.monotonic() - t0 >= nxt:
                log(f"t+{time.monotonic() - t0:4.1f} s sw1 serdes 4: {sds4()}")
                nxt += 8.0
            time.sleep(POLL_S)
        log(f"CARRIER {'yes' if carrier else 'no'} after {time.monotonic() - t0:.1f} s (limit {a.wait_s} s)")
        log("sw1 log:\n" + ssh(SW1, "dmesg | grep -i -E 'lan3|SerDes [^7]' | tail -4"))
        if a.board_dump:
            ssh(BOARD, "dmesg -c >/dev/null; echo 1 > /sys/module/us48_cmic_xe/parameters/tsc")
            log("board xe49:\n" + ssh(BOARD, "dmesg | grep 'xe49' | tail -6"))
    finally:
        log("sw1 lan3 down: " + ssh(SW1, "ip link set lan3 down; cat /sys/class/net/lan3/operstate"))
    return 0 if carrier else 1


if __name__ == "__main__":
    sys.exit(main())
