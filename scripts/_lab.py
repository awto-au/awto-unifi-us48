#!/usr/bin/env python3
"""lab: reader for the shared bench topology docs/lab/lab.toml (awto-dan#109). Stdlib only; vendored byte-identical as scripts/_lab.py in consumers.

Import:  import _lab as lab; lab.unit("sw1"); lab.adapter("us48"); lab.relay("sw2"); lab.cabling("us48"); lab.protected("sw1")
CLI:     lab.py check | show | cabling [UNIT]   (check = schema + every host name exists in ip-registry.toml; rc 1 on errors)
Location: $AWTO_DAN_REPO, else the awto-dan checkout beside this repo's main checkout, else this file's repo. Log: tmp/logs/lab.log (CLI only).
"""
import os
import subprocess
import sys
import tomllib
from pathlib import Path

STATES = ("fdb-verified", "reported", "conflict")
_HERE = Path(__file__).resolve().parent


def _help_guard(doc):
    if "-h" in sys.argv[1:] or "--help" in sys.argv[1:]:
        print(doc.strip())
        sys.exit(0)


def _git(*args):
    r = subprocess.run(["git", *args], capture_output=True, text=True, cwd=_HERE)
    return r.stdout.strip() if r.returncode == 0 else ""


def dan_repo():
    cands = []
    if os.environ.get("AWTO_DAN_REPO"):
        cands.append(Path(os.environ["AWTO_DAN_REPO"]))
    top = _git("rev-parse", "--show-toplevel")
    common = _git("rev-parse", "--path-format=absolute", "--git-common-dir")
    if common:
        cands.append(Path(common).parent.parent / "awto-dan")  # sibling of the main checkout (worktrees too)
    if top:
        cands += [Path(top), Path(top).parent / "awto-dan"]
    cands.append(_HERE.parent)
    for c in cands:
        if (c / "docs/lab/lab.toml").exists():
            return c
    raise FileNotFoundError("docs/lab/lab.toml not found; set AWTO_DAN_REPO (tried %s)" % [str(c) for c in cands])


_cache = {}


def load(root=None):
    root = Path(root) if root else dan_repo()
    if root not in _cache:
        with open(root / "docs/lab/lab.toml", "rb") as f:
            _cache[root] = tomllib.load(f)
    return _cache[root]


def unit(name):
    return load()["unit"][name]


def units():
    return list(load()["unit"])


def adapter(unit_name):
    """Serial adapter record of a unit ({chip, host, by_id: [names under /dev/serial/by-id/], src}) or None."""
    return next((a for a in load().get("adapter", []) if a["unit"] == unit_name), None)


def relay(unit_name):
    """ip-registry host name of the unit's power relay, or None."""
    return unit(unit_name).get("relay")


def end(s):
    """'sw1:lan7' -> ('sw1', 'lan7')."""
    u, _, p = s.partition(":")
    return u, p


def cabling(unit_name=None):
    """Cabling rows; with a unit, only rows that have it at one end."""
    rows = load().get("cabling", [])
    return [r for r in rows if unit_name is None or unit_name in (end(r["a"])[0], end(r["b"])[0])]


def peers(unit_name, port, other_unit=None):
    """Far ends ('unit:port') of unit:port, optionally only on other_unit; conflicting rows included."""
    me = "%s:%s" % (unit_name, port)
    out = [r["b"] if r["a"] == me else r["a"] for r in cabling() if me in (r["a"], r["b"])]
    return [p for p in out if other_unit is None or end(p)[0] == other_unit]


def protected(unit_name=None):
    return [p for p in load().get("protected", []) if unit_name is None or p["unit"] == unit_name]


def is_protected(unit_name, port):
    return any(p["port"] == port for p in protected(unit_name))


def hosts(root=None):
    """Every ip-registry host name referenced by lab.toml."""
    d = load(root)
    out = []
    for n, u in d["unit"].items():
        out += [(u[k], "unit.%s.%s" % (n, k)) for k in ("mgmt_host", "boot_host", "relay") if k in u]
    return out


def validate(root=None):
    root = Path(root) if root else dan_repo()
    d = load(root)
    err = []
    us = d.get("unit", {})
    for n, u in us.items():
        for k in ("desc", "kind", "owner", "src"):
            if not u.get(k):
                err.append("unit %s: no %s" % (n, k))
    owners = set()
    for a in d.get("adapter", []):
        if a["unit"] and a["unit"] not in us:
            err.append("adapter %s: unknown unit %r" % (a["by_id"], a["unit"]))
        if not a.get("src") or not a.get("by_id"):
            err.append("adapter %s: missing src/by_id" % a.get("chip"))
        if a["unit"] and a["unit"] in owners:
            err.append("unit %s: two adapters" % a["unit"])
        owners.add(a["unit"])
    ids = [b for a in d.get("adapter", []) for b in a["by_id"]]
    err += ["by-id %s listed twice" % b for b in set(ids) if ids.count(b) > 1]
    ends = set()
    for r in d.get("cabling", []):
        for k in "ab":
            if end(r[k])[0] not in us or not end(r[k])[1]:
                err.append("cabling %s: bad end %r" % (r["a"], r[k]))
            if r[k] in ends and r["state"] != "conflict":
                err.append("cabling %s: end %s reused without state conflict" % (r["a"], r[k]))
            ends.add(r[k])
        if r.get("state") not in STATES:
            err.append("cabling %s-%s: state %r not in %s" % (r["a"], r["b"], r.get("state"), STATES))
        if not r.get("src"):
            err.append("cabling %s-%s: no src" % (r["a"], r["b"]))
        if r.get("state") == "fdb-verified" and not r.get("date"):
            err.append("cabling %s-%s: fdb-verified needs a date" % (r["a"], r["b"]))
    for t in ("module", "protected"):
        for m in d.get(t, []):
            if m["unit"] not in us:
                err.append("%s %s: unknown unit" % (t, m))
            if not m.get("src"):
                err.append("%s %s:%s: no src" % (t, m["unit"], m["port"]))
    with open(root / "docs/network/ip-registry.toml", "rb") as f:
        names = {h["name"] for h in tomllib.load(f)["host"]}
    for h, where in hosts(root):
        if h not in names:
            err.append("%s: host %r not in ip-registry.toml" % (where, h))
    return err


def main(argv):
    import logging
    top = os.environ.get("LAB_LOG_ROOT") or _git("rev-parse", "--show-toplevel") or str(_HERE.parent)
    log = Path(top) / "tmp/logs/lab.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.FileHandler(log), logging.StreamHandler(sys.stdout)])
    cmd = argv[1] if len(argv) > 1 else "show"
    if cmd == "check":
        e = validate()
        for x in e:
            logging.info("ERR %s", x)
        logging.info("%d errors (units %d, cabling %d)", len(e), len(units()), len(cabling()))
        return 1 if e else 0
    if cmd == "show":
        for n in units():
            u = unit(n)
            a = adapter(n)
            logging.info("%s: owner %s, mgmt %s, relay %s, serial %s", n, u["owner"], u.get("mgmt_host"), u.get("relay"),
                         a["by_id"][0] if a else None)
        return 0
    if cmd == "cabling":
        for r in cabling(argv[2] if len(argv) > 2 else None):
            logging.info("%s <-> %s  %s  %s %s (%s)", r["a"], r["b"], r["medium"], r["state"], r.get("date", ""), r["src"])
        return 0
    logging.info(__doc__)
    return 2


if __name__ == "__main__":
    _help_guard(__doc__)
    sys.exit(main(sys.argv))
