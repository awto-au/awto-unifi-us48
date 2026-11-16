#!/usr/bin/env python3
"""u2_cmicregs — read named CMIC registers (regdb cpureg, blk 6) at the awto-uboot prompt into a file (warm vs cold diff, #46).

Usage: scripts/u2_cmicregs.py OUTFILE [NAME_REGEX]   (default regex: ^CMIC_(RXBUF|TXBUF))
- Only offsets listed in regdb/hr2_regs.tsv: reading undefined CMIC offsets hangs the AXI bus (0x11000 block, 2026-11-15) and the SP805 resets the board.
- One line per register: name offset value. Log: tmp/logs/u2_cmicregs.log
"""
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dev  # noqa: E402
import u2_cold  # noqa: E402
from dev import console, log  # noqa: E402
from _cli import help_guard  # noqa: E402


def main() -> int:
    out = Path(sys.argv[1])
    pat = re.compile(sys.argv[2] if len(sys.argv) > 2 else r"^CMIC_(RXBUF|TXBUF)")
    regs = [(r["name"], int(r["offset"], 16)) for r in csv.DictReader((dev.REPO / "regdb/hr2_regs.tsv").open(), delimiter="\t")
            if r["regtype_name"] == "cpureg" and pat.search(r["name"]) and int(r["numels"]) == 1]
    regs.sort(key=lambda x: x[1])
    if not dev._ensure_console() or not console.restart(console.BAUD):
        return 1
    s = console.Session()
    lines = []
    try:
        s.write(b"\x03\r\n")
        s.expect(dev.U2_PROMPTS, 5.0, "awto-uboot prompt")
        for name, off in regs:
            r = u2_cold.run(s, f"cmic r 0x{off:x} 1", 3.0)
            m = re.search(r"cmic 0 0x[0-9a-f]+ = (0x[0-9a-f]{8})", r)
            if not m:
                log(f"{name} 0x{off:x}: no value (reg {r[-100:]!r})", "FATAL")
                return 1
            lines.append(f"{name} 0x{off:05x} {m.group(1)}")
        out.write_text("\n".join(lines) + "\n")
        log(f"{len(lines)} registers -> {out}", "ALERT")
        return 0
    except console.Timeout as e:
        log(str(e), "FATAL")
        return 1
    finally:
        s.close()


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
