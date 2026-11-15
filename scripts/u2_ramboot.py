#!/usr/bin/env python3
"""u2_ramboot — at the awto-uboot prompt: TFTP an awto-uboot image to RAM, crc-check it, `bootm` it (no flash write).

Usage: scripts/u2_ramboot.py [--image FILE]   (default tmp/uboot/us48-uboot-chain.bin; board at the awto-uboot> prompt)
- Chip state is kept (no reset): unit 1 stays cold if the running image never touched it. Log: tmp/logs/u2_ramboot.log
- The image may exceed the env's `sf read` size (autochain.md); only a flash boot needs the size to fit.
"""
import shutil
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dev  # noqa: E402
from dev import console, log  # noqa: E402

BOOT_S = 8.0  # bootm copy ~0.25 s + banner + PCIe warm no-op + fans ~0.5 s (autochain.md timings) x ~5: log-and-fail on expiry


def main() -> int:
    args = sys.argv[1:]
    img = Path(args[args.index("--image") + 1]) if "--image" in args else dev.REPO / "tmp/uboot/us48-uboot-chain.bin"
    if img.parent.resolve() != dev.TFTP_DIR.resolve():
        shutil.copy2(img, dev.TFTP_DIR / img.name)
    data = img.read_bytes()
    host_crc = zlib.crc32(data) & 0xFFFFFFFF
    if not dev._ensure_console():
        return 1
    s = console.Session()
    try:
        s.write(b"\x03\r\n")
        s.expect(dev.U2_PROMPTS, dev.BAUD_REPLY_S, "uboot-2 prompt")
        if not dev._u2_tftp(s, img.name):
            return 1
        s.send_line("")
        s.expect(dev.U2_PROMPTS, 10.0, "prompt after tftp")
        crc = dev._crc32(s, int(dev.LOAD_ADDR, 16), len(data), "crc32 RAM")
        if crc is None or int(crc, 16) != host_crc:
            log(f"crc board {crc} host {host_crc:08x}: MISMATCH, not booting", "FATAL")
            return 1
        s.write(b"\x03\x03")
        s.send_line(f"bootm {dev.LOAD_ADDR}")
        s.expect([b"awto-uboot> "], BOOT_S, "new image prompt")
        log("ramboot: new image at its prompt", "ALERT")
        return 0
    except console.Timeout as e:
        log(str(e), "FATAL")
        return 1
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
