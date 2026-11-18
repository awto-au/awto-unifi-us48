#!/usr/bin/env python3
"""stock_clean_control — stock fully up (stock_ramboot.py --no-u1 + wait): sw1 lan3/lan7 up 3 min, then 3 bounces each, sample stock PCS + sw1 TGR_STD_1 (#78).

Usage: scripts/stock_clean_control.py [--hold-s 180] [--bounce-n 3] [--post-s 40]
Per sample: sw1 carrier + TGR_STD_1[0]/[1] + RX_IDLE for lan3/lan7; stock xe49 + xe50 regs c154/c120/c150/RX_LSS (tsc_read, read-only).
Bounce: down 3 s (user spec), up, sample every 10 s for post-s (40 s > one ~30 s sw1 calibration cycle). lan5 never touched.
Restore in finally: lan3 down, lan7 up (user's required end state). Log: tmp/logs/stock_clean_control.log
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sw1_link_test as t  # noqa: E402
import stock_sw1_probe as p  # noqa: E402

LOG = t.ROOT / "tmp/logs/stock_clean_control.log"
H = "192.168.25.206"
_DB = t.hwdb.lite()
XE = (49, 50)
LANS = tuple((_DB.sw1_peer(xe), xe) for xe in XE)  # (sw1 port, stock xe front), from ports.toml


def log(m: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {m}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def sample(tag: str) -> None:
    for lan, xe in LANS:
        c = t.ssh(t.SW1, f"cat /sys/class/net/{lan}/carrier 2>&1").strip()
        log(f"{tag} sw1 {lan} carrier={c} {t.sds(lan).split('serdes')[-1]} | stock xe{xe} {p.stock_regs(H, xe)}")


def wait(s: float, tag: str, every: float) -> None:
    t0 = time.monotonic()
    n = 0
    while time.monotonic() - t0 < s:
        sample(f"{tag} t+{time.monotonic() - t0:5.1f}s")
        n += 1
        while time.monotonic() - t0 < n * every and time.monotonic() - t0 < s:
            time.sleep(0.5)  # sample cadence only


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hold-s", type=float, default=180)
    ap.add_argument("--bounce-n", type=int, default=3)
    ap.add_argument("--post-s", type=float, default=40)
    a = ap.parse_args()
    orig = {l: t.admin_up(l) for l in t.RESTORE}
    log(f"original admin {orig}")
    try:
        for l in (lan for lan, _ in LANS):
            t.ssh(t.SW1, f"ip link set {l} up")
        log(" + ".join(lan for lan, _ in LANS) + " up")
        wait(a.hold_s, "hold", 20)
        for lan, xe in LANS:
            for i in range(a.bounce_n):
                t.ssh(t.SW1, f"ip link set {lan} down")
                time.sleep(3)  # user spec: down 3 s
                t.ssh(t.SW1, f"ip link set {lan} up")
                log(f"bounce {lan} #{i + 1} up")
                wait(a.post_s, f"{lan}-b{i + 1}", 10)
    finally:
        for l, up in {_DB.sw1_peer(50): False, _DB.sw1_peer(49): True}.items():
            t.ssh(t.SW1, f"ip link set {l} {'up' if up else 'down'}")
        log("restored: " + str({l: t.admin_up(l) for l in t.RESTORE}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
