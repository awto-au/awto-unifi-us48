#!/usr/bin/env python3
"""board_guard - refuse a dev.py command whose arguments target a protected lab host, port or serial adapter (#139).

Usage: board_guard.py --selftest | [<dev.py command> [args ...]]    exit 0 = ok, 5 = refused
- List: hwdb/lab/protected.toml (hosts by registry name -> IP via _ipreg, ports, serial by-id).
- Host in args: refused unless US48_BOARD_GUARD_ALLOW=<name,...>. Host + its protected port (sw1 lan5): always refused.
- Host-only check on the argument text (<10 ms, no network, no console traffic).
- Log: tmp/logs/board_guard.log
"""
import os
import re
import sys
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _ipreg  # noqa: E402
from _cli import help_guard  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LIST = ROOT / "hwdb/lab/protected.toml"
LOG = ROOT / "tmp/logs/board_guard.log"
REFUSED = 5


def log(msg: str) -> None:
    print(f"board_guard: {msg}", file=sys.stderr)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}\n")


def _mentions(text: str, name: str) -> bool:
    ip = _ipreg.ip(name)
    return bool(re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w-])|(?<![\d.]){re.escape(ip)}(?![\d])", text))


def check(argv: list[str], allow: str | None = None, lst: dict | None = None) -> list[str]:
    if lst is None:
        with LIST.open("rb") as fh:
            lst = tomllib.load(fh)
    allowed = {x for x in (os.environ.get("US48_BOARD_GUARD_ALLOW", "") if allow is None else allow).split(",") if x}
    why = []
    for arg in argv:
        for p in lst.get("port", []):
            if _mentions(arg, p["host"]) and re.search(rf"(?<![\w]){re.escape(p['port'])}(?![\w])", arg):
                why.append(f"{p['host']} {p['port']}: {p['why']}")
        for h in lst.get("host", []):
            if h["name"] not in allowed and _mentions(arg, h["name"]):
                why.append(f"{h['name']}: {h['why']} (US48_BOARD_GUARD_ALLOW={h['name']} to override)")
        for s in lst.get("serial", []):
            if s["by_id"] in arg:
                why.append(f"{s['by_id']}: {s['why']}")
    return why


def selftest() -> int:
    cases = [(["send", "ls"], None, False), (["flash-net", "192.168.25.206"], None, False),
             (["send", "ssh root@192.168.25.48 ip link"], None, True),
             (["send", "ssh root@192.168.25.48 ip link"], "st1008f-1-mgmt", False),
             (["send", "ssh root@192.168.25.48 ip link set lan5 down"], "st1008f-1-mgmt", True),
             (["send", "st1008f-2-mgmt"], None, True),
             (["console", "/dev/serial/by-id/usb-1a86_USB2.0-Serial-if00-port0"], "st1008f-2-mgmt", True),
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
