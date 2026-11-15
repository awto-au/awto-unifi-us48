#!/usr/bin/env python3
"""console_selftest — Session read/write against a fake tio socket, no board (#85 BlockingIOError). Log tmp/logs/console_selftest.log."""
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import console

LOG = Path(__file__).resolve().parent.parent / "tmp/logs/console_selftest.log"


def say(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.open("a").write(msg + "\n")
    print(msg)


def main() -> int:
    d = tempfile.mkdtemp(dir=LOG.parent)
    console.SOCK = Path(d) / "sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(console.SOCK))
    srv.listen(1)
    s = console.Session()
    peer, _ = srv.accept()
    fails = 0
    # 1. read with nothing pending: returns b"" after the budget, no exception, no early return
    t = time.monotonic()
    r = s._pump(0.3)
    dt = time.monotonic() - t
    ok = r == b"" and 0.28 <= dt < 0.5
    fails += not ok
    say(f"idle read: {r!r} after {dt:.2f} s {'ok' if ok else 'FAIL'}")
    # 2. data arriving mid-wait is returned at once
    threading.Timer(0.1, peer.send, (b"abc",)).start()
    t = time.monotonic()
    r = s._pump(1.0)
    dt = time.monotonic() - t
    ok = r == b"abc" and dt < 0.3
    fails += not ok
    say(f"late data: {r!r} after {dt:.2f} s {'ok' if ok else 'FAIL'}")
    # 3. backlog counts unread bytes, drops to 0 once the peer reads them
    s.write(b" ")
    b1 = s.backlog()
    peer.recv(16)
    b2 = s.backlog()
    ok = b1 > 0 and b2 == 0
    fails += not ok
    say(f"backlog: {b1} unread, {b2} after the peer read {'ok' if ok else 'FAIL'}")
    # 4. peer never reads: write fills the buffer and must raise console.Timeout, not BlockingIOError
    try:
        for _ in range(100000):
            s.write(b"x" * 4096)
        say("full buffer: no exception FAIL")
        fails += 1
    except console.Timeout as e:
        say(f"full buffer: Timeout ({e}) ok")
    except BlockingIOError:
        say("full buffer: BlockingIOError escaped FAIL")
        fails += 1
    say("selftest " + ("FAILED" if fails else "passed"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
