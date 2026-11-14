#!/usr/bin/env python3
"""u2_cmicrange — dump CMIC register ranges at the awto-uboot prompt to a file (for warm vs cold diff, #46).

Usage: scripts/u2_cmicrange.py OUTFILE   (board at the awto-uboot prompt)
- Ranges: 0x10000-0x13000 (SBUS/ring/rate), 0x1a000-0x1b000 (RXBUF/EP credits), 0x31000-0x32000 (CMC0). 64 words per `cmic r`.
- Log: tmp/logs/u2_cmicrange.log
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dev  # noqa: E402
import u2_cold  # noqa: E402
from dev import console, log  # noqa: E402

RANGES = [(0x10000, 0x13000), (0x1a000, 0x1b000), (0x31000, 0x32000)]


def main() -> int:
    out = Path(sys.argv[1])
    if not dev._ensure_console() or not console.restart(console.BAUD):
        return 1
    s = console.Session()
    lines = []
    try:
        s.write(b"\x03\r\n")
        s.expect(dev.U2_PROMPTS, 5.0, "awto-uboot prompt")
        for lo, hi in RANGES:
            for a in range(lo, hi, 256):
                r = u2_cold.run(s, f"cmic r 0x{a:x} 64", 6.0)
                if "NO PROMPT" in r:
                    log(f"stall at 0x{a:x}: {r[-120:]}", "FATAL")
                    return 1
                lines += [ln.strip() for ln in r.splitlines() if ln.startswith("cmic 0 0x")]
        out.write_text("\n".join(lines) + "\n")
        log(f"{len(lines)} words -> {out}", "ALERT")
        return 0
    except console.Timeout as e:
        log(str(e), "FATAL")
        return 1
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
