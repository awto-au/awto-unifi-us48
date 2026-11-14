#!/usr/bin/env python3
"""u2_state_dump — read back the chip registers the cold-init list writes (cat 2|4 regs) at the awto-uboot prompt.

Usage: scripts/u2_state_dump.py <tag> [--cats 6]   -> tmp/logs/state_<tag>.txt  ("blk addr = w0 [w1]")
- Read-only `schan r`. Compare two states: diff tmp/logs/state_warm.txt tmp/logs/state_cold.txt
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dev  # noqa: E402
import u2_cold  # noqa: E402
from dev import console, log  # noqa: E402

DATA = dev.REPO / "common/bcm-cmic/bcm_cmic_hr2_init_data.h"


def targets(cats: int) -> list[tuple[int, int, int]]:
    seen, out = set(), []
    for ln in DATA.read_text().splitlines():
        m = re.match(r"\s*0x([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2}), 0x([0-9a-f]{8})", ln)
        if not m:
            continue
        cat, op, blk, words, addr = (int(m.group(i), 16) for i in range(1, 6))
        if op == 0x0d and cat & cats and (blk, addr) not in seen:
            seen.add((blk, addr))
            out.append((blk, addr, min(words, 2)))
    return out


def main() -> int:
    tag = sys.argv[1]
    cats = int(sys.argv[sys.argv.index("--cats") + 1]) if "--cats" in sys.argv else 6
    if not dev._ensure_console():
        return 1
    s = console.Session()
    lines = []
    try:
        s.write(b"\x03\r\n")
        s.expect(dev.U2_PROMPTS, 5.0, "awto-uboot prompt")
        for blk, addr, words in targets(cats):
            txt = u2_cold.run(s, f"schan r {blk} 0x{addr:x} {words}", 3.0)
            vals = re.findall(r"\[(\d)\] = 0x([0-9a-f]{8})", txt)
            lines.append(f"{blk} {addr:08x} = " + (" ".join(v for _i, v in vals) if vals else "ERR"))
        path = dev.REPO / f"tmp/logs/state_{tag}.txt"
        path.write_text("\n".join(lines) + "\n")
        log(f"{len(lines)} registers -> {path.relative_to(dev.REPO)}", "ALERT")
        return 0
    except console.Timeout as e:
        log(str(e), "FATAL")
        return 1
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
