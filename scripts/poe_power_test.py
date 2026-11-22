#!/usr/bin/env python3
"""poe_power_test: PoE power-cycle and current-limit sweep on a real camera port (#26).

Usage: scripts/poe_power_test.py cycle --chip 8-0028 --port 2 [--n 30]
       scripts/poe_power_test.py sweep --chip 8-002b --ch 3 [--values 17,8,6,4,3,2,1]
- cycle: N x (driver port enable -> wait until the driver reports "delivering" -> disable -> wait until "disabled"), timed on the board (/proc/uptime, 10 ms).
  Prints per cycle: time to delivering, time to off, delivering power/voltage, final state; summary min/max/avg and failures. Never touches other ports.
- sweep (stock-firmware chip only): host current limit ICUT of one channel lowered step by step in semi-auto mode, request power, read the result
  (delivering / tripped), the event registers and the measured current; the camera is restored to the default limit at the end.
- Waits: power-up limit 8 s = about 5x the slowest observed enable -> delivering (detect 15 ticks, class window, power-good: ~1.5 s, 10 ms ticks),
  shutdown limit 3 s = 20x the observed shutdown (one register read). Expiry = recorded as FAIL with the last state, the cycle continues.
Log: tmp/logs/poe_power_test.log. Board address from _ipreg (US48_IP_US48_BOOT override).
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _ipreg  # noqa: E402
from _cli import help_guard  # noqa: E402
from lx import OPTS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/poe_power_test.log"
UP_LIMIT_CS = 800    # 8 s, see docstring
OFF_LIMIT_CS = 300   # 3 s


def sh(cmd, timeout=300):
    r = subprocess.run(["ssh", *OPTS, f"root@{_ipreg.ip('us48-boot')}", cmd], capture_output=True, text=True, timeout=timeout)
    return r.stdout + r.stderr


def say(fh, s):
    line = f"{time.strftime('%H:%M:%S')} {s}"
    print(line, flush=True)
    fh.write(line + "\n")
    fh.flush()


def cycle_script(chip, port, n):
    dev = f"/sys/bus/i2c/devices/{chip}"
    bus, addr = chip.split("-")
    bus, addr = int(bus), "0x" + addr[-2:]
    return f"""#!/bin/sh
exec > /tmp/poe_cycle.log 2>&1
up() {{ cut -d' ' -f1 /proc/uptime | tr -d .; }}
g() {{ i2cget -f -y {bus} {addr} $1; }}
i=1
while [ $i -le {n} ]; do
  k0=$(g 0x2d)
  echo 1 > {dev}/port{port}_enable
  t0=$(up); s=; while :; do s=$(cat {dev}/port{port}_status); case "$s" in delivering*) break;; esac; [ $(( $(up) - t0 )) -gt {UP_LIMIT_CS} ] && break; done
  t1=$(up); ups="$s"; sat=$(g 0x0d); pwr=$(g 0x10); rawd=$(g 0x1d)
  echo 0 > {dev}/port{port}_enable
  t2=$(up); while :; do s=$(cat {dev}/port{port}_status); case "$s" in disabled*) break;; esac; [ $(( $(up) - t2 )) -gt {OFF_LIMIT_CS} ] && break; done
  t3=$(up); k1=$(g 0x2d); k2=$(g 0x2d)
  echo "CYC $i up_cs $((t1 - t0)) off_cs $((t3 - t2)) | $ups | SAT $sat PWR $pwr D $rawd | tick $k0 $k1 $k2 | end: $s"
  i=$((i + 1))
done
echo DONE
"""


def sweep_script(chip, ch, values, hold_cs):
    """Board side: per ICUT value: channel off, program ICUT/ILIM, semi-auto, power request (reg 0x19), watch SAT_PWR for hold_cs."""
    bus, addr = chip.split("-")
    bus, addr = int(bus), "0x" + addr[-2:]
    icut, ilim = 0x47 + 5 * ch, 0x48 + 5 * ch
    sat = 0x0c + ch
    shift = 2 * ch
    bit = 1 << ch
    vals = " ".join(str(v) for v in values)
    return f"""#!/bin/sh
exec > /tmp/poe_sweep.log 2>&1
up() {{ cut -d' ' -f1 /proc/uptime | tr -d .; }}
g() {{ i2cget -f -y {bus} {addr} $1; }}
w() {{ i2cset -f -y {bus} {addr} $1 $2; }}
mode() {{ m=$(( $(g 0x12) & ~(3 << {shift}) | ($1 << {shift}) )); w 0x12 $m; }}
for v in {vals}; do
  mode 0
  t=$(up); while [ $(( $(up) - t )) -lt 60 ]; do :; done
  w 0x{icut:02x} $v; w 0x{ilim:02x} 0xc0
  c0="$(g 0x65) $(g 0x66) $(g 0x67)"
  g 0x04 > /dev/null; g 0x06 > /dev/null; g 0x08 > /dev/null; g 0x0a > /dev/null
  mode 2; w 0x19 {bit}
  t0=$(up); pg=-; first=-; peak=0
  while [ $(( $(up) - t0 )) -lt {hold_cs} ]; do
    p=$(g 0x10); s=$(g 0x{sat:02x})
    case "$p" in 0x*) if [ $(( p & (0x10 << {ch}) )) -ne 0 ] && [ "$pg" = "-" ]; then pg=$(( $(up) - t0 )); fi;; esac
  done
  p=$(g 0x10); s=$(g 0x{sat:02x}); cm=$(( $(g 0x{0x34 + 4 * ch:02x}) + 256 * $(g 0x{0x35 + 4 * ch:02x}) )); vm=$(( $(g 0x{0x36 + 4 * ch:02x}) + 256 * $(g 0x{0x37 + 4 * ch:02x}) ))
  e4=$(g 0x04); e6=$(g 0x06); e8=$(g 0x08); ea=$(g 0x0a)
  echo "SWEEP icut $v | first_pg_cs $pg | end SAT $s PWR $p CM $cm VM $vm | events 04 $e4 06 $e6 08 $e8 0a $ea | counters before $c0 after $(g 0x65) $(g 0x66) $(g 0x67) | ICUT now $(g 0x{icut:02x}) ILIM $(g 0x{ilim:02x}) OP $(g 0x12)"
done
mode 0
w 0x{icut:02x} 0xe2; w 0x{ilim:02x} 0xc0
echo DONE
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cycle")
    c.add_argument("--chip", required=True)
    c.add_argument("--port", type=int, required=True, help="port 1..4 on the chip")
    c.add_argument("--n", type=int, default=30)
    sw = sub.add_parser("sweep")
    sw.add_argument("--chip", required=True)
    sw.add_argument("--ch", type=int, required=True, help="channel 0..3 on the chip")
    sw.add_argument("--values", default="17,10,8,6,5,4,3,2,1")
    sw.add_argument("--hold-s", type=float, default=4.0, help="watch time per value; power-up takes ~1.5 s")
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        if a.cmd == "sweep":
            vals = [int(x) for x in a.values.split(",")]
            say(fh, f"=== sweep chip {a.chip} ch {a.ch} ICUT {vals}, hold {a.hold_s} s each (stock firmware chip, semi-auto + power request reg 0x19)")
            sh("cat > /tmp/poe_sweep.sh <<'EOS'\n" + sweep_script(a.chip, a.ch, vals, int(a.hold_s * 100)) + "EOS\nchmod +x /tmp/poe_sweep.sh; rm -f /tmp/poe_sweep.log; setsid nohup /tmp/poe_sweep.sh > /dev/null 2>&1 < /dev/null & echo started")
            out, last, t_last = "", "", time.monotonic()
            while "DONE" not in out:
                time.sleep(5)   # poll cadence: ssh round trip ~1 s, one value takes hold + 0.6 s
                out = sh("cat /tmp/poe_sweep.log")
                if out != last:
                    last, t_last = out, time.monotonic()
                elif time.monotonic() - t_last > 60:
                    say(fh, f"STALL: no new line for 60 s; last: {out.splitlines()[-1] if out else '(none)'}")
                    break
            for ln in out.splitlines():
                say(fh, ln)
            return 0
        if a.cmd == "cycle":
            say(fh, f"=== cycle chip {a.chip} port {a.port} x{a.n}")
            sh("cat > /tmp/poe_cycle.sh <<'EOS'\n" + cycle_script(a.chip, a.port, a.n) + "EOS\nchmod +x /tmp/poe_cycle.sh; rm -f /tmp/poe_cycle.log; setsid nohup /tmp/poe_cycle.sh > /dev/null 2>&1 < /dev/null & echo started")
            out, last, t_last = "", "", time.monotonic()
            while "DONE" not in out:
                time.sleep(5)   # poll cadence: one ssh round trip is ~1 s, a cycle takes 1..12 s
                out = sh("cat /tmp/poe_cycle.log")
                if out != last:
                    last, t_last = out, time.monotonic()
                elif time.monotonic() - t_last > 60:   # stall guard: no new line for 60 s = hung
                    say(fh, f"STALL: no new cycle line for 60 s; last: {out.splitlines()[-1] if out else '(none)'}")
                    break
            ups, offs, bad = [], [], 0
            for ln in out.splitlines():
                if not ln.startswith("CYC"):
                    continue
                say(fh, ln)
                p = ln.split()
                u, o = int(p[3]), int(p[5])
                tk = ln.split("tick")[1].split("|")[0].split()
                if len(tk) == 3 and tk[1] == tk[2] and tk[1] != '0x00':   # 0x2d is a tick counter in the rewrite image only (stock reads 0)
                    say(fh, f"  ^ cycle {p[1]}: tick counter did not advance ({tk}): firmware frozen")
                ups.append(u)
                offs.append(o)
                if "delivering" not in ln.split("|")[1] or "disabled" not in ln.split("end:")[1]:
                    bad += 1
            if not ups:
                say(fh, "FAIL no cycle output: " + out[:300])
                return 1
            say(fh, f"summary: {len(ups)} cycles, failures {bad}; to delivering {min(ups) * 10}..{max(ups) * 10} ms (avg {sum(ups) * 10 // len(ups)}), "
                    f"to off {min(offs) * 10}..{max(offs) * 10} ms")
            return 1 if bad else 0
    return 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
