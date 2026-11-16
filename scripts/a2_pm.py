#!/usr/bin/env python3
"""a2_pm — boot a2 Linux until it oopses, then inspect the frozen a2 post-mortem (#103).

- Debug a1 image (debugfs) + WORK-debug us48_a2.ko (`inspect` in debugfs `capture`), DCC poll on (console text = oops registers).
- Each boot: start, wait for the DCC marker (`dcc_stats` mark: 1 = "Run /init", -1 = oops/panic text), stop on a clean boot.
- On an oops: wait for the panic text to finish, parse the registers from `dcc_log`, then `inspect <lowmem register values>`:
  halts a2 (it sits in the panic loop), a2 view vs DDR for each pointer (256 B window, 4 KiB page, struct page), L1 clean/inv + PL310 clean+inv by PA
  per differing line, flush of a2's caches to DDR; then a2_dmesg.py (tail 100) and a1 context. Saved to tmp/logs/a2_pm/<stamp>-<label>-<n>.txt.
- Waits: marker limit 3 s (boot to "Run /init" about 1.5 s at 1000 MHz, 2x); oops text complete = "end Kernel panic" in dcc_log, limit 4 s (a2 blocks per char until drained).
Usage: a2_pm.py --boots N [--block 20] [--mhz 1000] [--max-catch 3] [--no-stage] [--label L] [k=v ...]    Log: tmp/logs/a2_pm.log
"""
import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LX = ROOT / "scripts/lx.py"
LOG = ROOT / "tmp/logs/a2_pm.log"
OUT = ROOT / "tmp/logs/a2_pm"
SETUP = r"""
D=/sys/class/remoteproc/remoteproc0; T=/sys/kernel/debug/remoteproc/remoteproc0; P=/sys/module/us48_a2/parameters
if [ -d $D ]; then [ "$(cat $D/state)" = offline ] || echo stop > $D/state; rmmod us48_a2 || echo FAIL rmmod; fi
insmod /tmp/us48_a2.ko payload=1 || echo FAIL insmod
echo 0 > $P/hdbgen_off; echo @MHZ@ > $P/cpu_mhz
@PARAMS@
echo us48-a2-boot.elf > $D/firmware
"""
BOOT = r"""
D=/sys/class/remoteproc/remoteproc0; T=/sys/kernel/debug/remoteproc/remoteproc0
[ -p /tmp/f ] || mkfifo /tmp/f; exec 3<>/tmp/f
[ "$(cat $D/state)" = offline ] || echo stop > $D/state
echo start > $D/state || exit 4
n=0; m=0
while [ $n -lt 30 ]; do
  read -t 0.1 x <&3; n=$((n+1))
  m=$(sed -n 's/.* mark //p' $T/dcc_stats)
  [ "$m" != 0 ] && break
done
echo "mark=$m polls=$n"
if [ "$m" = -1 ]; then
  n=0
  while [ $n -lt 40 ] && ! grep -q "end Kernel panic" $T/dcc_log; do read -t 0.1 x <&3; n=$((n+1)); done
  echo "panic_wait=$n"; echo "=== dcc_log"; cat $T/dcc_log; echo "=== end dcc_log"
elif [ "$m" = 1 ]; then echo stop > $D/state; fi
"""
CTX = r"""
echo "== a1 dmesg tail"; dmesg | tail -40
echo "== a1 loadavg/uptime"; cat /proc/loadavg /proc/uptime
echo "== a1 modules"; lsmod | cut -d' ' -f1 | tr '\n' ' '; echo
echo "== a1 processes"; ps | head -30
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
    """Thermal gate (milli-degrees): die >= 108 C or any LM75 >= 78 C."""
    for m in re.finditer(r"^(\S+) (\d+)$", out, re.M):
        n, v = m.group(1), int(m.group(2)) / 1000
        if (n.startswith("die") and v >= 108) or (n.startswith("lm75") and v >= 78):
            return f"{n} {v} C"
    return ""


def pointers(text: str) -> list[str]:
    """Data pointers above the kernel text (c0442000..) from the last oops: registers (not pc/lr/sp, not the stack) and the slab object starts of "Register rN information"; deduped by 256 B."""
    t = text[text.rfind("Booting Linux"):] if "Booting Linux" in text else text
    sp = re.search(r"\bsp\s*:\s*([0-9a-f]{8})", t)
    spv = int(sp.group(1), 16) if sp else 0
    vals = [m.group(1) for m in re.finditer(r"\bslab \S+ start ([0-9a-f]{8})", t)]
    vals += [m.group(2) for m in re.finditer(r"\b(r\d+|ip|fp)\s*:\s*([0-9a-f]{8})", t)]
    seen, out = set(), []
    for h in vals:
        v = int(h, 16)
        if 0xC0442000 <= v < 0xEF000000 and abs(v - spv) > 0x2000 and (v >> 8) not in seen:
            seen.add(v >> 8)
            out.append(f"{v:08x}")
    return out[:16]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--boots", type=int, default=20)
    ap.add_argument("--block", type=int, default=20)
    ap.add_argument("--mhz", type=int, default=1000)
    ap.add_argument("--max-catch", type=int, default=3)
    ap.add_argument("--ko", default=str(ROOT / "tmp/kosrc/WORK-debug.ko"))
    ap.add_argument("--dtb", default=None)
    ap.add_argument("--no-stage", action="store_true")
    ap.add_argument("--label", default="pm")
    ap.add_argument("--count-only", action="store_true", help="oops = count + stop a2, no inspect/ring/context (mitigation counts)")
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
    boots = catches = clean = other = 0
    while boots < a.boots and (catches < a.max_catch or a.count_only):
        if boots % a.block == 0:
            t = lx(TEMPS).stdout
            log(f"{a.label} block start boots={boots} temps: {' | '.join(t.split(chr(10)))}")
            if (h := hot(t)):
                log(f"{a.label} STOP thermal {h}")
                return 2
        boots += 1
        r = lx(BOOT)
        txt = r.stdout + r.stderr
        m = re.search(r"^mark=(-?\d+) polls=(\d+)", txt, re.M)
        if r.returncode or not m:
            other += 1
            log(f"{a.label} boot {boots}: ERROR rc={r.returncode} {txt[-300:]!r}")
            continue
        mark = int(m.group(1))
        if mark == 1:
            clean += 1
            log(f"{a.label} boot {boots}: ok ({int(m.group(2)) * 0.1:.1f} s)")
            continue
        if mark == 0:
            other += 1
            log(f"{a.label} boot {boots}: NO MARKER in 3 s; stopping a2")
            lx("echo stop > /sys/class/remoteproc/remoteproc0/state")
            continue
        catches += 1
        if a.count_only:
            log(f"{a.label} boot {boots}: OOPS")
            lx("echo stop > /sys/class/remoteproc/remoteproc0/state")
            continue
        ptrs = pointers(txt)
        ins = lx(f"echo 'inspect {' '.join(ptrs)}' > /sys/kernel/debug/remoteproc/remoteproc0/capture; cat /sys/kernel/debug/remoteproc/remoteproc0/capture")
        ring = subprocess.run([sys.executable, str(ROOT / "scripts/a2_dmesg.py"), "--tail", "100"], capture_output=True, text=True)
        ctx = lx(CTX)
        f = OUT / f"{time.strftime('%Y%m%d-%H%M%S')}-{a.label}-{boots}.txt"
        f.write_text(f"boot {boots} mhz {a.mhz} params {a.kv} pointers {ptrs}\n{txt}\n== inspect (rc {ins.returncode})\n{ins.stdout}{ins.stderr}\n"
                     f"== a2 printk ring from a2_ram (tail 100)\n{ring.stdout}{ring.stderr}\n{ctx.stdout}{ctx.stderr}")
        log(f"{a.label} boot {boots}: OOPS CAUGHT -> {f}")
        lx("echo stop > /sys/class/remoteproc/remoteproc0/state")
    log(f"{a.label} SUMMARY boots={boots} oops={catches} ok={clean} other={other}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
