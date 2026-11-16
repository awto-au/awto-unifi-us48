#!/usr/bin/env python3
"""a2_boot_cycles — N a2 Linux boots (start, wait for the vnet marker, stop); count oops/panics (#103).

- Board: any a1 image with us48_a2 + us48_vnet (normal image: no debugfs). Stages modules, a2 zImage, DTB, boot stub; one ssh call runs the loop.
- Each cycle: start; wait for `vnet0 10.48.0.2/30 up` (a2 /init) or an oops/panic line; stop. Oops text kept in /tmp/oops.<n> on a1, printed.
- Wait limit: a2 normal boot 1.5 s (L2 on) / 2.3 s (L2 off) to vnet up, docs/a2-linux.md; 4.5 s = ~2x (a failed boot ends it earlier on the oops line).
Usage: a2_boot_cycles.py <label> [--cycles 30] [--mhz 1000] [--dtb NAME] [--profile normal] [--no-stage] [k=v ...]
Log: tmp/logs/a2_boot_cycles.log
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LX = ROOT / "scripts/lx.py"
LOG = ROOT / "tmp/logs/a2_boot_cycles.log"
REL = {"normal": "7.4.0-rc1-norm+", "debug": "7.4.0-rc1-dbg+"}
REMOTE = r"""
D=/sys/class/remoteproc/remoteproc0
u() { read a b < /proc/uptime; echo ${a%.*}${a#*.}; }
[ -p /tmp/f ] || mkfifo /tmp/f; exec 3<>/tmp/f
[ -d $D ] || insmod /tmp/us48_a2.ko payload=1 || echo FAIL insmod a2
[ "$(cat $D/state)" = offline ] || echo stop > $D/state
echo @MHZ@ > /sys/module/us48_a2/parameters/cpu_mhz
@PARAMS@
echo us48-a2-boot.elf > $D/firmware
[ -d /sys/module/us48_vnet ] || insmod /tmp/us48_vnet.ko || echo FAIL insmod vnet
ip addr show vnet0 | grep -q 10.48.0.1 || ip addr add 10.48.0.1/30 dev vnet0
ip link set vnet0 up
[ -f /tmp/a2.pid ] && kill $(cat /tmp/a2.pid) 2>/dev/null
setsid sh -c 'echo $$ > /tmp/a2.pid; exec cat /dev/ttyA20 > /tmp/a2.out' < /dev/null > /dev/null 2>&1 &
BAD='Unable to handle\|Internal error\|Oops\|Kernel panic\|BUG:\|UBSAN'
ok=0; bad=0; none=0; i=0
while [ $i -lt @N@ ]; do
  i=$((i+1)); : > /tmp/a2.out
  echo start > $D/state 2>/tmp/err; rc=$?
  t0=$(u)
  if [ @PING@ = 1 ]; then
    while ! ping -c 1 -W 1 10.48.0.2 > /dev/null 2>&1 && [ $(( $(u) - t0 )) -lt 450 ]; do :; done
    [ $(( $(u) - t0 )) -lt 450 ] && echo "vnet0 10.48.0.2/30 up" > /tmp/a2.out
  else
  while ! grep -q "vnet0 10.48.0.2/30 up\|$BAD" /tmp/a2.out && [ $(( $(u) - t0 )) -lt 450 ]; do read -t 0.05 x <&3; done
  fi
  cs=$(( $(u) - t0 ))
  if grep -q "$BAD" /tmp/a2.out; then
    read -t 0.3 x <&3; cp /tmp/a2.out /tmp/oops.$i; bad=$((bad+1)); r=OOPS
  elif grep -q "vnet0 10.48.0.2/30 up" /tmp/a2.out; then ok=$((ok+1)); r=ok
  else none=$((none+1)); r=NOMARKER; cp /tmp/a2.out /tmp/oops.$i; fi
  echo stop > $D/state 2>>/tmp/err; src=$?
  [ @GAP@ -gt 0 ] && { g0=$(u); while [ $(( $(u) - g0 )) -lt @GAP@ ]; do read -t 0.05 x <&3; done; }
  echo "cycle $i start_rc=$rc $r cs=$cs stop_rc=$src $(cat /tmp/err | tr '\n' ' ')"; : > /tmp/err
done
echo "SUMMARY ok=$ok oops=$bad nomarker=$none of @N@"
dmesg | grep "a2 halt" | tail -5
for f in /tmp/oops.*; do [ -f "$f" ] && { echo "== $f"; grep -a -B2 -A25 "$BAD" $f | head -60; }; done
rm -f /tmp/oops.*
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("label")
    ap.add_argument("--cycles", type=int, default=30)
    ap.add_argument("--mhz", type=int, default=1000)
    ap.add_argument("--profile", default="normal")
    ap.add_argument("--dtb", default=None, help="DTB under tmp/a2 (default us48-a2-<mhz>-l2.dtb)")
    ap.add_argument("--gap-cs", type=int, default=0, help="wait after each stop before the next start (centiseconds; diagnostic)")
    ap.add_argument("--ping", action="store_true", help="marker = ping answered by a2 (no DCC reads; use with dcc_poll=0)")
    ap.add_argument("--no-stage", action="store_true")
    ap.add_argument("kv", nargs="*", help="us48_a2 module params k=v")
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if not a.no_stage:
        mod = ROOT / f"tmp/mainline/rootfs-{a.profile}/lib/modules/{REL[a.profile]}"
        dtb = ROOT / "tmp/a2" / (a.dtb or f"us48-a2-{a.mhz}-l2.dtb")
        for src, dst in ((mod / "us48_a2.ko", "/tmp/us48_a2.ko"), (mod / "us48_vnet.ko", "/tmp/us48_vnet.ko"),
                         (ROOT / "tmp/a2/zImage", "/lib/firmware/us48-a2-zImage"), (dtb, "/lib/firmware/us48-a2.dtb"),
                         (ROOT / "tmp/a2fw/us48-a2-boot.elf", "/lib/firmware/us48-a2-boot.elf")):
            r = subprocess.run([sys.executable, str(LX), "--put", str(src), dst], capture_output=True, text=True)
            if r.returncode:
                sys.exit(f"put {src}: {r.stderr}")
    params = "\n".join(f"echo {k.split('=')[1]} > /sys/module/us48_a2/parameters/{k.split('=')[0]}" for k in a.kv)
    cmd = REMOTE.replace("@N@", str(a.cycles)).replace("@MHZ@", str(a.mhz)).replace("@PARAMS@", params).replace("@GAP@", str(a.gap_cs)).replace("@PING@", "1" if a.ping else "0")
    r = subprocess.run([sys.executable, str(LX), cmd], capture_output=True, text=True)
    out = r.stdout + r.stderr
    with LOG.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {a.label} mhz {a.mhz} cycles {a.cycles} dtb {a.dtb}\n{out}\n")
    print(out)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
