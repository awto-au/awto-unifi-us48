#!/usr/bin/env python3
"""a2_halt_cycles — a2 stop/start cycles, record the halt wait of every start and stop (#102).

- Board: debug image (debugfs), us48_a2.ko staged by this script, payload=1 (Linux zImage + DTB + boot stub in /lib/firmware).
- Each cycle: start (halt in start()), a2 runs RUN_CS, stop (halt in stop()). Halt times = debugfs halt_last_us (us48_a2).
- Failures show as `a2 halt:` lines in dmesg (DSCR, limit, elapsed).
Usage: a2_halt_cycles.py <label> [--cycles 30] [--mhz 100] [--no-stage] [--dtb NAME]
Log: tmp/logs/a2_halt_cycles.log
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LX = ROOT / "scripts/lx.py"
LOG = ROOT / "tmp/logs/a2_halt_cycles.log"
# a2 Linux needs ~3 s to a shell at 1000 MHz (docs/a2-linux.md); 100 cs = the stop comes while it runs, not at its first instruction
RUN_CS = 100
REMOTE = r"""
D=/sys/class/remoteproc/remoteproc0; T=/sys/kernel/debug/remoteproc/remoteproc0
[ -p /tmp/f ] || mkfifo /tmp/f; exec 3<>/tmp/f
u() { read a b < /proc/uptime; echo ${a%.*}${a#*.}; }
w() { t0=$(u); while [ $(( $(u) - t0 )) -lt $1 ]; do read -t 0.05 x <&3; done; }
mountpoint -q /sys/kernel/debug || mount -t debugfs none /sys/kernel/debug
lsmod | grep -q us48_a2 || insmod /tmp/us48_a2.ko payload=1 || exit 3
[ "$(cat $D/state)" = offline ] || echo stop > $D/state
echo @MHZ@ > /sys/module/us48_a2/parameters/cpu_mhz
echo us48-a2-boot.elf > $D/firmware
i=0
while [ $i -lt @N@ ]; do
  i=$((i+1))
  echo start > $D/state 2>/tmp/err; rc=$?
  hs=$(cat $T/halt_last_us)
  [ $rc -eq 0 ] && w @RUN@
  echo stop > $D/state 2>>/tmp/err
  echo "cycle $i start_rc=$rc start_halt_us=$hs stop_halt_us=$(cat $T/halt_last_us) state=$(cat $D/state)"
done
echo "halt_n=$(cat $T/halt_n) halt_fail=$(cat $T/halt_fail) halt_max_us=$(cat $T/halt_max_us)"
dmesg | grep "a2 halt" | tail -5
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("label")
    ap.add_argument("--cycles", type=int, default=30)
    ap.add_argument("--mhz", type=int, default=100)
    ap.add_argument("--profile", default="debug")
    ap.add_argument("--dtb", help="a2 DTB under tmp/a2 (default us48-a2.dtb at 100 MHz, else us48-a2-<mhz>.dtb)")
    ap.add_argument("--no-stage", action="store_true")
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if not a.no_stage:
        rel = {"normal": "7.4.0-rc1-norm+", "debug": "7.4.0-rc1-dbg+"}[a.profile]
        dtb = ROOT / "tmp/a2" / (a.dtb or ("us48-a2.dtb" if a.mhz == 100 else f"us48-a2-{a.mhz}.dtb"))
        for src, dst in ((ROOT / f"tmp/mainline/rootfs-{a.profile}/lib/modules/{rel}/us48_a2.ko", "/tmp/us48_a2.ko"),
                         (ROOT / "tmp/a2/zImage", "/lib/firmware/us48-a2-zImage"), (dtb, "/lib/firmware/us48-a2.dtb"),
                         (ROOT / "tmp/a2fw/us48-a2-boot.elf", "/lib/firmware/us48-a2-boot.elf")):
            r = subprocess.run([sys.executable, str(LX), "--put", str(src), dst], capture_output=True, text=True)
            if r.returncode:
                sys.exit(f"put {src}: {r.stderr}")
    cmd = REMOTE.replace("@N@", str(a.cycles)).replace("@MHZ@", str(a.mhz)).replace("@RUN@", str(RUN_CS))
    r = subprocess.run([sys.executable, str(LX), cmd], capture_output=True, text=True)
    out = r.stdout + r.stderr
    with LOG.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {a.label} mhz {a.mhz} cycles {a.cycles}\n{out}\n")
    print(out)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
