#!/usr/bin/env python3
"""stock_sw1_probe — stock Linux running (stock_ramboot.py): sample stock xe49/xe50 PCS state against sw1 lan6/lan3 over time (#78 control).

Usage: scripts/stock_sw1_probe.py --sw1 lan6 --xe 49 [--samples 8] [--period-s 5]
Each sample: stock lane regs 0xc154 (RX PCS live: b0 SYNC b1 LINK b7 RF b8 LF), 0xc120 (TX encode status), 0xc150, XLMAC RX_LSS (tools/tsc_read via ssh,
read-only) + sw1 carrier and TGR_STD_1 reg0/reg1. sw1 admin: lan3/lan6 recorded first, only --sw1 bounced (down, up), restored in finally. lan5 refused.
Bounds: ssh 10 s per call; 8 x 5 s = 40 s = one sw1 calibration verdict (~30 s, #78) + margin. Log: tmp/logs/stock_sw1_probe.log
"""
import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sw1_link_test as t  # noqa: E402
import tsc_lane_dump as d  # noqa: E402
import hwdb  # noqa: E402

LOG = t.ROOT / "tmp/logs/stock_sw1_probe.log"


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def stock_regs(host: str, front: int) -> str:
    a, lane = d.FRONTS[front]
    req = "".join(f"{a} {lane} {r:x}\n" for r in (0xc154, 0xc120, 0xc150)) + f"S 2 {0x60b00 | lane:x}\n"
    r = d.ssh(host, f"/tmp/tsc_read {hwdb.cmic_base(1):#010x}", stdin=req.encode(), timeout=10)
    got = [x.split() for x in r.stdout.decode().splitlines()]
    if len(got) != 4:
        return f"stock read failed rc={r.returncode} {r.stderr[-80:]!r}"
    return f"c154={int(got[0][3], 16):04x} c120={int(got[1][3], 16):04x} c150={int(got[2][3], 16):04x} RX_LSS={int(got[3][4], 16):x}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sw1", required=True)
    ap.add_argument("--xe", type=int, required=True)
    ap.add_argument("--samples", type=int, default=8)
    ap.add_argument("--period-s", type=float, default=5.0)
    ap.add_argument("--host", default=os.environ.get("US48_SWITCH_IP", "192.168.25.206"))
    a = ap.parse_args()
    if not re.fullmatch(r"lan[1-8]", a.sw1) or a.sw1 == "lan5":
        sys.exit("refusing sw1 port")
    orig = {l: t.admin_up(l) for l in t.RESTORE}
    log(f"original admin state {orig}")
    try:
        log(f"stock before bounce: {stock_regs(a.host, a.xe)}")
        t.ssh(t.SW1, f"ip link set {a.sw1} down")
        read = time.monotonic()
        log(f"sw1 {a.sw1} down; stock: {stock_regs(a.host, a.xe)}")
        t.ssh(t.SW1, f"ip link set {a.sw1} up")
        t0 = time.monotonic()
        for i in range(a.samples):
            c = t.ssh(t.SW1, f"cat /sys/class/net/{a.sw1}/carrier").strip()
            log(f"t+{time.monotonic() - t0:4.1f}s sw1 carrier={c} {t.sds(a.sw1).split('TGR')[-1]} | stock {stock_regs(a.host, a.xe)}")
            while time.monotonic() - t0 < (i + 1) * a.period_s:
                time.sleep(0.2)  # sample period: sw1 calibration verdict cadence ~30 s, 5 s resolves it
    finally:
        for l, up in orig.items():
            t.ssh(t.SW1, f"ip link set {l} {'up' if up else 'down'}")
        log("restored: " + str({l: t.admin_up(l) for l in t.RESTORE}) + f" (want {orig})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
