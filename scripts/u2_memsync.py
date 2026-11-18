#!/usr/bin/env python3
"""u2_memsync — #46 option A: chip memory image from a warm (stock-initialised) chip, diff/sync on a cold chip.

Usage:
  scripts/u2_memsync.py warm                  board at the awto-uboot prompt on a WARM chip: `hr2mem dump`, write the image to
                                              flash kernel0 @0xBE0000 (inside kernel0, above the Linux slot's 10 MB image), verify crc
  scripts/u2_memsync.py cold [--sync] [--range A B] [--no-cycle | --here]
                                              power-cycle -> awto-uboot (no stock) -> ping (cold init) -> sf read image -> `hr2mem diff`
                                              (or `sync`), then ping + CMIC ch1 RXPKT
- Image length: tmp/memimg.len (written by `warm`). Raw image copy is not kept off the board (RAM -> flash only).
- Stall guard on every console command: no new output for 30 s = hung -> stop. Log: tmp/logs/u2_memsync.log
"""
import _ipreg  # noqa: E402
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dev  # noqa: E402
import u2_cold  # noqa: E402
from dev import console, log  # noqa: E402
from _cli import help_guard  # noqa: E402

RAM_IMG = 0x05000000
RAM_BACK = 0x06000000
FLASH_IMG = 0xBE0000          # kernel0 = 0xE0000..0xFE0000; Linux slot image (<= 10.2 MB) ends 0xB90000
FLASH_MAX = 0x400000
LEN_FILE = dev.REPO / "tmp/memimg.len"
STALL_S = 30.0                # a table prints within ~1 s at S-channel speed (<100 us/op x <=16k entries); 30 s = hung
OVERALL_S = 900.0
HOST = _ipreg.ip("selwyn-enp7s0")


def run(s, cmd: str, stall: float = STALL_S, overall: float = OVERALL_S) -> str:
    s.buf = b""
    s.send_line(cmd)
    t0 = last = time.monotonic()
    n = 0
    while not any(p in s.buf for p in dev.U2_PROMPTS):
        s._pump(0.05)
        now = time.monotonic()
        if len(s.buf) != n:
            n, last = len(s.buf), now
        if now - last > stall or now - t0 > overall:
            log(f"`{cmd}`: stalled {now - last:.0f} s / elapsed {now - t0:.0f} s, last: {s.buf[-200:]!r}", "FATAL")
            raise console.Timeout(cmd)
    return s.buf.decode(errors="replace")


def crc(s, addr: int, size: int) -> str:
    m = re.search(r"==> ([0-9a-f]{8})", run(s, f"crc32 0x{addr:x} 0x{size:x}"))
    return m.group(1) if m else "?"


def warm(s) -> int:
    t0 = time.monotonic()
    out = run(s, f"hr2mem dump 0x{RAM_IMG:x}")
    for ln in out.splitlines():
        if "nonzero" in ln or "hr2mem" in ln:
            log(ln.strip(), "ALERT")
    m = re.search(r"hr2mem dump: (\d+) bytes .* crc32 0x([0-9a-f]{8}), (\d+) read errors", out)
    if not m:
        log("no dump summary", "FATAL")
        return 1
    size, dcrc = int(m.group(1)), m.group(2)
    log(f"dump {size} B crc {dcrc}, {time.monotonic() - t0:.1f} s incl. console", "ALERT")
    if size > FLASH_MAX:
        log(f"image {size} > {FLASH_MAX}", "FATAL")
        return 1
    erase = (size + 0xFFFF) & ~0xFFFF
    for cmd in ("sf probe 0", f"sf erase {FLASH_IMG:x} {erase:x}", f"sf write 0x{RAM_IMG:x} {FLASH_IMG:x} {size:x}",
                f"sf read 0x{RAM_BACK:x} {FLASH_IMG:x} {size:x}"):
        t1 = time.monotonic()
        r = run(s, cmd, stall=60.0)
        log(f"{cmd}: {' '.join(r.split()[1:])[:100]} ({time.monotonic() - t1:.1f} s)", "WARN")
    back = crc(s, RAM_BACK, size)
    ok = back == dcrc
    log(f"flash read-back crc {back} vs dump {dcrc}: {'OK' if ok else 'MISMATCH'}", "ALERT" if ok else "FATAL")
    if ok:
        LEN_FILE.write_text(f"{size}\n")
    return 0 if ok else 1


def cold(s, a: list[str]) -> int:
    size = int(LEN_FILE.read_text())
    rng = a[a.index("--range") + 1: a.index("--range") + 3] if "--range" in a else []
    run(s, f"setenv ipaddr {dev.U2_IP}; setenv serverip {HOST}")
    s.buf = b""
    s.send_line(f"ping {HOST}")                  # cold init (24.5k ops) + AN wait; the ping itself fails (RX 0)
    t0 = time.monotonic()
    while not any(p in s.buf for p in dev.U2_PROMPTS):
        s._pump(0.05)
        if time.monotonic() - t0 > 30.0:
            s.write(b"\x03\r\n")
            s.expect(dev.U2_PROMPTS, 5.0, "prompt after Ctrl-C")
            break
    log(f"cold ping: {' '.join(s.buf.decode(errors='replace').split())[:200]}", "ALERT")
    run(s, "sf probe 0", stall=60.0)
    log(f"sf read image: {' '.join(run(s, f'sf read 0x{RAM_IMG:x} {FLASH_IMG:x} {size:x}', stall=60.0).split()[1:])[:100]}", "WARN")
    log(f"RAM image crc {crc(s, RAM_IMG, size)} ({size} B)", "ALERT")
    verb = "sync" if "--sync" in a else "diff"
    out = run(s, f"hr2mem {verb} 0x{RAM_IMG:x} " + " ".join(rng))
    for ln in out.splitlines():
        if "differ" in ln or "hr2mem" in ln or "mismatch" in ln:
            log(ln.strip(), "ALERT")
    if verb == "sync":
        s.buf = b""
        s.send_line(f"ping {HOST}")
        t1 = time.monotonic()
        while not any(p in s.buf for p in dev.U2_PROMPTS) and time.monotonic() - t1 < 6.0:
            s._pump(0.05)
        alive = "is alive" in s.buf.decode(errors="replace")
        if not any(p in s.buf for p in dev.U2_PROMPTS):
            s.write(b"\x03\r\n")
            s.expect(dev.U2_PROMPTS, 5.0, "prompt after Ctrl-C")
        rx = run(s, "cmic r 0x31488 1")
        log(f"after sync: ping alive={alive} RXPKT: {' '.join(rx.split()[2:6])}", "ALERT")
        return 0 if alive else 3
    return 0


def main() -> int:
    a = sys.argv[1:]
    if not a or a[0] not in ("warm", "cold"):
        print(__doc__)
        return 2
    if not dev._ensure_console() or not console.restart(console.BAUD):
        return 1
    s = console.Session()
    try:
        if a[0] == "warm":
            s.write(b"\x03\r\n")
            s.expect(dev.U2_PROMPTS, 5.0, "awto-uboot prompt")
            return warm(s)
        if "--here" in a:             # already at the awto-uboot prompt on the cold chip
            s.write(b"\x03\r\n")
            s.expect(dev.U2_PROMPTS, 5.0, "awto-uboot prompt")
            return cold(s, a)
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
    help_guard(__doc__, strict=True, known=('--here', '--no-cycle', '--range', '--sync'))
    sys.exit(main())
