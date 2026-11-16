#!/usr/bin/env python3
"""stock_ramboot — RAM-only: get the us48stock awto-uboot variant to its prompt, record flash state (reads), boot stock from kernel1 (#78).

Usage: scripts/stock_ramboot.py [--cycle | --reboot | --reset | --here] [--no-boot] [--no-u1]
  --here    already at an awto-uboot prompt (flashed or variant): no reset
  --cycle   relay power cycle first (default)      --reboot  `reboot` in mainline Linux over ssh (no power cycle)
Flow: catch the 1 s window of the flashed awto-uboot (spaces from the banner, as u2_run.py) -> TFTP tmp/uboot/us48-uboot-stock.bin
      (crc checked) -> bootm -> catch the window again -> sf read the bootsel record (0x1ee0000) + env (0xc0000), crc32 -> `us48stock`.
Never writes flash, never loads over serial. Log: tmp/logs/stock_ramboot.log. Image built by `build_uboot.py --stock`; never flash it.
"""
import re
import shutil
import subprocess
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dev  # noqa: E402
import u2_run  # noqa: E402
from dev import console, log  # noqa: E402

IMG = dev.REPO / ("tmp/uboot/us48-uboot-stock-nou1.bin" if "--no-u1" in sys.argv else "tmp/uboot/us48-uboot-stock.bin")
# after Linux `reboot`: shutdown ~2 s + uboot-1 + awto-uboot banner (docs/autochain.md ~3 s) -> same 15 s window as u2_run
CMD_S = 10.0   # one sf read of 64 KiB / crc32: ~0.1 s; 10 s = log-and-fail margin


def catch(s: console.Session) -> bool:
    """Spaces every 20 ms from the banner until the prompt; no relay."""
    import time
    t0 = time.monotonic()
    while time.monotonic() - t0 < u2_run.WINDOW_S:
        s._pump(0.02)
        tail = s.buf[-3000:]
        i = tail.rfind(b"awto-uboot 20")
        if i >= 0 and b"awto-uboot> " in tail[i:]:
            return True
        if i >= 0 and s.backlog() == 0:
            try:
                s.write(b" ")
            except console.Timeout:
                pass
    return False


def cmd(s: console.Session, line: str) -> str:
    """Type one line, collect until the console is idle 0.5 s (the UART loses typed chars while the CPU is in sf/crc32)."""
    s.buf = b""
    s.send_line(line)
    out = s.read_idle(0.5, CMD_S).decode(errors="replace")
    log(f"{line}\n{out.strip()}")
    return out


def main() -> int:
    a = sys.argv[1:]
    if not console.restart(console.BAUD):
        return 1
    s = console.Session()
    try:
        s.buf = b""
        if "--here" in a:
            s.write(b"\r")
        elif "--reset" in a:
            s.send_line("reset")  # warm reset at an awto-uboot prompt: uboot-1 -> flashed awto-uboot, its window is caught below
        elif "--reboot" in a:
            subprocess.run([sys.executable, str(dev.REPO / "scripts/lx.py"), "reboot"], capture_output=True)
        else:
            subprocess.run(u2_run.RELAY + ["off"], check=True, capture_output=True)
            subprocess.run(u2_run.RELAY + ["on"], check=True, capture_output=True)
        if "--here" in a:
            s.buf = b""
            s.send_line("")
            s.expect(dev.U2_PROMPTS, 5.0, "prompt")
        elif not catch(s):
            log(f"no awto-uboot> prompt in {u2_run.WINDOW_S} s; tail {s.buf[-300:]!r}", "FATAL")
            return 1
        log("flashed awto-uboot at its prompt", "ALERT")
        shutil.copy2(IMG, dev.TFTP_DIR / IMG.name)
        data = IMG.read_bytes()
        s.send_line("")
        s.expect(dev.U2_PROMPTS, 5.0, "prompt")
        if not dev._u2_tftp(s, IMG.name):
            return 1
        s.send_line("")
        s.expect(dev.U2_PROMPTS, 10.0, "prompt after tftp")
        crc = dev._crc32(s, int(dev.LOAD_ADDR, 16), len(data), "crc32 RAM")
        if crc is None or int(crc, 16) != zlib.crc32(data) & 0xFFFFFFFF:
            log(f"crc board {crc} != host {zlib.crc32(data) & 0xFFFFFFFF:08x}: not booting", "FATAL")
            return 1
        s.write(b"\x03\x03")
        s.buf = b""
        s.send_line(f"bootm {dev.LOAD_ADDR}")
        if not catch(s):
            log(f"stock variant: no prompt, tail {s.buf[-300:]!r}", "FATAL")
            return 1
        log("stock variant (RAM) at its prompt", "ALERT")
        s.send_line("")
        s.expect(dev.U2_PROMPTS, 5.0, "prompt")
        cmd(s, "sf probe 0")
        for name, off, size in (("bootsel", 0x1ee0000, 0x10000), ("env", 0xc0000, 0x10000)):
            cmd(s, f"sf read 0x01100000 0x{off:x} 0x{size:x}")
            out = cmd(s, f"crc32 0x01100000 0x{size:x}")
            log(f"{name} crc32 {re.findall(r'==> ([0-9a-f]{8})', out)}")
            if name == "bootsel":
                cmd(s, "md.b 0x01100000 0x30")
        if "--no-boot" in a:
            return 0
        s.read_idle(1.0, 3.0)   # the UART drops typed chars right after sf read / crc32 (CPU busy)
        s.buf = b""
        s.send_line("us48stock")
        s.read_idle(0.5, 2.0)
        log("us48stock sent; the stock boot log goes to the console capture (tmp/logs/tio-us48.log)", "ALERT")
        return 0
    except console.Timeout as e:
        log(str(e), "FATAL")
        return 1
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
