#!/usr/bin/env python3
"""Send lines to stock's FastPath CLI (127.0.0.1:23 on the switch) through an ssh tunnel; print the replies.

Usage: python3 scripts/stock_cli.py [--host IP] [--idle S] 'line' ['line' ...]
Logs to tmp/logs/stock_cli.log. Telnet options are all refused (IAC DONT/WONT).
"""
import argparse
import logging
import os
import pathlib
import socket
import subprocess
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
LOG = REPO / "tmp/logs/stock_cli.log"
LPORT = 12323
IAC, DONT, DO, WONT, WILL = 255, 254, 253, 252, 251


def read_until_idle(sock, idle):
    """Collect bytes until `idle` s of silence (CLI prints a prompt then waits); answer telnet options."""
    buf = b""
    sock.settimeout(idle)
    while True:
        try:
            d = sock.recv(4096)
        except socket.timeout:
            return buf
        if not d:
            return buf
        out, i = b"", 0
        while i < len(d):
            if d[i] == IAC and i + 2 < len(d):
                sock.sendall(bytes([IAC, WONT if d[i + 1] in (DO, DONT) else DONT, d[i + 2]]))
                i += 3
            else:
                out += d[i:i + 1]
                i += 1
        buf += out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("lines", nargs="+")
    ap.add_argument("--host", default=os.environ.get("US48_SWITCH_IP", "192.168.25.206"))
    ap.add_argument("--idle", type=float, default=3.0, help="silence that ends a reply (CLI answers in <1 s)")
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=LOG, level=logging.INFO, format="%(asctime)s %(message)s")
    ssh = subprocess.Popen(
        ["sshpass", "-e", "ssh", "-N", "-o", "ControlMaster=no", "-o", "ControlPath=none",
         "-o", "RequiredRSASize=1024", "-o", "HostKeyAlgorithms=+ssh-rsa",
         "-o", f"UserKnownHostsFile={REPO / 'private/known_hosts'}", "-o", "PubkeyAuthentication=no",
         "-o", "ConnectTimeout=5", "-L", f"{LPORT}:127.0.0.1:23", f"ubnt@{a.host}"],
        env=dict(os.environ, SSHPASS=os.environ.get("SSHPASS", "ubnt")))
    try:
        sock = None
        for _ in range(20):  # tunnel up within ~1 s; 20 x 0.25 s = 5 s = ConnectTimeout
            try:
                sock = socket.create_connection(("127.0.0.1", LPORT), 1)
                break
            except OSError:
                time.sleep(0.25)
        if sock is None:
            raise SystemExit("tunnel did not come up in 5 s")
        for ln in [None] + a.lines:
            if ln is not None:
                sock.sendall(ln.encode() + b"\r\n")
            r = read_until_idle(sock, a.idle).decode(errors="replace")
            logging.info("> %s\n%s", ln, r)
            print(f"> {ln}\n{r}")
    finally:
        ssh.terminate()


if __name__ == "__main__":
    main()
