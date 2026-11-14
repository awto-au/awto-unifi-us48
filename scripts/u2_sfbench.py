#!/usr/bin/env python3
"""u2_sfbench.py — time `sf read` at the uboot-2 prompt per probe mode (#20). Read-only: no erase/write.

Usage: u2_sfbench.py OFFSET_HEX LEN_HEX "HZ MODE_HEX" ["HZ MODE_HEX" ...]   (mode: 3 = 1-lane, 2003 = RX quad)
Log: tmp/logs/u2_sfbench.log. Each read is timed from command send to the next prompt; crc32 of the RAM copy printed.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import console  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
LOGF = REPO / "tmp/logs/u2_sfbench.log"
PROMPT = b"awto-uboot> "
# 14 MiB at the measured ~1.8 MB/s = 8 s; 20 s = 2.5x covers the slot read, expiry = hung read, power-cycle.
BUDGET_S = 30.0


def out(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    print(line, flush=True)
    LOGF.parent.mkdir(parents=True, exist_ok=True)
    with LOGF.open("a") as f:
        f.write(line + "\n")


def run(s: console.Session, cmd: str, budget: float = BUDGET_S) -> tuple[float, str]:
    s.buf = b""
    t0 = time.monotonic()
    s.send_line(cmd)
    while PROMPT not in s.buf:
        if time.monotonic() - t0 > budget:
            raise console.Timeout(f"{cmd}: no prompt in {budget:.1f} s (tail {s.buf[-120:]!r})")
        s._pump(0.05)
    return time.monotonic() - t0, s.buf.decode(errors="replace")


def main() -> int:
    off, ln = sys.argv[1], sys.argv[2]
    nbytes = int(ln, 16)
    with console.Session() as s:
        for spec in sys.argv[3:]:
            if spec.startswith("!"):  # raw command, timed (e.g. "!cp.l 1c1e0000 04000000 40000")
                dt, txt = run(s, spec[1:])
                out(f"'{spec[1:]}': {dt:.2f} s = {nbytes / dt / 1e6:.2f} MB/s over {ln} B; {txt.strip()[-60:]!r}")
                continue
            run(s, f"sf probe 0 {spec}", 10.0)
            for ram in ("0x02000000", "0x03000000"):
                dt, txt = run(s, f"sf read {ram} {off} {ln}")
                _dt2, crc = run(s, f"crc32 {ram} {ln}", BUDGET_S)
                out(f"probe '{spec}' read {ln}@{off} -> {ram}: {dt:.2f} s = {nbytes / dt / 1e6:.2f} MB/s; "
                    f"{'OK' if 'Read: OK' in txt else txt.strip()[-80:]!r}; {crc.split("==>")[-1].split()[0] if "==>" in crc else "no-crc"}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
