#!/usr/bin/env python3
"""u2_flash_env: at the awto-uboot prompt, replace the uboot-1 env (flash 0xc0000, 64 KiB) with a host-built image (#63).

Usage: scripts/u2_flash_env.py --expect-live CRC [--image FILE]   (default tmp/autochain/env.bin)
- Refuses unless the live env crc32 == --expect-live and the TFTP'd RAM copy crc32 == the host file's crc32.
- erase/write 0xc0000 0x10000, read back, compare. Approved by Dan 2026-11-15 ("write the env"); docs/autochain.md.
- Log: tmp/logs/u2_flash_env.log
"""
import shutil
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dev  # noqa: E402
from dev import console, log  # noqa: E402
from _cli import help_guard  # noqa: E402
import flash_guard  # noqa: E402

ENV_OFF, ENV_LEN = 0xC0000, 0x10000
LIVE_RAM, BACK_RAM = 0x02000000, 0x02100000


def norm(c) -> str:
    return f"{int(str(c), 16):08x}" if c is not None else "none"


def main() -> int:
    a = sys.argv[1:]
    img = Path(a[a.index("--image") + 1]) if "--image" in a else dev.REPO / "tmp/autochain/env.bin"
    want_live = a[a.index("--expect-live") + 1].lower()
    data = img.read_bytes()
    if len(data) != ENV_LEN:
        log(f"{img}: {len(data)} B, expected {ENV_LEN}", "FATAL")
        return 1
    host = f"{zlib.crc32(data):08x}"
    shutil.copy2(img, dev.TFTP_DIR / img.name)
    if not dev._ensure_console():
        return 1
    s = console.Session()
    try:
        s.write(b"\x03\r\n")
        s.expect(dev.U2_PROMPTS, dev.BAUD_REPLY_S, "awto-uboot prompt")
        if dev._uboot_cmd(s, "sf probe 0", dev.U2_PROMPTS, dev.SF_BUDGET_S, "sf probe") is None:
            return 1
        if dev._uboot_cmd(s, f"sf read {LIVE_RAM:x} {ENV_OFF:x} {ENV_LEN:x}", dev.U2_PROMPTS, dev.SF_BUDGET_S, "sf read live env") is None:
            return 1
        live = norm(dev._crc32(s, LIVE_RAM, ENV_LEN, "crc32 live env"))
        if live != want_live:
            log(f"live env crc {live} != expected {want_live}: not writing", "FATAL")
            return 1
        if not dev._u2_tftp(s, img.name):
            return 1
        s.send_line("")
        s.expect(dev.U2_PROMPTS, 10.0, "prompt after tftp")
        ram = norm(dev._crc32(s, int(dev.LOAD_ADDR, 16), ENV_LEN, "crc32 RAM env"))
        if ram != host:
            log(f"RAM env crc {ram} != host {host}: not writing", "FATAL")
            return 1
        log(f"writing env @0x{ENV_OFF:x}: live {live} -> {host}", "WARN")
        for cmd, what in ((f"sf erase {ENV_OFF:x} {ENV_LEN:x}", "sf erase env"),
                          (f"sf write {dev.LOAD_ADDR} {ENV_OFF:x} {ENV_LEN:x}", "sf write env"),
                          (f"sf read {BACK_RAM:x} {ENV_OFF:x} {ENV_LEN:x}", "sf read-back env")):
            if dev._uboot_cmd(s, cmd, dev.U2_PROMPTS, dev.SF_BUDGET_S, what) is None:
                return 1
        back = norm(dev._crc32(s, BACK_RAM, ENV_LEN, "crc32 read-back"))
        ok = back == host
        log(f"read-back {back} vs host {host}: {'OK' if ok else 'MISMATCH (bad env CRC -> uboot-1 boots stock with default env)'}",
            "ALERT" if ok else "FATAL")
        return 0 if ok else 1
    except console.Timeout as e:
        log(str(e), "FATAL")
        return 1
    finally:
        s.close()


if __name__ == "__main__":
    help_guard(__doc__, strict=True, known=('--image', '--expect-live'))
    flash_guard.allow("u-boot-env")  # the one approved env write (docs/autochain.md)
    sys.exit(main())
