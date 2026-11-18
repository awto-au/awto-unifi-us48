#!/usr/bin/env python3
"""u2_run — power-cycle the board, catch the awto-uboot 1 s autoboot window, run commands at the prompt.

Usage: u2_run.py [--no-cycle] [--stay] "cmd" ["cmd" ...]   (reply per command printed; log tmp/logs/u2_run.log)
Spaces every 20 ms from the awto-uboot banner until the prompt (same rule as dev.py _listen).
"""
import _ipreg  # noqa: E402
import subprocess
import sys
import time
from pathlib import Path

import console
from _cli import help_guard  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RELAY = ["/mnt/2tb/git/awto-terminal/.venv/bin/python3", str(ROOT / "scripts/relay_native.py"), _ipreg.ip("so-th-1-relay")]
# banner -> prompt measured ~1.0 s (docs/autochain.md); 15 s = cold start incl. uboot-1 + DDR; expiry = report tail
WINDOW_S = 15.0
# one command: reply ends at the prompt; 20 s cap (8 MiB crc32 uncached ~ few s), expiry = return what came
CMD_CAP_S = 20.0
LOG = ROOT / "tmp/logs/u2_run.log"


def catch_window(s: console.Session, cycle: bool = True) -> bool:
    """Power-cycle (cycle=True), then space every 20 ms from the awto-uboot banner until its prompt. False on WINDOW_S expiry."""
    if not cycle:  # already at the prompt
        s.write(b"\r")
        s.buf = s.read_idle(0.3, 2.0)
        return True
    subprocess.run(RELAY + ["off"], check=True, capture_output=True)
    s.buf = b""
    subprocess.run(RELAY + ["on"], check=True, capture_output=True)
    t0 = time.monotonic()
    while time.monotonic() - t0 < WINDOW_S:
        s._pump(0.02)
        tail = s.buf[-3000:]
        i = tail.rfind(b"awto-uboot 20")
        if i >= 0 and b"awto-uboot> " in tail[i:]:
            return True
        if i >= 0 and s.backlog() == 0:  # one space in flight at most: tio reads input only while the tty is idle
            try:
                s.write(b" ")
            except console.Timeout:  # tio stalled for 1 s: this space is lost, the next one is 20 ms away (logged by console)
                pass
    return False


def main() -> int:
    a = sys.argv[1:]
    cycle = "--no-cycle" not in a
    cmds = [x for x in a if not x.startswith("--")]
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if not console.restart(console.BAUD):
        print("console window not available")
        return 1
    s = console.Session()
    out = b""
    if not catch_window(s, cycle):
        print(f"no prompt in {WINDOW_S} s; tail={s.buf[-300:]!r}")
        return 1
    out = s.buf
    s.buf = b""
    s.read_idle(0.3, 1.0)
    rep = [out.decode(errors="replace")]
    for c in cmds:
        t = time.monotonic()
        s.send_line(c)
        while time.monotonic() - t < CMD_CAP_S and not s.buf.endswith(b"awto-uboot> "):
            s._pump(0.01)
        dt = time.monotonic() - t
        r = s.buf.decode(errors="replace")
        s.buf = b""
        rep.append(f"--- {c!r} ({dt:.2f} s to prompt)\n{r}")
    text = "\n".join(rep)
    print(text)
    LOG.open("a").write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n{text}\n")
    s.close()
    return 0


if __name__ == "__main__":
    help_guard(__doc__, strict=True, known=('--no-cycle',))
    sys.exit(main())
