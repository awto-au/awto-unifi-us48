#!/usr/bin/env python3
"""stock_wait_ready — poll a RAM-booted stock (stock_ramboot.py) until its SDK init is done; log a timeline (#78 clean control).

Usage: scripts/stock_wait_ready.py [--min-uptime-s 300] [--max-s 720] [--period-s 10]
Per poll (one ssh, 8 s limit): /proc/uptime, loadavg, CPU idle % over the poll gap, readiness files /var/run/fastpath/{switchdrvr_ready,.poe_ready},
switchdrvr/ubntapp present. Done = ready file + uptime >= min + CPU idle >= 90 % on 2 polls in a row.
Bounds: max-s = 3x the ~4 min SDK init (user's figure); on expiry logs the last state and exits 1. Log: tmp/logs/stock_wait_ready.log
"""
import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tsc_lane_dump as d  # noqa: E402

LOG = d.ROOT / "tmp/logs/stock_wait_ready.log"
HOST = "192.168.25.206"
CMD = ("cat /proc/uptime /proc/loadavg; head -1 /proc/stat; "
       "ls /var/run/fastpath/ 2>&1 | tr '\\n' ' '; echo; ls -a /var/run/fastpath/ 2>&1 | grep -c ready; "
       "ps | grep -E 'switchdrvr|ubntapp|ubnt-' | grep -v grep | head -5")


def log(m: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {m}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-uptime-s", type=float, default=300)
    ap.add_argument("--max-s", type=float, default=720)
    ap.add_argument("--period-s", type=float, default=10)
    a = ap.parse_args()
    t0 = time.monotonic()
    prev = None
    idle_ok = 0
    while time.monotonic() - t0 < a.max_s:
        try:
            r = d.ssh(HOST, CMD, timeout=8)
            out = r.stdout.decode(errors="replace") if r.returncode == 0 else ""
        except Exception as e:  # noqa: BLE001 - ssh not up yet is the normal early state
            out = ""
            log(f"ssh: {type(e).__name__}")
        if out:
            L = out.splitlines()
            up = float(L[0].split()[0])
            cpu = [int(x) for x in L[2].split()[1:]]
            tot, idle = sum(cpu), cpu[3]
            pct = None
            if prev and tot > prev[0]:
                pct = 100.0 * (idle - prev[1]) / (tot - prev[0])
            prev = (tot, idle)
            ready = "switchdrvr_ready" in L[3]
            log(f"uptime {up:.0f}s load {L[1].split()[:3]} idle {pct if pct is None else round(pct)}% ready={ready} nready={L[4].strip()} files[{L[3].strip()[:150]}] ps[{'; '.join(x.strip()[:60] for x in L[5:])}]")
            idle_ok = idle_ok + 1 if (pct is not None and pct >= 90) else 0
            if ready and up >= a.min_uptime_s and idle_ok >= 2:
                log(f"DONE: ready, uptime {up:.0f}s, idle>=90% x2")
                return 0
        else:
            log("no reply")
        time.sleep(a.period_s)
    log(f"TIMEOUT {a.max_s}s: last uptime/ready above")
    return 1


if __name__ == "__main__":
    sys.exit(main())
