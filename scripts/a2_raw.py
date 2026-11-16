#!/usr/bin/env python3
"""a2_raw — run an a2 ELF on the board (debugfs mounted, us48_a2.ko in /lib/modules) and print trace0 + pcsr (#79).

Usage: a2_raw.py ELF_NAME [cpu_mhz] [wait_s] [axi_mhz] [k=v ...]   (k=v = us48_a2 module params set before start, e.g. dcc_poll=0 hdbgen_off=1; dcc_poll and hdbgen_off reset to 1/0 when not given; ELF in /lib/firmware; cpu_mhz 0 = leave PLL; axi_mhz = unit 1 GENPLL ch3, default
keeps the module param (200); log tmp/logs/a2_raw.log). Last line: verdict (pcsr at the undefined vector 0xffff000c or r/m != 0 = FAIL).
Wait: trace0 re-read every 0.2 s until it stops changing for 1 s (a trace with `done=` only ends at done=00000001); wait_s (default 5) is the cap. rawtest runs < 1 s at 1000 MHz.
"""
import re
import subprocess
import sys
import time
from pathlib import Path
from _cli import help_guard  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REMOTE = r"""
D=/sys/class/remoteproc/remoteproc0; T=/sys/kernel/debug/remoteproc/remoteproc0
[ -p /tmp/f ] || mkfifo /tmp/f; exec 3<>/tmp/f
lsmod | grep -q us48_a2 || insmod /lib/modules/*/us48_a2.ko
[ "$(cat $D/state)" = offline ] || echo stop > $D/state
echo @MHZ@ > /sys/module/us48_a2/parameters/cpu_mhz
P=/sys/module/us48_a2/parameters
echo 1 > $P/dcc_poll; echo 0 > $P/hdbgen_off
@PARAMS@
[ -n "@AXI@" ] && echo @AXI@ > /sys/module/us48_a2/parameters/axi_mhz
echo @ELF@ > $D/firmware
echo start > $D/state
n=0; last=""; same=0
while [ $n -lt @N@ ]; do
  read -t 0.2 x <&3; n=$((n+1)); cur=$(cat $T/trace0)
  if [ "$cur" = "$last" ]; then same=$((same+1)); else same=0; fi
  case "$cur" in *done=00000001*) break;; *done=*) same=0;; esac  # stress ELF: trace stays constant until done
  last="$cur"; [ $same -ge 5 ] && break
done
echo "$cur"; echo "poll=$n"; cat $T/pcsr | tail -1; tail -2 $T/ccu
"""


def main() -> int:
    a = sys.argv[1:]
    elf, mhz, wait = a[0], a[1] if len(a) > 1 else "0", float(a[2]) if len(a) > 2 else 5.0
    axi = a[3] if len(a) > 3 else ""
    params = "\n".join(f"echo {kv.split('=')[1]} > $P/{kv.split('=')[0]}" for kv in a[4:])
    r = subprocess.run([sys.executable, str(ROOT / "scripts/lx.py"),
                        REMOTE.replace("@ELF@", elf).replace("@MHZ@", mhz).replace("@AXI@", axi).replace("@PARAMS@", params).replace("@N@", str(int(wait / 0.2)))],
                       capture_output=True, text=True)
    out = r.stdout + r.stderr
    print(out)
    bad = "pass=" not in out or "pcsr 0xffff000c" in out or re.search(r"[mr]=(?!0{8})[0-9a-f]{8}", out) is not None
    verdict = f"verdict cpu {mhz} axi {axi or 'param'} {' '.join(a[4:])}: {'FAIL' if bad else 'ok'}"
    print(verdict)
    (ROOT / "tmp/logs/a2_raw.log").open("a").write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {a}\n{out}\n{verdict}\n")
    return r.returncode


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
