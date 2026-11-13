#!/usr/bin/env python3
"""tftp_serve — read-only TFTP server for U-Boot `tftpboot`, no root (high port), serves tmp/tftp.

Usage: scripts/tftp_serve.py [--port 6969] [--dir tmp/tftp]   (U-Boot: setenv tftpdstp 6969)
- RFC 1350 RRQ + blksize/tsize options; every transfer on its own socket. Log: tmp/logs/tftp_serve.log
- Retransmit after 1 s (LAN RTT is ~1 ms), give up after 8 tries.
"""
import argparse
import logging
import os
import socket
import struct
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RETRY_S, TRIES = 1.0, 8


def transfer(addr, path: Path, opts: dict, log) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(RETRY_S)
    blk = min(int(opts.get("blksize", 512)), 1468)
    data = path.read_bytes()
    oack = b""
    if "blksize" in opts:
        oack += b"blksize\0" + str(blk).encode() + b"\0"
    if "tsize" in opts:
        oack += b"tsize\0" + str(len(data)).encode() + b"\0"

    def send_wait(pkt: bytes, want_block: int) -> bool:
        for _ in range(TRIES):
            s.sendto(pkt, addr)
            try:
                while True:
                    r, a = s.recvfrom(64)
                    if a == addr and struct.unpack("!HH", r[:4]) == (4, want_block):
                        return True
            except socket.timeout:
                continue
        return False

    if oack and not send_wait(struct.pack("!H", 6) + oack, 0):
        log.warning("%s: no ACK for OACK", addr)
        return
    n = 0
    for off in range(0, len(data) + 1, blk):
        n += 1
        if not send_wait(struct.pack("!HH", 3, n & 0xFFFF) + data[off:off + blk], n & 0xFFFF):
            log.warning("%s: block %d lost after %d tries", addr, n, TRIES)
            return
    log.info("%s: sent %s (%d B, %d blocks)", addr, path.name, len(data), n)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=6969)
    ap.add_argument("--bind", default="0.0.0.0", help="host address to listen on (dev.py passes the one that reaches the board)")
    ap.add_argument("--dir", type=Path, default=ROOT / "tmp/tftp")
    a = ap.parse_args()
    (ROOT / "tmp/logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.FileHandler(ROOT / "tmp/logs/tftp_serve.log"), logging.StreamHandler()])
    log = logging.getLogger("tftp")
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind((a.bind, a.port))
    log.info("serving %s on udp/%d", a.dir, a.port)
    while True:
        pkt, addr = srv.recvfrom(1024)
        if struct.unpack("!H", pkt[:2])[0] != 1:
            continue
        parts = pkt[2:].split(b"\0")
        name = parts[0].decode(errors="replace")
        opts = {parts[i].decode().lower(): parts[i + 1].decode() for i in range(2, len(parts) - 1, 2)}
        path = (a.dir / name).resolve()
        if a.dir.resolve() not in path.parents or not path.is_file():
            srv.sendto(struct.pack("!HH", 5, 1) + b"not found\0", addr)
            log.warning("%s: refused %r", addr, name)
            continue
        log.info("%s: RRQ %s opts=%s", addr, name, opts)
        threading.Thread(target=transfer, args=(addr, path, opts, log), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
