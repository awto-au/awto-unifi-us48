#!/usr/bin/env python3
"""rx_arid_cost — throughput cost of unit 0 PAXB RX_FREE_ARID_COUNT (us48_a2 `rx_arid`) on the normal image (#79, #88).

Usage: rx_arid_cost.py [--values 3 2 1 0] [--reps 3] [--phases stopped running] [--tests tcp_rx tcp_tx pl330]
- Board: normal-profile image over ssh (scripts/lx.py host); a2 Linux files already in /lib/firmware, /tmp/dmatest.ko staged.
- Per phase and value: stopped = a1 sets RX_DEBUG_CONTROL_0 [1:0] with devmem (0x1801210c, hwdb paxb0 + 0x10c), a2 offline;
  running = `us48_a2` params payload=1 cpu_mhz=1000 rx_arid=V, a2 started and shell confirmed over DCC (DCC poller on).
- Tests (each 1-2 s, median of --reps): 10 MB TCP both ways over ge1 (host python timing), PL330 dmatest 8 ch x 100 x 256 KiB noverify.
- Stops on: a2 not at a shell within A2_BOOT_S, a die >= TEMP_MAX_MC, a test failure. Register restored to 0x1f at the end.
Log: tmp/logs/rx_arid_cost.log; results tmp/logs/rx_arid_cost.json.
"""
import argparse
import json
import re
import socket
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/rx_arid_cost.log"
JSON = ROOT / "tmp/logs/rx_arid_cost.json"
HOST = "root@192.168.25.207"	# ip-registry us48-boot
BOARD_IP = "192.168.25.207"
OPTS = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=8"]
REG = "0x1801210c"	# PAXB0 RX_DEBUG_CONTROL_0 (hwdb gen: paxb0 0x18012000 + 0x10c); reset 0x1f, ARID count = bits 1:0
TCP_BYTES = 10 * 1024 * 1024
# a2 Linux shell expected 3.3-3.6 s after start (docs/a2-linux.md); 4x = 15 s, expiry = stop and report
A2_BOOT_S = 15
# a2 boot attempts per value: boots at rx_arid 3 fail about 1 in 3 (docs/a2-linux.md); each failure is logged
START_TRIES = 4
# one ssh call (module load, a2 start) is below 20 s; stall guard only
CALL_S = 40
# die0 runs 88-89 C on this box; 105 C is the stop line from the brief, PVTMON in milli-C
TEMP_MAX_MC = 100000
# TCP 10 MB at >= 1 MB/s is 10 s; a transfer slower than 12 s = wedged
TCP_LIMIT_S = 12.0


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def ssh(cmd: str, call_s: float = CALL_S) -> str:
    r = subprocess.run(["ssh", "-T", *OPTS, HOST, cmd], capture_output=True, text=True, timeout=call_s)
    if r.returncode:
        raise RuntimeError(f"ssh rc {r.returncode}: {cmd[:80]}\n{r.stdout}{r.stderr}")
    return r.stdout


def temps() -> dict:
    out = ssh('for h in /sys/class/hwmon/hwmon*; do n=$(cat $h/name); case $n in die*) echo $n $(cat $h/temp1_input);; esac; done')
    t = {l.split()[0]: int(l.split()[1]) for l in out.splitlines() if l.strip()}
    if any(v >= TEMP_MAX_MC for v in t.values()):
        raise RuntimeError(f"die temperature {t} >= {TEMP_MAX_MC} mC: stop")
    return t


def set_arid(v: int) -> None:
    ssh(f"x=$(devmem {REG} 32); devmem {REG} 32 $(( (x & ~3) | {v} )) ; echo $(devmem {REG} 32)")
    got = int(ssh(f"devmem {REG} 32").strip(), 16)
    if got & 3 != v:
        raise RuntimeError(f"RX_DEBUG_CONTROL_0 reads {got:#x}, wanted ARID {v}")


A2_START = r"""
D=/sys/class/remoteproc/remoteproc0; P=/sys/module/us48_a2/parameters
lsmod | grep -q us48_a2 || insmod /tmp/us48_a2.ko
[ "$(cat $D/state)" = offline ] || echo stop > $D/state
echo 1 > $P/payload; echo 1000 > $P/cpu_mhz; echo @V@ > $P/rx_arid
echo us48-a2-boot.elf > $D/firmware
[ -p /tmp/f ] || mkfifo /tmp/f; exec 3<>/tmp/f
u() { read a b < /proc/uptime; echo ${a%.*}${a#*.}; }
rm -f /tmp/a2.out; cat /dev/ttyA20 > /tmp/a2.out & R=$!
t0=$(u); echo start > $D/state || { kill $R; exit 4; }
while ! grep -q "us48-a2: Linux" /tmp/a2.out && [ $(( $(u) - t0 )) -lt @LIM@ ]; do read -t 0.2 x <&3; done
printf 'echo SHELL""UP\n' > /dev/ttyA20
while ! grep -q "^SHELLUP" /tmp/a2.out && [ $(( $(u) - t0 )) -lt @LIM@ ]; do read -t 0.2 x <&3; done
kill $R
grep -q "^SHELLUP" /tmp/a2.out && echo SHELL_OK $(( $(u) - t0 )) || echo SHELL_FAIL
"""


def a2_start(v: int) -> int:
    """Start a2 Linux, up to START_TRIES attempts (a failed boot = a2 panic from the stale read, counted); returns the attempt count."""
    for n in range(1, START_TRIES + 1):
        out = ssh(A2_START.replace("@V@", str(v)).replace("@LIM@", str(A2_BOOT_S * 100)))
        log(f"a2 start rx_arid={v} attempt {n}: {out.strip().splitlines()[-1]}")
        if "SHELL_OK" in out:
            return n
        log("a2 text: " + ssh("cat -v /tmp/a2.out | tail -3").replace("\n", " | "))
    raise RuntimeError(f"a2 did not reach a shell in {START_TRIES} attempts")


def a2_alive() -> bool:
    out = ssh(r"""rm -f /tmp/a2.out; cat /dev/ttyA20 > /tmp/a2.out & R=$!
printf 'echo ALIVE""OK\n' > /dev/ttyA20
[ -p /tmp/f ] || mkfifo /tmp/f; exec 3<>/tmp/f
n=0; while ! grep -q "^ALIVEOK" /tmp/a2.out && [ $n -lt 25 ]; do read -t 0.2 x <&3; n=$((n+1)); done
kill $R; grep -q "^ALIVEOK" /tmp/a2.out && echo YES || echo NO""")
    return "YES" in out


def a2_stop() -> None:
    ssh("D=/sys/class/remoteproc/remoteproc0; [ -d $D ] || exit 0; [ \"$(cat $D/state)\" = offline ] || echo stop > $D/state")


def host_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect((BOARD_IP, 9))
    ip = s.getsockname()[0]
    s.close()
    return ip


def tcp_rx() -> float:
    """host -> board: board nc -l to /dev/null; MB/s from connect to board close (EOF back)."""
    p = subprocess.Popen(["ssh", "-T", *OPTS, HOST, "nc -l -p 5001 > /dev/null"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        s = None
        t_conn = time.perf_counter()
        while s is None:
            try:
                s = socket.create_connection((BOARD_IP, 5001), timeout=2)
            except OSError:
                if time.perf_counter() - t_conn > TCP_LIMIT_S:
                    raise RuntimeError("tcp_rx: board nc never listened")
        s.settimeout(TCP_LIMIT_S)
        buf = b"\0" * 65536
        t0 = time.perf_counter()
        for _ in range(TCP_BYTES // len(buf)):
            s.sendall(buf)
        s.shutdown(socket.SHUT_WR)
        while s.recv(4096):
            pass
        dt = time.perf_counter() - t0
        s.close()
    finally:
        p.terminate()
        p.wait(5)
    return TCP_BYTES / 1e6 / dt


def tcp_tx() -> float:
    """board -> host: board dd | nc to the host; MB/s from first byte to EOF."""
    ls = socket.socket()
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.bind(("", 5002))
    ls.listen(1)
    ls.settimeout(TCP_LIMIT_S)
    p = subprocess.Popen(["ssh", "-T", *OPTS, HOST, f"dd if=/dev/zero bs=1M count=10 2>/dev/null | nc {host_ip()} 5002"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        c, _ = ls.accept()
        c.settimeout(TCP_LIMIT_S)
        n = 0
        d = c.recv(65536)
        t0 = time.perf_counter()
        n += len(d)
        while d:
            d = c.recv(65536)
            n += len(d)
        dt = time.perf_counter() - t0
        c.close()
    finally:
        ls.close()
        p.terminate()
        p.wait(5)
    if n != TCP_BYTES:
        raise RuntimeError(f"tcp_tx: got {n} of {TCP_BYTES} bytes")
    return (n - 65536) / 1e6 / dt	# first chunk arrived before t0


def pl330() -> float:
    # 8 channels x 100 x 256 KiB at ~300 MB/s = 0.7 s expected; poll 15 x 0.2 s = 3 s (4x), expiry = fewer than 8 summaries -> error with the dmesg
    out = ssh("rmmod dmatest 2>/dev/null; dmesg -c > /dev/null; [ -p /tmp/f ] || mkfifo /tmp/f; exec 3<>/tmp/f; "
              "insmod /tmp/dmatest.ko max_channels=8 threads_per_chan=1 iterations=100 test_buf_size=262144 norandom=1 noverify=1 "
              "timeout=500 wait=1 run=1; n=0; while [ $(dmesg | grep -c summary) -lt 8 ] && [ $n -lt 15 ]; do read -t 0.2 x <&3; n=$((n+1)); done; "
              "dmesg | grep -v 'Added\\|Started'; rmmod dmatest")
    tot = 0.0
    n = 0
    for m in re.finditer(r"summary (\d+) tests, (\d+) failures .* (\d+) KB/s", out):
        if m.group(1) != "100" or m.group(2) != "0":
            raise RuntimeError("dmatest not clean: " + out[-1200:])
        tot += int(m.group(3)) / 1000.0
        n += 1
    if n != 8:
        raise RuntimeError(f"dmatest: {n} summary lines\n{out[-1200:]}")
    return tot


TESTS = {"tcp_rx": tcp_rx, "tcp_tx": tcp_tx, "pl330": pl330}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--values", type=int, nargs="+", default=[3, 2, 1, 0])
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--phases", nargs="+", default=["stopped", "running"], choices=["stopped", "running"])
    ap.add_argument("--tests", nargs="+", default=list(TESTS), choices=list(TESTS))
    a = ap.parse_args()
    res: dict = {}
    log(f"start values={a.values} reps={a.reps} phases={a.phases} tests={a.tests} temps={temps()}")
    try:
        for ph in a.phases:
            for v in a.values:
                if ph == "stopped":
                    a2_stop()
                    set_arid(v)
                else:
                    tries = a2_start(v)
                    log(f"arid reg {ssh(f'devmem {REG} 32').strip()}")
                key = f"{ph}/{v}"
                res[key] = {t: [] for t in a.tests}
                for _ in range(a.reps):
                    for t in a.tests:
                        try:
                            res[key][t].append(round(TESTS[t](), 2))
                        except Exception as e:  # noqa: BLE001
                            raise RuntimeError(f"{key} {t} rep {len(res[key][t]) + 1}: {type(e).__name__} {e}") from e
                tmp = temps()
                alive = a2_alive() if ph == "running" else None
                log(f"{key} " + " ".join(f"{t}={statistics.median(x):.2f} {x}" for t, x in res[key].items()) + f" temps={tmp} a2_alive={alive}" + (f" start_attempts={tries}" if ph == "running" else ""))
                if alive is False:
                    raise RuntimeError("a2 shell dead after the tests")
    except Exception as e:  # noqa: BLE001 - report, leave the board for the operator
        log(f"FAIL: {e}")
        JSON.write_text(json.dumps(res, indent=1))
        return 1
    finally:
        try:
            a2_stop()
            set_arid(3)
        except Exception as e:  # noqa: BLE001
            log(f"cleanup failed: {e}")
    JSON.write_text(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
