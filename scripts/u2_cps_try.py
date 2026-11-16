#!/usr/bin/env python3
"""u2_cps_try — #46 option B finding: stock writes CMIC_CPS_RESET (0x10220) = 1 once, between two CMIC_RXBUF release pulses, before its S-channel init.

Usage: scripts/u2_cps_try.py   (board at the awto-uboot prompt on a WARM chip; new image with `cmic w`, `hr2init`)
- Baseline ping must be alive, then `hr2init 0x15f` (breaks RX like a cold chip), then trials; stops at the first alive ping:
  T2 stock order: RLS pulse, CPS_RESET=1, ring map/timeout/rate/purge, full replay, RLS pulse
  T3 T2 + MIIM_SCAN_CTRL 0x30001000 (T1, CPS_RESET + RLS alone on a broken chip, was dead and clears the ring map)
- Log: tmp/logs/u2_cps_try.log
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dev  # noqa: E402
import u2_regsync as rs  # noqa: E402
from dev import console, log  # noqa: E402
from _cli import help_guard  # noqa: E402

PULSE = ["cmic w 0x1a000 0", "cmic w 0x1a000 1"]
CPS = "cmic w 0x10220 1"


def run(s, cmds):
    for c in cmds:
        rs.ms.run(s, c, stall=30.0, overall=60.0)


def rd(s, off):
    r = rs.ms.run(s, f"cmic r 0x{off:x} 1")
    return r.split("=")[-1].split()[0] if "=" in r else "?"


def main() -> int:
    if not dev._ensure_console() or not console.restart(console.BAUD):
        return 1
    s = console.Session()
    try:
        s.write(b"\x03\r\n")
        s.expect(dev.U2_PROMPTS, 5.0, "awto-uboot prompt")
        rs.ms.run(s, f"setenv ipaddr {dev.U2_IP}; setenv serverip {rs.HOST}")
        log(f"CPS_RESET warm = {rd(s, 0x10220)}", "ALERT")
        if "--here" not in sys.argv:   # --here: chip already broken (skip the warm baseline and the breaking replay)
            if not rs.probe(s, "baseline warm", 30.0):
                log("warm baseline dead", "FATAL")
                return 1
            run(s, ["hr2init 0x15f"])
            log(f"CPS_RESET after replay = {rd(s, 0x10220)}", "ALERT")
            if rs.probe(s, "after replay"):
                log("replay did not break RX", "FATAL")
                return 1
        # CPS_RESET clears the SBUS ring map (S-channel dead until it is rewritten): stock rewrites ring map, timeout, rate right after it.
        ring = ["cmic w 0x10098 0x11122200", "cmic w 0x1009c 0x00430000", "cmic w 0x100a0 0x00005004", "cmic w 0x100a4 0",
                "cmic w 0x10094 0x7d0", "cmic w 0x11000 0x10028", "cmic w 0x11004 0x10028", "cmic w 0x10260 0x13"]
        trials = [("T2", PULSE + [CPS] + ring + ["hr2init 0x15f"] + PULSE), ("T3", PULSE + [CPS] + ring + ["hr2init 0x15f"] + PULSE + ["cmic w 0x11008 0x30001000"])]
        for name, cmds in trials:
            run(s, cmds)
            log(f"{name}: CPS_RESET = {rd(s, 0x10220)}", "WARN")
            if rs.probe(s, name, 30.0):
                log(f"{name}: RX ALIVE", "ALERT")
                return 0
        return 3
    except console.Timeout as e:
        log(str(e), "FATAL")
        return 1
    finally:
        s.close()


if __name__ == "__main__":
    help_guard(__doc__, strict=True, known=('--here',))
    sys.exit(main())
