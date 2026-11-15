#!/usr/bin/env python3
"""warm_reboot_test — warm reboot of the normal image after a2 never started / started+stopped (#90).

Usage: warm_reboot_test.py CASE [--reps N] [--no-cycle]
  CASE: a = a2 never started, b = a2 100 MHz started + stopped, c = a2 1000 MHz started + stopped
Each rep: power cycle (dev.py power-cycle, autochain boots kernel0), wait ssh, case setup, `reboot` over ssh, watch tio log.
Verdicts: ok (banner + ssh back), no-banner (hang, no U-Boot banner), banner-no-linux. A hang is left as is: the next rep power cycles.
Log: tmp/logs/warm_reboot_test.log (+ console tail of every failing rep).
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/warm_reboot_test.log"
TIO = ROOT / "tmp/logs/tio-us48.log"
# power-on to ssh ~20 s (docs: a2_up.py); 45 s = 2x
SSH_UP_S = 45.0
# a2 bustest first pass ~0.5 s after start (docs/pcie-harness.md); 3 s = 6x
A2_RUN_S = 3.0
# SP805/restart to first banner byte ~1 s after `reboot` (shutdown ~1-2 s + reset); 12 s = ~4x. Expiry = no-banner hang.
BANNER_S = 12.0
# banner to ssh: autochain boot ~20 s; 45 s = 2x
BOOT_S = 45.0
A2_START = r"""
M=$(ls /lib/modules/*/us48_a2.ko); insmod $M || exit 3
D=/sys/class/remoteproc/remoteproc0; P=/sys/module/us48_a2/parameters
echo 0 > $P/payload; echo @MHZ@ > $P/cpu_mhz; echo us48-a2-bustest.elf > $D/firmware
echo start > $D/state || exit 4
"""
A2_STOP = "echo stop > /sys/class/remoteproc/remoteproc0/state; cat /sys/class/remoteproc/remoteproc0/state"


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def lx(cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "scripts/lx.py"), cmd], capture_output=True, text=True)


def wait_ssh(bound: float) -> float | None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < bound:
        if lx("echo up").returncode == 0:
            return time.monotonic() - t0
        time.sleep(1)
    return None


def tio_tail(off: int) -> str:
    with TIO.open("rb") as f:
        f.seek(off)
        return f.read().decode(errors="replace")


def rep(case: str, n: int, cycle: bool) -> str:
    if cycle:
        r = subprocess.run([sys.executable, str(ROOT / "scripts/dev.py"), "power-cycle"], capture_output=True, text=True)
        if r.returncode:
            raise SystemExit(f"power-cycle failed: {r.stdout[-300:]}{r.stderr[-300:]}")
    up = wait_ssh(SSH_UP_S)
    if up is None:
        raise SystemExit(f"rep {n}: no ssh {SSH_UP_S} s after power cycle: stop and report")
    log(f"case {case} rep {n}: ssh up {up:.0f} s after cycle; {lx('uname -r').stdout.strip()}")
    if case in "bc":
        r = lx(A2_START.replace("@MHZ@", "100" if case == "b" else "1000"))
        if r.returncode:
            raise SystemExit(f"a2 start rc {r.returncode}: {r.stdout}{r.stderr}")
        time.sleep(A2_RUN_S)
        r = lx(A2_STOP)
        log(f"case {case} rep {n}: a2 stopped, state {r.stdout.strip()!r}; dmesg: {lx('dmesg | tail -4').stdout.strip()!r}")
        if r.stdout.strip() != "offline":
            raise SystemExit("a2 not offline after stop")
    off = TIO.stat().st_size
    lx("sync")
    t_reboot = time.monotonic()
    subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                    "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=8", "root@192.168.25.207", "reboot"],
                   capture_output=True, text=True)
    banner = None
    while time.monotonic() - t_reboot < BANNER_S:
        if "U-Boot" in tio_tail(off):
            banner = time.monotonic() - t_reboot
            break
        time.sleep(0.2)
    if banner is None:
        v = "no-banner"
    else:
        v = "ok" if wait_ssh(BOOT_S) is not None else "banner-no-linux"
    tail = "\n".join(tio_tail(off).splitlines()[-12:])
    log(f"case {case} rep {n}: {v} (banner {'-' if banner is None else f'{banner:.1f} s'}); console tail:\n{tail}")
    return v


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("case", choices=("a", "b", "c"))
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--no-cycle", action="store_true", help="first rep uses the board as it is (must be normal-image Linux)")
    a = ap.parse_args()
    res = [rep(a.case, i + 1, not (a.no_cycle and i == 0)) for i in range(a.reps)]
    log(f"case {a.case}: {res}")
    return 0 if all(r == "ok" for r in res) else 1


if __name__ == "__main__":
    sys.exit(main())
