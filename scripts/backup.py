#!/usr/bin/env python3
"""backup — read-only, verified full-flash backup of a UniFi US-48-500W over SSH.

- Reads each /dev/mtdNro listed in /proc/mtd (expects 7); device sha256 before + after, local sha256,
  size check. Any mismatch aborts; the partial file is left as *.partial.
- Writes private/backups/<UTC stamp>/: mtdN-<name>.bin, full-flash.bin (32 MiB, flash order), manifest.tsv,
  device-metadata.txt, live-support.tar (modules + updater), SHA256SUMS. Never overwrites a backup dir.
- Backups hold config + EEPROM identity: private, mode 0700/0600, never commit.
- SSH: password from $SSHPASS (sshpass -e), legacy RSA per-invocation, host key pinned in private/known_hosts.
- Log: tmp/logs/backup.log (+ backup.log inside the backup dir).

Usage: SSHPASS=... backup.py [--host HOST] [--user USER]
"""

import argparse
import hashlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/backup.log"
KNOWN_HOSTS = ROOT / "private/known_hosts"
PARTS = 7
FLASH_BYTES = 32 * 2**20
METADATA_CMD = ("set -e; date -u; id; uname -a; cat /etc/board.info /etc/version /proc/cmdline /proc/cpuinfo "
                "/proc/meminfo /proc/modules /proc/mounts; mca-cli-op info; dmesg")
SUPPORT_CMD = ("set -e; set -- /lib/modules /sbin/fwupdate /usr/etc/syswrapper.sh /etc/board.info /etc/version; "
               "[ -f /etc/fw_env.config ] && set -- \"$@\" /etc/fw_env.config; tar -cf - \"$@\"")
_logs: list[Path] = [LOG]


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    print(line, flush=True)
    for p in _logs:
        with p.open("a") as f:
            f.write(line + "\n")


def die(msg: str) -> None:
    log("ERROR: " + msg)
    sys.exit(1)


def ssh_base(host: str, user: str) -> list[str]:
    # ConnectTimeout 5 s: LAN host answers in ms; ServerAlive 10 s x3 bounds a stalled 15 MiB read.
    return ["sshpass", "-e", "ssh", "-n", "-T",
            "-o", "RequiredRSASize=1024", "-o", "HostKeyAlgorithms=+ssh-rsa",
            "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
            "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=5",
            "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
            "-o", "NumberOfPasswordPrompts=1", "-o", "PubkeyAuthentication=no", f"{user}@{host}"]


def ssh(base: list[str], cmd: str) -> bytes:
    r = subprocess.run(base + [cmd], capture_output=True)
    if r.returncode:
        die(f"ssh rc={r.returncode} for {cmd!r}: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout


def remote_sha(base: list[str], dev: str) -> str:
    h = ssh(base, f"sha256sum /dev/{dev}ro").decode().split()[0]
    if not re.fullmatch(r"[0-9a-f]{64}", h):
        die(f"bad device hash for {dev}: {h!r}")
    return h


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default=os.environ.get("SWITCH_HOST", "192.168.25.206"))  # DHCP reservation
    ap.add_argument("--user", default="ubnt")
    a = ap.parse_args()
    if not os.environ.get("SSHPASS"):
        sys.exit("Set SSHPASS to the switch SSH password.")
    os.umask(0o077)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    out = ROOT / "private/backups" / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.mkdir()
    _logs.append(out / "backup.log")
    base = ssh_base(a.host, a.user)
    log(f"read-only flash backup from {a.host} into {out.relative_to(ROOT)}")

    mtd = ssh(base, "cat /proc/mtd").decode()
    (out / "proc-mtd.txt").write_text(mtd)
    (out / "device-metadata.txt").write_bytes(ssh(base, METADATA_CMD))
    parts = re.findall(r'^(mtd[0-6]): ([0-9a-f]+) [0-9a-f]+ "([A-Za-z0-9_-]+)"$', mtd, re.M)
    if len(parts) != PARTS:
        die(f"expected {PARTS} partitions, parsed {len(parts)}; see proc-mtd.txt")

    rows = ["device\tname\tbytes\tsha256"]
    for dev, size_hex, name in parts:
        size = int(size_hex, 16)
        dest = out / f"{dev}-{name}.bin"
        log(f"reading {dev} ({name}) {size} bytes")
        before = remote_sha(base, dev)
        data = ssh(base, f"cat /dev/{dev}ro")
        partial = dest.with_suffix(".bin.partial")
        partial.write_bytes(data)
        after = remote_sha(base, dev)
        local = hashlib.sha256(data).hexdigest()
        if len(data) != size or not before == after == local:
            die(f"{dev}: size {len(data)}/{size} before={before} after={after} local={local}")
        partial.rename(dest)
        rows.append(f"{dev}\t{name}\t{size}\t{local}")
    (out / "manifest.tsv").write_text("\n".join(rows) + "\n")

    flash = b"".join((out / f"{d}-{n}.bin").read_bytes() for d, _, n in parts)
    if len(flash) != FLASH_BYTES:
        die(f"full-flash.bin is {len(flash)} bytes, expected {FLASH_BYTES}")
    (out / "full-flash.bin").write_bytes(flash)
    (out / "live-support.tar").write_bytes(ssh(base, SUPPORT_CMD))

    sums = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}"
            for p in sorted(out.glob("*.bin")) + [out / "live-support.tar"]]
    (out / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    log(f"verified {PARTS} partitions + full-flash.bin; backup complete: {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
