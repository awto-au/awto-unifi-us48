#!/usr/bin/env python3
"""phy_test — copper PHY (phylib) checks on the running mainline image, over ssh (#58, docs/phy.md).

Usage: phy_test.py [--dry-run] [--only show,bound,stats,aneg,forced,eee,cable,admin,octal,check] [--ecd] [--eee] [--open-port geN]
- show: `ethtool` on every copper netdev: rc 0, no half-duplex link modes (the MAC runs full duplex only).
- bound: after opening the loop ports, the PHY driver name + ID of each (sysfs phydev).
- stats: `ethtool -S` on a loop port lists the phy_* counters.
- aneg: `ethtool -r` on a loop port: both ends back at 1000 within LINK_LIMIT_S; time logged.
- forced: both ends of a loop pair `autoneg off speed 100 duplex full` -> link_speed 100 + 2 ARP probes each way; then autoneg on -> 1000.
- eee: `ethtool --show-eee`; with --eee also `--set-eee eee on` / off (needs insmod phy_eee=1; the MAC has no LPI yet).
- cable: `ethtool --cable-test` on a loop port and on an open port (--open-port, default ge5); needs --ecd (sets
  /sys/module/us48_cmic/parameters/phy_ecd=1 for the run, restored after). Unproven ECD: results are logged, not judged,
  except that the test completes and the link returns.
- admin: copper_admin_test.py (down/up timing vs the #133 limits).
- octal: per unit + GPORT block, `copper bring-up ... block init` appears at most once since module load, no TOP_MISC_GLOBAL_RESET error.
- check: copper_check.py 8/8.
- Default: all; --dry-run prints the board commands and runs nothing. Log: tmp/logs/phy_test.log. Exit 0 = all PASS.
"""
import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402
import _board  # noqa: E402
import hwdb  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/phy_test.log"
_DB = hwdb.lite()
PAIRS = [(_DB.netdev(g), _DB.netdev(o)) for _, g, o in _DB.loop_pairs()]   # loop cables, hwdb/board/ports.toml
COPPER = [_DB.netdev(p["front"]) for p in _DB.ports if p["front"] and p["phy"] in ("gphy", "octal")]
# 1000BASE-T AN + master/slave training ~3 s (IEEE 802.3 cl. 28/40, typical) + 1 s phylib poll (PHY_STATE_TIME) = 4 s; 1.25x.
LINK_LIMIT_S = 5.0
POLL_S = 0.25
CALL_S = 15.0       # one board call; the cable test below is the longest (ECD ~ 1 s per pair, 4 pairs) + ssh start
CABLE_S = 20.0      # --cable-test: link break + 4 pair results + AN restart: 4 s per phase, 1.25x, rounded up
ARP_TARGET = "10.48.31.1"   # unused address: DAD probes need a target, nothing answers (copper_check.py)
ARP_N, ARP_W_S = 2, 3
ECD_PARAM = "/sys/module/us48_cmic/parameters/phy_ecd"
results: list[tuple[str, bool, str]] = []
dry = False


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def check(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    log(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")


def sh(cmd: str, call_s: float = CALL_S) -> tuple[int, str]:
    if dry:
        log(f"dry-run board: {cmd}")
        return 0, ""
    rc, out = _board.ssh(cmd, call_s=call_s)
    if rc == 255:
        raise SystemExit(f"ssh failed: {out}")
    return rc, out


def carrier(*devs: str) -> dict[str, str]:
    _, out = sh(" ; ".join(f"echo {d} $(cat /sys/class/net/{d}/carrier 2>/dev/null)" for d in devs))
    return {f[0]: (f[1] if len(f) > 1 else "") for f in (ln.split() for ln in out.splitlines()) if f}


def speeds(*devs: str) -> dict[str, str]:
    _, out = sh(" ; ".join(f"echo {d} $(cat /sys/class/net/{d}/link_speed 2>/dev/null || echo -)" for d in devs))
    return {f[0]: f[1] for f in (ln.split() for ln in out.splitlines()) if len(f) == 2}


def wait_link(devs: tuple[str, ...], speed: str) -> tuple[bool, float]:
    """Both ends carrier 1 at `speed` Mb/s (sysfs link_speed, #130); limit LINK_LIMIT_S."""
    if dry:
        return True, 0.0
    t0 = time.monotonic()
    while True:
        c, s = carrier(*devs), speeds(*devs)
        el = time.monotonic() - t0
        if all(c.get(d) == "1" and s.get(d) == speed for d in devs):
            return True, el
        if el > LINK_LIMIT_S:
            log(f"link wait: limit {LINK_LIMIT_S} s hit after {el:.1f} s, carrier {c} speed {s}")
            return False, el
        time.sleep(POLL_S)


def rx(dev: str) -> int:
    _, out = sh(f"cat /sys/class/net/{dev}/statistics/rx_packets")
    return int(out.split()[0]) if out.split() else 0


def arp_both(a: str, b: str) -> dict[str, int]:
    res = {}
    for src, dst in ((a, b), (b, a)):
        r0 = rx(dst)
        sh(f"arping -D -q -c {ARP_N} -w {ARP_W_S} -I {src} {ARP_TARGET}")
        res[f"{src}>{dst}"] = rx(dst) - r0
    return res


def up_pairs() -> None:
    for a, b in PAIRS:
        sh(f"ip link set {a} up; ip link set {b} up")
        ok, el = wait_link((a, b), "1000")
        log(f"pair {a}<->{b} up: {ok} in {el:.1f} s")


def t_show() -> None:
    bad, half, n = [], [], 0
    for d in COPPER:
        rc, out = sh(f"ethtool {d}")
        n += 1
        if rc != 0 or "Supported link modes" not in out and not dry:
            bad.append(d)
        if "Half" in out.split("Advertised link modes")[0]:
            half.append(d)
    check("show", not bad and not half, f"{n} ports: ethtool failed {bad}, half-duplex modes listed {half}")


def t_bound() -> None:
    for a, b in PAIRS:
        for d in (a, b):
            _, out = sh(f"echo $(readlink /sys/class/net/{d}/phydev/driver) $(cat /sys/class/net/{d}/phydev/phy_id)")
            ok = bool(re.search(r"(BCM56150 integrated GPHY|BCM54282 octal GPHY) 0x600d(85b|845)", out)) or dry
            check(f"bound {d}", ok, out.strip() or "no phydev")


def t_stats() -> None:
    a = PAIRS[0][0]
    rc, out = sh(f"ethtool -S {a}")
    check("stats", rc == 0 and "phy_receive_errors" in out or dry, f"{a}: " + " ".join(re.findall(r"phy_\w+: \d+", out)))


def t_aneg() -> None:
    for a, b in PAIRS:
        sh(f"ethtool -r {a}")
        t0 = time.monotonic()
        ok, el = wait_link((a, b), "1000")
        check(f"aneg restart {a}", ok, f"both ends 1000 again after {el:.1f} s (limit {LINK_LIMIT_S} s, wall {time.monotonic() - t0:.1f} s)")


def t_forced() -> None:
    for a, b in PAIRS:
        sh(f"ethtool -s {a} autoneg off speed 100 duplex full; ethtool -s {b} autoneg off speed 100 duplex full")
        ok, el = wait_link((a, b), "100")
        arp = arp_both(a, b) if ok else {}
        check(f"forced 100/full {a}<->{b}", ok and all(v >= ARP_N for v in arp.values()) or dry,
              f"link_speed 100 after {el:.1f} s, far-end rx deltas {arp}")
        _, show = sh(f"ethtool {a}")
        log(f"info ethtool {a} forced: " + " ".join(re.findall(r"(?:Speed|Duplex|Auto-negotiation): \S+", show)))
        sh(f"ethtool -s {a} autoneg on; ethtool -s {b} autoneg on")
        ok, el = wait_link((a, b), "1000")
        check(f"autoneg back {a}<->{b}", ok, f"1000 after {el:.1f} s")


def t_eee(change: bool) -> None:
    a = PAIRS[0][0]
    rc, out = sh(f"ethtool --show-eee {a}")
    log(f"info show-eee {a} rc {rc}: {' '.join(out.split())}")
    check("eee show", rc == 0 or "not supported" in out or dry, f"rc {rc}")
    if change:
        for state in ("on", "off"):
            rc, out = sh(f"ethtool --set-eee {a} eee {state}")
            _, show = sh(f"ethtool --show-eee {a}")
            check(f"eee {state}", rc == 0 or dry, f"rc {rc}: {' '.join(out.split())} | {' '.join(show.split())}")
        ok, el = wait_link(PAIRS[0], "1000")
        check("eee link after", ok, f"1000 after {el:.1f} s")


def t_cable(ecd: bool, open_port: str) -> None:
    if not ecd:
        log("cable: skipped (--ecd not given; the ECD registers are unproven on the BCM54282)")
        return
    _, old = sh(f"cat {ECD_PARAM}")
    sh(f"echo 1 > {ECD_PARAM}")
    sh(f"ip link set {open_port} up")
    for d in (PAIRS[0][0], open_port):
        rc, out = sh(f"ethtool --cable-test {d}", call_s=CABLE_S)
        log(f"info cable-test {d} rc {rc}: {' | '.join(out.splitlines())}")
        check(f"cable-test {d} completes", rc == 0 and "Cable test completed" in out or dry, f"rc {rc}")
    ok, el = wait_link(PAIRS[0], "1000")
    check("cable-test link back", ok, f"1000 after {el:.1f} s")
    sh(f"echo {'Y' if old.strip() == 'Y' else 'N'} > {ECD_PARAM}")


def t_script(name: str, args: list[str]) -> None:
    cmd = [sys.executable, str(ROOT / "scripts" / name), *args]
    if dry:
        log("dry-run host: " + " ".join(cmd))
        return
    # Overall limit 3x the measured cold run (copper_check ~1 min, copper_admin_test 3 reps ~1 min); stall = no output.
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=360)
    tail = (r.stdout.strip().splitlines() or ["(no output)"])[-1]
    check(name, r.returncode == 0, f"rc {r.returncode}: {tail}")


def t_octal() -> None:
    _, out = sh("dmesg")
    seen: dict[tuple[int, int], int] = {}
    for m in re.finditer(r"ge(\d+): copper bring-up done \(block (\d+) lane \d+, block init\)", out):
        unit = 0 if int(m.group(1)) <= 24 else 1
        seen[(unit, int(m.group(2)))] = seen.get((unit, int(m.group(2))), 0) + 1
    bad_reset = [ln for ln in out.splitlines() if "TOP_MISC_GLOBAL_RESET still" in ln]
    multi = {k: v for k, v in seen.items() if v > 1}
    check("octal top reset once", not multi and not bad_reset or dry,
          f"block inits (unit, block): {seen}, repeated {multi}, read-back errors {len(bad_reset)}")


def main() -> int:
    global dry
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", default="show,bound,stats,aneg,forced,eee,cable,admin,octal,check")
    ap.add_argument("--ecd", action="store_true", help="enable the unproven ECD for the cable test")
    ap.add_argument("--eee", action="store_true", help="also set EEE on/off (image loaded with phy_eee=1)")
    ap.add_argument("--open-port", default="ge5", help="copper port with no cable, for the cable test")
    a = ap.parse_args()
    dry = a.dry_run
    only = set(a.only.split(","))
    log(f"--- phy_test on {_board.HOST}{' (dry run)' if dry else ''}: {sh('uname -v')[1].strip()}")
    if not dry:
        hot = _board.too_hot(_board.temps())
        if hot:
            log(f"STOP: {hot}")
            return 1
    if only & {"bound", "stats", "aneg", "forced", "eee", "cable"}:
        up_pairs()
    steps = {"show": t_show, "bound": t_bound, "stats": t_stats, "aneg": t_aneg, "forced": t_forced,
             "eee": lambda: t_eee(a.eee), "cable": lambda: t_cable(a.ecd, a.open_port),
             "admin": lambda: t_script("copper_admin_test.py", ["--reps", "3"]), "octal": t_octal,
             "check": lambda: t_script("copper_check.py", [])}
    for name, fn in steps.items():
        if name in only:
            fn()
    fails = [r for r in results if not r[1]]
    log(f"=== {'PASS' if not fails else 'FAIL'}: {len(results) - len(fails)}/{len(results)}")
    return 1 if fails else 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
