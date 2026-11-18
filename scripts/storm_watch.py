#!/usr/bin/env python3
"""storm_watch - always-on host-NIC multicast/broadcast rx-rate sampler; cuts the US-48 relay on a storm (#139).

Usage: storm_watch.py [--guard] [--stop] [--status] [--nic IF] [--selftest]
- dev.py starts `--guard` (detached) for every hardware command; singleton via tmp/storm_watch.pid (flock), exits
  IDLE_S after the last dev.py process. `--stop` ends it, `--selftest` feeds fake counters (no NIC, no relay).
- NIC = the one routing to us48-mgmt (`ip route get`), counters rx_multicast_packets + rx_broadcast_packets (ethtool ioctl).
- Sampling every SAMPLE_S; baseline = mean of the first BASELINE_S; trip = rate > max(FLOOR_PPS, 20 x baseline) on 2 samples in a row.
- Threshold: Selwyn enp8s0 idles 1-10 pps mcast+bcast (30 x 1 s samples, 2026-11-19); floor 200 pps = 20 x that peak,
  a forwarding loop is thousands of pps (st1008f #19: 13k pps at +80 s). Unicast traffic is not counted.
- Trip: US-48 relay OFF (dev.relay_set), retried; logged: baseline, limit, each over-limit sample, kill result.
- Log: tmp/logs/storm_watch.log
"""
import argparse
import array
import fcntl
import os
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/storm_watch.log"
PIDFILE = ROOT / "tmp/storm_watch.pid"
SAMPLE_S = 1.0  # 2 over-limit samples = 2 s to detect; a loop ramps over tens of seconds
BASELINE_S = 3.0
FLOOR_PPS = 200
FACTOR = 20
IDLE_S = 300  # board keeps forwarding after dev.py exits; 300 s = ~4x the st1008f loop ramp (+80 s)
RELAY_KILL_TRIES = 3  # each dev.relay_set already retries 3x (8 s each); expiry = "cut power by hand" logged


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(line + "\n")


class NicStats:
    """rx multicast + broadcast via ETHTOOL_GSTATS (sysfs 'multicast' omits broadcast on some drivers)."""

    GSSET_INFO, GSTRINGS, GSTATS, SS_STATS, SIOCETHTOOL = 0x37, 0x1B, 0x1D, 1, 0x8946
    WANT = ("rx_multicast_packets", "rx_broadcast_packets")

    def __init__(self, nic: str):
        self.nic, self.sock = nic.encode(), socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        info = bytearray(struct.pack("IIQI", self.GSSET_INFO, 0, 1 << self.SS_STATS, 0))
        self._ioctl(info)
        self.n = struct.unpack_from("I", info, 16)[0]
        names = bytearray(struct.pack("III", self.GSTRINGS, self.SS_STATS, self.n) + bytes(32 * self.n))
        self._ioctl(names)
        keys = [names[12 + 32 * i:44 + 32 * i].split(b"\0")[0].decode() for i in range(self.n)]
        missing = [k for k in self.WANT if k not in keys]
        if missing:
            raise SystemExit(f"storm_watch: {nic} lacks ethtool stats {missing}; pick another NIC with --nic")
        self.idx = [keys.index(k) for k in self.WANT]

    def _ioctl(self, buf: bytearray) -> None:
        ptr = array.array("B", buf)
        fcntl.ioctl(self.sock.fileno(), self.SIOCETHTOOL, struct.pack("16sP", self.nic, ptr.buffer_info()[0]))
        buf[:] = ptr.tobytes()

    def read(self) -> int:
        buf = bytearray(struct.pack("II", self.GSTATS, self.n) + bytes(8 * self.n))
        self._ioctl(buf)
        vals = struct.unpack_from(f"{self.n}Q", buf, 8)
        return sum(vals[i] for i in self.idx)


def default_nic() -> str:
    import _ipreg
    out = subprocess.run(["ip", "route", "get", _ipreg.ip("us48-mgmt")], capture_output=True, text=True, timeout=5)  # local, <10 ms
    words = out.stdout.split()
    if out.returncode or "dev" not in words:
        raise SystemExit(f"storm_watch: no route to us48-mgmt: {out.stderr.strip()}")
    return words[words.index("dev") + 1]


def relay_off() -> bool:
    import dev  # heavy import, only on a trip
    host = os.environ.get("US48_RELAY", dev.RELAY_DEFAULT)
    for i in range(1, RELAY_KILL_TRIES + 1):
        try:
            log(f"KILL relay {host}: {dev.relay_set(host, False)} (try {i})")
            return True
        except OSError as e:
            log(f"KILL relay try {i}/{RELAY_KILL_TRIES} failed: {e}")
    log("KILL FAILED: cut the US-48 power by hand")
    return False


def watch(read, now, sleep, kill, idle=None, duration=None) -> int:
    """Sampling loop with injectable counter/clock/sleep/kill. Returns 2 on a storm, 0 on idle/duration end."""
    t0 = now()
    prev, prev_t, rates = read(), t0, []
    while now() - t0 < BASELINE_S:
        sleep(SAMPLE_S)
        cur, t = read(), now()
        rates.append((cur - prev) / (t - prev_t))
        prev, prev_t = cur, t
    base = sum(rates) / len(rates)
    limit = max(FLOOR_PPS, FACTOR * base)
    log(f"baseline {base:.1f} pps -> trip above {limit:.0f} pps for 2 samples; ARMED")
    over, last_busy = 0, now()
    while duration is None or now() - t0 < duration:
        sleep(SAMPLE_S)
        if idle is not None:
            if idle():
                last_busy = now()
            elif now() - last_busy > IDLE_S:
                log(f"idle {IDLE_S} s after the last dev.py, no storm: exit")
                return 0
        cur, t = read(), now()
        pps = (cur - prev) / (t - prev_t)
        prev, prev_t = cur, t
        over = over + 1 if pps > limit else 0
        if over:
            log(f"over limit {over}/2: {pps:.0f} pps")
        if over >= 2:
            log(f"STORM {pps:.0f} pps > {limit:.0f}: cutting the relay")
            kill()
            return 2
    return 0


def devpy_running() -> bool:
    me = os.getpid()
    for pid in os.listdir("/proc"):
        if pid.isdigit() and int(pid) != me:
            try:
                argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if any(a.endswith(b"dev.py") for a in argv[:3]):
                return True
    return False


def selftest() -> int:
    fails = 0

    def run(rates, what, want_kill):
        nonlocal fails
        clock, count, cuts = [0.0], [0], []
        seq = iter(rates)

        def sleep(s):
            clock[0] += s
            count[0] += int(next(seq, rates[-1]) * s)
        rc = watch(lambda: count[0], lambda: clock[0], sleep, lambda: cuts.append(1), duration=40)
        ok = (rc == 2) == want_kill and bool(cuts) == want_kill
        fails += not ok
        log(f"selftest {what}: rc={rc} cuts={len(cuts)} {'ok' if ok else 'FAIL'}")

    run([3] * 3 + [5] * 40, "quiet 3-10 pps (no trip)", False)
    run([3] * 3 + [150] * 40, "150 pps burst below floor (no trip)", False)
    run([3] * 3 + [3000, 3] + [3] * 40, "single 1 s spike (no trip)", False)
    run([3] * 3 + [3000, 3000], "2 samples at 3000 pps (trip)", True)
    run([400] * 3 + [5000, 5000], "noisy baseline 400 pps: limit 8000 (5000 no trip)", False)
    run([400] * 3 + [9000, 9000], "noisy baseline 400 pps, 9000 pps (trip)", True)
    log(f"{'PASS' if not fails else 'FAIL'} storm_watch selftest")
    return 1 if fails else 0


def stop() -> int:
    try:
        pid = int(PIDFILE.read_text())
        os.kill(pid, signal.SIGTERM)
        log(f"stopped pid {pid}")
        return 0
    except (OSError, ValueError):
        log("no running storm_watch")
        return 1


def main() -> int:
    help_guard(__doc__)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--nic")
    ap.add_argument("--guard", action="store_true", help="singleton; exits IDLE_S after the last dev.py")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.stop:
        return stop()
    if a.status:
        print(PIDFILE.read_text().strip() if PIDFILE.exists() else "not running")
        return 0
    PIDFILE.parent.mkdir(parents=True, exist_ok=True)
    lock = PIDFILE.with_suffix(".lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0
    PIDFILE.write_text(str(os.getpid()))
    nic = a.nic or default_nic()
    stats = NicStats(nic)
    log(f"=== start nic={nic} pid={os.getpid()} floor={FLOOR_PPS} factor={FACTOR}")
    try:
        return watch(stats.read, time.monotonic, time.sleep, relay_off, idle=devpy_running if a.guard else None,
                     duration=None if a.guard else 900)
    finally:
        PIDFILE.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
