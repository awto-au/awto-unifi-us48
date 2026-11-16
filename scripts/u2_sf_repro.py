#!/usr/bin/env python3
"""u2_sf_repro — #104: which step after a TFTP kills ge1 at the awto-uboot prompt.

Usage: scripts/u2_sf_repro.py [--len BYTES] [--tftp NAME] [--flashlike] [--stop-after STEP]   (board at awto-uboot>; log tmp/logs/u2_sf_repro.log)
- Steps, each followed by a `ping` of the host (10 s U-Boot ping timeout + 5 s margin = 15 s, dev.U2_PING_S):
  tftp -> sf probe -> sf read 64 KiB @0x2e0000 -> sf erase -> sf write (same bytes back) -> sf read-back.
- Writes only the first --len (default 64 KiB) of the kernel slot (0x2e0000), identical content; crc compared.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dev  # noqa: E402
from dev import console, log  # noqa: E402

OFF, LEN = 0x2E0000, 0x10000  # --len N: more sectors (64 KiB each), at most 0x400000
SRC, BACK = 0x02000000, 0x02800000
NAME = "regprobe_u0.bin"  # --tftp NAME: file for both TFTPs (a 10 MB file + --len 0x980000 = the flash-kernel0 shape)


def ping(s, host: str, what: str) -> bool:
    s.send_line(f"ping {host}")
    try:
        hit = s.expect([b"is alive", b"not alive"], dev.U2_PING_S, f"ping after {what}")
    except console.Timeout:
        hit = b"timeout"
    s.write(b"\x03\x03")
    s.send_line("")
    s.expect(dev.U2_PROMPTS, dev.BAUD_REPLY_S, "prompt after ping")
    log(f"ping after {what}: {hit.decode()}", "ALERT" if hit == b"is alive" else "FATAL")
    return hit == b"is alive"


def main() -> int:
    global LEN, SRC, BACK, NAME
    if "--tftp" in sys.argv:
        NAME = sys.argv[sys.argv.index("--tftp") + 1]
    if "--flashlike" in sys.argv:  # RAM copy where TFTP puts it, read-back at 0x02000000 as dev.py flash-kernel0
        SRC, BACK = 0x01000000, 0x02000000
    if "--len" in sys.argv:
        LEN = int(sys.argv[sys.argv.index("--len") + 1], 0)
    stop = sys.argv[sys.argv.index("--stop-after") + 1] if "--stop-after" in sys.argv else None
    host = dev._host_ip_for(dev.U2_IP)
    if not dev._ensure_console():
        return 1
    s = console.Session()
    try:
        s.write(b"\x03\r\n")
        s.expect(dev.U2_PROMPTS, dev.BAUD_REPLY_S, "awto-uboot prompt")
        if not dev._u2_tftp(s, NAME):
            return 1
        s.send_line("")
        s.expect(dev.U2_PROMPTS, 10.0, "prompt after tftp")
        if not ping(s, host, "tftp"):
            return 2
        crcs = {}
        for name, cmd in (("sf probe", "sf probe 0"),
                          ("sf read", f"sf read {SRC:#x} {OFF:x} {LEN:x}"),
                          ("sf erase", f"sf erase {OFF:x} {LEN:x}"),
                          ("sf write", f"sf write {SRC:#x} {OFF:x} {LEN:x}"),
                          ("sf read-back", f"sf read {BACK:#x} {OFF:x} {LEN:x}")):
            if name == "sf erase":
                crcs["src"] = dev._crc32(s, SRC, LEN, "crc32 of the slot bytes")
                log(f"slot sector crc {crcs['src']} (RAM copy @{SRC:#x})", "WARN")
            if dev._uboot_cmd(s, cmd, dev.U2_PROMPTS, dev.SF_BUDGET_S, name) is None:
                return 1
            if not ping(s, host, name):
                return 2
            if stop == name:
                return 0
        crcs["back"] = dev._crc32(s, BACK, LEN, "crc32 of read-back")
        ok = crcs["src"] == crcs["back"]
        log(f"read-back crc {crcs['back']} vs {crcs['src']}: {'OK' if ok else 'MISMATCH'}", "ALERT" if ok else "FATAL")
        if not dev._u2_tftp(s, NAME):
            return 2
        log("second tftp after the write: OK", "ALERT")
        return 0 if ok else 1
    except console.Timeout as e:
        log(str(e), "FATAL")
        return 1
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
