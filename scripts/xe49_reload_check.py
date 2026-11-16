#!/usr/bin/env python3
"""xe49_reload_check: reload us48_cmic_xe N times, check the sw1 lan port comes back and re-learns the xe MAC (#78, #87).

Usage: scripts/xe49_reload_check.py [--n 2] [--xe xe49] [--lan lan7] [--ip 192.168.25.209]
- Per reload: sw1 carrier_changes before/after + sw1 dmesg lines of the lan port, board xe carrier, ping to sw1 over the xe
  port (registry address, runtime only, removed after) so sw1 re-learns the MAC; FDB port printed from `bridge fdb show`.
- Carrier wait bound: sw1 STP forwarding takes ~5 s after link up (#87); 30 s = 6x. Expiry = FAIL.
- FDB flush first (bridge fdb del ... master) is not done: the entry must be learned fresh by the frame, so the entry is
  deleted on sw1 before the reload (dynamic entry only) to prove the re-learn.
- Log: tmp/logs/xe49_reload_check.log
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import xe49_traffic as t  # noqa: E402

ROOT = t.ROOT
LOG = ROOT / "tmp/logs/xe49_reload_check.log"
CARRIER_WAIT_S = 30.0
FWD_WAIT_S = 15.0  # STP listening+learning measured ~4.5 s after carrier; x3


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def fdb_port(mac: str) -> str:
    for ln in t.ssh(t.SW1, f"bridge fdb show | grep -i {mac}").splitlines():
        if "master" in ln and " dev lan" in ln:
            return ln.split(" dev ")[1].split()[0]
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, default=2)
    ap.add_argument("--xe", default="xe49")
    ap.add_argument("--lan", default="lan7")
    ap.add_argument("--ip", default="192.168.25.209")
    a = ap.parse_args()
    if a.lan in ("lan5", "lan8"):
        sys.exit("refusing lan5/lan8")
    mac = t.MAC[a.xe]
    bad = 0
    for i in range(1, a.n + 1):
        cc0 = t.ssh(t.SW1, f"cat /sys/class/net/{a.lan}/carrier_changes").strip()
        up0 = t.ssh(t.SW1, "cut -d. -f1 /proc/uptime").strip()
        t.ssh(t.SW1, f"bridge fdb del {mac} dev {a.lan} master")
        log(f"[{i}] before: sw1 {a.lan} carrier_changes={cc0}, fdb port={fdb_port(mac) or 'none'}")
        r = subprocess.run([sys.executable, str(ROOT / "scripts/mod_reload.py"), "us48_cmic_xe"], capture_output=True, text=True, timeout=120)
        log(f"[{i}] mod_reload rc={r.returncode}: " + " | ".join(r.stdout.strip().splitlines()[-2:]))
        t0 = time.monotonic()
        ok = False
        while time.monotonic() - t0 < CARRIER_WAIT_S:
            if t.ssh(t.BOARD, f"cat /sys/class/net/{a.xe}/carrier").strip() == "1" and t.ssh(t.SW1, f"cat /sys/class/net/{a.lan}/carrier").strip() == "1":
                ok = True
                break
            time.sleep(1.0)
        log(f"[{i}] carrier board+sw1 {'yes' if ok else 'NO'} after {time.monotonic() - t0:.1f} s (limit {CARRIER_WAIT_S} s)")
        t1 = time.monotonic()
        while time.monotonic() - t1 < FWD_WAIT_S and "state forwarding" not in t.ssh(t.SW1, f"bridge link show dev {a.lan}"):
            time.sleep(0.5)
        log(f"[{i}] sw1 {a.lan} STP forwarding after {time.monotonic() - t1:.1f} s more (limit {FWD_WAIT_S} s)")
        t.ssh(t.BOARD, f"ip link set {a.xe} up; ip addr add {a.ip}/24 dev {a.xe}; ip route add {t.SW1_IP}/32 dev {a.xe}")
        log(f"[{i}] ping: " + t.ssh(t.BOARD, f"ping -c 4 -W 2 -I {a.xe} {t.SW1_IP} | tail -2", 20))
        port = fdb_port(mac)
        t.ssh(t.BOARD, f"ip route del {t.SW1_IP}/32 dev {a.xe}; ip addr del {a.ip}/24 dev {a.xe}")
        cc1 = t.ssh(t.SW1, f"cat /sys/class/net/{a.lan}/carrier_changes").strip()
        log(f"[{i}] sw1 {a.lan} carrier_changes {cc0} -> {cc1}; fdb port={port or 'none'}; sw1 log:\n"
            + t.ssh(t.SW1, f"dmesg | awk -F'[][]' '$2+0 >= {up0}' | grep '{a.lan}' | tail -6"))
        log(f"[{i}] RESULT {'PASS' if ok and port == a.lan else 'FAIL'}")
        bad += not (ok and port == a.lan)
    log("counters: " + t.counters(a.lan, a.xe))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
