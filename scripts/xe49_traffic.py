#!/usr/bin/env python3
"""xe49_traffic: ping + TCP both ways over a US-48 xe port <-> sw1 lan port, with sw1 port counters (#78, #87).

Usage: scripts/xe49_traffic.py [--xe xe49] [--lan lan7] [--ip 192.168.25.209] [--peer 192.168.25.30] [--mib 10,100]
- --ip is the registry address us48-xe49 (awto-dan docs/network/ip-registry.toml); runtime only (ip addr add), removed in finally.
- Board: arp_ignore=1 on all ifaces (so only the xe port answers for its address; ge1 shares the /24), host routes to
  sw1 .48 and the peer via the xe port; both restored/removed in finally.
- Peer = this host (default Selwyn .30, 10G on the sw1 lan5 side). TCP timing is taken on this host (accurate clock):
  board->host = first byte to EOF at the receiver; host->board = connect to the board's close after shutdown(WR).
- Before the run the sw1 FDB must show the xe MAC on --lan (reported, else abort).
- Stall guard: 15 s without data on a socket = fail (10 MiB at the slowest plausible 1 MB/s is 10 s).
- Log: tmp/logs/xe49_traffic.log
"""
import argparse
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hwdb  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/xe49_traffic.log"
SW1 = "root@192.168.25.48"
BOARD = "root@192.168.25.207"
SW1_IP = "192.168.25.48"
PORT = 5201
STALL_S = 15.0
_DB = hwdb.lite()
MAC = {p["linux"]: hwdb.mac(p["front"]) for p in _DB.ports if "sw1_peer" in p and "linux" in p}
SW1_PEER = {p["linux"]: p["sw1_peer"] for p in _DB.ports if "sw1_peer" in p and "linux" in p}
SSH = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", "-o", "StrictHostKeyChecking=no",
       "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR"]


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def ssh(host: str, cmd: str, call_s: float = 20.0) -> str:
    try:
        r = subprocess.run(SSH + [host, cmd], capture_output=True, text=True, timeout=call_s)
    except subprocess.TimeoutExpired:
        return f"ssh {host} exceeded {call_s} s"
    return (r.stdout + r.stderr).strip()


def counters(lan: str, xe: str) -> str:
    sw = ssh(SW1, f"ip -s link show {lan}")
    nums = [ln.split() for ln in sw.splitlines()]
    rx = tx = "?"
    for i, ln in enumerate(sw.splitlines()):
        if ln.strip().startswith("RX:") and i + 1 < len(nums):
            rx = " ".join(nums[i + 1])
        if ln.strip().startswith("TX:") and i + 1 < len(nums):
            tx = " ".join(nums[i + 1])
    st = ssh(BOARD, f"cd /sys/class/net/{xe}/statistics && for f in rx_packets rx_errors rx_dropped tx_packets tx_errors tx_dropped; do echo -n $f=$(cat $f)' '; done")
    return f"sw1 {lan} RX[bytes pkts errs drop miss mcast]={rx} TX[bytes pkts errs drop carrier coll]={tx} | board {xe} {st}"


def tcp_board_to_host(ip: str, mib: int) -> str:
    """Board dd | nc -> this host; receiver times first byte -> EOF."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((ip_local(), PORT))
    srv.listen(1)
    srv.settimeout(STALL_S)
    res = {}

    def send() -> None:
        res["board"] = ssh(BOARD, f"dd if=/dev/zero bs=1048576 count={mib} 2>/dev/null | nc -s {ip} {ip_local()} {PORT}", mib * 2 + 20)
    th = threading.Thread(target=send)
    th.start()
    try:
        c, _ = srv.accept()
    except socket.timeout:
        srv.close()
        th.join()
        return f"FAIL: no connection from the board within {STALL_S} s ({res})"
    c.settimeout(STALL_S)
    n = 0
    t0 = None
    try:
        while True:
            b = c.recv(1 << 20)
            if not b:
                break
            if t0 is None:
                t0 = time.monotonic()
            n += len(b)
    except socket.timeout:
        return f"FAIL: stalled after {n} bytes"
    finally:
        t1 = time.monotonic()
        c.close()
        srv.close()
        th.join()
    dt = max(t1 - (t0 or t1), 1e-6)
    return f"board->host {mib} MiB: got {n} B in {dt:.3f} s = {n / dt / 1e6:.1f} MB/s ({n * 8 / dt / 1e9:.2f} Gb/s)" + ("" if n == mib << 20 else "  *** SHORT")


def tcp_host_to_board(ip: str, mib: int) -> str:
    """Board nc -l | wc -c; this host connects, sends, shutdown(WR), waits for the board to close."""
    res = {}

    def listen() -> None:
        res["board"] = ssh(BOARD, f"nc -l -s {ip} -p {PORT} | wc -c", mib * 2 + 20)
    th = threading.Thread(target=listen)
    th.start()
    s = None
    t_end = time.monotonic() + STALL_S
    while time.monotonic() < t_end:
        try:
            s = socket.create_connection((ip, PORT), timeout=1.0)
            break
        except OSError:
            time.sleep(0.2)
    if s is None:
        th.join()
        return f"FAIL: could not connect to the board nc ({res})"
    s.settimeout(STALL_S)
    buf = b"\0" * (1 << 20)
    t0 = time.monotonic()
    try:
        for _ in range(mib):
            s.sendall(buf)
        s.shutdown(socket.SHUT_WR)
        while s.recv(4096):
            pass
    except (socket.timeout, OSError) as e:
        return f"FAIL: {e}"
    finally:
        t1 = time.monotonic()
        s.close()
        th.join()
    dt = t1 - t0
    got = re.search(r"\d+", res.get("board", ""))
    n = int(got.group()) if got else -1
    return f"host->board {mib} MiB: board wc -c = {n} in {dt:.3f} s = {mib * 1048576 / dt / 1e6:.1f} MB/s ({mib * 1048576 * 8 / dt / 1e9:.2f} Gb/s)" + ("" if n == mib << 20 else "  *** SHORT")


_LOCAL = ""


def ip_local() -> str:
    return _LOCAL


def main() -> int:
    global _LOCAL
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--xe", default="xe49")
    ap.add_argument("--lan", default=SW1_PEER["xe49"])
    ap.add_argument("--ip", default="192.168.25.209")
    ap.add_argument("--peer", default="192.168.25.30", help="this host's address on the house LAN")
    ap.add_argument("--mib", default="10,100")
    a = ap.parse_args()
    if a.lan in ("lan5", "lan8"):
        sys.exit("refusing lan5/lan8")
    _LOCAL = a.peer
    sizes = [int(x) for x in a.mib.split(",")]
    old = ssh(BOARD, "cat /proc/sys/net/ipv4/conf/all/arp_ignore").strip()
    log(f"board arp_ignore(all) was {old}; {counters(a.lan, a.xe)}")
    try:
        log("setup: " + ssh(BOARD, f"for i in all default ge1 {a.xe}; do echo 1 > /proc/sys/net/ipv4/conf/$i/arp_ignore; done; "
                                   f"ip addr add {a.ip}/24 dev {a.xe}; ip route add {SW1_IP}/32 dev {a.xe}; echo ok"))
        log("ping board->sw1: " + ssh(BOARD, f"ping -c 5 -W 2 -I {a.xe} {SW1_IP} | tail -2", 20))
        if "0 packets received" in ssh(BOARD, f"ping -c 2 -W 2 -I {a.xe} {SW1_IP} | tail -2", 12):
            log("*** xe port does not transmit: abort before the peer host route (it would black-hole ssh from the peer)")
            return 2
        ssh(BOARD, f"ip route add {a.peer}/32 dev {a.xe}")
        log("ping board->host: " + ssh(BOARD, f"ping -c 5 -W 2 -I {a.xe} {a.peer} | tail -2", 20))
        r = subprocess.run(["ping", "-c", "5", "-W", "2", a.ip], capture_output=True, text=True, timeout=20)
        log("ping host->board: " + " | ".join(r.stdout.strip().splitlines()[-2:]))
        fdb = ssh(SW1, f"bridge fdb show | grep -i {MAC[a.xe]}")
        log(f"sw1 fdb: {fdb}")
        if f"dev {a.lan} " not in fdb:
            log(f"*** xe MAC not learned on {a.lan}: abort")
            return 2
        for mib in sizes:
            for fn in (tcp_board_to_host, tcp_host_to_board):
                log(fn(a.ip, mib))
        log("after: " + counters(a.lan, a.xe))
    finally:
        log("cleanup: " + ssh(BOARD, f"ip route del {SW1_IP}/32 dev {a.xe}; ip route del {a.peer}/32 dev {a.xe}; ip addr del {a.ip}/24 dev {a.xe}; "
                                     f"for i in all default ge1 {a.xe}; do echo {old or 0} > /proc/sys/net/ipv4/conf/$i/arp_ignore; done; "
                                     f"ip addr show {a.xe} | grep -c inet"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
