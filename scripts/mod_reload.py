#!/usr/bin/env python3
"""mod_reload — reload a kernel module on the board, safe even when it carries the ssh link (ge1 = us48_cmic).

Usage: mod_reload.py <module> [insmod args...] [--host H] [--limit-s N] [--no-net]
- Dependents of <module> (from /proc/modules) are removed first and loaded again after it, in order.
- Board side: one detached script (setsid nohup sh /tmp/mod_reload.sh): rmmod ...; insmod ...; netdev up + ge1 address.
  It logs to /tmp/mod_reload.log on the board; this script then waits for ssh and fetches that log.
- ssh does not return in time: falls back to the console socket (scripts/console.py), prints the board log there
  and brings the interfaces up, then retries ssh once.
- Module args: `us48_cmic` defaults to `unit0=1` (as etc/rc.d/S04us48-net); other modules none.
- Host $US48_SSH (default root@192.168.25.207), ssh BatchMode like pcie_harness.py. Log: tmp/logs/mod_reload.log.
- Exit: 0 reloaded + done marker seen, 1 failed or marker missing.
"""
import _ipreg  # noqa: E402
import argparse
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/mod_reload.log"
HOST = os.environ.get("US48_SSH", f"root@{_ipreg.ip('us48-boot')}")
BOARD_LOG = "/tmp/mod_reload.log"
BOARD_SH = "/tmp/mod_reload.sh"
IP = f"{_ipreg.ip('us48-boot')}/24"	# ip-registry us48-boot, same as mainline/rootfs/overlay/etc/rc.d/S04us48-net
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hwdb  # noqa: E402

_DB = hwdb.lite()
MGMT = _DB.mgmt_netdev()
NETDEVS = {"us48_cmic": [MGMT], "us48_cmic_xe": [p["linux"] for p in _DB.ports if p["unit"] == 1 and "linux" in p and p["front"]] + ["ge40"]}
DEFAULT_ARGS = {"us48_cmic": ["unit0=1"]}
# One ssh round trip on the LAN is < 1 s; connect 2 s = the board is rebooting/reloading, not slow.
CONNECT_S = 2
# First-run backstop (no measurement yet): rmmod + cold init of both chips + TSC download is expected in a few seconds.
# Expiry = console fallback. Elapsed time is logged for tightening.
LIMIT_S = 30.0
POLL_S = 0.5
# A single ssh call (cat the log, write the script) is a stall guard, not a measurement.
CALL_S = 10.0


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def ssh(host: str, cmd: str, stdin: str | None = None, call_s: float = CALL_S) -> tuple[int, str]:
    argv = ["ssh", "-T", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={CONNECT_S}", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=2", host, cmd]
    try:
        r = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=call_s)
    except subprocess.TimeoutExpired:
        return 255, f"ssh call exceeded {call_s} s"
    return r.returncode, r.stdout + r.stderr


def dependents(host: str, mod: str) -> list[str]:
    """Modules that use `mod` (transitively), leaves first: the order to remove them in."""
    rc, out = ssh(host, "cat /proc/modules")
    if rc:
        sys.exit(f"cannot read /proc/modules: {out.strip()}")
    users = {}
    for ln in out.splitlines():
        f = ln.split()
        if len(f) >= 4:
            users[f[0]] = [d for d in f[3].split(",") if d and d != "-"]
    if mod not in users:
        sys.exit(f"{mod} is not loaded")
    order: list[str] = []

    def visit(m: str) -> None:
        for d in users.get(m, []):
            visit(d)
        if m not in order:
            order.append(m)
    visit(mod)
    return order[:-1]


def board_script(rid: str, stop: list[str], start: list[tuple[str, list[str]]], net: bool) -> str:
    L = ["#!/bin/sh", f"exec >>{BOARD_LOG} 2>&1", "M=/lib/modules/$(uname -r)",
         f"echo \"== {rid} start $(cat /proc/uptime)\""]
    for m in stop:
        L.append(f"grep -q '^{m} ' /proc/modules && {{ rmmod {m} && echo 'rmmod {m} ok' || {{ echo 'rmmod {m} FAILED'; "
                 f"echo '== {rid} failed'; exit 1; }}; }}")
    for m, args in start:
        L.append(f"insmod $M/{m}.ko {' '.join(args)} && echo 'insmod {m} ok' || {{ echo 'insmod {m} FAILED'; "
                 f"echo '== {rid} failed'; exit 1; }}")
    if net:
        for m, _a in start:
            for nd in NETDEVS.get(m, []):
                L.append(f"[ -d /sys/class/net/{nd} ] && {{ echo 1 > /proc/sys/net/ipv6/conf/{nd}/disable_ipv6; ip link set {nd} up; }}")
                if nd == MGMT:
                    L.append(f"ip addr add {IP} dev {MGMT}")
    L.append(f"echo \"== {rid} done $(cat /proc/uptime)\"")
    return "\n".join(L) + "\n"


def wait_done(host: str, rid: str, limit_s: float) -> str | None:
    """Poll the board log until the done/failed marker of this run id; None on expiry."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < limit_s:
        rc, out = ssh(host, f"cat {BOARD_LOG}")
        if rc == 0 and (f"== {rid} done" in out or f"== {rid} failed" in out):
            log(f"ssh back + marker after {time.monotonic() - t0:.1f} s")
            return out
        time.sleep(POLL_S)
    return None


def console_fallback(rid: str, net_cmds: list[str]) -> str | None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import console  # noqa: E402
    log("ssh did not return: console socket fallback")
    try:
        with console.Session() as s:
            s.send_line("")
            s.read_idle(0.3, 2.0)
            s.send_line(f"cat {BOARD_LOG}; ip addr show")
            out = s.read_idle(0.5, 5.0).decode(errors="replace")
            log("console: " + out.replace("\r", ""))
            for c in net_cmds:
                s.send_line(c)
                s.read_idle(0.3, 2.0)
            return out
    except (OSError, ConnectionError, console.Timeout) as e:
        log(f"console fallback failed: {e}")
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("module")
    ap.add_argument("args", nargs="*", help="insmod args for the module")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--limit-s", type=float, default=LIMIT_S)
    ap.add_argument("--no-net", action="store_true", help="skip netdev up + ge1 address after the reload")
    a = ap.parse_args()
    mod = a.module.removesuffix(".ko")
    deps = dependents(a.host, mod)
    stop = deps + [mod]
    start = [(mod, a.args or DEFAULT_ARGS.get(mod, []))] + [(d, DEFAULT_ARGS.get(d, [])) for d in reversed(deps)]
    rid = uuid.uuid4().hex[:8]
    log(f"reload {mod} {' '.join(a.args)} (dependents: {deps or 'none'}) run {rid}")
    sh = board_script(rid, stop, start, not a.no_net)
    rc, out = ssh(a.host, f"cat > {BOARD_SH}", stdin=sh)
    if rc:
        log(f"cannot write {BOARD_SH}: {out.strip()}")
        return 1
    t0 = time.monotonic()
    # Detached: survives the ssh session dying when the reload removes the netdev that carries it.
    rc, out = ssh(a.host, f"N=; command -v nohup >/dev/null && N=nohup; setsid $N sh {BOARD_SH} </dev/null >/dev/null 2>&1 &")
    if rc:
        log(f"cannot start the detached reload: {out.strip()}")
        return 1
    board_log = wait_done(a.host, rid, a.limit_s)
    if board_log is None:
        net_cmds = [f"ip link set {nd} up" for m, _a in start for nd in NETDEVS.get(m, [])]
        net_cmds += [f"ip addr add {IP} dev ge1"] if "us48_cmic" in [m for m, _a in start] else []
        console_fallback(rid, net_cmds)
        board_log = wait_done(a.host, rid, a.limit_s / 3)
    log(f"elapsed {time.monotonic() - t0:.1f} s")
    if board_log is None:
        log("FAILED: no done marker (ssh not back); see the console output above")
        return 1
    log("board log:\n" + "\n".join(board_log.splitlines()[-30:]))
    return 0 if f"== {rid} done" in board_log else 1


if __name__ == "__main__":
    sys.exit(main())
