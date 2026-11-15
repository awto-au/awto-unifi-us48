#!/usr/bin/env python3
"""a2_sram_sweep — SRAM-only stress (stress.c) at rising a2 clocks, 2 runs each, stop at first failure (#79).

Usage: a2_sram_sweep.py [RUNS=2] [MHZ ...]  (default list = build_a2fw.STRESS_MHZ >= 1200); log tmp/logs/a2_sram_sweep.log
Per run: die0/die1/LM75 before+after (hwmon on the board), a2_raw.py ELF MHZ 20 200 dcc_poll=0, pass count, pass/MHz.
Stop: any non-ok verdict, no `done`, die >= 110 C, LM75 >= 80 C. On stop the a2 is stopped (never a reboot).
"""
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/a2_sram_sweep.log"
DEFAULT = [1200, 1400, *range(1550, 2000, 50)]
DIE_MAX_C, LM75_MAX_C = 110, 80
READ = r'for h in /sys/class/hwmon/hwmon*; do echo "$(cat $h/name) $(cat $h/temp1_input 2>/dev/null)"; done'


def lx(*a: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "scripts/lx.py", *a], capture_output=True, text=True, cwd=ROOT)


def log(s: str) -> None:
    print(s, flush=True)
    with LOG.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {s}\n")


def temps() -> dict[str, float]:
    t: dict[str, float] = {}
    for line in lx(READ).stdout.splitlines():
        p = line.split()
        if len(p) == 2 and p[1].isdigit():
            n, i = p[0], 1
            while f"{n}#{i}" in t:
                i += 1
            t[f"{n}#{i}" if n == "lm75b" else n] = int(p[1]) / 1000
    return t


def main() -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    a = sys.argv[1:]
    runs = int(a[0]) if a else 2
    steps = [int(x) for x in a[1:]] or DEFAULT
    for mhz in steps:
        elf = f"us48-a2-stress{mhz}.elf"
        lx("--put", f"tmp/a2fw/{elf}", f"/lib/firmware/{elf}")
        for n in range(runs):
            b = temps()
            r = subprocess.run([sys.executable, "scripts/a2_raw.py", elf, str(mhz), "20", "200", "dcc_poll=0"], capture_output=True, text=True, cwd=ROOT)
            out = r.stdout + r.stderr
            e = temps()
            m = re.search(r"pass=([0-9a-f]{8}) m=([0-9a-f]{8}) r=([0-9a-f]{8}) done=([0-9a-f]{8})", out)
            fid = re.search(r"ARM (\d+) Hz", out)
            ok = bool(m) and m[2] == m[3] == "0" * 8 and m[4] == "00000001" and out.rstrip().endswith("ok")
            ps = int(m[1], 16) if m else 0
            log(f"{mhz} run{n + 1} {'ok' if ok else 'FAIL'} pass={ps:#x} ({ps} ; {ps / mhz:.3f}/MHz) ARM_Hz={fid[1] if fid else '?'} "
                f"die0 {b.get('die0')}->{e.get('die0')} die1 {b.get('die1')}->{e.get('die1')} lm75 max {max([v for k, v in e.items() if k.startswith('lm75b')] or [0])}")
            if not ok:
                log(f"FAIL output:\n{out}")
                lx('echo stop > /sys/class/remoteproc/remoteproc0/state')
                return 1
            if any(v >= DIE_MAX_C for k, v in e.items() if k.startswith("die")) or any(v >= LM75_MAX_C for k, v in e.items() if k.startswith("lm75b")):
                log(f"temperature limit hit {e}")
                lx('echo stop > /sys/class/remoteproc/remoteproc0/state')
                return 2
    log("sweep done, no failure")
    return 0


if __name__ == "__main__":
    sys.exit(main())
