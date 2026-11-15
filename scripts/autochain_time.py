#!/usr/bin/env python3
"""autochain_time — at the uboot-1 `u-boot> ` prompt: time `sf probe` + `sf read` of the awto-uboot image (#63).

  scripts/autochain_time.py [--size 842736] [--crc bf43ffdd]

- Candidates: probe default / 12.5 / 25 / 50 MHz, read 0xd0000 vs exact size; each twice. Host timestamps per command.
- Read-only: sf probe/read into 0x01000000 + crc32 compare. Log: tmp/logs/autochain_time.log
- Per-command budget 5 s: 0.85 MB at 100 kHz-class worst case would be longer, but the stock sf read is ~1 s (I);
  expiry = FATAL, stop (no plan B).
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import console  # noqa: E402
import dev  # noqa: E402
from dev import log  # noqa: E402

PROMPT = b"u-boot> "
BUDGET_S = 5.0


def run(s: console.Session, cmd: str) -> tuple[float, str]:
    s.read_idle(0.1, 0.5)
    t0 = time.monotonic()
    s.send_line(cmd)
    while not s.buf.endswith(PROMPT):
        if time.monotonic() - t0 > BUDGET_S:
            raise console.Timeout(f"{cmd}: no prompt in {BUDGET_S} s (tail {s.buf[-120:]!r})")
        s._pump(0.02)
    dt = time.monotonic() - t0
    out, s.buf = s.buf.decode(errors="replace"), b""
    return dt, out


def bootm_time(a: argparse.Namespace) -> int:
    """Typed-ahead chars are lost while sf read runs (16 B UART FIFO), so each command is its own line."""
    s = console.Session()
    try:
        s.write(b"\r\n")
        s.expect([PROMPT], 2.0, "u-boot prompt")
        s.buf = b""
        if a.bootm == "sf":
            for c in ("sf probe 0 50000000", f"sf read 0x01000000 0xe0000 0x{a.size:x}"):
                dt, _ = run(s, c)
                log(f"sf: {c!r} {dt:.3f} s")
        t0 = time.monotonic()
        s.send_line("bootm 0x01000000" if a.bootm == "sf" else "bootm 0x1c0e0000")
        for pat in (b"Verifying Checksum", b"Starting kernel", b"awto-uboot ", b"awto-uboot> "):
            s.expect([pat], 15.0, pat.decode())
            log(f"{a.bootm}: bootm -> {pat.decode()!r} at +{time.monotonic() - t0:.3f} s")
        return 0
    except console.Timeout as e:
        log(str(e), "FATAL")
        return 1
    finally:
        s.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--size", type=lambda x: int(x, 0), default=842736)
    ap.add_argument("--crc", default="bf43ffdd")
    ap.add_argument("--bootm", choices=["sf", "xip"], help="time command -> awto-uboot banner/prompt instead")
    a = ap.parse_args()
    if a.bootm:
        return bootm_time(a)
    cases = [("probe default, read 0xd0000", "sf probe 0", 0xD0000),
             ("probe default, read exact", "sf probe 0", a.size)]
    for hz in (12500000, 25000000, 50000000):
        cases.append((f"probe {hz / 1e6:g} MHz, read exact", f"sf probe 0 {hz}", a.size))
    s = console.Session()
    try:
        s.write(b"\r\n")
        s.expect([PROMPT], 2.0, "u-boot prompt")
        rows = []
        for rep in (1, 2):
            for name, probe, size in cases:
                dp, op = run(s, probe)
                s.buf = b""
                dr, orr = run(s, f"sf read 0x01000000 0xe0000 0x{size:x}")
                s.buf = b""
                dc, oc = run(s, f"crc32 0x01000000 0x{a.size:x}")
                got = oc.split("==>")[-1].split()[0] if "==>" in oc else "?"
                ok = got == a.crc if size >= a.size else None
                rows.append((rep, name, dp, dr, dc, got, ok))
                log(f"rep{rep} {name}: probe {dp:.3f} s, read {dr:.3f} s, crc {got} "
                    f"({'OK' if ok else 'n/a (read > image: crc of first bytes)' if ok is None else 'MISMATCH'})")
                s.buf = b""
        return 0
    except console.Timeout as e:
        log(str(e), "FATAL")
        return 1
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
