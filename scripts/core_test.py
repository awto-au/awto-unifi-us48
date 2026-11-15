#!/usr/bin/env python3
"""core_test — 1-2 s core SoC tests on a1 or a2 of the mainline debug image (#84, table in docs/core-bringup.md).

One subcommand per #84 item; each prints and logs ONE result line (tmp/logs/core_test.log):
  <ISO time> core_test item=<item> unit=<a1|a2> status=<OK|FAIL|BLOCKED|N/A> key=value ...

  caches   PL310 id/ctrl/aux + latency/bandwidth by working set (us48bench lat, bw)
  clock    dependent-add loop MHz (us48bench spin) against --mhz
  timers   clocksource, tick drift vs this host (a1 only), cyclictest latency (1 s)
  irq      /proc/interrupts deltas over 1 s
  dma      IDM reset check, pl330 + dmatest (--idm-release to deassert dmac_idm.RESET_CONTROL)
  memory   memtester + meminfo split
  rng      rngtest on 20000 bits of /dev/hwrng
  wdt      SP805 sysfs state and counter reads (no reset test)
  flash    QSPI mtd read throughput (a1 only)
  buses    I2C adapters and GPIO chips (listing only, no bus probe)
  selftest test_bitmap + locktorture + rcutorture, ~2 s each (debug image)
  all      every item above
Options: --unit a1|a2 (default a1), --dry-run (print the board commands only), --idm-release, --mhz N, --secs N.

Access: a1 = ssh root@us48-boot (scripts/lx.py settings). a2 = a1's /dev/ttyA20 (docs/a2-linux.md): the reader
`cat /dev/ttyA20 > /tmp/a2.out &` must be running on a1; commands go in with printf, results are read from /tmp/a2.out
after a marker. Board commands use the tools in the debug image (build_rootfs.py --tools). No sleep in board shell
commands: waits happen here (A2_POLL_S, test durations inside the board tools).
"""
import argparse
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import hwdb  # noqa: E402
import lx  # noqa: E402  (HOST, OPTS)

LOG = ROOT / "tmp/logs/core_test.log"
A2_TTY, A2_OUT = "/dev/ttyA20", "/tmp/a2.out"
# a1 call limit = ssh connect 8 s (lx.py OPTS) + 2 s test, x1.25 = 13 s; a test that needs more passes its own `limit`
A1_LIMIT_S = 13
# a2 call limit: ~100-char command at DCC input speed (~40 chars per 0.3 s, a2-linux.md) + 2 s test + output readout; unmeasured, x3 guess
A2_LIMIT_S = 40
A2_POLL_S = 1.0  # marker poll interval over ssh (one ssh round trip is ~0.2 s)
DRY = False
DB = hwdb.load()


class Fail(Exception):
    pass


def a1_sh(cmd: str, limit: float = A1_LIMIT_S) -> tuple[int, str]:
    full = ["ssh", *lx.OPTS, f"root@{lx.HOST}", cmd]
    if DRY:
        print("  a1$", cmd)
        return 0, ""
    try:
        r = subprocess.run(full, capture_output=True, text=True, timeout=limit)
    except subprocess.TimeoutExpired:
        raise Fail(f"timeout {limit:.0f} s on a1: {cmd[:60]}")
    if r.returncode == 255:
        raise Fail(f"ssh to a1 failed: {r.stderr.strip()[:80]}")
    return r.returncode, r.stdout


def a2_sh(cmd: str, limit: float = A2_LIMIT_S) -> tuple[int, str]:
    if DRY:
        print("  a2$", cmd)
        return 0, ""
    rc, out = a1_sh(f"wc -c < {A2_OUT}")
    if rc:
        raise Fail(f"{A2_OUT} missing on a1: start `cat {A2_TTY} > {A2_OUT} &` before the a2 console")
    off = int(out.strip() or 0)
    mark = f"@@{int(time.time()) % 100000}@@"
    typed = f"{cmd}; echo {mark}$?".replace("'", "'\\''")  # survive the single quotes around printf's argument
    a1_sh(f"printf '%s\\n' '{typed}' > {A2_TTY}")
    t0 = time.monotonic()
    while time.monotonic() - t0 < limit:
        time.sleep(A2_POLL_S)  # host-side wait for a2 to finish the command; expiry = Fail below
        _, txt = a1_sh(f"tail -c +{off + 1} {A2_OUT}")
        m = re.search(re.escape(mark) + r"(\d+)", txt)
        if m:
            return int(m.group(1)), txt[:m.start()]
    raise Fail(f"timeout {limit:.0f} s on a2 waiting for {mark}: {cmd[:60]}")


class Unit:
    def __init__(self, name: str):
        self.name = name
        self.sh = a1_sh if name == "a1" else a2_sh

    def run(self, cmd: str, limit: float | None = None) -> tuple[int, str]:
        return self.sh(cmd, limit) if limit else self.sh(cmd)


def kv(text: str, key: str, cast=str):
    m = re.search(rf"{key}[=:]\s*(\S+)", text)
    return cast(m.group(1)) if m else None


def devmem(u: Unit, addr: int) -> int:
    rc, out = u.run(f"devmem 0x{addr:x} 32")
    if DRY:
        return 0
    m = re.search(r"0x[0-9a-fA-F]+", out)
    if rc or not m:
        raise Fail(f"devmem 0x{addr:x} failed: {out.strip()[:60]}")
    return int(m.group(0), 16)


# ---- tests: each returns (status, "key=value ...") ----

def t_caches(u, a):
    base = DB.addr("pl310")
    cid, ctrl, aux = (devmem(u, base + o) for o in (0x0, 0x100, 0x104))
    # stock aux 0x0a120000, ours 0x4e120001 (#84 item 1); L2 off is the a2 default
    _, out = u.run(f"us48bench lat {2048 if u.name == 'a2' else 16384}; us48bench bw")
    lat = re.search(r"lat_ns (.*)", out)
    bw = re.search(r"bw_MBps (.*)", out)
    st = "OK" if DRY or (lat and bw) else "FAIL"
    return st, f"l2_id=0x{cid:08x} l2_en={ctrl & 1} l2_aux=0x{aux:08x} lat_ns[{lat.group(1) if lat else '?'}] bw_MBps[{bw.group(1) if bw else '?'}]"


def t_clock(u, a):
    _, out = u.run("us48bench spin")
    mhz = kv(out, "spin_mhz", float)
    if DRY:
        return "OK", ""
    if mhz is None:
        return "FAIL", "no spin_mhz"
    # 1 add/cycle with the I-cache on; far below = I-cache off or a slower clock, so only a ratio is shown
    return "OK", f"spin_mhz={mhz:.0f} ref_mhz={a.mhz} ratio={mhz / a.mhz:.2f}"


def t_timers(u, a):
    _, cs = u.run("cat /sys/devices/system/clocksource/clocksource0/current_clocksource")
    ppm = "n/a"
    if u.name == "a1" and not DRY:  # streamed lines, host stamps each arrival; a2's DCC poll is too coarse for ppm
        p = subprocess.Popen(["ssh", *lx.OPTS, f"root@{lx.HOST}", f"us48bench tick {a.secs}"], stdout=subprocess.PIPE, text=True)
        stamps = []
        t_end = time.monotonic() + a.secs + 10  # connect 8 s + sleep + 2 s
        for line in p.stdout:
            m = re.search(r"mono_ns=(\d+)", line)
            if m:
                stamps.append((time.monotonic(), int(m.group(1))))
            if time.monotonic() > t_end:
                p.kill()
                raise Fail("timeout on tick stream")
        p.wait()
        if len(stamps) == 2:
            host = stamps[1][0] - stamps[0][0]
            board = (stamps[1][1] - stamps[0][1]) / 1e9
            ppm = f"{(board / host - 1) * 1e6:.0f}(+-{2000 / a.secs:.0f}ms/s jitter)"
    elif DRY:
        u.run(f"us48bench tick {a.secs}")
    _, out = u.run("cyclictest -q -t1 -p80 -i1000 -l1000 -m")
    m = re.search(r"Min:\s*(\d+)\s+Act:\s*(\d+)\s+Avg:\s*(\d+)\s+Max:\s*(\d+)", out)
    if DRY:
        return "OK", ""
    if not m:
        return "FAIL", f"clocksource={cs.strip()} cyclictest: no result ({out.strip()[:60]})"
    return "OK", f"clocksource={cs.strip()} tick_drift_ppm={ppm} cyclictest_us min={m[1]} avg={m[3]} max={m[4]}"


def t_irq(u, a):
    _, out = u.run("cat /proc/interrupts; us48bench tick 1 >/dev/null; echo @@; cat /proc/interrupts")
    if DRY:
        return "OK", ""
    before, _, after = out.partition("@@")

    def counts(txt):
        d = {}
        for line in txt.splitlines():
            m = re.match(r"\s*(\S+):\s+(\d+)\s+(.*)", line)
            if m:
                d[m[1] + " " + m[3].strip()[:24]] = int(m[2])
        return d

    b, e = counts(before), counts(after)
    deltas = sorted(((e[k] - b.get(k, 0), k) for k in e if e[k] - b.get(k, 0) > 0), reverse=True)[:4]
    return ("OK" if deltas else "FAIL"), "irq_per_1s[" + "; ".join(f"{k}={d}" for d, k in deltas) + "] msi=n/a(#54)"


def t_dma(u, a):
    idm = DB.addr("dmac_idm") + 0x800
    rst = devmem(u, idm)
    if rst & 1:
        if not a.idm_release:
            return "BLOCKED", "dmac in IDM reset (RESET_CONTROL=0x1): rerun with --idm-release (conf C reset value; releasing it is untested)"
        u.run(f"devmem 0x{idm:x} 32 0")
        rst = devmem(u, idm)
    rel = "$(uname -r)"
    mod = f"/lib/modules/{rel}"
    _, out = u.run(f"insmod {mod}/pl330.ko; insmod {mod}/dmatest.ko max_channels=1 threads_per_chan=1 iterations=20 "
                   f"test_buf_size=16384 timeout=1000 wait=1 run=1; dmesg | grep -E 'dmatest: .*summary|pl330' | tail -n 4; "
                   f"rmmod dmatest; rmmod pl330", limit=A1_LIMIT_S + 10)
    if DRY:
        return "OK", ""
    m = re.search(r"summary (\d+) tests, (\d+) failures (\d+)\.(\d+) iops (\d+) KB/s", out)
    if not m:
        return "FAIL", f"idm_reset=0x{rst:x} no dmatest summary: {out.strip()[-120:]!r}"
    return ("OK" if m[2] == "0" else "FAIL"), f"idm_reset=0x{rst:x} tests={m[1]} failures={m[2]} iops={m[3]} KBps={m[5]}"


def t_memory(u, a):
    size = "512K" if u.name == "a2" else "1M"
    _, out = u.run(f"memtester {size} 1; echo rc=$?; grep -E 'MemTotal|HighTotal|LowTotal' /proc/meminfo", limit=a.mem_limit)
    if DRY:
        return "OK", ""
    ok = "rc=0" in out
    mem = " ".join(f"{k}={v}" for k, v in re.findall(r"(\w+Total):\s+(\d+) kB", out))
    return ("OK" if ok else "FAIL"), f"memtester {size} x1 rc={'0' if ok else '!=0'} {mem}"


def t_rng(u, a):
    _, out = u.run("cat /sys/class/misc/hw_random/rng_current; head -c 20000 /dev/hwrng | rngtest")
    if DRY:
        return "OK", ""
    m = re.search(r"rngtest: blocks=(\d+) fail=(\d+) monobit=(\d+) poker=(\d+) runs=(\d+) longrun=(\d+)", out)
    if not m:
        return "FAIL", f"no rngtest result: {out.strip()[:80]!r}"
    cur = out.splitlines()[0].strip() if out else "?"
    return ("OK" if m[1] != "0" and m[2] == "0" else "FAIL"), f"driver={cur} blocks={m[1]} fail={m[2]} (mono={m[3]} poker={m[4]} runs={m[5]} long={m[6]})"


def t_wdt(u, a):
    val = DB.addr("sp805") + 0x4
    v1 = devmem(u, val)
    _, out = u.run("cat /sys/class/watchdog/watchdog0/identity /sys/class/watchdog/watchdog0/timeout /sys/class/watchdog/watchdog0/state 2>&1")
    v2 = devmem(u, val)
    if DRY:
        return "OK", ""
    f = out.split()
    # VALUE counts down at APB 50 MHz; a rising second read = reload (fed) between the reads
    return "OK", f"sysfs[{' '.join(f)}] value1=0x{v1:08x} value2=0x{v2:08x} counting={'yes' if v2 != v1 else 'no'}"


def t_flash(u, a):
    if u.name == "a2":
        return "N/A", "unit 1 has no flash on QSPI (docs/a2.md)"
    _, out = u.run("head -n 3 /proc/mtd; us48bench rd /dev/mtd0ro 1048576")
    if DRY:
        return "OK", ""
    m = re.search(r"MBps=([\d.]+)", out)
    return ("OK" if m else "FAIL"), f"mtd0 1MiB MBps={m[1] if m else '?'}"


def t_buses(u, a):
    _, out = u.run("i2cdetect -l; echo @@; mount -t debugfs d /sys/kernel/debug 2>/dev/null; grep -c gpiochip /sys/kernel/debug/gpio")
    if DRY:
        return "OK", ""
    i2c, _, gp = out.partition("@@")
    n = len([l for l in i2c.splitlines() if l.startswith("i2c-")])
    return ("OK" if n else "FAIL"), f"i2c_adapters={n} gpiochips={gp.strip() or '?'} (listing only, no probe)"


def torture(u, mod_args: str, mod: str, tag: str, ok: str, secs: int = 2):
    rel = "$(uname -r)"
    path = f"/lib/modules/{rel}"
    # insmod starts the run, tick 2 = the 2 s test window (nanosleep in the board tool), rmmod prints the summary
    _, out = u.run(f"insmod {path}/torture.ko; insmod {path}/{mod}.ko {mod_args}; us48bench tick {secs} >/dev/null; "
                   f"rmmod {mod}; dmesg | grep -E '{tag}.*(End of test|SUCCESS|FAILURE|ERROR)' | tail -n 2; rmmod torture", limit=A1_LIMIT_S + 10)
    return out


def t_selftest(u, a):
    rel = "$(uname -r)"
    path = f"/lib/modules/{rel}"
    _, bm = u.run(f"insmod {path}/test_bitmap.ko; dmesg | grep test_bitmap | tail -n 2; rmmod test_bitmap")
    lk = torture(u, "torture_type=spin_lock", "locktorture", "lock-torture", "SUCCESS")
    rc = torture(u, "", "rcutorture", "rcu-torture", "SUCCESS")
    if DRY:
        return "OK", ""
    parts, st = [], "OK"
    for name, txt, good in (("bitmap", bm, r"all \d+ tests passed|passed"), ("locktorture", lk, "SUCCESS"), ("rcutorture", rc, "SUCCESS")):
        ok = bool(re.search(good, txt)) and "FAILURE" not in txt
        st = st if ok else "FAIL"
        parts.append(f"{name}={'ok' if ok else 'FAIL'}")
    return st, " ".join(parts)


TESTS = {"caches": t_caches, "clock": t_clock, "timers": t_timers, "irq": t_irq, "dma": t_dma, "memory": t_memory,
         "rng": t_rng, "wdt": t_wdt, "flash": t_flash, "buses": t_buses, "selftest": t_selftest}


def report(item: str, unit: str, status: str, text: str) -> None:
    line = f"{datetime.now().astimezone().isoformat(timespec='seconds')} core_test item={item} unit={unit} status={status} {text}".rstrip()
    print(line)
    if not DRY:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(line + "\n")


def main() -> int:
    global DRY
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("item", choices=[*TESTS, "all"])
    ap.add_argument("--unit", choices=("a1", "a2"), default="a1")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--idm-release", action="store_true", help="dma: write 0 to dmac_idm.RESET_CONTROL if it reads 1")
    ap.add_argument("--mhz", type=int, default=1000, help="clock: reference ARM MHz (1000 accepted on both units)")
    ap.add_argument("--secs", type=int, default=2, help="timers: tick window")
    ap.add_argument("--mem-limit", type=float, default=None, help="memory: limit in s (default a1 20, a2 60; unmeasured, first run sets it)")
    a = ap.parse_args()
    DRY = a.dry_run
    if a.mem_limit is None:
        a.mem_limit = 60 if a.unit == "a2" else 20
    u = Unit(a.unit)
    rc = 0
    for item in (TESTS if a.item == "all" else [a.item]):
        try:
            status, text = TESTS[item](u, a)
        except Fail as e:
            status, text = "FAIL", str(e)
        report(item, a.unit, status, text)
        rc |= status == "FAIL"
    return rc


if __name__ == "__main__":
    sys.exit(main())
