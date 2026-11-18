#!/usr/bin/env python3
"""copper_check — Part A board check for the copper ports (#123) on the running mainline image, over ssh.

Usage: copper_check.py [--no-octal] [--down]
- netdevs: ge1-ge48 + xe49/xe50 exist.
- pairs: ip link set up the loop pairs ge8<->ge17 (unit 0) and ge32<->ge41 (unit 1, scripts/cabling.json); decided from
  state, not a log line (#130): both ends carrier 1, operstate up, link_speed 1000, and 2 ARP probes (arping -D) each
  way counted in the far end's rx_packets. A link that was already up passes the same way.
- octal: with a pair linked, ip link set up a second port of the same octal (ge18 / ge42): the linked pair must keep
  carrier and log no 'link down' over 3 link polls, and the second port must not run the block init (octal top reset).
  Judged from the driver's /sys/class/net/<port>/copper_ready + block_ready (#126): bring-up runs once per module load,
  so on a re-run the second port is already up (copper_ready 1) and no bring-up line is expected.
- xe49/xe50 carrier and the dmesg WARN/BUG/Oops/FAILED count; temperatures (stop: die >= 108 C, LM75 >= 78 C).
- --down: set the test ports down at the end. Log: tmp/logs/copper_check.log. Exit 0 = all PASS.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402
import _board  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/copper_check.log"
PAIRS = [("ge8", "ge17", "ge18"), ("ge32", "ge41", "ge42")]	# (GPHY end, octal end, second port of that octal)
# 1000BASE-T AN + master/slave training ~3 s (IEEE 802.3 cl. 28/40, typical) + 1 s link poll = 4 s; 1.25x.
LINK_LIMIT_S = 5.0
LINK_POLL_S = 1.0	# driver LINK_POLL_MS
POLL_S = 0.25
ARP_N = 2		# probes per direction across the loop cable
ARP_W_S = 3		# busybox arping: 2 probes 1 s apart + 1 s for the last = 2 s; 1.25x, rounded up (whole seconds)
ARP_TARGET = "10.48.31.1"	# unused test address: DAD probes need a target, nothing answers
results: list[tuple[str, bool, str]] = []


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
    rc, out = _board.ssh(cmd)
    if rc == 255:
        raise SystemExit(f"ssh failed: {out}")
    return out


def carrier(*devs: str) -> dict[str, str]:
    out = sh(" ; ".join(f"echo {d} $(cat /sys/class/net/{d}/carrier 2>/dev/null)" for d in devs))
    return {f[0]: (f[1] if len(f) > 1 else "") for f in (ln.split() for ln in out.splitlines()) if f}


def copper_state(dev: str) -> tuple[bool, bool] | None:
    """(copper_ready, block_ready) from the us48_cmic netdev sysfs attrs; None if the driver has none."""
    out = sh(f"cat /sys/class/net/{dev}/copper_ready /sys/class/net/{dev}/block_ready 2>/dev/null").split()
    return (out[0] == "1", out[1] == "1") if len(out) == 2 else None


def link_state(*devs: str) -> dict[str, tuple[str, str]]:
    """{dev: (operstate, link_speed)} from sysfs (link_speed: us48_cmic copper attr, #130)."""
    out = sh(" ; ".join(f"echo {d} $(cat /sys/class/net/{d}/operstate) $(cat /sys/class/net/{d}/link_speed 2>/dev/null || echo -)"
                        for d in devs))
    return {f[0]: (f[1], f[2]) for f in (ln.split() for ln in out.splitlines()) if len(f) == 3}


def rx_tx(*devs: str) -> dict[str, tuple[int, int]]:
    out = sh(" ; ".join(f"echo {d} $(cat /sys/class/net/{d}/statistics/rx_packets /sys/class/net/{d}/statistics/tx_packets)"
                        for d in devs))
    return {f[0]: (int(f[1]), int(f[2])) for f in (ln.split() for ln in out.splitlines()) if len(f) == 3}


def cross_frames(a: str, b: str) -> dict[str, int]:
    """ARP probes (DAD mode: sender 0.0.0.0, no address needed) a -> b and b -> a; {"a>b": far-end rx delta, ...}.
    The rx delta can include other frames; tx delta < ARP_N means the probe was not sent (counted as 0)."""
    res = {}
    for src, dst in ((a, b), (b, a)):
        c0 = rx_tx(src, dst)
        sh(f"arping -D -q -c {ARP_N} -w {ARP_W_S} -I {src} {ARP_TARGET}")
        c1 = rx_tx(src, dst)
        sent = c1[src][1] - c0[src][1]
        res[f"{src}>{dst}"] = c1[dst][0] - c0[dst][0] if sent >= ARP_N else 0
    return res


def dmesg_lines() -> list[str]:
    return sh("dmesg").splitlines()


def dmesg_ts(ln: str) -> float:
    """Kernel time stamp of a dmesg line, -1 if none (a line count breaks once the ring wraps)."""
    try:
        return float(ln.split("]", 1)[0].lstrip("[ "))
    except ValueError:
        return -1.0


def wait_carrier(devs: tuple[str, ...]) -> tuple[bool, float]:
    t0 = time.monotonic()
    while True:
        c = carrier(*devs)
        el = time.monotonic() - t0
        if all(c.get(d) == "1" for d in devs):
            return True, el
        if el > LINK_LIMIT_S:
            log(f"carrier wait: limit {LINK_LIMIT_S} s hit after {el:.1f} s, carrier {c}")
            return False, el
        time.sleep(POLL_S)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-octal", action="store_true", help="skip the second-port-of-octal test")
    ap.add_argument("--down", action="store_true", help="set the test ports down at the end")
    a = ap.parse_args()
    log(f"--- copper_check on {_board.HOST}: {sh('uname -v').strip()}")
    hot = _board.too_hot(_board.temps())
    if hot:
        log(f"STOP: {hot}")
        return 1
    names = set(sh("ls /sys/class/net").split())
    want = {f"ge{i}" for i in range(1, 49)} | {"xe49", "xe50"}
    check("netdevs", want <= names, f"{len(want & names)}/50 present" + (f", missing {sorted(want - names)}" if want - names else ""))
    test_ports = []
    for gphy, octal, second in PAIRS:
        sh(f"ip link set {gphy} up; ip link set {octal} up")
        test_ports += [gphy, octal]
        ok, el = wait_carrier((gphy, octal))
        st = link_state(gphy, octal)
        g1000 = all(s == ("up", "1000") for s in st.values())
        frames = cross_frames(gphy, octal) if ok else {}
        crossed = bool(frames) and all(n >= ARP_N for n in frames.values())
        check(f"link {gphy}<->{octal}", ok and g1000 and crossed,
              f"carrier after {el:.1f} s, (operstate, Mb/s) {st}, far-end rx of {ARP_N} probes {frames}")
        if a.no_octal or not ok:
            continue
        pre = copper_state(second)
        d1 = max((dmesg_ts(ln) for ln in dmesg_lines()), default=-1.0)
        sh(f"ip link set {second} up")
        test_ports.append(second)
        t_end = time.monotonic() + 3 * LINK_POLL_S + 0.5
        drops = []
        while time.monotonic() < t_end:
            c = carrier(gphy, octal)
            if c.get(gphy) != "1" or c.get(octal) != "1":
                drops.append(c)
            time.sleep(POLL_S)
        new = [ln for ln in dmesg_lines() if dmesg_ts(ln) > d1]
        post = copper_state(second)
        down = [ln for ln in new if "link down" in ln and (f" {gphy}:" in ln or f" {octal}:" in ln)]
        brought = [ln for ln in new if f"{second}: copper bring-up" in ln]
        # driver state (#126): bring-up runs once per load; block init (octal top reset) only if block_ready was 0
        if pre is None or post is None:
            no_blk, how = False, "no copper_ready/block_ready sysfs attrs (image older than #126)"
        elif pre[0]:
            no_blk, how = not brought and post[0], f"already brought up this load (copper_ready 1), new lines {brought}"
        else:
            no_blk = pre[1] and post[0] and len(brought) == 1 and "done" in brought[0] and "block init" not in brought[0]
            how = f"block_ready before {int(pre[1])}, copper_ready after {int(post[0])}, bring-up {brought}"
        check(f"octal {second} vs {octal}", not drops and not down and no_blk,
              f"carrier drops {len(drops)}, link-down lines {down}, {how}")
    c = carrier("xe49", "xe50", "ge1")
    log(f"info xe/ge1 carrier: {c}")
    check("ge1", c.get("ge1") == "1", "management carrier")
    bad = [ln for ln in dmesg_lines() if any(k in ln for k in ("WARNING", "BUG", "Oops", "FAILED", "Call trace", "Call Trace"))]
    check("dmesg", not bad, f"{len(bad)} WARN/BUG/FAILED lines" + (f": {bad[:3]}" if bad else ""))
    t = _board.temps()
    log(f"info temps: {t}")
    hot = _board.too_hot(t)
    check("thermal", not hot, hot or "below limits")
    if a.down:
        sh("; ".join(f"ip link set {p} down" for p in test_ports))
    fails = [r for r in results if not r[1]]
    log(f"=== {'PASS' if not fails else 'FAIL'}: {len(results) - len(fails)}/{len(results)}")
    return 1 if fails else 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
