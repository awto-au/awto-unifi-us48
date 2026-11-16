#!/usr/bin/env python3
"""u2_replay_bisect — #46: find the stock-replay entry that kills CPU RX on a warm chip (binary search over entry ordinals).

Usage: scripts/u2_replay_bisect.py [--lo N] [--hi N] [--first K] [--mask 0x15f]
- Predicate broken(K): fresh warm chip (dev stock-fresh + boot-flash uboot slot, ~5 min), ping alive, `hr2init MASK 0 K`, ping dead.
- Search assumes monotonic: prefix 0..K breaks RX iff the killer entry is <= K. lo = largest known good K (-1 none), hi = smallest known bad K
  (default: last entry). --first K probes K before bisecting. Result: first bad ordinal with its header line (gen_bcm_hr2_init data).
- Stops on any harness failure (no retry, no substitute). Log: tmp/logs/u2_replay_bisect.log
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dev  # noqa: E402
import u2_regsync as rs  # noqa: E402
from dev import console, log  # noqa: E402
from _cli import help_guard  # noqa: E402

DATA = dev.REPO / "common/bcm-cmic/bcm_cmic_hr2_init_data.h"


def header_lines() -> list[str]:
    return [ln.strip() for ln in DATA.read_text().splitlines() if re.match(r"\s*0x[0-9a-f]+,", ln)]


def warm_chip() -> bool:
    if dev.cmd_stock_fresh([]):
        return False
    return dev.cmd_boot_flash(["--slot", "uboot"]) == 0


def broken(k: int, mask: str) -> bool | None:
    """True = RX dead after prefix replay, False = alive, None = harness failure."""
    if not warm_chip():
        return None
    if not dev._ensure_console() or not console.restart(console.BAUD):
        return None
    s = console.Session()
    try:
        s.write(b"\x03\r\n")
        s.expect(dev.U2_PROMPTS, 5.0, "awto-uboot prompt")
        rs.ms.run(s, f"setenv ipaddr {dev.U2_IP}; setenv serverip {rs.HOST}")
        if not rs.probe(s, f"K={k} warm baseline", 30.0):
            log(f"K={k}: warm baseline does not ping", "FATAL")
            return None
        r = rs.ms.run(s, f"hr2init {mask} 0 {k}", stall=30.0, overall=120.0)
        log(f"K={k}: {' '.join(r.split()[1:])[:140]}", "ALERT")
        alive = rs.probe(s, f"K={k} after prefix replay")
        log(f"K={k}: RX {'alive' if alive else 'DEAD'}", "ALERT")
        return not alive
    except console.Timeout as e:
        log(str(e), "FATAL")
        return None
    finally:
        s.close()


def arg(name: str, default):
    a = sys.argv
    return type(default)(a[a.index(name) + 1], 0) if name in a and isinstance(default, int) else (a[a.index(name) + 1] if name in a else default)


def main() -> int:
    n = len(header_lines())
    lo, hi = arg("--lo", -1), arg("--hi", n - 1)
    mask = arg("--mask", "0x15f")
    first = arg("--first", -2)
    log(f"entries {n}, lo {lo}, hi {hi}, mask {mask}", "ALERT")
    while hi - lo > 1:
        k = first if first >= 0 and lo < first < hi else (lo + hi) // 2
        first = -2
        b = broken(k, mask)
        if b is None:
            log(f"harness failure at K={k}; state lo={lo} hi={hi}", "FATAL")
            return 1
        if b:
            hi = k
        else:
            lo = k
        log(f"lo={lo} hi={hi}", "ALERT")
    log(f"first bad entry ordinal {hi}: {header_lines()[hi]}", "ALERT")
    for i in range(max(0, hi - 3), min(n, hi + 2)):
        log(f"  [{i}] {header_lines()[i]}", "ALERT")
    return 0


if __name__ == "__main__":
    help_guard(__doc__, strict=True, known=('--first', '--hi', '--lo', '--mask'))
    sys.exit(main())
