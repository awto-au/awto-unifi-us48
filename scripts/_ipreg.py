#!/usr/bin/env python3
"""_ipreg — look up house LAN addresses by host name in awto-dan docs/network/ip-registry.toml.

Usage: from _ipreg import ip;  ip("us48-boot") -> "192.168.25.207"
       _ipreg.py [NAME ...]    print name -> address (all names when none given)
Override one name: $US48_IP_<NAME upper, '-' -> '_'>, e.g. US48_IP_US48_BOOT=192.168.25.209.
Registry path: $AWTO_IP_REGISTRY, else /mnt/2tb/git/awto-dan/docs/network/ip-registry.toml. Missing file or name = error, no fallback.
"""
import os
import sys
import tomllib
from pathlib import Path

REGISTRY = Path(os.environ.get("AWTO_IP_REGISTRY", "/mnt/2tb/git/awto-dan/docs/network/ip-registry.toml"))
_cache: dict[str, str] | None = None


def table() -> dict[str, str]:
    global _cache
    if _cache is None:
        with REGISTRY.open("rb") as fh:
            _cache = {h["name"]: h["ip"] for h in tomllib.load(fh).get("host", [])}
    return _cache


def ip(name: str) -> str:
    # Override for a down path (e.g. US48_IP_US48_BOOT=192.168.25.209 when ge1 has no link, #145).
    over = os.environ.get("US48_IP_" + name.upper().replace("-", "_"))
    if over:
        return over
    try:
        return table()[name]
    except KeyError:
        raise KeyError(f"{name!r} not in {REGISTRY}") from None


if __name__ == "__main__":
    if "-h" in sys.argv[1:] or "--help" in sys.argv[1:]:
        print(__doc__.strip())
        sys.exit(0)
    for n in sys.argv[1:] or sorted(table()):
        print(f"{n} {ip(n)}")
