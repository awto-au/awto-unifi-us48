#!/usr/bin/env python3
"""Run tools/bde_trace.c on stock: push over ssh stdin, trace a restarted switchdrvr to ready, pull the log.

Usage: python3 scripts/bde_trace_run.py [--host IP] [--out tmp/bde_stock_init.log]
Steps: pidof switchdrvr -> push tmp/bde_trace -> start tracer (-n switchdrvr -x OLD, -R ready files)
       -> `pm app-restart switchdrvr` -> wait for tracer exit -> pull /tmp/bde.log. Never writes flash.
Log: tmp/logs/bde_trace_run.log
"""
import argparse
import os
import pathlib
import subprocess
import sys
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
LOG = REPO / "tmp/logs/bde_trace_run.log"
READY = "/var/run/fastpath/switchdrvr_ready,/var/run/fastpath/.poe_ready"
# switchdrvr_ready measured 213 s after reset (dev.py STOCK_READY_S); tracing slows SDK init, 3x = 640 s backstop.
RUN_LIMIT_S = 1500  # bde_trace: ready in 640 s worst case; mmio_trace with the CMC0 page write-trapped is slower
UDP_PORT = 9777            # mmio_trace log stream (host side listener)
HOST_IP = "192.168.25.30"  # this host as seen from the switch


def log(msg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}\n")
    print(msg, flush=True)


def ssh(host, cmd, stdin=None, out=None, timeout=30):
    argv = ["sshpass", "-e", "ssh", "-T", "-o", "ControlMaster=no", "-o", "ControlPath=none",
            "-o", "RequiredRSASize=1024", "-o", "HostKeyAlgorithms=+ssh-rsa",
            "-o", f"UserKnownHostsFile={REPO / 'private/known_hosts'}", "-o", "PubkeyAuthentication=no",
            "-o", "ConnectTimeout=5", f"ubnt@{host}", cmd]
    return subprocess.run(argv, env=dict(os.environ, SSHPASS=os.environ.get("SSHPASS", "ubnt")),
                          stdin=stdin or subprocess.DEVNULL, stdout=out or subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default=os.environ.get("US48_SWITCH_IP", "192.168.25.206"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--tool", default="bde_trace", choices=["bde_trace", "mmio_trace"], help="mmio_trace = direct CMIC MMIO tracer (#46 option B)")
    ap.add_argument("--extra", default="", help="extra tracer args, e.g. '-k 0x31000,0x32000'")
    a = ap.parse_args()
    tool = a.tool
    a.out = a.out or str(REPO / ("tmp/bde_stock_init.log" if tool == "bde_trace" else "tmp/mmio_stock_init.log"))
    old = ssh(a.host, "pidof switchdrvr").stdout.decode().split()
    old_pid = old[0] if old else "0"
    log(f"old switchdrvr pid {old_pid}")
    with open(REPO / "tmp" / tool, "rb") as f:
        r = ssh(a.host, f"cat > /tmp/{tool} && chmod +x /tmp/{tool}", stdin=f, timeout=60)
    log(f"push rc={r.returncode} {r.stderr.decode().strip()}")
    cmd = (f"rm -f /tmp/bde.log /var/run/fastpath/switchdrvr_ready /var/run/fastpath/.poe_ready; "
           f"(/tmp/{tool} -n switchdrvr -x {old_pid} -o /tmp/bde.log -R {READY} {a.extra} >/tmp/bde.err 2>&1 &) ; "
           f"pm app-restart switchdrvr")
    try:
        r = ssh(a.host, cmd, timeout=10 if tool == "mmio_trace" else 30)   # a traced restart can outlast the ssh; the tracer is detached anyway
        log(f"restart rc={r.returncode} {r.stdout.decode().strip()} {r.stderr.decode().strip()}")
    except subprocess.TimeoutExpired:
        log("restart ssh timed out (expected under mmio_trace)")
    t0 = time.monotonic()
    if tool == "mmio_trace":
        # No tracer shutdown (detach reboots the switch). The switch's own network is the chip: ssh is gone until switchdrvr is up,
        # so the log is pulled once switchdrvr_ready exists (+20 s for late CMIC traffic); then power-cycle the board.
        done_at = None
        while time.monotonic() - t0 < RUN_LIMIT_S:
            time.sleep(5)  # poll period: switchdrvr init under trace takes minutes
            try:
                r = ssh(a.host, "test -e /var/run/fastpath/switchdrvr_ready && echo READY", timeout=15)
            except subprocess.TimeoutExpired:
                continue
            ready = b"READY" in r.stdout
            log(f"t={time.monotonic() - t0:.0f}s ready={ready} ssh_rc={r.returncode} ")
            if ready and done_at is None:
                done_at = time.monotonic()
            if done_at and time.monotonic() - done_at > 20:
                break
    else:
      while time.monotonic() - t0 < RUN_LIMIT_S:
        time.sleep(5)  # poll period: tracer exit is human-scale (minutes)
        try:
            r = ssh(a.host, f"pidof {tool}", timeout=15)
        except subprocess.TimeoutExpired:
            log("poll timeout")
            continue
        alive = bool(r.stdout.decode().split())
        log(f"t={time.monotonic() - t0:.0f}s tracer alive={alive}")
        if not alive:
            break
      else:
        log(f"limit {RUN_LIMIT_S}s hit: interrupting tracer")
        ssh(a.host, f"kill -INT $(pidof {tool})")
        time.sleep(3)
    with open(a.out, "wb") as f:    # the tracer keeps running (mmio_trace has no safe shutdown): log file is line-buffered
        r = ssh(a.host, "cat /tmp/bde.log", out=f, timeout=120)
    log(f"pulled {a.out} rc={r.returncode} {pathlib.Path(a.out).stat().st_size} bytes")
    err = ssh(a.host, "cat /tmp/bde.err").stdout.decode()
    log(f"tracer stderr: {err.strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
