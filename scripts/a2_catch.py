#!/usr/bin/env python3
"""a2_catch — boot a2 Linux repeatedly with the vector-catch armed for a kernel NULL data abort and dump each catch (#103).

- Debug a1 image (debugfs) + a WORK-debug us48_a2.ko (`a2_build_ko.py WORK --profile debug`). dcc_poll=0.
- Each boot: `arm vc null LIMIT_MS` (resume everything except a kernel-mode data abort at DFAR < 0x1000 from kernel-image code), start, wait for the catch.
- A catch keeps a2 started: a1 dmesg tail + a2 printk ring (`a2_dmesg.py --tail 100`) are saved with the dump to tmp/logs/a2_catch/<stamp>-<n>.txt, then a2 is stopped.
- Limit: oops seen within ~0.7 s of the a2 kernel start (#103); LIMIT_MS = 1500 (about 2x that); expiry = clean boot, logged as such.
Usage: a2_catch.py --boots N [--block 20] [--mhz 1000] [--max-catch 3] [--ko FILE] [--no-stage] [k=v ...]
Log: tmp/logs/a2_catch.log
"""
import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LX = ROOT / "scripts/lx.py"
LOG = ROOT / "tmp/logs/a2_catch.log"
OUT = ROOT / "tmp/logs/a2_catch"
SETUP = r"""
D=/sys/class/remoteproc/remoteproc0; T=/sys/kernel/debug/remoteproc/remoteproc0; P=/sys/module/us48_a2/parameters
if [ -d $D ]; then [ "$(cat $D/state)" = offline ] || echo stop > $D/state; rmmod us48_a2 || echo FAIL rmmod; fi
insmod /tmp/us48_a2.ko payload=1 || echo FAIL insmod
echo 0 > $P/dcc_poll; echo 0 > $P/hdbgen_off; echo @MHZ@ > $P/cpu_mhz
@PARAMS@
echo us48-a2-boot.elf > $D/firmware
"""
BOOT = r"""
D=/sys/class/remoteproc/remoteproc0; T=/sys/kernel/debug/remoteproc/remoteproc0
[ -p /tmp/f ] || mkfifo /tmp/f; exec 3<>/tmp/f
[ "$(cat $D/state)" = offline ] || echo stop > $D/state
echo disarm > $T/capture
echo 'arm vc null @LIMIT@' > $T/capture || exit 3
echo start > $D/state || exit 4
n=0
while [ $n -lt @POLLS@ ]; do
  read -t 0.2 x <&3; n=$((n+1))
  case "$(head -n1 $T/capture)" in "state armed") ;; *) break;; esac
done
echo "polls=$n"; cat $T/capture
"""
CATCH = r"""
echo "== a1 dmesg tail"; dmesg | tail -40
echo "== a1 loadavg/uptime"; cat /proc/loadavg /proc/uptime
echo "== a1 modules"; lsmod | cut -d' ' -f1 | tr '\n' ' '; echo
echo "== a1 processes (top CPU)"; ps | head -30
echo "== temps"; for h in /sys/class/hwmon/hwmon*; do echo "$(cat $h/name) $(cat $h/temp1_input 2>&1)"; done
"""
TEMPS = r"""for h in /sys/class/hwmon/hwmon*; do echo "$(cat $h/name) $(cat $h/temp1_input 2>&1)"; done"""


def lx(cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(LX), cmd], capture_output=True, text=True)


def log(msg: str) -> None:
    print(msg, flush=True)
    with LOG.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}\n")


def hot(out: str) -> str:
    """Thermal gate: die >= 108 C or any LM75 >= 78 C (milli-degrees); returns the reason or ''."""
    for m in re.finditer(r"^(\S+) (\d+)$", out, re.M):
        n, v = m.group(1), int(m.group(2)) / 1000
        if (n.startswith("die") and v >= 108) or (n.startswith("lm75") and v >= 78):
            return f"{n} {v} C"
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--boots", type=int, default=20)
    ap.add_argument("--block", type=int, default=20)
    ap.add_argument("--mhz", type=int, default=1000)
    ap.add_argument("--limit-ms", type=int, default=1500)
    ap.add_argument("--max-catch", type=int, default=3)
    ap.add_argument("--ko", default=str(ROOT / "tmp/kosrc/WORK-debug.ko"))
    ap.add_argument("--dtb", default=None)
    ap.add_argument("--no-stage", action="store_true")
    ap.add_argument("--label", default="catch")
    ap.add_argument("kv", nargs="*", help="us48_a2 module params k=v")
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    if not a.no_stage:
        dtb = ROOT / "tmp/a2" / (a.dtb or f"us48-a2-{a.mhz}-l2.dtb")
        for src, dst in ((a.ko, "/tmp/us48_a2.ko"), (ROOT / "tmp/a2/zImage", "/lib/firmware/us48-a2-zImage"), (dtb, "/lib/firmware/us48-a2.dtb"),
                         (ROOT / "tmp/a2fw/us48-a2-boot.elf", "/lib/firmware/us48-a2-boot.elf")):
            r = subprocess.run([sys.executable, str(LX), "--put", str(src), dst], capture_output=True, text=True)
            if r.returncode:
                sys.exit(f"put {src}: {r.stderr}")
    params = "\n".join(f"echo {k.split('=')[1]} > $P/{k.split('=')[0]}" for k in a.kv)
    r = lx(SETUP.replace("@MHZ@", str(a.mhz)).replace("@PARAMS@", params))
    log(f"{a.label} setup rc={r.returncode} {r.stdout.strip()} {r.stderr.strip()}")
    if r.returncode or "FAIL" in r.stdout:
        return 1
    boot = BOOT.replace("@LIMIT@", str(a.limit_ms)).replace("@POLLS@", str(int((a.limit_ms + 1000) / 200)))
    boots = catches = clean = other = 0
    while boots < a.boots and catches < a.max_catch:
        if boots % a.block == 0:
            t = lx(TEMPS).stdout
            log(f"{a.label} block start boots={boots} temps: {' | '.join(t.split(chr(10)))}")
            if (h := hot(t)):
                log(f"{a.label} STOP thermal {h}")
                return 2
        boots += 1
        r = lx(boot)
        txt = r.stdout + r.stderr
        st = re.search(r"^state (\w+)", txt, re.M)
        state = st.group(1) if st else "?"
        if r.returncode or state == "?":
            other += 1
            log(f"{a.label} boot {boots}: ERROR rc={r.returncode} {txt[-300:]!r}")
            continue
        if state == "done":
            catches += 1
            ring = subprocess.run([sys.executable, str(ROOT / "scripts/a2_dmesg.py"), "--tail", "100"], capture_output=True, text=True)
            ctx = lx(CATCH)
            f = OUT / f"{time.strftime('%Y%m%d-%H%M%S')}-{a.label}-{boots}.txt"
            f.write_text(f"boot {boots} mhz {a.mhz} params {a.kv}\n{txt}\n== a2 printk ring (tail 100)\n{ring.stdout}{ring.stderr}\n{ctx.stdout}{ctx.stderr}")
            log(f"{a.label} boot {boots}: CATCH -> {f}")
        elif "capture ended: -110" in txt:
            clean += 1
            log(f"{a.label} boot {boots}: clean (no NULL abort in {a.limit_ms} ms)")
        else:
            other += 1
            log(f"{a.label} boot {boots}: other state {state}: {txt[-300:]!r}")
    lx("echo disarm > /sys/kernel/debug/remoteproc/remoteproc0/capture; echo stop > /sys/class/remoteproc/remoteproc0/state")
    log(f"{a.label} SUMMARY boots={boots} catches={catches} clean={clean} other={other}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
