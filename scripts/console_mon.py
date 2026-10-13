#!/usr/bin/env python3
"""console_mon — runs INSIDE the one US-48 console window; keeps tio alive there.

- Starts tio on the window's tty (tio -S needs a real tty), socket tmp/tio.us48.sock.
- Baud change (console.restart): writes tmp/tio.us48.baud + tmp/tio.us48.respawn, SIGTERMs tio;
  this loop sees the respawn flag and starts a new tio at the new baud in the SAME window.
- tio exiting without the flag (user typed ctrl-t q, adapter gone) ends the window.
"""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import console  # noqa: E402


def main() -> int:
    while True:
        baud = console.read_baud()
        if console.SOCK.exists():
            console.SOCK.unlink()
        print(f"\n[console_mon] tio {console.TTY} @ {baud}\n", flush=True)
        rc = subprocess.call(console.tio_argv(baud))
        if console.RESPAWN.exists():
            console.RESPAWN.unlink()
            continue
        print(f"[console_mon] tio exited rc={rc}; window closing", flush=True)
        return rc


if __name__ == "__main__":
    os.chdir(console.ROOT)
    sys.exit(main())
