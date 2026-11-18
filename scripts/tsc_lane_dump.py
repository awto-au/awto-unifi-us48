#!/usr/bin/env python3
"""tsc_lane_dump — read-only TSC lane + XLPORT/XLMAC dump of unit 1 xe49 (lane 0) / xe50 (lane 2), ours vs stock (#78).

Usage:
  tsc_lane_dump.py ours  [--out tmp/tsc_dump/ours.txt]     our Linux (tscreg + tsc module params, via lx.py)
  tsc_lane_dump.py stock [--out tmp/tsc_dump/stock.txt] [--host IP] --base 0xPHYS
                                                           stock Linux: pushes tmp/tsc_read (build_bde_trace.py --tscread), MIIM + S-channel reads
  tsc_lane_dump.py diff A.txt B.txt                        registers that differ (same key = front/aer/reg)
Output lines: "<front> <aer> <reg-hex> <val-hex>" and "mac <front> <name> <val>". Log: tmp/logs/tsc_lane_dump.log
Bounds: ours = one ssh per front (~1 s), stock = one ssh per front (~100 MIIM reads x ~30 us + ssh); 30 s ssh limit each.
"""
import _ipreg  # noqa: E402
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from _cli import help_guard  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/tsc_lane_dump.log"
FRONTS = {49: (2, 0), 50: (4, 2)}  # front -> (MIIM A, lane)
# (lo, hi, aer): aer "lane" = the port's lane, 0 = core. devad 1 = 0x1xxxx. Ranges <= 64 (tscreg limit).
RANGES = [(0xa000, 0xa03f, "lane"), (0xa040, 0xa07f, "lane"), (0xa080, 0xa0bf, "lane"),
          (0x9000, 0x903f, 0), (0x9000, 0x903f, "lane"), (0x9100, 0x913f, "lane"),
          (0x9200, 0x923f, "lane"), (0x9240, 0x927f, "lane"),
          (0xc000, 0xc03f, "lane"), (0xc040, 0xc07f, "lane"), (0xc100, 0xc13f, "lane"), (0xc140, 0xc17f, "lane"),
          (0xc180, 0xc1bf, "lane"), (0xc1c0, 0xc1ff, "lane"), (0xc200, 0xc23f, "lane"), (0xc240, 0xc27f, "lane"),
          (0xc280, 0xc2bf, "lane"), (0xc300, 0xc33f, "lane"), (0xc340, 0xc37f, "lane"),
          (0xf000, 0xf03f, 0), (0xffc0, 0xffcf, 0), (0x19200, 0x19212, "lane")]
# XLPORT/XLMAC: name, addr (lane OR-ed into the low byte unless fixed), words
MAC = [("XLMAC_CTRL", 0x60000, 2, 1), ("RX_LSS", 0x60b00, 2, 1), ("MODE", 0x60100, 2, 1), ("TX_CTRL", 0x60400, 2, 1),
       ("ENABLE", 0x2020b00, 1, 0), ("FAULT", 0x20600, 1, 1),
       ("TXFIFO_CTRL", 0x20300, 1, 1), ("XLPORT_MODE_REG", 0x2020a00, 1, 0), ("LINKSTATUS_DOWN", 0x2022900, 1, 0)]


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    print(msg, flush=True)
    with LOG.open("a") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%S%z ") + msg + "\n")


def lx(cmd: str) -> str:
    r = subprocess.run([sys.executable, str(ROOT / "scripts/lx.py"), cmd], capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise SystemExit(f"lx rc={r.returncode}: {r.stderr[-300:]}")
    return r.stdout


def ours(out: Path) -> None:
    lines = []
    sysfs = "/sys/module/us48_cmic_xe/parameters"
    for front, (_, lane) in FRONTS.items():
        cmd = ["dmesg -c >/dev/null"]
        for lo, hi, aer in RANGES:
            a = "" if aer == "lane" else f" {aer}"
            cmd.append(f"echo '{front} {lo:x} {hi:x}{a}' > {sysfs}/tscreg")
        cmd.append(f"echo 1 > {sysfs}/tsc")
        cmd.append("dmesg")
        txt = lx("; ".join(cmd))
        # core regs are listed per aer: re-key by order of the ranges issued
        blocks = re.findall(rf"tsc xe{front} range:((?: [0-9a-f]+=[0-9a-f]+)+)", txt)
        regs = [(r, v) for b in blocks for r, v in re.findall(r"([0-9a-f]+)=([0-9a-f]+)", b)]
        i = 0
        for lo, hi, aer in RANGES:
            for reg in range(lo, hi + 1):
                if i >= len(regs) or int(regs[i][0], 16) != reg:
                    raise SystemExit(f"parse: expected {reg:x} at {i}, got {regs[i] if i < len(regs) else None}")
                lines.append(f"{front} {lane if aer == 'lane' else aer} {reg:x} {regs[i][1]}")
                i += 1
        for m in re.finditer(rf"tsc xe{front} mac:(.*)", txt):
            for k, v in re.findall(r"(\w+)=([0-9a-fx]+)", m.group(1)):
                lines.append(f"mac {front} {k} {v}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    log(f"ours: {len(lines)} lines -> {out}")


def ssh(host: str, cmd: str, stdin=None, timeout: int = 30) -> subprocess.CompletedProcess:
    argv = ["sshpass", "-e", "ssh", "-T", "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "RequiredRSASize=1024",
            "-o", "HostKeyAlgorithms=+ssh-rsa", "-o", f"UserKnownHostsFile={ROOT / 'private/known_hosts'}",
            "-o", "PubkeyAuthentication=no", "-o", "ConnectTimeout=5", f"ubnt@{host}", cmd]
    return subprocess.run(argv, env=dict(os.environ, SSHPASS=os.environ.get("SSHPASS", "ubnt")), input=stdin,
                          capture_output=True, timeout=timeout)


def stock(out: Path, host: str, base: str) -> None:
    with open(ROOT / "tmp/tsc_read", "rb") as f:
        r = ssh(host, "cat > /tmp/tsc_read && chmod +x /tmp/tsc_read", stdin=f.read(), timeout=60)
    if r.returncode:
        raise SystemExit(f"push rc={r.returncode} {r.stderr!r}")
    lines = []
    for front, (a, lane) in FRONTS.items():
        req = []
        for lo, hi, aer in RANGES:
            req += [f"{a} {lane if aer == 'lane' else aer} {reg:x}" for reg in range(lo, hi + 1)]
        req += [f"S {w} {ad | lane if lane_or else ad:x}" for _, ad, w, lane_or in MAC]
        r = ssh(host, f"/tmp/tsc_read {base}", stdin=("\n".join(req) + "\n").encode(), timeout=60)
        if r.returncode:
            raise SystemExit(f"tsc_read rc={r.returncode} {r.stderr[-300:]!r}")
        got = [x.split() for x in r.stdout.decode().splitlines()]
        miim = [g for g in got if g[0] != "S"]
        sch = [g for g in got if g[0] == "S"]
        for g in miim:
            lines.append(f"{front} {g[1]} {int(g[2], 16):x} {int(g[3], 16):04x}" if g[3] != "ffffffff" else f"{front} {g[1]} {int(g[2], 16):x} timeout")
        for (name, ad, w, lane_or), g in zip(MAC, sch):
            lines.append(f"mac {front} {name} {int(g[4], 16):08x}" + ("" if g[3] != "ffffffff" else " timeout"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    log(f"stock: {len(lines)} lines -> {out}")


def load(p: Path) -> dict:
    d = {}
    for ln in p.read_text().splitlines():
        t = ln.split()
        if t[0] == "mac":
            d[("mac", t[1], t[2])] = t[3]
        else:
            d[(t[0], t[1], t[2])] = t[3]
    return d


def diff(a: Path, b: Path) -> None:
    da, db = load(a), load(b)
    n = 0
    for k in sorted(set(da) | set(db), key=lambda k: (k[0], k[1], int(k[2], 16) if k[2].isalnum() and all(c in "0123456789abcdef" for c in k[2]) else 0, k[2])):
        va, vb = da.get(k), db.get(k)
        if va is None or vb is None:
            continue
        if va.lstrip("0") != vb.lstrip("0"):
            n += 1
            print(f"{' '.join(k)}: {a.stem}={va} {b.stem}={vb}")
    log(f"diff {a.name} vs {b.name}: {n} differ of {len(set(da) & set(db))} common")


def main() -> int:
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        return 2
    opt = lambda n, d: a[a.index(n) + 1] if n in a else d  # noqa: E731
    if a[0] == "ours":
        ours(ROOT / opt("--out", "tmp/tsc_dump/ours.txt"))
    elif a[0] == "stock":
        stock(ROOT / opt("--out", "tmp/tsc_dump/stock.txt"), opt("--host", os.environ.get("US48_SWITCH_IP", _ipreg.ip("us48-mgmt"))), opt("--base", ""))
    elif a[0] == "diff" and len(a) == 3:
        diff(Path(a[1]), Path(a[2]))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
