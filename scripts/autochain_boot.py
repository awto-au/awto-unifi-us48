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
          (b"Starting kernel", "bootm start"), (b"awto-uboot 20", "awto-uboot banner"), (b"pcie:", "awto-uboot pcie"),
          (b"cpu: US48", "awto-uboot cpu line"), (b"fans", "awto-uboot fans"),
          (b"us48boot: header ok", "us48boot header ok"), (b"us48boot: image read", "us48boot read"),
          (b"Booting Linux", "kernel start"), (b"Freeing unused kernel", "kernel init done"),
          (b"starting pid", "userspace (rc.sh)"), (b"us48-net: done", "us48-net done"),
          (b"Please press Enter", "shell prompt"), (b"awto-uboot> ", "prompt")]
BOARD_IP = "192.168.25.207"  # ip-registry us48-boot
# --full: autoboot to Linux. The stage list is cumulative: the awto-uboot/kernel names repeat across the two bootm hops,
# so a name is only recorded once (the first occurrence). Expected total ~30 s; 120 s backstop, expiry = FATAL.
FULL_OVERALL_S = 120.0
OVERALL_S = 40.0
STALL_S = 15.0
# after the shell prompt wait this long for ping/ssh, then report. Measured 2026-11-15: ge1 link +3.2 s (GPHY AN) after
# the driver probe, ssh ~2.5 s after the prompt; 15 s = 4x backstop, expiry = logged, not fatal.
NET_GRACE_S = 15.0
FULL_STALL_S = 40.0  # kernel init is silent for a while with quiet console; 40 s = hung
SIZE = 0x200000  # the env reads the whole 2 MiB uboot slot; bootm checks only the header length


def probe_net(t0: float, seen: dict) -> None:
    """Host-side: first ICMP echo reply and first TCP connect to :22, 0.2 s apart (the board has no log line for either)."""
    import socket
    import subprocess
    while "ssh port open" not in seen and time.monotonic() - t0 < FULL_OVERALL_S:
        if "ping answers" not in seen:
            r = subprocess.run(["ping", "-c1", "-W1", "-n", BOARD_IP], capture_output=True)
            if r.returncode == 0:
                seen["ping answers"] = time.monotonic() - t0
        try:
            socket.create_connection((BOARD_IP, 22), 0.2).close()
            seen["ssh port open"] = time.monotonic() - t0
        except OSError:
            pass
        threading.Event().wait(0.2)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manual", action="store_true")
    ap.add_argument("--warm", action="store_true", help="no relay: send `reset` at the awto-uboot prompt (implies --manual)")
    ap.add_argument("--full", action="store_true", help="env/awto-uboot autoboot: run until the board answers ssh (port 22)")
    ap.add_argument("--xip", action="store_true", help="with --manual: bootm 0x1c0e0000 instead of sf read + bootm")
    a = ap.parse_args()
    a.manual = a.manual or a.warm
    s = console.Session()
    relay_err: list[str] = []	# set by the relay-on thread: stop at once, the board may be off (#111)
    try:
        s.read_idle(0.2, 1.0)
        if a.warm:
            s.send_line("reset")
            t0 = time.monotonic()  # t=0 = `reset` sent
        else:
            host = dev.os.environ.get("US48_RELAY", dev.RELAY_DEFAULT)
            try:
                log(f"relay {host}: off -> {dev.relay_set(host, False)}", "WARN")
            except OSError as e:
                log(f"{e}", "FATAL")
                return 1
            threading.Event().wait(dev.RELAY_GAP_S)
            t0 = time.monotonic()  # t=0 = start of the relay ON call (the POST itself is part of the power-up)

            def relay_on() -> None:
                try:
                    log(f"relay on -> {dev.relay_set(host, True)}", "ALERT")
                except OSError as e:
                    relay_err.append(str(e))
                    log(f"{e}", "FATAL")
            threading.Thread(target=relay_on, daemon=True).start()
        seen: dict[str, float] = {}
        if a.full:
            threading.Thread(target=probe_net, args=(t0, seen), daemon=True).start()
        seen_lines = 0
        last = t0
        last_space = 0.0
        manual_step = 0
        limit, stall = (FULL_OVERALL_S, FULL_STALL_S) if a.full else (OVERALL_S, STALL_S)
        while time.monotonic() - t0 < limit:
            if relay_err:
                log(f"STOP: relay on failed ({relay_err[0]}); board may be powered OFF", "FATAL")
                return 1
            if s._pump(0.02):
                last = time.monotonic()
            if time.monotonic() - last > stall and not (a.full and "ping answers" in seen):
                log(f"stall {stall} s, last: {s.buf[-100:]!r}", "FATAL")
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
            if a.full and "ssh port open" not in seen and "shell prompt" in seen and \
                    time.monotonic() - t0 > seen["shell prompt"] + NET_GRACE_S:
                log("no ssh within the grace period: " + ", ".join(f"{k} +{v:.2f}" for k, v in seen.items()), "ALERT")
                return 0
            if a.full and "ssh port open" in seen:
                log("stages: " + ", ".join(f"{k} +{v:.2f}" for k, v in seen.items()), "ALERT")
                return 0
            if not a.full and b"awto-uboot> " in s.buf[-14:]:
                log("stages: " + ", ".join(f"{k} +{v:.2f}" for k, v in seen.items()), "ALERT")
                return 0
        log(f"overall {limit} s expired, stages {seen}, tail {s.buf[-150:]!r}", "FATAL")
        return 1
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
