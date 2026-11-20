#!/usr/bin/env python3
"""tsc1g_matrix: reload us48_cmic with each tsc1g_altcredit / tsc1g_linkup_speedset setting, bring xe51 up, send ARPs, read TSC + MAC state (#78).

Usage: scripts/tsc1g_matrix.py --ko-dir <dir with us48_cmic.ko + us48_cmic_xe.ko> [--apply] [--front 51]
- Dry run unless --apply. Copies the two modules to the board, then for each (altcredit, linkup_speedset) in 0/0, 1/0, 0/1, 1/1:
  mod_reload us48_cmic <params>, xe49 address restored, xe<front> up, wait carrier, read c100-c105/c154/c254/9248, 6 ARP requests
  (temporary link-local address), MAC TPKT/TBYT/TERR deltas. Prints a wall-clock line per step for the far end's counters.
- No reboot. xe<front> ends admin down. Far end (sw2 session) must hold its port up with AN on and sample its RX counters.
- Waits: carrier <= 5 s (phylink poll 1 s x2 + AN 0.1 s, x1.25 rounded up), 2 s after link before the burst (credits settle after AN, 2 polls).
Log: tmp/logs/tsc1g_matrix.log. Board path: xe49 address from _ipreg (us48-xe49); the board-side script restores xe49 and ge1 after the reload.
"""
import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _ipreg  # noqa: E402
from _cli import help_guard  # noqa: E402
from lx import OPTS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/tsc1g_matrix.log"
XE = "/sys/module/us48_cmic_xe/parameters"
SC = "/sys/module/us48_cmic/parameters/schan"
LANE = {51: 0, 52: 2}
CTR = {"TPKT": 0x4c00, "TBYT": 0x6f00, "TERR": 0x5d00, "TFRG": 0x5c00, "TUFL": 0x6100, "RPKT": 0xb00}
COMBOS = [(0, 0), (1, 0), (0, 1), (1, 1)]


MGMT = _ipreg.ip("us48-xe49")  # primary path (ge1 .207 flaps with the dual-homed ARP, #146): a reload of us48_cmic_xe drops xe49, so the board script re-adds it


def reload_board(alt, lu):
    """Detached board-side reload (rmmod xe, core; insmod with params; xe49 + ge1 addresses back), then wait for ssh on xe49."""
    script = ("rmmod us48_cmic_xe; rmmod us48_cmic; "
              f"insmod /lib/modules/$(uname -r)/us48_cmic.ko unit0=1 tsc1g_altcredit={alt} tsc1g_linkup_speedset={lu}; "
              "insmod /lib/modules/$(uname -r)/us48_cmic_xe.ko; "
              f"ip link set xe49 up; ip addr add {MGMT}/24 dev xe49; ip link set ge1 up; ip addr add {_ipreg.ip('us48-boot')}/24 dev ge1; "
              "echo RELOAD_DONE > /tmp/matrix_reload.done")
    sh(f"rm -f /tmp/matrix_reload.done; setsid nohup sh -c '{script}' > /tmp/matrix_reload.log 2>&1 < /dev/null & echo started")
    t0 = time.monotonic()
    while time.monotonic() - t0 < 30:  # backstop: reload takes ~11 s (mod_reload log, #143)
        time.sleep(1)
        try:
            if "RELOAD_DONE" in sh("cat /tmp/matrix_reload.done 2>/dev/null"):
                return True
        except subprocess.TimeoutExpired:
            pass
    return False


def sh(cmd):
    # 40 s: ssh round trip 1-2 s; a module reload takes ~11 s; longer = board path gone.
    r = subprocess.run(["ssh", *OPTS, f"root@{MGMT}", cmd], capture_output=True, text=True, timeout=40)
    return r.stdout + r.stderr


def reg(f, lo, hi):
    sh(f'echo "{f} {lo:x} {hi:x}" > {XE}/tscreg')
    return sh(f"dmesg | grep 'xe{f} range' | tail -n 1 | sed 's/.*range: //'").strip()


def ctrs(lane):
    out = sh('echo "' + "; ".join(f"0 rreg 5 {a | lane:x} 2" for a in CTR.values()) + f'" > {SC}; cat {SC}')
    v = re.findall(r":\s*([0-9a-f]{8}) ([0-9a-f]{8})", out)
    return {k: (int(h, 16) << 32 | int(lo, 16)) for k, (lo, h) in zip(CTR, v)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ko-dir", required=True)
    ap.add_argument("--front", type=int, default=51, choices=sorted(LANE))
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    f, ko = a.front, Path(a.ko_dir)
    print(f"matrix on xe{f}: combos {COMBOS}, modules from {ko}")
    if not a.apply:
        print("dry run (use --apply)")
        return 0
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        def say(s):
            line = f"{time.strftime('%H:%M:%S')} {s}"
            print(line, flush=True)
            fh.write(line + "\n")
            fh.flush()
        rel = sh("uname -r").strip()
        for m in ("us48_cmic.ko", "us48_cmic_xe.ko"):
            r = subprocess.run(["scp", "-O", *OPTS, str(ko / m), f"root@{MGMT}:/lib/modules/{rel}/"], capture_output=True, text=True, timeout=30)
            if r.returncode:
                say(f"FAIL scp {m}: {r.stderr.strip()}")
                return 1
        for alt, lu in COMBOS:
            say(f"=== altcredit={alt} linkup_speedset={lu}: reload")
            if not reload_board(alt, lu):
                say("FAIL board reload did not finish in 30 s")
                return 1
            sh(f"echo 0 > /proc/sys/net/ipv6/conf/xe{f}/disable_ipv6; ip link set xe{f} up")
            t0 = time.monotonic()
            while time.monotonic() - t0 < 5 and sh(f"cat /sys/class/net/xe{f}/carrier").strip() != "1":
                time.sleep(0.25)
            time.sleep(2)
            say(f"carrier {sh(f'cat /sys/class/net/xe{f}/carrier').strip()} after {time.monotonic() - t0:.1f} s; "
                f"credits {reg(f, 0xc100, 0xc105)} | c154 {reg(f, 0xc154, 0xc154)} | c254 {reg(f, 0xc254, 0xc254)} | 9248 {reg(f, 0x9248, 0x9248)}")
            c0 = ctrs(LANE[f])
            sh(f"ip addr add 169.254.48.51/16 dev xe{f}")
            say("burst start (6 ARP requests)")
            sh("ping -c 6 -i 0.3 -W 1 169.254.48.52 >/dev/null 2>&1; true")
            say("burst end")
            c1 = ctrs(LANE[f])
            sh(f"ip addr del 169.254.48.51/16 dev xe{f}")
            say(f"MAC deltas {({k: c1[k] - c0[k] for k in c0})}; credits after burst {reg(f, 0xc100, 0xc103)}")
            sh(f"ip link set xe{f} down")
            time.sleep(8)  # far end counters settle between runs (its sampler is 2 s)
    return 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
