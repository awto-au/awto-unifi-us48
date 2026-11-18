#!/usr/bin/env python3
"""fan_temp_step: step the fan floor on the running Linux image and log every temperature sensor over time.

Usage: scripts/fan_temp_step.py [--level 3] [--hold-s 300] [--every-s 15]
- Samples all thermal zones (LM75 x4, die0, die1), sets `us48_fan.level=<level> quiet=0`, samples for --hold-s,
  restores `level=0 quiet=1` (quiet rule, docs/fan.md), samples another --hold-s. Restore runs on any exit.
- Abort (restore + exit 1): die >= 108 C or any LM75 >= 78 C (below the 110/80 C trips).
Log: tmp/logs/fan_temp_step.log (one CSV line per sample).
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/fan_temp_step.log"
LX = [sys.executable, str(ROOT / "scripts/lx.py")]
READ = 'for z in /sys/class/thermal/thermal_zone*; do echo "$(cat $z/type) $(cat $z/temp)"; done; cat /sys/class/hwmon/hwmon6/pwm1'
P = "/sys/module/us48_fan/parameters"


def lx(cmd):
    # 30 s: ssh to the board takes about 1-2 s (dropbear handshake); 30 s means the board is gone.
    r = subprocess.run(LX + [cmd], capture_output=True, text=True, timeout=30)
    if r.returncode:
        raise RuntimeError(f"lx failed rc={r.returncode}: {r.stderr.strip()}")
    return r.stdout


def sample(phase, t0, log):
    out = lx(READ).split()
    zones = dict(zip(out[0:-1:2], (int(v) / 1000 for v in out[1:-1:2])))
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')},{phase},{time.monotonic() - t0:.0f}," + \
        ",".join(f"{k}={v:.1f}" for k, v in zones.items()) + f",pwm={out[-1]}"
    print(line, flush=True)
    log.write(line + "\n")
    log.flush()
    die = max(v for k, v in zones.items() if k.startswith("die"))
    lm = max(v for k, v in zones.items() if k.startswith("lm75"))
    if die >= 108 or lm >= 78:
        raise RuntimeError(f"thermal abort: die {die} C, lm75 {lm} C")


def hold(phase, secs, every, t0, log):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        time.sleep(every)
        sample(phase, t0, log)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--level", type=int, default=3, choices=range(4))
    ap.add_argument("--hold-s", type=int, default=300)
    ap.add_argument("--every-s", type=int, default=15)
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    with LOG.open("a") as log:
        log.write(f"# start level={a.level} hold={a.hold_s}s every={a.every_s}s\n")
        try:
            sample("base", t0, log)
            lx(f"echo 0 > {P}/quiet; echo {a.level} > {P}/level")
            hold(f"level{a.level}", a.hold_s, a.every_s, t0, log)
        finally:
            lx(f"echo 0 > {P}/level; echo 1 > {P}/quiet; cat {P}/level {P}/quiet")
            log.write("# restored quiet rule\n")
        hold("quiet", a.hold_s, a.every_s, t0, log)
    return 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
