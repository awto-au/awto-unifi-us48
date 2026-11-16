#!/usr/bin/env python3
"""u2_warm_prompt — `reboot` over ssh (no relay), catch the awto-uboot 1 s autoboot window, stay at the prompt (#98).

Usage: u2_warm_prompt.py      Log: tmp/logs/u2_warm_prompt.log
Same window rule as u2_run.catch_window: a space every 20 ms from the banner until the prompt.
"""
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import console  # noqa: E402
import u2_run  # noqa: E402
from _cli import help_guard  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/u2_warm_prompt.log"


def main() -> int:
    s = console.Session()
    s.buf = b""
    # ssh returns when dropbear dies in the shutdown; 15 s = shutdown ~2 s x 7, expiry = still go on and watch the console
    subprocess.run([sys.executable, str(ROOT / "scripts/lx.py"), "reboot"], capture_output=True, timeout=15)
    t0 = time.monotonic()
    ok = False
    while time.monotonic() - t0 < u2_run.WINDOW_S:
        s._pump(0.02)
        tail = s.buf[-3000:]
        i = tail.rfind(b"awto-uboot 20")
        if i >= 0 and b"awto-uboot> " in tail[i:]:
            ok = True
            break
        if i >= 0:
            s.write(b" ")
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text(s.buf.decode("latin1", "replace"))
    s.close()
    print("at awto-uboot>" if ok else f"no prompt in {u2_run.WINDOW_S} s; log {LOG}")
    return 0 if ok else 1


if __name__ == "__main__":
    help_guard(__doc__, strict=True, known=())
    sys.exit(main())
