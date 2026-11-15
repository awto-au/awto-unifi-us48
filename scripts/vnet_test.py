#!/usr/bin/env python3
"""vnet_test — us48-vnet a1 <-> a2 test (#85): start a2 Linux, ping both ways, 10 MB TCP both ways, stop/start cycles, a1 module reload.

- Board must be up with the image's us48_pci (S04us48-net). Stages us48_a2.ko / us48_vnet.ko from the given profile's stage
  dir, the a2 zImage + DTB + boot stub; loads us48_a2 (payload=1) and us48_vnet on a1; a1 vnet0 = 10.48.0.1/30.
- a2 side: /init loads us48_vnet and sets vnet0 10.48.0.2/30; commands are typed on /dev/ttyA20 (DCC), output in /tmp/a2.out.
- TCP: busybox nc, 10 MiB of zeros; MB/s = 10.486 MB / wall time of the sender (nc exit), byte count checked on the receiver.
- Waits: a2 banner+vnet 150 s (debug-profile a2 boots in 50 s; normal 3 s) is a backstop, command limits are per call below.
Usage: vnet_test.py <label> [--mhz 100|1000] [--profile normal|debug] [--cycles N] [--reload] [--no-stage] [--no-tcp] [--dtb NAME] [--traffic]
Log: tmp/logs/vnet_test.log
"""
import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LX = ROOT / "scripts/lx.py"
LOG = ROOT / "tmp/logs/vnet_test.log"
REL = {"normal": "7.4.0-rc1-norm+", "debug": "7.4.0-rc1-dbg+"}

PRE = r"""
D=/sys/class/remoteproc/remoteproc0
u() { read a b < /proc/uptime; echo ${a%.*}${a#*.}; }
[ -p /tmp/f ] || mkfifo /tmp/f; exec 3<>/tmp/f
# w FILE PATTERN LIMIT_CS: wait for the pattern (limit = expiry, returns 1)
w() { t0=$(u); while ! grep -q "$2" $1 && [ $(( $(u) - t0 )) -lt $3 ]; do read -t 0.2 x <&3; done; grep -q "$2" $1; }
# a2c CMD LIMIT_CS: type CMD on the a2 console, wait for the marker, print the new output
a2c() { n=$(( $(cat /tmp/a2.n 2>/dev/null || echo 0) + 1 )); echo $n > /tmp/a2.n; off=$(wc -c < /tmp/a2.out)
  printf '%s\necho\necho DO""NE%s\n' "$1" $n > /dev/ttyA20
  w /tmp/a2.out "^DONE$n" $2 || echo "a2c: no marker after $2 cs"; tail -c +$((off+1)) /tmp/a2.out; }
"""


def lx(cmd: str, pre: bool = True) -> str:
    r = subprocess.run([sys.executable, str(LX), (PRE if pre else "") + cmd], capture_output=True, text=True)
    out = r.stdout + r.stderr
    with LOG.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} $ {cmd.strip()[:300]}\n{out}")
    return out


def put(src: Path, dst: str) -> None:
    r = subprocess.run([sys.executable, str(LX), "--put", str(src), dst], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"put {src}: {r.stderr}")


def say(msg: str) -> None:
    print(msg, flush=True)
    with LOG.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} == {msg}\n")


def start_a2(limit_cs: int = 15000) -> str:
    """Reader on /dev/ttyA20 first (DCC must be drained), then start; wait for the vnet line or the failure line."""
    return lx(f"""
[ "$(cat $D/state)" = offline ] || echo stop > $D/state
[ -f /tmp/a2.pid ] && kill $(cat /tmp/a2.pid) 2>/dev/null
rm -f /tmp/a2.out; : > /tmp/a2.out
setsid sh -c 'echo $$ > /tmp/a2.pid; exec cat /dev/ttyA20 > /tmp/a2.out' < /dev/null > /dev/null 2>&1 &
t0=$(u); echo start > $D/state
w /tmp/a2.out "vnet0 10.48.0.2/30 up\\|insmod us48_vnet failed\\|Kernel panic" {limit_cs}
echo wall_cs=$(( $(u) - t0 ))
grep -a "us48-a2:" /tmp/a2.out
""")


def pings(n: int = 10) -> None:
    out = lx(f"ping -c {n} 10.48.0.2 | tail -3")
    say("a1 -> a2 ping: " + " | ".join(l.strip() for l in out.strip().splitlines()[-2:]))
    out = lx(f'a2c "ping -c {n} 10.48.0.1 | tail -3" 3000')
    say("a2 -> a1 ping: " + " | ".join(l.strip() for l in out.strip().splitlines() if "packets" in l or "round-trip" in l))


def tcp() -> None:
    lx("dd if=/dev/zero of=/tmp/10m bs=1048576 count=10 2>/dev/null; ls -l /tmp/10m | wc -l")
    # a1 -> a2
    lx('a2c "(nc -l -p 5001 | wc -c) &" 200')
    out = lx("t0=$(u); nc 10.48.0.2 5001 < /tmp/10m; t1=$(u); echo cs=$((t1-t0)); w /tmp/a2.out '# 10485760\\|^10485760' 3000; echo rx_seen=$?")
    m = re.search(r"cs=(\d+)", out)
    cs = int(m[1]) if m else 0
    say(f"TCP a1 -> a2: 10 MiB in {cs / 100:.2f} s = {10.486 / (cs / 100):.2f} MB/s, receiver bytes seen: {'rx_seen=0' in out}" if cs else f"TCP a1 -> a2 failed: {out}")
    # a2 -> a1
    lx("rm -f /tmp/rx2; setsid sh -c 'nc -l -p 5002 | wc -c > /tmp/rx2' < /dev/null > /dev/null 2>&1 &")
    out = lx("""a2c "time sh -c 'dd if=/dev/zero bs=1048576 count=10 2>/dev/null | nc 10.48.0.1 5002'" 6000""")
    m = re.search(r"real\s+(\d+)m\s+([\d.]+)s", out)
    t = int(m[1]) * 60 + float(m[2]) if m else 0.0
    rx = lx("w /tmp/rx2 '10485760' 500; cat /tmp/rx2")
    say(f"TCP a2 -> a1: 10 MiB in {t:.2f} s = {10.486 / t:.2f} MB/s, receiver: {rx.strip()}" if t else f"TCP a2 -> a1 failed: {out} {rx}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("label")
    ap.add_argument("--mhz", type=int, default=100)
    ap.add_argument("--profile", choices=("normal", "debug"), default="normal")
    ap.add_argument("--cycles", type=int, default=0)
    ap.add_argument("--reload", action="store_true")
    ap.add_argument("--no-stage", action="store_true")
    ap.add_argument("--no-tcp", action="store_true")
    ap.add_argument("--traffic", action="store_true", help="each cycle: stop a2 while a 10 MiB TCP transfer a1 -> a2 is running (#102)")
    ap.add_argument("--dtb", help="a2 DTB under tmp/a2 (default us48-a2.dtb at 100 MHz, else us48-a2-<mhz>.dtb; --l2 builds: us48-a2-<mhz>-l2.dtb)")
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    say(f"== {a.label}: a2 {a.mhz} MHz, profile {a.profile}")
    if not a.no_stage:
        mod = ROOT / f"tmp/mainline/rootfs-{a.profile}/lib/modules/{REL[a.profile]}"
        dtb = ROOT / "tmp/a2" / (a.dtb or ("us48-a2.dtb" if a.mhz == 100 else f"us48-a2-{a.mhz}.dtb"))
        for f, d in ((mod / "us48_a2.ko", "/tmp/us48_a2.ko"), (mod / "us48_vnet.ko", "/tmp/us48_vnet.ko"),
                     (ROOT / "tmp/a2/zImage", "/lib/firmware/us48-a2-zImage"), (dtb, "/lib/firmware/us48-a2.dtb"),
                     (ROOT / "tmp/a2fw/us48-a2-boot.elf", "/lib/firmware/us48-a2-boot.elf")):
            put(f, d)
    out = lx(f"""
[ -d /sys/class/remoteproc/remoteproc0 ] || insmod /tmp/us48_a2.ko payload=1 || echo FAIL insmod a2
[ "$(cat $D/state)" = offline ] || echo stop > $D/state
echo {a.mhz} > /sys/module/us48_a2/parameters/cpu_mhz
echo us48-a2-boot.elf > $D/firmware
[ -d /sys/module/us48_vnet ] || insmod /tmp/us48_vnet.ko || echo FAIL insmod vnet
ip addr show vnet0 | grep -q 10.48.0.1 || ip addr add 10.48.0.1/30 dev vnet0
ip link set vnet0 up
cat /sys/module/us48_a2/parameters/cpu_mhz
""")
    say("a1 setup: " + out.strip().replace("\n", " | "))
    out = start_a2()
    say("a2 start: " + out.strip().replace("\n", " | ")[-400:])
    if "vnet0 10.48.0.2/30 up" not in out:
        say("FAIL: a2 vnet did not come up")
        return 1
    pings()
    if not a.no_tcp:
        tcp()
    for i in range(1, a.cycles + 1):
        if a.traffic:
            lx('a2c "(nc -l -p 5001 > /dev/null) &" 200')
            lx("dd if=/dev/zero of=/tmp/10m bs=1048576 count=10 2>/dev/null; (nc 10.48.0.2 5001 < /tmp/10m &) ; read -t 1.5 x <&3")
        out = lx("echo stop > $D/state; cat $D/state; dmesg | grep vnet0 | tail -2")
        say(f"cycle {i} stop: " + out.strip().replace("\n", " | "))
        out = start_a2()
        up = "vnet0 10.48.0.2/30 up" in out
        p = lx("ping -c 3 10.48.0.2 | tail -2")
        say(f"cycle {i} start: a2 up={up}; a1 ping: " + p.strip().replace("\n", " | "))
    if a.reload:
        out = lx("""rmmod us48_vnet; echo rmmod rc=$?; insmod /tmp/us48_vnet.ko; echo insmod rc=$?
ip addr add 10.48.0.1/30 dev vnet0; ip link set vnet0 up
read -t 1 x <&3; ping -c 5 10.48.0.2 | tail -2; dmesg | tail -4""")
        say("a1 reload: " + out.strip().replace("\n", " | "))
        pings(5)
    say("halt stats: " + lx("T=/sys/kernel/debug/remoteproc/remoteproc0; echo n=$(cat $T/halt_n) fail=$(cat $T/halt_fail) max_us=$(cat $T/halt_max_us); dmesg | grep 'a2 halt' | tail -3", pre=False).strip().replace("\n", " | "))
    say("dmesg problems: " + lx("dmesg | grep -i 'kasan\\|BUG:\\|WARNING\\|Oops\\|Call trace\\|rx ring' | head; echo end").strip().replace("\n", " | "))
    return 0


if __name__ == "__main__":
    sys.exit(main())
