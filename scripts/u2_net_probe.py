#!/usr/bin/env python3
"""u2_net_probe — #104: is the first ping after a long gap dead, without any flash op.

Usage: scripts/u2_net_probe.py [--gap S] [--rounds N] [--tftp NAME]   (board at awto-uboot>; log tmp/logs/u2_net_probe.log)
- Each round: ping, `sleep S` (U-Boot sleep, SP805 fed), ping, immediate ping. Reports alive/dead per ping.
- --gap 0: no sleep. --tftp NAME: one TFTP of tmp/tftp/NAME as the first net use after the gap (instead of the ping).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dev  # noqa: E402
import u2_sf_repro  # noqa: E402
from dev import console, log  # noqa: E402


def main() -> int:
    gap = int(sys.argv[sys.argv.index("--gap") + 1]) if "--gap" in sys.argv else 45
    rounds = int(sys.argv[sys.argv.index("--rounds") + 1]) if "--rounds" in sys.argv else 1
    tftp = sys.argv[sys.argv.index("--tftp") + 1] if "--tftp" in sys.argv else None
    host = dev._host_ip_for(dev.U2_IP)
    if not dev._ensure_console():
        return 1
    s = console.Session()
    dead = 0
    try:
        s.write(b"\x03\r\n")
        s.expect(dev.U2_PROMPTS, dev.BAUD_REPLY_S, "awto-uboot prompt")
        s.read_idle(0.5, 2.0)
        for i in range(rounds):
            u2_sf_repro.ping(s, host, f"round {i} before gap")
            # sleep S s: the idle gap under test; budget = S + 5 s
            if gap and dev._uboot_cmd(s, f"sleep {gap}", dev.U2_PROMPTS, gap + 5.0, "sleep") is None:
                return 1
            if tftp:
                ok = dev._u2_tftp(s, tftp)
                log(f"tftp {tftp} after the gap: {'OK' if ok else 'FAILED'}", "ALERT" if ok else "FATAL")
                dead += not ok
                s.send_line("")
                s.expect(dev.U2_PROMPTS, 10.0, "prompt after tftp")
            else:
                dead += not u2_sf_repro.ping(s, host, f"round {i} after {gap} s gap")
            dead += not u2_sf_repro.ping(s, host, f"round {i} immediate 2nd")
        log(f"dead pings: {dead}", "WARN")
        return 0
    except console.Timeout as e:
        log(str(e), "FATAL")
        return 1
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
