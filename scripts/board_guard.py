#!/usr/bin/env python3
"""board_guard - refuse a dev.py command whose arguments target another session's lab unit, protected port or serial adapter (#139).

Usage: board_guard.py --selftest | [<dev.py command> [args ...]]    exit 0 = ok, 5 = refused
- List: awto-dan docs/lab/lab.toml via scripts/_lab.py: units owned by another repo (mgmt host + relay by ip-registry name/IP),
  [[protected]] unit:port, adapters of other units. Nothing is stored here.
- Unit in args: refused unless US48_BOARD_GUARD_ALLOW=<unit,...> (sw1_bounce/sw1_link_test set sw1). Unit + protected port
  (sw1 lan5), or another unit's serial by-id: always refused.
- Text match on the arguments only (<10 ms, no network, no console traffic). Log: tmp/logs/board_guard.log
"""
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _ipreg  # noqa: E402
import _lab  # noqa: E402
from _cli import help_guard  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/board_guard.log"
OURS = "awto-unifi-us48"
REFUSED = 5


def log(msg: str) -> None:
    print(f"board_guard: {msg}", file=sys.stderr)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}\n")


def _mentions(text: str, name: str) -> bool:
    ip = _ipreg.ip(name)
    return bool(re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w-])|(?<![\d.]){re.escape(ip)}(?![\d])", text))


def check(argv: list[str], allow: str | None = None) -> list[str]:
    allowed = {x for x in (os.environ.get("US48_BOARD_GUARD_ALLOW", "") if allow is None else allow).split(",") if x}
    lab = _lab.load()
    others = {n: u for n, u in lab["unit"].items() if u["owner"] != OURS}
    why = []
    for arg in argv:
        for n, u in others.items():
            hosts = [h for h in (u.get("mgmt_host"), u.get("relay")) if h]
            if not any(_mentions(arg, h) for h in hosts):
                continue
            for p in _lab.protected(n):
                if re.search(rf"(?<!\w){re.escape(p['port'])}(?!\w)", arg):
                    why.append(f"{n} {p['port']}: {p['rule']}")
            if n not in allowed:
                why.append(f"{n}: owned by {u['owner']} (US48_BOARD_GUARD_ALLOW={n} to override)")
        for a in lab.get("adapter", []):
            if a["unit"] in others and any(b in arg for b in a["by_id"]):
                why.append(f"serial {a['unit']} adapter {a['chip']}: another session's console")
    return why


def selftest() -> int:
    cases = [(["send", "ls"], None, False), (["flash-net", "192.168.25.206"], None, False),
             (["send", "ssh root@192.168.25.48 ip link"], None, True),
             (["send", "ssh root@192.168.25.48 ip link"], "sw1", False),
             (["send", "ssh root@192.168.25.48 ip link set lan5 down"], "sw1", True),
             (["send", "st1008f-2-mgmt"], None, True),
             (["console", "/dev/serial/by-id/usb-1a86_USB2.0-Serial-if00-port0"], "sw2", True),
             (["send", "ping 192.168.25.480"], None, False)]
    fails = 0
    for argv, allow, want in cases:
        got = bool(check(argv, allow or ""))
        if got != want:
            fails += 1
            log(f"FAIL {argv} allow={allow}: refused={got}, want {want}")
    log(f"{'PASS' if not fails else 'FAIL'} board_guard selftest: {len(cases) - fails}/{len(cases)}")
    return 1 if fails else 0


def main() -> int:
    argv = sys.argv[1:]
    if argv == ["--selftest"]:
        return selftest()
    why = check(argv)
    for w in why:
        log(f"REFUSED {' '.join(argv)}: {w}")
    if not why:
        log(f"ok {' '.join(argv)}")
    return REFUSED if why else 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
