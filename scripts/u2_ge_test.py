#!/usr/bin/env python3
"""u2_ge_test — one pass at the uboot-2 prompt: ping the host, TFTP an image, crc32 vs host, rate.

Usage: scripts/u2_ge_test.py [--image FILE]   (default tmp/tftp/us48-initramfs.bin; board at the awto-uboot> prompt)
- Active port = uboot-2 `ethact` (ge1 default); set it by hand first to test xe1.
- Single transfer, no repeats (Dan: no soaks). Log: tmp/logs/u2_ge_test.log
"""
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dev  # noqa: E402
from dev import console, log  # noqa: E402

PING_S = 15.0  # ARP + ICMP; GPHY AN up to 4 s in start(), sw1 STP may add more; expiry = no answer, logged


def main() -> int:
    args = sys.argv[1:]
    img = Path(args[args.index("--image") + 1]) if "--image" in args else dev.TFTP_DIR / dev.U2_IMAGE
    name = dev._u2_image(["--image", str(img)])
    size = (dev.TFTP_DIR / name).stat().st_size
    host_crc = zlib.crc32((dev.TFTP_DIR / name).read_bytes()) & 0xFFFFFFFF
    if not dev._ensure_console():
        return 1
    s = console.Session()
    try:
        s.write(b"\x03\r\n")
        s.expect(dev.U2_PROMPTS, dev.BAUD_REPLY_S, "uboot-2 prompt")
        with dev._tftp_server() as (host, port):
            s.send_line(f"setenv ipaddr {dev.U2_IP}; setenv serverip {host}")
            s.expect(dev.U2_PROMPTS, dev.BAUD_REPLY_S, "prompt")
            for attempt in (1, 2):  # the first ping after boot can die in the sw1 STP forwarding delay
                s.send_line(f"ping {host}")
                hit = s.expect([b"is alive", b"not alive", b"ping failed"], PING_S, "ping")
                log(f"ping {host} try {attempt}: {hit.decode(errors='replace')}", "ALERT")
                s.send_line("")
                s.expect(dev.U2_PROMPTS, 15.0, "prompt after ping")
                if hit == b"is alive":
                    break
        t0 = time.monotonic()
        if not dev._u2_tftp(s, name):
            return 1
        dt = time.monotonic() - t0
        s.send_line("")  # the prompt may already have been consumed by the tftp reader
        s.expect(dev.U2_PROMPTS, 10.0, "prompt after tftp")
        crc = dev._crc32(s, int(dev.LOAD_ADDR, 16), size, "crc32 of RAM image")
        ok = crc is not None and int(crc, 16) == host_crc
        log(f"tftp {size} B in {dt:.1f} s = {size / dt / 1024:.0f} KiB/s; crc board {crc} host {host_crc:08x}: "
            f"{'OK' if ok else 'MISMATCH'}", "ALERT" if ok else "FATAL")
        return 0 if ok else 1
    except console.Timeout as e:
        log(str(e), "FATAL")
        return 1
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
