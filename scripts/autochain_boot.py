#!/usr/bin/env python3
"""autochain_boot — power-cycle via the relay and log every console line with a host timestamp until `awto-uboot> ` (#63).

  scripts/autochain_boot.py [--manual]      --manual: abort the uboot-1 countdown and type sf probe/sf read/bootm
                                            (simulates the env bootcmd without writing the env); --xip: bootm from the BSPI window

- t=0 is the relay ON reply. Stage table printed at the end. Log: tmp/logs/autochain_boot.log
- Budget: 40 s overall (expected ~12 s: uboot-1 ~6 s + DDR + PCIe + chain); expiry = FATAL with the last line.
  Stall: 15 s without a console line = hung (awto-uboot WDT is 10 s, so a hang shows as a repeated banner instead).
"""
import argparse
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import console  # noqa: E402
import dev  # noqa: E402
from dev import log  # noqa: E402

STAGES = [(b"U-Boot usw", "uboot-1 banner"), (b"DDR Tune Completed", "DDR done"), (b"port 0", "PCIe line"),
          (b"Hit any key", "autoboot countdown"), (b"Verifying Checksum", "bootm verify"),
          (b"Starting kernel", "bootm start"), (b"awto-uboot 20", "awto-uboot banner"), (b"awto-uboot> ", "prompt")]
OVERALL_S = 40.0
STALL_S = 15.0
SIZE = 842736


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manual", action="store_true")
    ap.add_argument("--xip", action="store_true", help="with --manual: bootm 0x1c0e0000 instead of sf read + bootm")
    a = ap.parse_args()
    s = console.Session()
    try:
        s.read_idle(0.2, 1.0)
        host = dev.os.environ.get("US48_RELAY", dev.RELAY_DEFAULT)
        log(f"relay {host}: off -> {dev._relay_post(host, 'off')}", "WARN")
        threading.Event().wait(dev.RELAY_GAP_S)
        t0 = time.monotonic()  # t=0 = start of the relay ON call (the POST itself is part of the power-up)
        threading.Thread(target=lambda: log(f"relay on -> {dev._relay_post(host, 'on')}", "ALERT"), daemon=True).start()
        seen: dict[str, float] = {}
        seen_lines = 0
        last = t0
        last_space = 0.0
        manual_step = 0
        while time.monotonic() - t0 < OVERALL_S:
            if s._pump(0.02):
                last = time.monotonic()
            if time.monotonic() - last > STALL_S:
                log(f"stall {STALL_S} s, last: {s.buf[-100:]!r}", "FATAL")
                return 1
            lines = s.buf.split(b"\n")
            for ln in lines[seen_lines:-1]:
                t = time.monotonic() - t0
                txt = ln.decode(errors="replace").strip()
                if txt:
                    log(f"+{t:6.2f} s  {txt}")
            seen_lines = len(lines) - 1
            for pat, name in STAGES:
                if name not in seen and pat in s.buf:
                    seen[name] = time.monotonic() - t0
                    log(f"STAGE {name} at +{seen[name]:.2f} s", "ALERT")
            if a.manual and "autoboot countdown" in seen and manual_step == 0:
                if time.monotonic() - last_space > 0.1:  # any char aborts the countdown (docs/uboot.md)
                    s.write(b" ")
                    last_space = time.monotonic()
                if b"u-boot> " in s.buf[-12:]:
                    manual_step = 1
            if a.manual and manual_step in (1, 2, 3) and s.buf.endswith(b"u-boot> "):
                cmds = (["bootm 0x1c0e0000"] if a.xip else
                        ["sf probe 0 50000000", f"sf read 0x01000000 0xe0000 0x{SIZE:x}", "bootm 0x01000000"])
                if manual_step > len(cmds):
                    manual_step = 4
                    continue
                cmd = cmds[manual_step - 1]
                s.send_line(cmd)
                manual_step += 1
                s.buf += b"\n"
            if b"awto-uboot> " in s.buf[-14:]:
                log("stages: " + ", ".join(f"{k} +{v:.2f}" for k, v in seen.items()), "ALERT")
                return 0
        log(f"overall {OVERALL_S} s expired, stages {seen}, tail {s.buf[-150:]!r}", "FATAL")
        return 1
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
