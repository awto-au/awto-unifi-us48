#!/usr/bin/env python3
"""sw1_bounce: start-up order experiments for one 10G pair (US-48 xe49/xe50 <-> sw1 lan7/lan3) (#78, #87).

Usage: scripts/sw1_bounce.py --pair 49|50 [--exp a,b,c,d,e] [--wait-s 40]
- 49 = xe49 (lane 0) - sw1 lan7;  50 = xe50 (lane 2) - sw1 lan3. lan5 is never touched. Admin state of lan3/lan6/lan7 is
  recorded first and restored in a finally block.
- a: sw1 port down 3 s, up.  b: sw1 up, board module us48_cmic_xe reloaded (scripts/mod_reload.py).
  c: 5x (sw1 down 1 s, up, 3 s apart).  d: 100 s idle, sw1 up (3 calibration cycles of ~30.4 s).
  e: board netdev down/up (ndo_stop/ndo_open: netif state only, no PCS write) with sw1 up.
- Wait bound: sw1 verdict every ~30.4 s (#78) -> 40 s = one verdict + margin. Expiry = "no carrier".
- Thermal stop: die >= 108 C or any LM75 >= 78 C (read every ~10 s), sets abort.
- Per experiment: sw1 carrier, board carrier, PCS_LIVE, sw1 dmesg lines, sw1 SerDes TGR_STD_0/1 of the port.
- Log: tmp/logs/sw1_bounce.log
"""
import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hwdb  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/sw1_bounce.log"
SW1 = "root@192.168.25.48"
BOARD = "root@192.168.25.207"
_DB = hwdb.lite()
PAIRS = {str(f): (_DB.netdev(f), lan, _DB.lane(f)) for f, lan in _DB.sw1_cages().items()}
RESTORE = _DB.sw1_ports()
POLL_S = 0.5
SSH = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", "-o", "StrictHostKeyChecking=no",
       "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR"]
abort = False


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def ssh(host: str, cmd: str, call_s: float = 10.0) -> str:
    # call_s = stall guard for one ssh round trip (LAN, < 1 s); expiry returns a marker string
    try:
        r = subprocess.run(SSH + [host, cmd], capture_output=True, text=True, timeout=call_s)
    except subprocess.TimeoutExpired:
        return f"ssh {host} exceeded {call_s} s"
    return (r.stdout + r.stderr).strip()


def thermal() -> None:
    global abort
    out = ssh(BOARD, "cat /sys/class/hwmon/hwmon*/temp1_input")
    try:
        t = [int(x) for x in out.split()]
    except ValueError:
        log(f"THERMAL read failed: {out[:80]}")
        return
    die = max(t[4:6]) / 1000 if len(t) >= 6 else 0
    lm = max(t[:4]) / 1000 if len(t) >= 4 else 0
    if die >= 108 or lm >= 78:
        abort = True
        log(f"THERMAL STOP die {die} lm75 {lm}")


def admin_up(lan: str) -> bool:
    out = ssh(SW1, f"ip -o link show {lan}")
    m = re.search(r"<([^>]*)>", out)
    if not m:
        raise RuntimeError(f"cannot read admin state of {lan}: {out[:100]}")
    return "UP" in m.group(1).split(",")


def sw1_carrier(lan: str) -> str:
    return ssh(SW1, f"cat /sys/class/net/{lan}/carrier").strip()


def sw1_set(lan: str, up: bool) -> None:
    assert lan in RESTORE
    ssh(SW1, f"ip link set {lan} {'up' if up else 'down'}")


def sw1_uptime() -> int:
    return int(ssh(SW1, "cut -d. -f1 /proc/uptime").strip() or 0)


def sds_row(lan: str) -> str:
    idx = int(lan[3:]) + 1
    return ssh(SW1, f"grep -E '^(TGR_STD_0|TGR_STD_1) ' /sys/kernel/debug/realtek_otto_serdes/serdes.{idx}/registers | cut -c1-80")


def board_state(xe: str) -> str:
    bc = ssh(BOARD, f"cat /sys/class/net/{xe}/carrier")
    ssh(BOARD, "dmesg -c >/dev/null; echo 1 > /sys/module/us48_cmic_xe/parameters/tsc")
    d = ssh(BOARD, f"dmesg | grep '{xe}' | grep -E 'PCS_LIVE|CRED0|mac:'")
    live = re.search(r"PCS_LIVE=(\w+)", d)
    return f"carrier={bc} PCS_LIVE={live.group(1) if live else '?'}\n{d}"


def watch(lan: str, wait_s: float, tag: str) -> bool:
    """Poll sw1 carrier up to wait_s; True if it came up."""
    t0 = time.monotonic()
    nxt = 10.0
    while time.monotonic() - t0 < wait_s and not abort:
        if sw1_carrier(lan) == "1":
            log(f"[{tag}] SW1 CARRIER yes after {time.monotonic() - t0:.1f} s")
            return True
        if time.monotonic() - t0 >= nxt:
            thermal()
            nxt += 10.0
        time.sleep(POLL_S)
    log(f"[{tag}] SW1 CARRIER no after {time.monotonic() - t0:.1f} s (limit {wait_s} s)")
    return False


def report(tag: str, xe: str, lan: str, base: int, carrier: bool) -> None:
    log(f"[{tag}] sw1 dmesg since uptime {base}:\n" + ssh(SW1, f"dmesg | awk -F'[][]' '$2+0 >= {base}' | tail -12"))
    log(f"[{tag}] sw1 {lan} SerDes:\n" + sds_row(lan))
    log(f"[{tag}] board {xe} " + board_state(xe))
    log(f"[{tag}] RESULT sw1_carrier={sw1_carrier(lan)} yes={carrier}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pair", required=True, choices=PAIRS)
    ap.add_argument("--exp", default="a,b,c,d,e")
    ap.add_argument("--wait-s", type=float, default=40.0)
    a = ap.parse_args()
    xe, lan, _lane = PAIRS[a.pair]
    orig = {l: admin_up(l) for l in RESTORE}
    log(f"== pair {xe} - sw1 {lan}; original admin state {orig}")
    thermal()
    try:
        for e in a.exp.split(","):
            if abort:
                break
            tag = f"{xe}/{e}"
            log(f"[{tag}] board before: " + board_state(xe).splitlines()[0])
            if e == "a":
                sw1_set(lan, False)
                time.sleep(3.0)	# let sw1 drop its PCS/SerDes state; 3 s >> 1 s calibration start latency
                base = sw1_uptime()
                sw1_set(lan, True)
                ok = watch(lan, a.wait_s, tag)
            elif e == "b":
                sw1_set(lan, True)
                time.sleep(2.0)
                base = sw1_uptime()
                r = subprocess.run([sys.executable, str(ROOT / "scripts/mod_reload.py"), "us48_cmic_xe"], capture_output=True, text=True, timeout=90)
                log(f"[{tag}] mod_reload rc={r.returncode}: " + r.stdout.strip().splitlines()[-1] if r.stdout.strip() else f"[{tag}] mod_reload rc={r.returncode} {r.stderr[-200:]}")
                ok = watch(lan, a.wait_s, tag)
            elif e == "c":
                base = sw1_uptime()
                ok = False
                for i in range(5):
                    sw1_set(lan, False)
                    time.sleep(1.0)
                    sw1_set(lan, True)
                    time.sleep(3.0)	# calibration verdict arrives ~1-2 s after up
                    if sw1_carrier(lan) == "1":
                        ok = True
                        log(f"[{tag}] carrier after bounce {i + 1}")
                        break
                ok = ok or watch(lan, a.wait_s, tag)
            elif e == "d":
                sw1_set(lan, True)
                base = sw1_uptime()
                ok = watch(lan, 100.0, tag)
            elif e == "e":
                sw1_set(lan, True)
                base = sw1_uptime()
                log(f"[{tag}] board netdev down/up: " + ssh(BOARD, f"ip link set {xe} down; ip link set {xe} up; echo ok"))
                ok = watch(lan, a.wait_s, tag)
            else:
                continue
            report(tag, xe, lan, base, ok)
            if ok:
                log(f"[{tag}] LINK UP, stopping further experiments for this pair")
                break
    finally:
        for l, up in orig.items():
            sw1_set(l, up)
        log("restored admin state: " + str({l: admin_up(l) for l in RESTORE}) + f" (want {orig})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
