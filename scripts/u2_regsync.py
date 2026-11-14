#!/usr/bin/env python3
"""u2_regsync — #46: register diff warm vs cold (`hr2reg`) and replay of the cold-init write list on a warm chip (`hr2init`).

Usage:
  scripts/u2_regsync.py warm                    awto-uboot on a WARM chip: ping check, `hr2reg dump`, image -> flash 0xF00000 (inside kernel0), crc verify
  scripts/u2_regsync.py cold [--no-cycle|--here] [--sync IDX[,IDX..]]
                                                power-cycle -> awto-uboot (no stock) -> ping (cold init) -> sf read image -> `hr2reg diff` (log: tmp/logs/regdiff_cold.txt);
                                                --sync: write the warm value of those instance indexes, then ping + RXPKT
  scripts/u2_regsync.py replay MASK [FIRST LAST] [--xmask N]
                                                WARM chip at awto-uboot: ping must be alive, `hr2init MASK [FIRST LAST]`, ping + RXPKT
- Stall guard on every console command: no new output for 30 s = hung -> stop. Log: tmp/logs/u2_regsync.log
"""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dev  # noqa: E402
import u2_bisect  # noqa: E402
import u2_cold  # noqa: E402
import u2_memsync as ms  # noqa: E402
from dev import console, log  # noqa: E402

RAM_IMG = 0x05800000
RAM_BACK = 0x06800000
FLASH_IMG = 0xF00000          # kernel0 window 0xE0000..0xFE0000; memory image ends below 0xD20000
FLASH_MAX = 0xE0000
LEN_FILE = dev.REPO / "tmp/regimg.len"
DIFF_TXT = dev.REPO / "tmp/logs/regdiff_cold.txt"
HOST = ms.HOST


def probe(s, tag: str, budget: float = u2_bisect.PING_S) -> bool:
    """Ping alive only: RXPKT is cumulative on a warm chip, so a nonzero count proves nothing."""
    out = u2_cold.run(s, f"ping {HOST}", budget)
    alive = "is alive" in out
    if not any(p in s.buf for p in dev.U2_PROMPTS):
        s.write(b"\x03\r\n")
        s.expect(dev.U2_PROMPTS, 5.0, "prompt after Ctrl-C")
    rx = u2_cold.run(s, "cmic r 0x31488 1").split("=")[-1].split()[0]
    log(f"[{tag}] ping alive={alive} RXPKT={rx}", "ALERT")
    return alive


def warm(s) -> int:
    run = ms.run
    run(s, f"setenv ipaddr {dev.U2_IP}; setenv serverip {HOST}")
    if not probe(s, "warm baseline", 30.0):
        log("warm chip does not ping: not dumping", "FATAL")
        return 1
    out = run(s, f"hr2reg dump 0x{RAM_IMG:x}")
    m = re.search(r"hr2reg dump: (\d+) bytes .* crc32 0x([0-9a-f]{8}), (\d+) read errors", out)
    if not m:
        log(f"no dump summary: {out[-200:]!r}", "FATAL")
        return 1
    size, dcrc = int(m.group(1)), m.group(2)
    log(f"reg dump {size} B crc {dcrc}, {m.group(3)} read errors", "ALERT")
    if size > FLASH_MAX:
        return 1
    erase = (size + 0xFFFF) & ~0xFFFF
    for cmd in ("sf probe 0", f"sf erase {FLASH_IMG:x} {erase:x}", f"sf write 0x{RAM_IMG:x} {FLASH_IMG:x} {size:x}",
                f"sf read 0x{RAM_BACK:x} {FLASH_IMG:x} {size:x}"):
        r = run(s, cmd, stall=60.0)
        log(f"{cmd}: {' '.join(r.split()[1:])[:100]}", "WARN")
    back = ms.crc(s, RAM_BACK, size)
    ok = back == dcrc
    log(f"flash read-back crc {back} vs {dcrc}: {'OK' if ok else 'MISMATCH'}", "ALERT" if ok else "FATAL")
    if ok:
        LEN_FILE.write_text(f"{size}\n")
    return 0 if ok else 1


def cold(s, a: list[str]) -> int:
    size = int(LEN_FILE.read_text())
    ms.run(s, f"setenv ipaddr {dev.U2_IP}; setenv serverip {HOST}")
    s.buf = b""
    s.send_line(f"ping {HOST}")
    t0 = time.monotonic()
    while not any(p in s.buf for p in dev.U2_PROMPTS):
        s._pump(0.05)
        if time.monotonic() - t0 > 30.0:
            s.write(b"\x03\r\n")
            s.expect(dev.U2_PROMPTS, 5.0, "prompt after Ctrl-C")
            break
    log(f"cold ping: {' '.join(s.buf.decode(errors='replace').split())[:200]}", "ALERT")
    ms.run(s, "sf probe 0", stall=60.0)
    ms.run(s, f"sf read 0x{RAM_IMG:x} {FLASH_IMG:x} {size:x}", stall=60.0)
    log(f"RAM image crc {ms.crc(s, RAM_IMG, size)} ({size} B)", "ALERT")
    out = ms.run(s, f"hr2reg diff 0x{RAM_IMG:x}")
    DIFF_TXT.parent.mkdir(parents=True, exist_ok=True)
    DIFF_TXT.write_text(out)
    lines = [ln for ln in out.splitlines() if ln.startswith("REG ")]
    log(f"diff: {len(lines)} registers differ ({sum('(ctr)' in ln for ln in lines)} counters) -> {DIFF_TXT}", "ALERT")
    for ln in out.splitlines():
        if "hr2reg" in ln:
            log(ln.strip(), "ALERT")
    if "--sync" in a:
        for idx in a[a.index("--sync") + 1].split(","):
            r = ms.run(s, f"hr2reg sync 0x{RAM_IMG:x} {idx} {idx}")
            log(f"sync {idx}: {' | '.join(x.strip() for x in r.splitlines() if x.startswith(('REG', 'hr2reg')))}", "ALERT")
        probe(s, "after reg sync")
    return 0


def replay(s, a: list[str]) -> int:
    args = [x for x in a[1:] if not x.startswith("--")]
    mask = args[0]
    rng = args[1:3]
    ms.run(s, f"setenv ipaddr {dev.U2_IP}; setenv serverip {HOST}")
    if not probe(s, "warm baseline (before replay)", 30.0):
        log("warm chip does not ping: replay not run", "FATAL")
        return 1
    r = ms.run(s, f"hr2init {mask} {' '.join(rng)}", stall=30.0, overall=120.0)
    log(f"hr2init {mask} {' '.join(rng)}: {' '.join(r.split()[1:])[:160]}", "ALERT")
    if "--xmask" in a:
        r = ms.run(s, f"hr2x {a[a.index('--xmask') + 1]}", stall=30.0, overall=120.0)
        log(f"hr2x: {' '.join(r.split()[1:])[:160]}", "ALERT")
    ok = probe(s, f"after replay mask {mask} {' '.join(rng)}")
    return 0 if ok else 3


def main() -> int:
    a = sys.argv[1:]
    if not a or a[0] not in ("warm", "cold", "replay"):
        print(__doc__)
        return 2
    if not dev._ensure_console() or not console.restart(console.BAUD):
        return 1
    s = console.Session()
    try:
        if a[0] != "cold" or "--here" in a:
            s.write(b"\x03\r\n")
            s.expect(dev.U2_PROMPTS, 5.0, "awto-uboot prompt")
            return {"warm": lambda: warm(s), "replay": lambda: replay(s, a), "cold": lambda: cold(s, a)}[a[0]]()
        if "--no-cycle" not in a and not u2_cold.cycle_to_prompt(s):
            return 1
        _n, off, length, _i = dev._slot(["--slot", "uboot"])
        for cmd in ("sf probe 0", f"sf read {dev.LOAD_ADDR} {dev.KERNEL0_OFF + off:x} {length:x}"):
            if dev._uboot_cmd(s, cmd, dev.PROMPT, dev.SF_BUDGET_S, cmd) is None:
                return 1
        if dev._bootm_watch(s, "uboot"):
            return 1
        return cold(s, a)
    except console.Timeout as e:
        log(str(e), "FATAL")
        return 1
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
