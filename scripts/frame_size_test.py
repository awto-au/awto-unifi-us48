#!/usr/bin/env python3
"""frame_size_test: largest frame the unit 0 copper MACs pass, over the ge8 <-> ge17 loop cable (#173).

Usage: scripts/frame_size_test.py [--mtus 1500,1504,1508,1518] [--tx ge8] [--rx ge17]
- The receiver sits in a netns made like switchdev_test's host A (unshare + nsenter); TX and RX MTU are both raised, then `ping -s <mtu-28>` sends a frame of
  mtu + 18 bytes on the wire (14 header + 4 FCS). 3 pings per size; the table shows replies per MTU, so the first failing MTU gives the MAC limit.
- Needs the loop cable and both ports free (not in a bridge). Restores MTU 1500 and removes the netns. Board address from _ipreg.
- Waits: 3 pings x 1 s timeout = 6 s per MTU worst case (one ARP exchange + 3 echoes on a direct cable are ms); expiry = counted as lost.
Log: tmp/logs/frame_size_test.log
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

LOG = Path(__file__).resolve().parent.parent / "tmp/logs/frame_size_test.log"


def sh(cmd, timeout=120):
    r = subprocess.run(["ssh", *OPTS, f"root@{_ipreg.ip('us48-boot')}", cmd], capture_output=True, text=True, timeout=timeout)
    return (r.stdout + r.stderr).strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mtus", default="1500,1504,1508,1518")
    ap.add_argument("--tx", default="ge8")
    ap.add_argument("--rx", default="ge17")
    a = ap.parse_args()
    mtus = [int(x) for x in a.mtus.split(",")]
    LOG.parent.mkdir(parents=True, exist_ok=True)
    st, ns = "/tmp/fst", None
    ns = f"nsenter -t $(cat {st}/A.pid) -n"
    # holder netns as switchdev_test (busybox has no `ip netns`): a process in a new netns blocked on a fifo
    setup = (f"mkdir -p {st}; rm -f {st}/hold; mkfifo {st}/hold; setsid unshare -n sh -c 'read x <> {st}/hold' </dev/null >/dev/null 2>&1 & "
             f"echo $! > {st}/A.pid; ip link set {a.rx} down; ip link set {a.rx} netns $(cat {st}/A.pid); "
             f"{ns} sh -c 'ip link set lo up; ip addr add 10.99.0.2/24 dev {a.rx}; ip link set {a.rx} up'; "
             f"ip addr add 10.99.0.1/24 dev {a.tx}; ip link set {a.tx} up; echo setup rc=$?")
    out = [sh(setup)]
    # link-up wait: copper ports negotiate in about 3 s; poll both carriers, expiry 15 s = about 5x
    t0 = time.monotonic()
    while time.monotonic() - t0 < 15:
        c = sh(f"echo $(cat /sys/class/net/{a.tx}/carrier 2>&1) $({ns} cat /sys/class/net/{a.rx}/carrier 2>&1)")
        if c.split()[-2:] == ["1", "1"]:
            break
    out.append(f"carrier {a.tx} {a.rx}: {c}")
    rows = []
    for m in mtus:
        r = sh(f"ip link set {a.tx} mtu {m}; {ns} ip link set {a.rx} mtu {m}; "
               f"ping -c 3 -W 1 -s {m - 28} 10.99.0.2 2>&1 | grep -E 'packets transmitted|bad|too'")
        rows.append(f"mtu {m} (frame {m + 18} B on the wire): {r.replace(chr(10), ' | ')}")
    out.append(sh(f"ip link set {a.tx} mtu 1500; {ns} sh -c 'ip link set {a.rx} mtu 1500; ip addr flush dev {a.rx}; "
                  f"ip link set {a.rx} netns 1'; ip addr flush dev {a.tx}; kill $(cat {st}/A.pid); rm -f {st}/A.pid {st}/hold; echo restored rc=$?"))
    text = "\n".join(out + rows)
    print(text)
    with LOG.open("a") as fh:
        fh.write(time.strftime("%Y-%m-%dT%H:%M:%S%z\n") + text + "\n")
    return 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
