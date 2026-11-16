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
    s.read_idle(0.5, 2.0)  # the two ^C leave extra prompts in the buffer: drain, or the next expect matches a stale one
    log(f"ping after {what}: {hit.decode()}", "ALERT" if hit == b"is alive" else "FATAL")
    return hit == b"is alive"


# ge1 (unit 0) DMA/PHY/MAC state, reads only. Addresses: bcm_cmic_hr2.c DMA_*, GE_DMA_REGION in us48_cmic_eth.c.
INSPECT = ("cmic r 0x31140 2", "cmic r 0x31150", "cmic r 0x31158 2", "cmic r 0x1a000",
           "miim r int 0 9 0", "miim r int 0 9 1", "miim r int 0 9 1", "miim r int 0 9 5", "miim r int 0 9 10",
           "schan r 2 0x10207 1", "schan r 2 0x1b07 2", "schan r 2 0x3807 2",
           "md.l 0x03100000 20", "md.l 0x0310103c 1", "md.l 0x0310107c 1")


def inspect(s, tag: str) -> None:
    for cmd in INSPECT:
        out = dev._uboot_cmd(s, cmd, dev.U2_PROMPTS, dev.BAUD_REPLY_S * 3, f"inspect {tag}")
        body = " | ".join(x.strip() for x in (out or "NO REPLY").splitlines() if x.strip() and "awto-uboot" not in x and x.strip() != cmd)
        log(f"INSPECT {tag}: {cmd} -> {body}", "WARN")


def main() -> int:
    global LEN, SRC, BACK, NAME
    if "--tftp" in sys.argv:
        NAME = sys.argv[sys.argv.index("--tftp") + 1]
    if "--flashlike" in sys.argv:  # RAM copy where TFTP puts it, read-back at 0x02000000 as dev.py flash-kernel0
        SRC, BACK = 0x01000000, 0x02000000
    if "--len" in sys.argv:
        LEN = int(sys.argv[sys.argv.index("--len") + 1], 0)
    insp = "--inspect" in sys.argv
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
        crcs, dead, erased = {}, [], False
        for name, cmd in (("sf probe", "sf probe 0"),
                          ("sf read", f"sf read {SRC:#x} {OFF:x} {LEN:x}"),
                          ("sf erase", f"sf erase {OFF:x} {LEN:x}"),
                          ("sf write", f"sf write {SRC:#x} {OFF:x} {LEN:x}"),
                          ("sf read-back", f"sf read {BACK:#x} {OFF:x} {LEN:x}")):
            if name == "sf erase":
                crcs["src"] = dev._crc32(s, SRC, LEN, "crc32 of the slot bytes")
                log(f"slot sector crc {crcs['src']} (RAM copy @{SRC:#x})", "WARN")
                if insp:
                    inspect(s, "before erase")
            if dev._uboot_cmd(s, cmd, dev.U2_PROMPTS, dev.SF_BUDGET_S, name) is None:
                return 1
            if name == "sf erase" and insp:
                inspect(s, "after erase, before ping")
            alive = ping(s, host, name)
            if name == "sf erase":  # slot is erased: never leave before the write-back; probe the dead state
                erased = True
                if insp:
                    inspect(s, f"after ping (alive={alive})")
                if not alive:
                    dead.append("first ping after erase")
                    if not ping(s, host, "erase, 2nd ping"):
                        dead.append("2nd ping after erase")
                    if insp:
                        inspect(s, "after 2nd ping")
            elif not alive:
                dead.append(f"ping after {name}")
                if not erased:
                    return 2
            if stop == name:
                return 0
        crcs["back"] = dev._crc32(s, BACK, LEN, "crc32 of read-back")
        ok = crcs["src"] == crcs["back"]
        log(f"dead pings: {dead or 'none'}", "WARN")
        log(f"read-back crc {crcs['back']} vs {crcs['src']}: {'OK' if ok else 'MISMATCH'}", "ALERT" if ok else "FATAL")
        if not dev._u2_tftp(s, NAME):
            return 2
        log("second tftp after the write: OK", "ALERT")
        return 0 if ok else 1  # exit 0 = slot restored; dead pings are in the log
    except console.Timeout as e:
        log(str(e), "FATAL")
        return 1
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
