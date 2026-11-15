#!/usr/bin/env python3
"""a2_boot_time — boot Linux on a2 from the running a1 image and time it (#79).

- Optionally stages local files into /lib/firmware (--zimage, --dtb, --boot-elf) and module params (--param k=v).
- On the board (one ssh call, uptime-timed in centiseconds): stop a2, start the DCC reader into /tmp/a2.out, `echo start`,
  wait for the /init banner, type a marker command, wait for its output = shell reached.
- Output: t_start (firmware load + copy + PLL + release), t_banner and t_shell since `echo start` (a1 wall), a2 printk time
  of the last timestamped line before the banner, a2 printk time of key lines. Log: tmp/logs/a2_boot_time.log, a2 text
  kept in tmp/logs/a2_boot_<label>.txt.

Usage: a2_boot_time.py <label> [--zimage F] [--dtb F] [--boot-elf F] [--param k=v ...] [--limit S]
Limit: 150 s for the banner = 3x the 50 s measured on 2026-11-15 (debug profile); expiry = report, a2 left running.
"""
import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LX = ROOT / "scripts/lx.py"
LOG = ROOT / "tmp/logs/a2_boot_time.log"

REMOTE = r"""
D=/sys/class/remoteproc/remoteproc0
u() { read a b < /proc/uptime; echo ${a%.*}${a#*.}; }
[ -p /tmp/f ] || mkfifo /tmp/f; exec 3<>/tmp/f
[ "$(cat $D/state)" = offline ] || echo stop > $D/state
for kv in @PARAMS@; do echo ${kv#*=} > /sys/module/us48_a2/parameters/${kv%%=*}; done
echo us48-a2-boot.elf > $D/firmware
rm -f /tmp/a2.out
cat /dev/ttyA20 > /tmp/a2.out & P=$!
t0=$(u); echo start > $D/state; t1=$(u)
while ! grep -q "us48-a2: Linux" /tmp/a2.out && [ $(( $(u) - t0 )) -lt @LIMIT@ ]; do read -t 0.2 x <&3; done
t2=$(u)
printf 'echo SHELL""UP\n' > /dev/ttyA20
while ! grep -q "^SHELLUP" /tmp/a2.out && [ $(( $(u) - t0 )) -lt @LIMIT@ ]; do read -t 0.2 x <&3; done
t3=$(u)
kill $P
echo T start=$((t1-t0)) banner=$((t2-t0)) shell=$((t3-t0))
echo CCU; cat /sys/kernel/debug/remoteproc/remoteproc0/ccu | tail -1
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("label")
    ap.add_argument("--zimage", type=Path)
    ap.add_argument("--dtb", type=Path)
    ap.add_argument("--boot-elf", type=Path)
    ap.add_argument("--param", action="append", default=[])
    ap.add_argument("--limit", type=int, default=150)
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)

    def lx(*args) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(LX), *args], capture_output=True, text=True)

    for f, name in ((a.zimage, "us48-a2-zImage"), (a.dtb, "us48-a2.dtb"), (a.boot_elf, "us48-a2-boot.elf")):
        if f:
            r = lx("--put", str(f), f"/lib/firmware/{name}")
            if r.returncode:
                print(f"put {f}: rc={r.returncode} {r.stderr}")
                return 1
    r = lx(REMOTE.replace("@PARAMS@", " ".join(a.param)).replace("@LIMIT@", str(a.limit * 100)))
    out = r.stdout
    txt = lx("cat /tmp/a2.out").stdout
    (ROOT / f"tmp/logs/a2_boot_{a.label}.txt").write_text(txt)
    m = re.search(r"T start=(\d+) banner=(\d+) shell=(\d+)", out)
    stamps = [float(x) for x in re.findall(r"^\[\s*(\d+\.\d+)\]", txt, re.M)]
    ban = txt.find("us48-a2: Linux")
    before = [float(x) for x in re.findall(r"^\[\s*(\d+\.\d+)\]", txt[:ban], re.M)] if ban >= 0 else []
    key = {k: re.search(r"^\[\s*(\d+\.\d+)\].*" + k, txt, re.M) for k in ("Run /init", "Freeing unused kernel", "Kernel command line")}
    s = (f"{a.label}: " + (f"a1 wall since start: load={int(m[1]) / 100:.2f}s banner={int(m[2]) / 100:.2f}s shell={int(m[3]) / 100:.2f}s"
                           if m else "NO TIMES") +
         f"; a2 printk at banner={before[-1] if before else None} last={stamps[-1] if stamps else None}; " +
         "; ".join(f"{k}={v[1]}" for k, v in key.items() if v) + f"; {out.splitlines()[-1] if out else ''}")
    print(s)
    with LOG.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {s}\n")
    return 0 if m and int(m[3]) < a.limit * 100 else 1


if __name__ == "__main__":
    sys.exit(main())
