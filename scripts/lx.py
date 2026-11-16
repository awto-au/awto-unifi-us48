#!/usr/bin/env python3
"""lx — run a command / copy a file on the mainline Linux image of the board (dropbear, key auth, root@us48-boot).

Usage: lx.py "cmd"            run on the board, stream output, rc = remote rc
       lx.py --put LOCAL REMOTE   scp -O
Host key is ignored: dropbear regenerates it on every boot (initramfs rootfs). Log: tmp/logs/lx.log.
"""
import subprocess
import sys
import time
from pathlib import Path
from _cli import help_guard  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/lx.log"
HOST = "192.168.25.207"	# ip-registry us48-boot
# connect 8 s: dropbear banner took >3 s once on the debug kernel; expiry = ssh rc 255
OPTS = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=8"]


def main() -> int:
    a = sys.argv[1:]
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if a[:1] == ["--put"] and len(a) == 3:
        cmd = ["scp", "-O", *OPTS, a[1], f"root@{HOST}:{a[2]}"]
    elif len(a) == 1:
        cmd = ["ssh", *OPTS, f"root@{HOST}", a[0]]
    else:
        print(__doc__)
        return 2
    r = subprocess.run(cmd, capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    with LOG.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {a} rc={r.returncode}\n{r.stdout}{r.stderr}")
    return r.returncode


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
