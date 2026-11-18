#!/usr/bin/env python3
"""cpu_sweep — one A9 clock step under mainline Linux: cold cycle, variant uboot, RAM-boot mainline, 60 s watch, 10 s stress (#17).

Usage: scripts/cpu_sweep.py MHZ [--l2off IMAGE]   MHZ = 1550 (flash awto-uboot as is) or a us48-uboot-cpuMHZ.bin variant
       (build: build_uboot.py --cpu-mhz N; docs/cpu-sweep.md). --l2off IMAGE: boot that mainline image instead.
- RAM boot only, no flash/env writes. Result line -> tmp/logs/cpu_sweep.log (also stdout).
- Fails on the first BUG/Oops/Bad page/bad magic/Unable to handle in dmesg or the console log, a stress md5 mismatch,
  ssh unreachable, or die >= 110 C / LM75 >= 80 C (fans -> 100 %, stop).
"""
import _ipreg  # noqa: E402
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dev  # noqa: E402
from _cli import help_guard  # noqa: E402

REPO = dev.REPO
LOG = REPO / "tmp/logs/cpu_sweep.log"
TIO_LOG = REPO / "tmp/logs/tio-us48.log"
SSH = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
       "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", f"root@{_ipreg.ip('us48-boot')}"]
BAD = re.compile(rb"BUG:|Oops|Bad page|bad magic|Unable to handle|Internal error|imbalanced put|Kernel panic|Bad pte|"
                 rb"Segmentation fault|Illegal instruction|unhandled prefetch|Unhandled fault", re.I)
UP_S = 40.0     # handover -> ssh: 4 s userspace + ge1 up ~12 s (dev.py U2_PING_S) x 3 ; expiry = FAIL ssh
WATCH_S = 60.0  # Dan: 60 s dmesg watch
POLL_S = 5.0    # one ssh per poll; the 2 s corruption window is covered by the console log scan
STRESS_S = 10.0 # Dan: 10 s stress
DIE_MAX_MC, LM75_MAX_MC = 110000, 80000  # fan quiet rule (docs/fan.md)
TEMPS = ("for h in /sys/class/hwmon/hwmon*; do n=$(cat $h/name); case $n in die*|lm75b) echo $n $(cat $h/temp1_input);; esac; done")
# 48 MiB random file in a 64 MiB tmpfs, md5 reference once, then md5sum + cp + cmp loop for STRESS_S
STRESS = f"""mkdir -p /mnt/st; mount -t tmpfs -o size=64m tmpfs /mnt/st || exit 3
dd if=/dev/urandom of=/mnt/st/a bs=1M count=24 2>/dev/null
ref=$(md5sum /mnt/st/a | cut -d' ' -f1); n=0; end=$(( $(cut -d. -f1 /proc/uptime) + {int(STRESS_S)} ))
while [ $(cut -d. -f1 /proc/uptime) -lt $end ]; do
  cp /mnt/st/a /mnt/st/b || exit 4
  [ "$(md5sum /mnt/st/b | cut -d' ' -f1)" = "$ref" ] || {{ echo MISMATCH iter $n; exit 5; }}
  n=$((n+1)); done; echo ok iters $n; umount /mnt/st"""


def say(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def ssh(cmd: str, bound_s: float) -> tuple[int, str]:
    try:
        r = subprocess.run(SSH + [cmd], capture_output=True, text=True, timeout=bound_s)
        return r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return 124, "ssh timeout"


def run(argv: list[str], bound_s: float) -> int:
    try:
        r = subprocess.run([sys.executable] + argv, cwd=REPO, capture_output=True, text=True, timeout=bound_s)
    except subprocess.TimeoutExpired:
        say(f"{' '.join(argv)} -> bound {bound_s:.0f} s exceeded")
        return 124
    say(f"{' '.join(argv)} -> rc {r.returncode}: {r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr.strip()[-200:]}")
    return r.returncode


def temps(out: str) -> tuple[int, int]:
    die = [int(m.group(1)) for m in re.finditer(r"^die\d+ (\d+)", out, re.M)]
    lm = [int(m.group(1)) for m in re.finditer(r"^lm75b (\d+)", out, re.M)]
    return (max(die) if die else -1, max(lm) if lm else -1)


def fans_max() -> None:
    ssh("echo 255 > /sys/class/hwmon/hwmon6/pwm1", 5)  # emergency only (docs/fan.md quiet rule)


def console_bad(offset: int) -> str | None:
    data = TIO_LOG.read_bytes()[offset:]
    m = BAD.search(data)
    if not m:
        return None
    s = data.rfind(b"\n", 0, m.start()) + 1
    return data[s:data.find(b"\n", m.end())].decode("latin1")[:200]


def step(mhz: int, image: str | None) -> dict:
    res = {"mhz": mhz, "l2off": bool(image), "result": "FAIL", "first_error": "", "die_max_mC": -1, "lm75_max_mC": -1}
    off = TIO_LOG.stat().st_size if TIO_LOG.exists() else 0
    if run(["scripts/autochain_boot.py"], 60):
        res["first_error"] = "cold boot to awto-uboot failed"
        return res
    # first net use after power-up loses ARP in the sw1 forwarding delay (seen 2026-11-15): ping until alive, <= 4 x 15 s
    alive = False
    sess = dev.console.Session()
    try:
        sess.write(b"\x03\r\n")
        sess.expect(dev.U2_PROMPTS, dev.BAUD_REPLY_S, "prompt")
        sess.send_line(f"setenv ipaddr {dev.U2_IP}; setenv serverip {_ipreg.ip('selwyn-enp7s0')}")
        sess.expect(dev.U2_PROMPTS, dev.BAUD_REPLY_S, "prompt")
        for n in range(4):
            sess.send_line(f"ping {_ipreg.ip('selwyn-enp7s0')}")
            hit = sess.expect([b"is alive", b"not alive"], 16.0, "ping")
            sess.send_line("")
            sess.expect(dev.U2_PROMPTS, dev.BAUD_REPLY_S, "prompt")
            if hit == b"is alive":
                alive = True
                break
    except dev.console.Timeout as e:
        say(f"net warm-up: {e}")
    finally:
        sess.close()
    say(f"net warm-up alive={alive}")
    if mhz != 1550 and run(["scripts/u2_ramboot.py", "--image", f"tmp/uboot/us48-uboot-cpu{mhz}.bin"], 120):
        res["first_error"] = "variant ramboot failed"
        return res
    img = image or "tmp/mainline/us48-mainline-debug.bin"
    if run(["scripts/dev.py", "u2-boot", "--from", "tftp", "--image", img], 150):
        res["first_error"] = "mainline u2-boot failed"
        return res
    t0 = time.monotonic()
    up = False
    while time.monotonic() - t0 < UP_S:
        bad = console_bad(off)
        if bad:
            res["first_error"] = f"console: {bad}"
            return res
        rc, out = ssh("echo up", 5)
        if rc == 0:
            up = True
            break
        time.sleep(1.0)
    if not up:
        res["first_error"] = f"ssh not up in {UP_S:.0f} s (console tail: {TIO_LOG.read_bytes()[-200:].decode('latin1')!r})"
        return res
    say(f"ssh up {time.monotonic() - t0:.1f} s after handover")
    t0 = time.monotonic()
    while time.monotonic() - t0 < WATCH_S:
        rc, out = ssh("dmesg | grep -aiE 'BUG:|Oops|Bad page|bad magic|Unable to handle|Internal error|imbalanced put' | head -3; " + TEMPS, 15)
        d, lm = temps(out)
        res["die_max_mC"], res["lm75_max_mC"] = max(res["die_max_mC"], d), max(res["lm75_max_mC"], lm)
        bad = console_bad(off)
        hit = next((l for l in out.splitlines() if BAD.search(l.encode())), None) or bad
        if hit or rc:
            res["first_error"] = f"watch +{time.monotonic() - t0:.0f} s: {hit or out[-150:]}"
            return res
        if d >= DIE_MAX_MC or lm >= LM75_MAX_MC:
            fans_max()
            res["first_error"] = f"THERMAL die {d} lm75 {lm} mC: fans 100 %, stop"
            res["result"] = "STOP"
            return res
        time.sleep(POLL_S)
    rc, out = ssh(STRESS + "; " + TEMPS, 40)
    d, lm = temps(out)
    res["die_max_mC"], res["lm75_max_mC"] = max(res["die_max_mC"], d), max(res["lm75_max_mC"], lm)
    bad = console_bad(off)
    if rc or "ok iters" not in out or bad:
        res["first_error"] = f"stress rc {rc}: {bad or out[-200:]}"
        return res
    res["stress"] = re.search(r"ok iters \d+", out).group(0)
    res["result"] = "PASS"
    return res


def main() -> int:
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        return 2
    image = a[a.index("--l2off") + 1] if "--l2off" in a else None
    res = step(int(a[0]), image)
    say("RESULT " + json.dumps(res))
    return 0 if res["result"] == "PASS" else 1


if __name__ == "__main__":
    help_guard(__doc__, strict=True, known=('--l2off',))
    sys.exit(main())
