#!/usr/bin/env python3
"""console_mon — runs INSIDE the one US-48 console window; keeps tio alive there and captures everything.

- Starts tio on the window's tty (tio -S needs a real tty), socket tmp/tio.us48.sock.
- Capture: a second socket client writes every byte to tmp/logs/console/us48-<YYYYMMDD-HHMMSS>.log,
  flushed per chunk (tio's own --log-file is buffered and lags the screen).
- Baud change (console.restart): writes tmp/tio.us48.baud + tmp/tio.us48.respawn, SIGTERMs tio;
  this loop sees the respawn flag and starts a new tio at the new baud in the SAME window.
- tio exiting without the flag (user typed ctrl-t q, adapter gone) ends the window.
"""
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import console  # noqa: E402

CAPTURE_DIR = console.ROOT / "tmp/logs/console"


def capture(proc: subprocess.Popen, path: Path) -> None:
    # tio binds its socket within ~100 ms; poll until bound or tio exits.
    while proc.poll() is None and not console.SOCK.exists():
        time.sleep(console.POLL_S)
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(str(console.SOCK))
    except OSError as e:
        console.log(f"capture: cannot connect: {e}")
        return
    with path.open("ab") as f:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            f.write(chunk)
            f.flush()
    s.close()


def main() -> int:
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = CAPTURE_DIR / f"us48-{time.strftime('%Y%m%d-%H%M%S')}.log"
    while True:
        baud = console.read_baud()
        if console.SOCK.exists():
            console.SOCK.unlink()
        print(f"\n[console_mon] tio {console.TTY} @ {baud}  capture → {path.relative_to(console.ROOT)}\n",
              flush=True)
        with path.open("ab") as f:
            f.write(f"\n=== {time.strftime('%Y-%m-%dT%H:%M:%S%z')} tio @ {baud} ===\n".encode())
        proc = subprocess.Popen(console.tio_argv(baud))
        threading.Thread(target=capture, args=(proc, path), daemon=True).start()
        rc = proc.wait()
        if console.RESPAWN.exists():
            console.RESPAWN.unlink()
            continue
        print(f"[console_mon] tio exited rc={rc}; window closing", flush=True)
        return rc


if __name__ == "__main__":
    os.chdir(console.ROOT)
    sys.exit(main())
