#!/usr/bin/env python3
"""debug_exercise — run the debug-kernel (KASAN/lockdep/kmemleak) path tests on the board and scan dmesg after each (#84).

Usage: debug_exercise.py [a2|dma|net|reload|modules|leak|all ...]   (board already up on the debug image, debugfs mounted)
- Tests are 1-2 s each (a2 start/stop: ~7 s a1 wall, normal-profile a2 zImage). dmesg is never cleared; each step logs the
  dmesg lines added since the previous step that match BUG/WARNING/KASAN/UBSAN/lockdep/kmemleak/pagealloc/hung.
- a2 needs tmp/a2/zImage-normal (copy of build-normal zImage), tmp/a2/us48-a2.dtb, tmp/a2fw/us48-a2-boot.elf.
- Log: tmp/logs/debug_exercise.log
"""
import re
import subprocess
import sys
import time
from pathlib import Path
from _cli import help_guard  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
LOG = ROOT / "tmp/logs/debug_exercise.log"
PAT = re.compile(r"BUG:|WARNING|WARN:|KASAN|UBSAN|lockdep|circular locking|inconsistent|kmemleak|pagealloc|hung|Oops|kfence|DMA-API|poison|corrupt", re.I)
# one dmatest run: 8 threads x 20 x 16 KiB verified = ~0.5 s measured; poll 0.2 s x 100 = 20 s cap (40x), expiry = report
DMA_POLL = 100
seen = 0


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def lx(cmd: str) -> str:
    r = subprocess.run([PY, str(ROOT / "scripts/lx.py"), cmd], capture_output=True, text=True)
    return r.stdout + r.stderr


def scan(step: str) -> int:
    """New dmesg lines since the last scan that look like a report."""
    global seen
    out = lx("dmesg").splitlines()
    new = out[seen:] if len(out) >= seen else out
    seen = len(out)
    hits = [x for x in new if PAT.search(x) and "kmemleak: Kernel memory leak detector initialized" not in x]
    log(f"{step}: {len(new)} new dmesg lines, {len(hits)} report lines")
    for h in hits[:12]:
        log("  " + h[:200])
    return len(hits)


def t_a2() -> None:
    lx('grep -q us48_a2 /proc/modules || insmod /lib/modules/$(uname -r)/us48_a2.ko payload=1')
    for i in (1, 2, 3):
        r = subprocess.run([PY, str(ROOT / "scripts/a2_boot_time.py"), f"dbg{i}", "--limit", "15"] +
                           (["--zimage", "tmp/a2/zImage-normal", "--dtb", "tmp/a2/us48-a2.dtb", "--boot-elf", "tmp/a2fw/us48-a2-boot.elf"] if i == 1 else []),
                           capture_output=True, text=True, cwd=ROOT)
        log(f"a2 start {i}: {r.stdout.strip()[:160]}")
    lx("echo stop > /sys/class/remoteproc/remoteproc0/state")
    scan("a2")


def t_dma() -> None:
    M = "/lib/modules/$(uname -r)"
    wait = (f"n=0; while [ $(dmesg | grep -c 'summary') -lt $((b0+8)) ] && [ $n -lt {DMA_POLL} ]; do read -t 0.2 x <&3; n=$((n+1)); done; ")
    pre = "[ -p /tmp/f ] || mkfifo /tmp/f; exec 3<>/tmp/f; "
    for k in range(1):
        out = lx(pre + f"M={M}; b0=$(dmesg | grep -c summary); insmod $M/dmatest.ko max_channels=8 threads_per_chan=1 iterations=20 test_buf_size=16384 timeout=1000 run=1; "
                 + wait + "dmesg | grep summary | tail -8; rmmod dmatest")
        log("dmatest 8ch verify:\n" + out.strip())
    scan("dma")


def t_net() -> None:
    O = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=8"]
    H = "root@192.168.25.207"
    t = time.monotonic()
    r = subprocess.run(["bash", "-c", f"head -c 10485760 /dev/urandom | ssh {' '.join(O)} {H} 'wc -c'"], capture_output=True, text=True, timeout=60)
    log(f"tcp up 10 MiB: {r.stdout.strip()} B {10 / (time.monotonic() - t):.2f} MB/s")
    t = time.monotonic()
    r = subprocess.run(["bash", "-c", f"ssh {' '.join(O)} {H} 'head -c 10485760 /dev/zero' | wc -c"], capture_output=True, text=True, timeout=60)
    log(f"tcp down 10 MiB: {r.stdout.strip()} B {10 / (time.monotonic() - t):.2f} MB/s")
    scan("net")


def t_reload() -> None:
    for m in ("us48_cmic", "us48_cmic", "us48_cmic_xe"):
        r = subprocess.run([PY, str(ROOT / "scripts/mod_reload.py"), m], capture_output=True, text=True, cwd=ROOT)
        log(f"mod_reload {m}: rc={r.returncode} {r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ''}")
        scan(f"reload {m}")


def t_modules() -> None:
    out = lx("M=/lib/modules/$(uname -r); for m in us48_fan us48_pvtmon us48_cmic_mspi us48_cmic_gpio; do rmmod $m; echo rmmod $m rc=$?; done; "
             "for m in us48_cmic_gpio us48_cmic_mspi us48_pvtmon us48_fan; do insmod $M/$m.ko; echo insmod $m rc=$?; done")
    log("fan/pvtmon/mspi/gpio unload+reload:\n" + out.strip())
    scan("modules")


def t_leak() -> None:
    out = lx("echo scan > /sys/kernel/debug/kmemleak; cat /sys/kernel/debug/kmemleak | head -120; echo count=$(grep -c unreferenced /sys/kernel/debug/kmemleak); cat /proc/sys/kernel/tainted")
    log("kmemleak:\n" + out.strip())
    scan("leak")


TESTS = {"a2": t_a2, "dma": t_dma, "net": t_net, "reload": t_reload, "modules": t_modules, "leak": t_leak}


def main() -> int:
    global seen
    LOG.parent.mkdir(parents=True, exist_ok=True)
    names = sys.argv[1:] or ["all"]
    if names == ["all"]:
        names = list(TESTS)
    seen = len(lx("dmesg").splitlines())
    boot = [x for x in lx("dmesg").splitlines() if PAT.search(x) and "kmemleak: Kernel memory leak detector initialized" not in x]
    log(f"boot dmesg report lines: {len(boot)}")
    for x in boot[:12]:
        log("  " + x[:200])
    for n in names:
        TESTS[n]()
    return 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
