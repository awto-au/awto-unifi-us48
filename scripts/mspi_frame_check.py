#!/usr/bin/env python3
"""mspi_frame_check: host test of the MSPI board-IO frames (no board). Compiles old us48_fan.c (own select pins +
spi_write) and new us48_fan.c + us48_boardio against host stubs (kernel/us48-boardio/test), runs one scenario.

Usage: scripts/mspi_frame_check.py [--old-ref 9480d5c]
- Fans: every transfer (select pins A/B, length, bytes) and return code of the new code equals the old one, step by
  step: probe, thermal states 0-3, manual pwm, quiet rule fail level, loud fail level, sensor loss/bogus, floor
  param, write errors + fallback, unbind.
- PoE LEDs: exact frames per step (bit layout docs/board.md), no frame for unchanged values, field 3 / wrong length
  rejected, nothing sent when a select pin does not read back; A=0 frames are always 6 B, A=1 frames always 7 B.
Log: tmp/logs/mspi_frame_check.log. Exit 0 = all checks pass.
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BIO = ROOT / "kernel/us48-boardio"
TEST = BIO / "test"
FAN = ROOT / "kernel/us48-fan/us48_fan.c"
WORK = ROOT / "tmp/mspi_frame_check"
LOG = ROOT / "tmp/logs/mspi_frame_check.log"
CFLAGS = ["-std=gnu11", "-Wall", "-Wno-unused-function", "-Wno-unused-variable", "-Wno-unused-but-set-variable",
          "-Wno-missing-braces"]

Z6 = "00 00 00 00 00 00"
# PoE step -> (expected A=0 transfers, return code); front N -> card (N-1)//24, byte i>>2, bits 7-2(i&3)
POE = {
    "poe led ge1 1": (["A=0 B=0 len=6: 40 00 00 00 00 00"], 0),
    "poe led ge24 1": (["A=0 B=0 len=6: 40 00 00 00 00 01"], 0),
    "poe led ge1 0": (["A=0 B=0 len=6: 00 00 00 00 00 01"], 0),
    "poe led ge24 0": ([f"A=0 B=0 len=6: {Z6}"], 0),
    "poe led ge25 1": (["A=0 B=1 len=6: 40 00 00 00 00 00"], 0),
    "poe led ge48 1": (["A=0 B=1 len=6: 40 00 00 00 00 01"], 0),
    "poe led ge48 1 unchanged": ([], 0),
    "poe led ge48 brightness 255 clamps to 1": ([], 0),
    "poe pattern all 2": (["A=0 B=0 len=6: aa aa aa aa aa aa", "A=0 B=1 len=6: aa aa aa aa aa aa"], 48),
    "poe pattern with 3": ([], -22),
    "poe pattern 47 digits": ([], -22),
    "poe pattern front 1 = 2": (["A=0 B=0 len=6: 80 00 00 00 00 00", f"A=0 B=1 len=6: {Z6}"], 48),
    "poe pattern off": ([f"A=0 B=0 len=6: {Z6}"], 4),  # sysfs store returns the length
    "poe fan frame between": ([], 0),
    "poe led ge2 1 after fan": (["A=0 B=0 len=6: 10 00 00 00 00 00"], 0),
    "poe fan frame before stuck A": ([], 0),
    "poe stuck A led ge3 1": ([], -5),
    "poe unstuck A led ge3 1": (["A=0 B=0 len=6: 14 00 00 00 00 00"], 0),
    "poe stuck B led ge26 1": ([], -5),
    "poe unstuck B led ge26 1": (["A=0 B=1 len=6: 10 00 00 00 00 00"], 0),
}


def log(fh, msg):
    print(msg, flush=True)
    fh.write(msg + "\n")


def run(fh, cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    fh.write(f"$ {' '.join(map(str, cmd))}\n{r.stdout}{r.stderr}")
    if r.returncode:
        raise SystemExit(f"FAILED rc={r.returncode}: {' '.join(map(str, cmd))}\n{r.stderr[-2000:]}")
    return r.stdout


def fake_headers(sources):
    """Empty <linux/...> headers: every kernel API comes from stub.h (force-included)."""
    inc = WORK / "inc"
    for src in sources:
        for h in re.findall(r"#include <(linux/[^>]+)>", src.read_text()):
            p = inc / h
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("")
    return inc


def steps(out):
    """stdout -> {step: (transfer lines, ret)} in order"""
    res, cur = {}, None
    for line in out.splitlines():
        if line.startswith("# "):
            cur = line[2:]
            res[cur] = ([], None)
        elif line.startswith("xfer ") and cur:
            res[cur][0].append(line[5:])
        elif line.startswith("ret ") and cur:
            res[cur] = (res[cur][0], int(line.rsplit("=", 1)[1]))
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--old-ref", default="9480d5c", help="commit with the pre-boardio us48_fan.c")
    a = ap.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    fails = 0
    with LOG.open("w") as fh:
        old_src = WORK / "us48_fan_old.c"
        old_src.write_text(run(fh, ["git", "-C", str(ROOT), "show", f"{a.old_ref}:kernel/us48-fan/us48_fan.c"]))
        inc = fake_headers([old_src, FAN, BIO / "us48_boardio_core.c", BIO / "us48_poe_led.c"])
        base = ["gcc", *CFLAGS, "-include", str(TEST / "stub.h"), "-I", str(inc), "-I", str(BIO)]
        run(fh, base + [f'-DFAN_C="{old_src}"', str(TEST / "h_fan.c"), str(TEST / "stub.c"), str(TEST / "main.c"),
                        "-o", str(WORK / "old")])
        run(fh, base + ["-DNEW", f'-DFAN_C="{FAN}"', f'-DPOE_C="{BIO / "us48_poe_led.c"}"', str(TEST / "h_fan.c"),
                        str(TEST / "h_poe.c"), str(BIO / "us48_boardio_core.c"), str(TEST / "stub.c"),
                        str(TEST / "main.c"), "-o", str(WORK / "new")])
        old, new = steps(run(fh, [str(WORK / "old")])), steps(run(fh, [str(WORK / "new")]))

        # 1. fans: byte-identical transfers (A=1) and return codes per step
        nfan = 0
        for st, (ox, oret) in old.items():
            nx, nret = new.get(st, (None, None))
            if nx is None:
                log(fh, f"FAIL fan step {st!r} missing in new")
                fails += 1
                continue
            nx = [x for x in nx if x.startswith("A=1")]
            nfan += len(ox)
            if ox != nx or oret != nret:
                log(fh, f"FAIL fan step {st!r}: old {ox} ret {oret} / new {nx} ret {nret}")
                fails += 1
        log(fh, f"fan: {len(old)} steps, {nfan} transfers compared old vs new")

        # 2. chain safety over every new transfer
        for st, (xs, _) in new.items():
            for x in xs:
                bad = (x.startswith("A=0") and "len=6" not in x) or (x.startswith("A=1") and "len=7" not in x)
                if bad:
                    log(fh, f"FAIL chain/length mismatch in {st!r}: {x}")
                    fails += 1
        probe0 = [x for x in new["probe"][0] if x.startswith("A=0")]
        if probe0 != [f"A=0 B=0 len=6: {Z6}", f"A=0 B=1 len=6: {Z6}"]:
            log(fh, f"FAIL probe PoE zero frames: {probe0}")
            fails += 1
        if new["probe"][0][:2] != [x for x in old["probe"][0]][:2]:
            log(fh, "FAIL probe: fan frames are not the first transfers")
            fails += 1

        # 3. PoE LED frames per step
        for st, (want, wret) in POE.items():
            xs, ret = new.get(st, (None, None))
            if xs is None:
                log(fh, f"FAIL poe step {st!r} missing")
                fails += 1
                continue
            got = [x for x in xs if x.startswith("A=0")]
            if got != want or ret != wret:
                log(fh, f"FAIL {st}: got {got} ret {ret}, want {want} ret {wret}")
                fails += 1
        log(fh, f"poe: {len(POE)} steps checked")
        log(fh, "PASS" if not fails else f"{fails} FAIL")
    return 1 if fails else 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
