#!/usr/bin/env python3
"""us48_ssh — run commands on the stock US-48 over one persistent SSH master connection.

- First call opens a ControlMaster (socket tmp/us48-ssh.sock, ControlPersist 30 min); later calls reuse it.
- Password from $SSHPASS, default vendor `ubnt`. Host key pinned in private/known_hosts.
- Log: tmp/logs/us48_ssh.log (command + output).

Usage: us48_ssh.py [--host H] "cmd"     |     us48_ssh.py --close
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOCK = ROOT / "tmp/us48-ssh.sock"
LOG = ROOT / "tmp/logs/us48_ssh.log"
KNOWN = ROOT / "private/known_hosts"
CONNECT_TIMEOUT_S = 5  # LAN host answers in < 1 s; 5 s = slow-auth margin on the 3.6 kernel's dropbear


def base(host: str) -> list[str]:
    return ["ssh", "-T", "-o", "RequiredRSASize=1024", "-o", "HostKeyAlgorithms=+ssh-rsa",
            "-o", f"UserKnownHostsFile={KNOWN}", "-o", "PubkeyAuthentication=no",
            "-o", f"ConnectTimeout={CONNECT_TIMEOUT_S}", "-o", f"ControlPath={SOCK}",
            "-o", "ControlMaster=auto", "-o", "ControlPersist=30m", f"ubnt@{host}"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="192.168.25.206")
    ap.add_argument("--close", action="store_true")
    ap.add_argument("cmd", nargs="?")
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if a.close:
        return subprocess.run(["ssh", "-o", f"ControlPath={SOCK}", "-O", "exit", a.host]).returncode
    if not a.cmd:
        ap.error("cmd required")
    env = dict(os.environ, SSHPASS=os.environ.get("SSHPASS", "ubnt"))
    r = subprocess.run(["sshpass", "-e"] + base(a.host) + [a.cmd], env=env,
                       stdin=subprocess.DEVNULL, capture_output=True, text=True)
    out = r.stdout + r.stderr
    sys.stdout.write(out)
    with LOG.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} $ {a.cmd}\n{out}rc={r.returncode}\n")
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
