#!/usr/bin/env python3
"""Push a file to the OpenWrt shell over the serial console (base64 + busybox base64 -d), verify sha256.

Usage: console_push.py LOCAL REMOTE      e.g. kernel/us48-cmic/us48_cmic.ko /tmp/us48_cmic.ko
Serial 115200 8N1 = ~11.5 kB/s; base64 inflates by 4/3, 512-byte lines: 106 kB takes ~15 s. Stall guard: a chunk that
does not return the shell prompt echo within 5 s (>10x one line's wire time) aborts. Log: tmp/logs/console_push.log
"""
import base64
import hashlib
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import console  # noqa: E402

LOG = pathlib.Path(__file__).resolve().parent.parent / "tmp/logs/console_push.log"
LINE = 288          # raw bytes per line -> 384 base64 chars
LINES_PER_BATCH = 2     # ~0.8 kB per shell line: busybox ash line editing breaks on longer lines


def log(msg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    print(msg, flush=True)
    with LOG.open("a") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%S%z ") + msg + "\n")


def main():
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    data = pathlib.Path(sys.argv[1]).read_bytes()
    remote = sys.argv[2]
    sha = hashlib.sha256(data).hexdigest()
    lines = [base64.b64encode(data[i:i + LINE]).decode() for i in range(0, len(data), LINE)]
    t0 = time.monotonic()
    with console.Session() as s:
        s.write(b"\x03\r\n")
        s.read_idle(0.5, 2.0)
        s.send_line(f"rm -f {remote}.b64; echo START")
        s.expect([b"START\r\n"], 5.0, "shell")
        for b in range(0, len(lines), LINES_PER_BATCH):
            chunk = lines[b:b + LINES_PER_BATCH]
            s.send_line("printf '%s\\n' " + " ".join(f"'{x}'" for x in chunk) + f" >> {remote}.b64; echo B{b}")
            s.expect([f"B{b}\r\n".encode()], 5.0, f"batch {b}")
        s.send_line(f"base64 -d {remote}.b64 > {remote}; sha256sum {remote}")
        out = s.expect([sha.encode()], 10.0, "sha256 match")
    log(f"pushed {sys.argv[1]} -> {remote}: {len(data)} B, sha256 {sha[:12]} verified in {time.monotonic() - t0:.0f} s")


if __name__ == "__main__":
    main()
