#!/usr/bin/env python3
"""flash_guard - refuse a U-Boot/Linux flash-write command that targets a protected region (#139).

Usage: flash_guard.py --selftest | "<console line>" ...
- Regions: hwdb/board/flash.toml (protect = true): u-boot, u-boot-env, shmoo, kernel1, bootsel 0x1ee0000, EEPROM.
- Checked: sf write/update/erase (hex offsets), saveenv, env save, fw_setenv, ubnt EEPROM setters, flashcp/flash_erase/
  dd of=/dev/mtdN. Unparseable sf ranges are refused (fail closed). Reads are never touched.
- Called by console.Session.send_line (refuses, raises FlashGuardError) and console_mon (alert only: typed by hand).
- Allow a region on purpose: flash_guard.allow("u-boot-env") or US48_FLASH_ALLOW=kernel1,... (logged).
- Log: tmp/logs/flash_guard.log
"""
import os
import re
import sys
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/flash_guard.log"
MAP = ROOT / "hwdb/board/flash.toml"
EEPROM_CMDS = ("usetbid", "usetbrev", "usetmac", "uclearcal", "usetsshkey")  # docs/uboot.md s9: EEPROM read-modify-write
_allowed: set[str] = set()


class FlashGuardError(Exception):
    pass


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(line + "\n")


def regions() -> list[dict]:
    with MAP.open("rb") as fh:
        return tomllib.load(fh)["region"]


def allow(name: str) -> None:
    log(f"allow {name}")
    _allowed.add(name)


def _allowed_now() -> set[str]:
    return _allowed | {x for x in os.environ.get("US48_FLASH_ALLOW", "").split(",") if x}


def _hit(name: str, what: str) -> None:
    if name not in _allowed_now():
        raise FlashGuardError(f"REFUSED: {what}: writes protected region {name} (hwdb/board/flash.toml)")


def check_range(off: int, size: int, what: str) -> None:
    if size <= 0:
        raise FlashGuardError(f"REFUSED: {what}: empty range")
    for r in regions():
        if r["protect"] and off < r["start"] + r["size"] and off + size > r["start"]:
            _hit(r["name"], f"{what} ({off:#x}+{size:#x})")


def check_mtd(dev: str, what: str) -> None:
    m = re.fullmatch(r"/dev/mtd(?:block)?(\d+)(?:ro)?", dev)
    if not m:
        raise FlashGuardError(f"REFUSED: {what}: unknown mtd device {dev!r} (fail closed)")
    for r in regions():
        if r.get("mtd") == int(m.group(1)) and r["protect"]:
            _hit(r["name"], what)
            if r["name"] == "kernel1":
                _hit("bootsel", what)


def _hex(tok: str) -> int:
    return int(tok.lstrip("+"), 16)


def _check_segment(seg: str) -> None:
    t = seg.split()
    if not t:
        return
    what = seg.strip()
    if t[0] == "sf" and len(t) > 1 and t[1] in ("write", "update", "erase"):
        a = t[2:]
        try:
            off, size = (_hex(a[1]), _hex(a[2])) if t[1] != "erase" else (_hex(a[0]), _hex(a[1]))
        except (IndexError, ValueError):
            raise FlashGuardError(f"REFUSED: {what}: sf range not parseable (fail closed)") from None
        check_range(off, size, what)
    elif t[0] == "saveenv" or t[:2] == ["env", "save"] or t[0] == "fw_setenv":
        _hit("u-boot-env", what)
    elif t[0] in EEPROM_CMDS:
        _hit("EEPROM", what)
    elif t[0] in ("flashcp", "flash_erase", "flash_eraseall", "nandwrite"):
        for d in [x for x in t[1:] if x.startswith("/dev/mtd")]:
            check_mtd(d, what)
    for m in re.finditer(r"\bof=(/dev/mtd\S*)", seg):
        check_mtd(m.group(1), what)


def check_cmd(line: str) -> None:
    """Raise FlashGuardError if any command in `line` writes a protected region."""
    for seg in re.split(r"[;\r\n|&]+", line):
        _check_segment(seg)


_SCAN = re.compile(r"\b(sf (?:write|update|erase)\b.*|saveenv\b.*|env save\b.*|fw_setenv\b.*|"
                   r"(?:usetbid|usetbrev|usetmac|uclearcal|usetsshkey)\b.*|(?:flashcp|flash_erase)\b.*)")


def scan_line(line: str) -> str | None:
    """Monitor use: echoed console line (prompt prefix allowed). Returns the refusal text, or None."""
    m = _SCAN.search(line)
    if not m:
        return None
    try:
        check_cmd(m.group(1))
    except FlashGuardError as e:
        return str(e)
    return None


def selftest() -> int:
    ok = ["sf probe 0", "sf read 0x2000000 0xe0000 0x1000", "sf erase 200000 d00000",
          "sf write 0x1000000 0x200000 0xd00000", "sf erase 0xe0000 0x100000", "dd if=/dev/mtd4 of=/tmp/x",
          "flashcp x /dev/mtd3", "sf update 0x2000000 0xbe0000 0x400000", "setenv bootcmd 'sf probe 0'"]
    bad = ["sf erase 0xc0000 0x10000", "sf write 0x1000000 0x1ee0000 0x30", "sf write 0x1000000 0xfe0000 0x100000",
           "sf erase 0x1ff0000 0x10000", "sf update 0x2000000 0x0 0x20000", "sf erase 0xd0000 0x10000", "saveenv",
           "env save", "fw_setenv bootcmd x", "usetmac 00:11:22:33:44:55", "sf write 0x1000000 zz 10", "sf erase 0x0",
           "flashcp x /dev/mtd4", "dd if=x of=/dev/mtdblock1", "sf probe 0; sf erase 0xc0000 0x1000",
           "sf erase 0xdf000 0x2000"]
    fails = 0
    for c in ok:
        try:
            check_cmd(c)
        except FlashGuardError as e:
            fails += 1
            log(f"FAIL allowed line refused: {c!r}: {e}")
    for c in bad:
        try:
            check_cmd(c)
            fails += 1
            log(f"FAIL not refused: {c!r}")
        except FlashGuardError:
            pass
    allow("u-boot-env")
    try:
        check_cmd("sf erase 0xc0000 0x10000")
    except FlashGuardError:
        fails += 1
        log("FAIL allow(u-boot-env) did not permit the env write")
    _allowed.clear()
    if scan_line("=> sf erase 0xc0000 0x10000") is None or scan_line("=> sf read 0 0 1") is not None:
        fails += 1
        log("FAIL scan_line")
    n = len(ok) + len(bad) + 2
    log(f"{'PASS' if not fails else 'FAIL'} flash_guard selftest: {n - fails}/{n}")
    return 1 if fails else 0


if __name__ == "__main__":
    help_guard(__doc__)
    if sys.argv[1:] == ["--selftest"]:
        sys.exit(selftest())
    rc = 0
    for arg in sys.argv[1:]:
        try:
            check_cmd(arg)
            log(f"ok: {arg}")
        except FlashGuardError as e:
            log(str(e))
            rc = 1
    sys.exit(rc)
