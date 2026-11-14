#!/usr/bin/env python3
"""u2_rls_try — #46: replay the cold-init list with a CMIC credit-release pulse injected at entry K, then ping (chip already replayed/broken is fine).

Usage: scripts/u2_rls_try.py K [K ...]    (board at the awto-uboot prompt, new image with `cmic w` and `hr2init`)
- Per K: `hr2init 0x15f 0 K`, `cmic w 0x1a000 0; cmic w 0x1a000 1` (CMIC_RXBUF_EPINTF_RELEASE_ALL_CREDITS pulse), `hr2init 0x15f K+1 END`, ping.
- The TOP reset at the start of the list re-initialises the chip, so trials chain on one chip without a warm cycle.
- Log: tmp/logs/u2_rls_try.log
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dev  # noqa: E402
import u2_regsync as rs  # noqa: E402
from dev import console, log  # noqa: E402

END = 25097


def main() -> int:
    ks = [int(x, 0) for x in sys.argv[1:]]
    if not ks or not dev._ensure_console() or not console.restart(console.BAUD):
        return 2
    s = console.Session()
    try:
        s.write(b"\x03\r\n")
        s.expect(dev.U2_PROMPTS, 5.0, "awto-uboot prompt")
        rs.ms.run(s, f"setenv ipaddr {dev.U2_IP}; setenv serverip {rs.HOST}")
        for k in ks:
            a = rs.ms.run(s, f"hr2init 0x15f 0 {k}", stall=30.0, overall=60.0)
            rs.ms.run(s, "cmic w 0x1a000 0")
            rs.ms.run(s, "cmic w 0x1a000 1")
            b = rs.ms.run(s, f"hr2init 0x15f {k + 1} {END}", stall=30.0, overall=60.0)
            log(f"K={k}: {' '.join(a.split()[-9:-1])} | {' '.join(b.split()[-9:-1])}", "WARN")
            alive = rs.probe(s, f"pulse at K={k}", 30.0)
            log(f"K={k}: ping {'ALIVE' if alive else 'dead'}", "ALERT")
            if alive:
                return 0
        return 3
    except console.Timeout as e:
        log(str(e), "FATAL")
        return 1
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
