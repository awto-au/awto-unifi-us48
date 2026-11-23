#!/usr/bin/env python3
"""loop_throughput: TCP rate over the ge8 <-> ge17 loop cable, both ends on the board (CPU-path bench for #170/#174).

Usage: scripts/loop_throughput.py [--mb 50] [--tx ge8] [--rx ge17]
- Receiver in a netns (as frame_size_test: unshare + nsenter), `nc -l` there; sender `dd | nc` in the root ns. Both ends load the same A9, so the number is
  the CPU-path rate of RX + TX together. Prints MB, seconds, Mb/s, DMA interrupt counts before/after and `top` idle.
- Needs the loop cable, both ports free (not in a bridge). Restores everything. Limit 60 s per run (50 MB at 100 Mb/s = 4 s, 15x); expiry = FAIL.
Log: tmp/logs/loop_throughput.log
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _ipreg  # noqa: E402
from _cli import help_guard  # noqa: E402
from lx import OPTS  # noqa: E402

LOG = Path(__file__).resolve().parent.parent / "tmp/logs/loop_throughput.log"


def sh(cmd, timeout=120):
    r = subprocess.run(["ssh", *OPTS, f"root@{_ipreg.ip('us48-boot')}", cmd], capture_output=True, text=True, timeout=timeout)
    return (r.stdout + r.stderr).strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mb", type=int, default=50)
    ap.add_argument("--tx", default="ge8")
    ap.add_argument("--rx", default="ge17")
    a = ap.parse_args()
    st = "/tmp/lpt"
    ns = f"nsenter -t $(cat {st}/A.pid) -n"
    setup = (f"mkdir -p {st}; rm -f {st}/hold; mkfifo {st}/hold; setsid unshare -n sh -c 'read x <> {st}/hold' </dev/null >/dev/null 2>&1 & "
             f"echo $! > {st}/A.pid; ip link set {a.rx} down; ip link set {a.rx} netns $(cat {st}/A.pid); "
             f"{ns} sh -c 'ip link set lo up; ip addr add 10.99.1.2/24 dev {a.rx}; ip link set {a.rx} up'; "
             f"ip addr add 10.99.1.1/24 dev {a.tx}; ip link set {a.tx} up; echo setup rc=$?")
    print(sh(setup))
    t0 = time.monotonic()
    while time.monotonic() - t0 < 15:   # copper negotiation ~3 s; 15 s = 5x
        c = sh(f"echo $(cat /sys/class/net/{a.tx}/carrier) $({ns} cat /sys/class/net/{a.rx}/carrier)")
        if c.split()[-2:] == ["1", "1"]:
            break
    cmd = (f"grep us48-cmic-dma /proc/interrupts | tr -s ' ' | cut -d' ' -f3 | tr '\\n' ' '; "
           f"({ns} sh -c 'nc -l -p 5002 > /dev/null' &) ; sleep 1; "
           f"read up _ < /proc/uptime; echo T0 $up; dd if=/dev/zero bs=1M count={a.mb} 2>&1 | nc 10.99.1.2 5002 2>&1; read up _ < /proc/uptime; echo T1 $up; grep us48-cmic-dma /proc/interrupts | tr -s ' ' | cut -d' ' -f3 | tr '\\n' ' '")
    try:
        out = sh(cmd, timeout=90)
    finally:
        tt = {w: float(out.split(w)[1].split()[0]) for w in ("T0", "T1") if w in out}
    secs = tt["T1"] - tt["T0"] if len(tt) == 2 else 0
    teardown = sh(f"pkill nc; {ns} sh -c 'ip link set {a.rx} netns 1'; ip addr flush dev {a.tx}; ip link set {a.rx} up; "
                  f"kill $(cat {st}/A.pid); rm -f {st}/A.pid {st}/hold; echo restored")
    rate = f"{a.mb * 8 / secs:.0f} Mb/s in {secs:.2f} s" if secs else "n/a"
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {a.mb} MB {rate} | {out.replace(chr(10), ' ')} | {teardown}"
    print(line)
    with LOG.open("a") as fh:
        fh.write(line + "\n")
    return 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
