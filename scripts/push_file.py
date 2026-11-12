#!/usr/bin/python3
"""push_file — copy a local file to the US-48 over the serial console (no network needed), md5-verified.

Usage: scripts/push_file.py <local> <remote> [--chunk 256]
- Sends base64 in short `echo` lines (the console cuts lines over ~400 chars), decodes with busybox base64.
- Needs a shell prompt on the console (OpenWrt or stock). Log: tmp/logs/push_file.log
"""
import argparse
import base64
import hashlib
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import console  # noqa: E402

LOG = Path(__file__).resolve().parent.parent / "tmp/logs/push_file.log"
# 20 chunk lines per marker; each line echoes back in ~25 ms at 115200 (256 B). 10 s = 20x backstop per batch.
BATCH = 20
BATCH_BUDGET_S = 10.0


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def run(s, cmd: str, budget: float) -> str:
    """Send cmd plus an exit marker; return the output before the marker."""
    s.buf = b""
    s.send_line(f'{cmd}; echo __p""f=$?__')
    t0 = time.monotonic()
    while not re.search(rb"__pf=(\d+)__", s.buf):
        if time.monotonic() - t0 > budget:
            raise RuntimeError(f"no marker after {budget} s for {cmd[:40]!r}")
        s._pump(0.1)
    return s.buf.decode(errors="replace")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("local", type=Path)
    ap.add_argument("remote")
    ap.add_argument("--chunk", type=int, default=256)
    a = ap.parse_args()
    data = a.local.read_bytes()
    want = hashlib.md5(data).hexdigest()
    b64 = base64.b64encode(data).decode()
    chunks = [b64[i:i + a.chunk] for i in range(0, len(b64), a.chunk)]
    tmp = a.remote + ".b64"
    log(f"{a.local.name}: {len(data)} B, {len(chunks)} chunks -> {a.remote}")
    t0 = time.monotonic()
    with console.Session() as s:
        run(s, f"rm -f {tmp}", BATCH_BUDGET_S)
        for i in range(0, len(chunks), BATCH):
            for c in chunks[i:i + BATCH]:
                s.send_line(f"echo -n {c} >> {tmp}")
            run(s, "true", BATCH_BUDGET_S)
        out = run(s, f"base64 -d {tmp} > {a.remote} && rm -f {tmp} && md5sum {a.remote}", BATCH_BUDGET_S)
    got = re.search(r"\b([0-9a-f]{32})\s+" + re.escape(a.remote), out)
    if not got or got.group(1) != want:
        log(f"FAILED md5: want {want}, got {got.group(1) if got else 'none'}")
        return 1
    log(f"ok md5 {want} in {time.monotonic() - t0:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
