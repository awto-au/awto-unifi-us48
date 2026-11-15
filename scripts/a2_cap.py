#!/usr/bin/env python3
"""a2_cap — arm the us48_a2 debugfs `capture` (vector catch), start a2 and print the dump (#79).

Usage: a2_cap.py ELF_OR_LINUX CPU_MHZ [--axi 200] [--limit-ms N] [--plain] [--skip] [--runs N] [k=v ...]
  LINUX = payload boot (us48-a2-boot.elf + zImage + DTB in /lib/firmware, payload=1); anything else = an ELF in /lib/firmware.
  Needs the debug a1 image (debugfs) and a us48_a2.ko with `capture` at /tmp/us48_a2.ko (insmod'd if absent). k=v = module params.
  Armed while a2 is offline: DBGVCR is set before the release, so the first und/pabt/dabt halts a2 at the vector fetch.
Log: tmp/logs/a2_cap.log. Wait: poll `capture` every 0.2 s until state leaves `armed`; cap = limit-ms + 2 s (the driver's own limit logs expiry).
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/a2_cap.log"
REMOTE = r"""
D=/sys/class/remoteproc/remoteproc0; T=/sys/kernel/debug/remoteproc/remoteproc0; P=/sys/module/us48_a2/parameters
[ -p /tmp/f ] || mkfifo /tmp/f; exec 3<>/tmp/f
lsmod | grep -q us48_a2 || insmod /tmp/us48_a2.ko
[ "$(cat $D/state)" = offline ] || echo stop > $D/state
echo disarm > $T/capture
echo 0 > $P/dcc_poll; echo 0 > $P/hdbgen_off; echo @PAYLOAD@ > $P/payload
echo @MHZ@ > $P/cpu_mhz; @AXI@
@PARAMS@
echo @FW@ > $D/firmware
echo @ARM@ > $T/capture || exit 3
echo start > $D/state || exit 4
n=0
while [ $n -lt @N@ ]; do
  read -t 0.2 x <&3; n=$((n+1))
  case "$(head -n1 $T/capture)" in "state armed") ;; *) break;; esac
done
echo "poll=$n"; cat $T/capture; tail -1 $T/ccu
for h in /sys/class/hwmon/hwmon*; do case "$(cat $h/name)" in die*) echo "$(cat $h/name) $(cat $h/temp1_input)";; esac; done
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("what")
    ap.add_argument("mhz")
    ap.add_argument("--axi", default="")
    ap.add_argument("--limit-ms", type=int, default=5000)
    ap.add_argument("--plain", action="store_true", help="PCSR polling instead of the vector catch")
    ap.add_argument("--skip", action="store_true", help="vc: resume past normal page faults (user-mode origin, kernel access to a user address)")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("kv", nargs="*")
    a = ap.parse_args()
    linux = a.what == "LINUX"
    cmd = (REMOTE.replace("@PAYLOAD@", "1" if linux else "0").replace("@MHZ@", a.mhz)
           .replace("@AXI@", f"echo {a.axi} > $P/axi_mhz" if a.axi else "")
           .replace("@PARAMS@", "\n".join(f"echo {k.split('=')[1]} > $P/{k.split('=')[0]}" for k in a.kv))
           .replace("@FW@", "us48-a2-boot.elf" if linux else a.what)
           .replace("@ARM@", f"'arm {'' if a.plain else 'vc '}{'skip ' if a.skip else ''}{a.limit_ms}'")
           .replace("@N@", str(int((a.limit_ms + 2000) / 200))))
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rc = 0
    for i in range(a.runs):
        r = subprocess.run([sys.executable, str(ROOT / "scripts/lx.py"), cmd], capture_output=True, text=True)
        out = f"== {a.what} cpu {a.mhz} axi {a.axi or 'param'} {' '.join(a.kv)} {'plain' if a.plain else 'vc'}{' skip' if a.skip else ''} run {i + 1}\n{r.stdout}{r.stderr}"
        print(out)
        LOG.open("a").write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {out}\n")
        rc = rc or r.returncode
    return rc


if __name__ == "__main__":
    sys.exit(main())
