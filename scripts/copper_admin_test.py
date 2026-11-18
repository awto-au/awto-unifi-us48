#!/usr/bin/env python3
"""copper_admin_test — admin down/up of a copper port drops/restores the link at the far end (#133), over ssh.

Usage: copper_admin_test.py [--reps N] [--pair ge8,ge17] [--down]
- Loop cables from hwdb/board/ports.toml (ge8<->ge17 unit 0, ge32<->ge41 unit 1); both directions, N reps (default 5).
- One rep (random 0-1 s start offset vs the 1 s driver link poll), timed on the board (/proc/uptime, 10 ms): `ip link set X down` -> far end carrier 0 (limit DOWN_LIMIT_S);
  `ip link set X up` -> both ends carrier 1 + link_speed 1000 (limit UP_LIMIT_S). Expiry = FAIL with the elapsed time.
- Never the management port (ge1). Temperatures before/after (stop: die >= 108 C, LM75 >= 78 C).
- --down: set the test ports down at the end. Log: tmp/logs/copper_admin_test.log. Exit 0 = all PASS.
"""
import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402
import _board  # noqa: E402
import hwdb  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/copper_admin_test.log"
_DB = hwdb.lite()
PAIRS = [(_DB.netdev(g), _DB.netdev(o)) for _, g, o in _DB.loop_pairs()]   # loop cables, hwdb/board/ports.toml
# Far-end carrier loss: PHY link fail after the peer's POWER_DOWN + up to one 1 s driver link poll. 1.25x the measured
# worst case of the #133 runs (random poll phase); expiry = FAIL.
DOWN_LIMIT_S = 1.25 * 1.77
# 1000BASE-T AN restart + master/slave training + 1 s link poll, both ends. 1.25x the measured worst case (#133).
UP_LIMIT_S = 1.25 * 3.49
LINK_POLL_S = 1.0       # driver LINK_POLL_MS: each rep starts at a random poll phase
PRE_LIMIT_S = 5.0       # initial link before rep 1: copper_check.py LINK_LIMIT_S
CALL_S = 15.0           # one board call: the longest loop (UP_LIMIT_S) + ssh start
results: list[tuple[str, bool, str]] = []

# Board side (busybox ash): centisecond clock from /proc/uptime, busy-poll sysfs with the read builtin (no fork).
SH_CS = 'cs() { read u _ < /proc/uptime; echo "${u%.*}${u#*.}"; }; '


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def check(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    log(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")


def sh(cmd: str) -> str:
    rc, out = _board.ssh(cmd, call_s=CALL_S)
    if rc == 255:
        raise SystemExit(f"ssh failed: {out}")
    return out


def up_loop(a: str, b: str, limit_s: float, first: str = ":") -> str:
    """Board loop (clock starts before `first`): until both carrier 1 + link_speed 1000, or the limit; prints 'up <cs> <state>'."""
    return (f"t0=$(cs); {first}; lim=$((t0 + {int(limit_s * 100)})); while :; do "
            f"read ca < /sys/class/net/{a}/carrier; read cb < /sys/class/net/{b}/carrier; "
            f"read sa < /sys/class/net/{a}/link_speed; read sb < /sys/class/net/{b}/link_speed; "
            f"[ \"$ca$cb\" = 11 ] && [ \"$sa$sb\" = 10001000 ] && break; [ $(cs) -gt $lim ] && break; done; "
            f"echo up $(($(cs) - t0)) $ca$cb $sa/$sb")


def rep(dn: str, far: str) -> tuple[float, float, str]:
    """(down s, up s, state) for one admin down/up of dn; -1 = limit hit. Down and up each start at a random poll phase."""
    time.sleep(random.uniform(0, LINK_POLL_S))
    out = sh(SH_CS + f"t0=$(cs); ip link set {dn} down; lim=$((t0 + {int(DOWN_LIMIT_S * 100)})); "
             f"while read c < /sys/class/net/{far}/carrier; [ \"$c\" = 1 ]; do [ $(cs) -gt $lim ] && break; done; "
             f"echo down $(($(cs) - t0)) $c").split()
    time.sleep(random.uniform(0, LINK_POLL_S))
    out += sh(SH_CS + up_loop(dn, far, UP_LIMIT_S, f"ip link set {dn} up")).split()
    d = out.index("down")
    u = out.index("up")
    down_s = int(out[d + 1]) / 100 if out[d + 2] == "0" else -1.0
    up_ok = out[u + 2] == "11" and out[u + 3] == "1000/1000"
    return down_s, int(out[u + 1]) / 100 if up_ok else -1.0, " ".join(out[u + 2:u + 4])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--pair", help="one pair a,b instead of both loop cables")
    ap.add_argument("--down", action="store_true", help="set the test ports down at the end")
    a = ap.parse_args()
    pairs = [tuple(a.pair.split(","))] if a.pair else PAIRS
    if any(_DB.mgmt_netdev() in p for p in pairs):
        log(f"refused: {_DB.mgmt_netdev()} is the management port")
        return 2
    log(f"--- copper_admin_test on {_board.HOST}: {sh('uname -v').strip()}; limits down {DOWN_LIMIT_S:.2f} s, "
        f"up {UP_LIMIT_S:.2f} s, {a.reps} reps per direction")
    hot = _board.too_hot(_board.temps())
    if hot:
        log(f"STOP: {hot}")
        return 1
    for x, y in pairs:
        sh(f"ip link set {x} up; ip link set {y} up")
        pre = sh(SH_CS + up_loop(x, y, PRE_LIMIT_S)).split()
        if pre[2] != "11":
            check(f"pre-link {x}<->{y}", False, f"no link: {pre}")
            continue
        for dn, far in ((x, y), (y, x)):
            downs, ups = [], []
            for i in range(a.reps):
                d, u, st = rep(dn, far)
                downs.append(d)
                ups.append(u)
                log(f"  {dn} down -> {far} carrier 0 after {d:.2f} s; up -> link {st} after {u:.2f} s (rep {i + 1})")
            ok = all(v >= 0 for v in downs + ups)
            check(f"admin {dn} (far {far}) x{a.reps}", ok,
                  f"down s {downs} (max {max(downs):.2f}, limit {DOWN_LIMIT_S:.2f}), up s {ups} "
                  f"(max {max(ups):.2f}, limit {UP_LIMIT_S:.2f}); -1 = limit hit")
    bad = [ln for ln in sh("dmesg").splitlines() if any(k in ln for k in ("WARNING", "BUG", "Oops", "PHY power"))]
    check("dmesg", not bad, f"{len(bad)} WARN/BUG/PHY power lines" + (f": {bad[:3]}" if bad else ""))
    t = _board.temps()
    log(f"info temps: {t}")
    hot = _board.too_hot(t)
    check("thermal", not hot, hot or "below limits")
    if a.down:
        sh("; ".join(f"ip link set {p} down" for pr in pairs for p in pr))
    fails = [r for r in results if not r[1]]
    log(f"=== {'PASS' if not fails else 'FAIL'}: {len(results) - len(fails)}/{len(results)}")
    return 1 if fails else 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
