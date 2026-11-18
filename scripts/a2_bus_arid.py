#!/usr/bin/env python3
"""a2_bus_arid — a2 bustest bandwidth per unit 0 RX_FREE_ARID_COUNT (`us48_a2 rx_arid`) and unit 1 AXI clock (`axi_mhz`) (#79, #80, #88).

Usage: a2_bus_arid.py [--values 3 2 1 0] [--axi 200 400] [--reps 3] [--mhz 1000]
- Board: debug image with `us48_a2.ko` + `us48_bustest.ko` loaded; a2 runs `us48-a2-bustest.elf` (no Linux on a2, a1 idle).
- Results page = debugfs `us48_bustest/a2` (the module maps the a2 pool through us48_a2, #89).
- Ticks: global timer = ARM/2, so BT_TICK_HZ is set to mhz/2 (header value 200 MHz is for ARM 400).
- Metrics are pcie_harness.a2_metrics of one pass; each rep waits for a new `pass` value. Median of --reps per value.
Log: tmp/logs/a2_bus_arid.log; json tmp/logs/a2_bus_arid.json.
"""
import _ipreg  # noqa: E402
import argparse
import json
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import pcie_harness as ph  # noqa: E402

LOG = ROOT / "tmp/logs/a2_bus_arid.log"
JSON = ROOT / "tmp/logs/a2_bus_arid.json"
HOST = f"root@{_ipreg.ip('us48-boot')}"
OPTS = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
        "-o", "ConnectTimeout=8"]
# a2 first pass ~0.5 s (docs/pcie-harness.md), pass period about the same; wait 3 s (6x) for the first, 3 s for each next, expiry = error
WAIT_S = 3.0
CALL_S = 30


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def ssh(cmd: str, binary: bool = False):
    r = subprocess.run(["ssh", "-T", *OPTS, HOST, cmd], capture_output=True, timeout=CALL_S)
    if r.returncode:
        raise RuntimeError(f"ssh rc {r.returncode}: {cmd[:80]}\n{r.stdout[-200:]!r}{r.stderr[-200:]!r}")
    return r.stdout if binary else r.stdout.decode(errors="replace")


def read_page() -> dict:
    line = ssh("cat /sys/kernel/debug/us48_bustest/a2")
    if "a2=running" not in line:
        return {"magic": 0, "pass": 0, "hb": 0, "mmu": 0}
    return {k: int(v, 0) for k, v in ph.kv(line).items() if re.fullmatch(r"-?\w+", v) and k != "a2"}


START = r"""
D=/sys/class/remoteproc/remoteproc0; P=/sys/module/us48_a2/parameters
[ "$(cat $D/state)" = offline ] || echo stop > $D/state
echo 0 > $P/payload; echo @O@ > $P/paxb_order; echo @AXI@ > $P/axi_mhz; echo @MHZ@ > $P/cpu_mhz; echo @V@ > $P/rx_arid
echo us48-a2-bustest.elf > $D/firmware
echo start > $D/state && echo STARTED
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--values", type=int, nargs="+", default=[3, 2, 1, 0])
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--mhz", type=int, default=1000)
    ap.add_argument("--axi", type=int, nargs="+", default=[200], help="us48_a2 axi_mhz values (unit 1 powers on at 400, module default 200)")
    ap.add_argument("--order", type=int, default=1, help="us48_a2 paxb_order (EP_TX_WR_RD_ORDER); 0 = a2 reads may pass writes")
    a = ap.parse_args()
    c = ph.header_consts()
    c["BT_TICK_HZ"] = a.mhz * 1000000 // 2	# global timer = PERIPHCLK = ARM/2 (BT_TICK_HZ in the header is the 400 MHz value)
    res = {}
    try:
        for axi, v in [(x, y) for x in a.axi for y in a.values]:
            if "STARTED" not in ssh(START.replace("@V@", str(v)).replace("@MHZ@", str(a.mhz)).replace("@AXI@", str(axi)).replace("@O@", str(a.order))):
                raise RuntimeError("a2 start failed")
            reg = ssh("devmem 0x1801210c 32").strip()
            samples, last = [], -1
            t0 = time.monotonic()
            while len(samples) < a.reps + 1:	# first pass is cold, dropped
                p = read_page()
                if p["magic"] == c["BT_MAGIC"] and p["pass"] != last and p["pass"] >= 1 and p["mmu"]:
                    last = p["pass"]
                    samples.append(p)
                    t0 = time.monotonic()
                elif time.monotonic() - t0 > WAIT_S:
                    raise RuntimeError(f"axi {axi} rx_arid {v}: no new pass in {WAIT_S} s (magic {p['magic']:#x} pass {p['pass']} hb {p['hb']})")
            bad = [s["pass"] for s in samples if s["mismatch"] or s["y_mismatch"]]
            ms = [ph.a2_metrics(s, c) for s in samples[1:]]
            med = {k: statistics.median(m[k] for m in ms) for k in ms[0] if all(m.get(k) is not None for m in ms)}
            res[f"axi{axi}_arid{v}"] = {"reg": reg, "median": med, "all": ms, "mismatch_passes": bad, "passes": [s["pass"] for s in samples]}
            log(f"order {a.order} mhz {a.mhz} axi {axi} rx_arid {v} reg {reg}: " + " ".join(f"{k}={x:.1f}" for k, x in med.items() if k in (
                "a2_rd_c_MBps", "a2_wr_c_MBps", "a2_cpy_c_MBps", "a2_rd_u_MBps", "a2_wr_u_MBps", "a2_lat_u_ns", "a2_lat_line_ns"))
                + f" mismatch_passes={bad}")
    finally:
        try:
            ssh("echo stop > /sys/class/remoteproc/remoteproc0/state; devmem 0x1801210c 32")
        except Exception as e:  # noqa: BLE001
            log(f"cleanup failed: {e}")
        JSON.write_text(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
