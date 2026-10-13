"""watch — continuous US-48 state from serial lines + LAN, event-driven (no fixed waits).

- Serial: second client on the tio socket (tmp/tio.us48.sock); every line classified → board state.
- LAN: every 0.1 s read the neighbour table for the switch MAC (any IP) + ping its last IP.
- On every state/IP change: one line to tmp/logs/us48-watch.log (and stdout) + tmp/us48-status.json.
- Readers (dev.py status, other commands, Monitor) react to changes instead of sleeping.
"""
from __future__ import annotations

import json
import re
import socket
import subprocess
import time
from pathlib import Path

import console

ROOT = console.ROOT
STATUS = ROOT / "tmp/us48-status.json"
LOG = ROOT / "tmp/logs/us48-watch.log"
MAC = "80:2a:a8:9c:da:d8"
POLL_S = 0.1          # neighbour-table poll: cheap (/proc read), catches the MAC within 100 ms of any ARP
PING_EVERY_S = 1.0    # ICMP to the last-known IP; 1 s keeps it off the switch CPU's back
# Serial silence after a halt/panic line = board needs power; silence otherwise is just idle.

# (regex on one console line, state). First match wins; order = most specific first.
RULES = [
    (rb"Reboot failed -- System halted", "halted"),
    (rb"Kernel panic", "panic"),
    (rb"U-Boot usw", "uboot-banner"),
    (rb"Hit any key to stop autoboot", "uboot-countdown"),
    (rb"u-boot> ", "uboot-prompt"),
    (rb"Ready for binary \(ymodem\)", "uboot-loady"),
    (rb"Verifying 'kernel1'", "stock-loading"),
    (rb"Linux version 3\.6\.5", "stock-kernel"),
    (rb"Linux version 6\.|Linux version 7\.", "openwrt-kernel"),
    (rb"cfgmtd", "stock-init"),
    (rb"Please press Enter to activate this console", "stock-console"),
    (rb"US-48-500W(-US)?\.[0-9.]+[#$] ", "stock-shell"),
    (rb"US-48-500W login:", "stock-login"),
    (rb"root@OpenWrt", "openwrt-shell"),
    (rb"BusyBox v1\.3[0-9]", "openwrt-userspace"),
]


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def read_status() -> dict:
    try:
        return json.loads(STATUS.read_text())
    except (OSError, ValueError):
        return {}


def _neigh_ip() -> str | None:
    try:
        out = subprocess.run(["ip", "neigh", "show"], capture_output=True, text=True, timeout=1).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if MAC in line.lower() and "FAILED" not in line and "INCOMPLETE" not in line:
            return line.split()[0]
    return None


def run() -> int:
    st = read_status() or {}
    st.setdefault("state", "unknown")
    st.setdefault("ip", "192.168.25.206")  # DHCP reservation (ip-registry us48-mgmt); neigh table overrides
    st.setdefault("ping", False)

    def save(**kv) -> None:
        changed = {k: v for k, v in kv.items() if st.get(k) != v and k != "line"}
        st.update(kv)
        if not changed:
            return  # same state, new line: keep the line, don't log
        st["since"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        st["t"] = time.time()
        STATUS.write_text(json.dumps(st))
        log(" ".join(f"{k}={v}" for k, v in changed.items()))

    sock, buf, last_ping, pinger = None, b"", 0.0, None
    # Seed from the newest console capture: the last classified line is the current state.
    caps = sorted((ROOT / "tmp/logs/console").glob("us48-2*.log"), key=lambda p: p.stat().st_mtime)
    if caps:
        for line in re.split(rb"\r?\n", caps[-1].read_bytes()[-20000:]):
            for pat, state in RULES:
                if re.search(pat, line):
                    st["state"], st["line"] = state, line[-120:].decode(errors="replace").strip()
                    break
    st["since"], st["t"] = time.strftime("%Y-%m-%dT%H:%M:%S%z"), time.time()
    STATUS.write_text(json.dumps(st))
    log(f"watch start: state={st['state']} ip={st['ip']} (seeded from console capture)")
    while True:
        if sock is None and console.SOCK.exists():
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(str(console.SOCK))
                sock.setblocking(False)
            except OSError:
                sock = None
        if sock is not None:
            try:
                chunk = sock.recv(65536)
                if not chunk:
                    sock = None  # tio respawned (baud change) — reconnect next loop
                else:
                    buf += chunk
                    *lines, buf = re.split(rb"\r?\n", buf)
                    # a prompt has no newline: classify the unterminated tail too
                    for line in lines + [buf]:
                        for pat, state in RULES:
                            if re.search(pat, line):
                                save(state=state, line=line[-120:].decode(errors="replace").strip())
                                break
            except BlockingIOError:
                pass
            except OSError:
                sock = None
        ip = _neigh_ip() or st.get("ip")
        if ip != st.get("ip"):
            save(ip=ip)
        now = time.monotonic()
        if pinger is not None and pinger.poll() is not None:
            save(ping=pinger.returncode == 0)
            pinger = None
        if ip and pinger is None and now - last_ping >= PING_EVERY_S:
            last_ping = now
            # non-blocking: serial reading must never stall on a dead host
            pinger = subprocess.Popen(["ping", "-c", "1", "-W", "1", ip],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(POLL_S)
