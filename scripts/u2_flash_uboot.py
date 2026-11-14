#!/usr/bin/env python3
"""u2_flash_uboot — at the uboot-2 prompt: TFTP the uboot-2 chain image, write the kernel0 uboot slot, verify.

Usage: scripts/u2_flash_uboot.py [--image FILE]   (default tmp/uboot/us48-uboot-chain.bin; board at the awto-uboot> prompt)
- Writes flash 0xe0000.. only (kernel0 uboot slot); uboot-2's `sf` refuses anything outside 0xe0000..0x1e0000.
- Read-back at 0x02000000 and crc32 vs the RAM copy. Log: tmp/logs/u2_flash_uboot.log
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dev  # noqa: E402
from dev import console, log  # noqa: E402


def main() -> int:
    args = sys.argv[1:]
    img = Path(args[args.index("--image") + 1]) if "--image" in args else dev.REPO / "tmp/uboot/us48-uboot-chain.bin"
    name = img.name
    if img.parent.resolve() != dev.TFTP_DIR.resolve():
        shutil.copy2(img, dev.TFTP_DIR / name)
    size = img.stat().st_size
    _n, off, length, _i = dev._slot(["--slot", "uboot"])
    if size > length:
        log(f"{size} B > slot 0x{length:x}", "FATAL")
        return 1
    flash_off = dev.KERNEL0_OFF + off
    erase_len = (size + 0xFFFF) & ~0xFFFF
    if not dev._ensure_console():
        return 1
    s = console.Session()
    try:
        s.write(b"\x03\r\n")
        s.expect(dev.U2_PROMPTS, dev.BAUD_REPLY_S, "uboot-2 prompt")
        if not dev._u2_tftp(s, name):
            return 1
        s.send_line("")  # the prompt may already have been consumed by the tftp reader
        s.expect(dev.U2_PROMPTS, 10.0, "prompt after tftp")
        ram = dev._crc32(s, int(dev.LOAD_ADDR, 16), size, "crc32 RAM")
        log(f"writing uboot slot @0x{flash_off:x}: erase 0x{erase_len:x}, write 0x{size:x}, RAM crc {ram}", "WARN")
        for cmd, what in (("sf probe 0", "sf probe"),
                          (f"sf erase {flash_off:x} {erase_len:x}", "sf erase"),
                          (f"sf write {dev.LOAD_ADDR} {flash_off:x} {size:x}", "sf write"),
                          (f"sf read 0x02000000 {flash_off:x} {size:x}", "sf read-back")):
            if dev._uboot_cmd(s, cmd, dev.U2_PROMPTS, dev.SF_BUDGET_S, what) is None:
                return 1
        back = dev._crc32(s, 0x02000000, size, "crc32 read-back")
        ok = ram is not None and back == ram
        log(f"read-back crc {back} vs RAM {ram}: {'OK' if ok else 'MISMATCH - slot NOT good, uboot-1 + stock untouched'}",
            "ALERT" if ok else "FATAL")
        return 0 if ok else 1
    except console.Timeout as e:
        log(str(e), "FATAL")
        return 1
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
