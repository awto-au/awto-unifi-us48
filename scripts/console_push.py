#!/usr/bin/env python3
"""Push a file to the OpenWrt shell over the serial console with XMODEM-CRC (busybox `rx`), verify sha256.

Usage: console_push.py LOCAL REMOTE      e.g. kernel/us48-cmic/us48_cmic.ko /tmp/us48_cmic.ko
Serial 115200 8N1 = ~11.5 kB/s wire, 1 KiB blocks + 5 B framing: a 110 kB module takes ~10 s. Per-block ACK timeout and
retries are console.py's (ACK_BUDGET_S, RETRIES); expiry raises with the block number. Log: tmp/logs/console_push.log
"""
import hashlib
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import console  # noqa: E402

LOG = pathlib.Path(__file__).resolve().parent.parent / "tmp/logs/console_push.log"


def log(msg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    print(msg, flush=True)
    with LOG.open("a") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%S%z ") + msg + "\n")


def main():
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    local, remote = pathlib.Path(sys.argv[1]), sys.argv[2]
    sha = hashlib.sha256(local.read_bytes()).hexdigest()
    t0 = time.monotonic()
    with console.Session() as s:
        s.write(b"\x03\r\n")
        s.read_idle(0.5, 2.0)
        s.send_line(f"rx {remote}")
        console.xmodem_send(s, local, lambda i, n, t: log(f"block {i}/{n} {t:.0f} s") if i == n else None)
        s.read_idle(0.5, 2.0)
        s.send_line(f"sha256sum {remote}")
        s.expect([sha.encode()], 10.0, "sha256 match")
    log(f"pushed {local} -> {remote}: sha256 {sha[:12]} verified in {time.monotonic() - t0:.0f} s")


if __name__ == "__main__":
    main()
