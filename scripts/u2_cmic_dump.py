#!/usr/bin/env python3
"""u2_cmic_dump — dump unit-0 CMIC register ranges at the awto-uboot prompt to tmp/logs/cmic_<tag>.txt (offset=value).

Usage: scripts/u2_cmic_dump.py <tag>      (board at the awto-uboot prompt)
- Read-only `cmic r`. Ranges: cpureg 0x10000-0x14000, 0x1a000-0x1a200, CMC0 0x31000-0x31600.
- Compare two tags with: diff tmp/logs/cmic_warm.txt tmp/logs/cmic_cold.txt
"""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dev  # noqa: E402
import u2_cold  # noqa: E402
from dev import console, log  # noqa: E402

RANGES = [(0x10000, 0x14000), (0x1a000, 0x1a200), (0x31000, 0x31600)]
CHUNK = 64  # words per command: keeps each reply short


def main() -> int:
    tag = sys.argv[1]
    if not dev._ensure_console():
        return 1
    s = console.Session()
    out = {}
    try:
        s.write(b"\x03\r\n")
        s.expect(dev.U2_PROMPTS, 5.0, "awto-uboot prompt")
        for lo, hi in RANGES:
            for off in range(lo, hi, CHUNK * 4):
                n = min(CHUNK, (hi - off) // 4)
                txt = u2_cold.run(s, f"cmic r 0x{off:x} {n}", 4.0)
                for m in re.finditer(r"cmic 0 0x([0-9a-f]+) = 0x([0-9a-f]+)", txt):
                    out[int(m.group(1), 16)] = m.group(2)
        path = dev.REPO / f"tmp/logs/cmic_{tag}.txt"
        path.write_text("".join(f"{k:06x}={v}\n" for k, v in sorted(out.items())))
        log(f"{len(out)} registers -> {path.relative_to(dev.REPO)}", "ALERT")
        return 0
    except console.Timeout as e:
        log(str(e), "FATAL")
        return 1
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
