#!/usr/bin/env python3
"""u2_cold — power-cycle -> uboot-1 countdown abort -> awto-uboot from the kernel0 uboot slot (no stock), then dump the chip.

Usage: scripts/u2_cold.py [--no-cycle] [--dump] [--ping]
- --no-cycle: skip the relay cycle (board already at the uboot-1 countdown/prompt).
- --dump: read-only chip state at the awto-uboot prompt (log tmp/logs/u2_cold.log).
- --mask=0xNN: set env us48_init_mask before the ping (init categories, gen_bcm_hr2_init.py)
- --diag: DIAG register list after the ping
- --ping: one `ping` of the TFTP host over ge1 (runs the ge1 bring-up).
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dev  # noqa: E402
from dev import console, log  # noqa: E402

DUMPS = [
    "cmic r 0x31140 4", "cmic r 0x31150 1",
    "schan r 2 0x12207",                 # UMAC_REV_ID, ge1 lane 7
    "schan r 16 0x2030400 1",            # TOP_SOFT_RESET_REG (stock trace address)
    "schan r 16 0x2031400 1",            # TOP_MISC_CONTROL_1
    "schan r 10 0x46002700 2",           # EPC_LINK_BMAP_64
    "smem r 10 0x04000002 8",            # PORT_TAB[2]
    "smem r 10 0x04000000 8",            # PORT_TAB[0] (CPU)
    "smem r 10 0x14000001 4",            # VLAN_TAB[1]
    "smem r 11 0x100c0001 3",            # EGR_VLAN[1]
    "schan r 12 0x0 1",                  # MMU reg 0 (sanity)
    "miim r int 0 9 0", "miim r int 0 9 2", "miim r int 0 9 3", "miim r int 0 9 1",
]


DIAG = [
    "cmic r 0x31140 4", "cmic r 0x31150 1", "cmic r 0x31480 4",             # DMA ctrl/stat, per-channel packet counts
    "schan r 2 0x1b07 2", "schan r 2 0x3807 2", "schan r 2 0x10207 1",      # GRPKT, GTPKT, COMMAND_CONFIG (lane 7)
    "smem r 10 0x04000002 8", "smem r 10 0x04000000 8",                     # PORT_TAB[2], [0]
    "smem r 10 0x14000065 4", "smem r 10 0x14000001 4",                     # VLAN_TAB[101], [1]
    "schan r 10 0x46002700 2", "smem r 11 0x345c0002 1", "smem r 11 0x345c0000 1",   # EPC_LINK, EGR_ENABLE[2], [0]
    "miim r int 0 9 0", "miim r int 0 9 1", "miim r int 0 9 2",
]


def run(s: console.Session, cmd: str, budget: float = 4.0) -> str:
    """Send one command, return everything printed before the next awto-uboot prompt."""
    s.buf = b""
    s.send_line(cmd)
    t0 = time.monotonic()
    while not any(p in s.buf for p in dev.U2_PROMPTS):
        if time.monotonic() - t0 > budget:
            return f"NO PROMPT in {budget} s: {s.buf!r}"
        s._pump(0.05)
    return s.buf.decode(errors="replace")


def cycle_to_prompt(s: console.Session) -> bool:
    host = dev.os.environ.get("US48_RELAY", dev.RELAY_DEFAULT)
    try:
        log(f"relay {host}: off -> {dev._relay_post(host, 'off')}", "WARN")
    except OSError as e:
        log(f"relay: {e}; power NOT cycled", "FATAL")
        return False
    t0 = time.monotonic()
    while time.monotonic() - t0 < dev.RELAY_GAP_S:  # drain the dying board's output while the PSU discharges
        s.read_idle(0.2, 0.5)
    s.buf = b""
    log(f"relay {host}: on -> {dev._relay_post(host, 'on')}", "ALERT")
    try:
        s.expect([b"U-Boot usw"], dev.REBOOT_TO_UBOOT_S, "U-Boot banner")
        s.expect([b"Hit any key"], dev.BANNER_TO_COUNTDOWN_S, "autoboot countdown")
    except console.Timeout as e:
        log(str(e), "FATAL")
        return False
    t1 = time.monotonic()
    while time.monotonic() - t1 < dev.ABORT_BUDGET_S:
        s.write(b" ")
        s._pump(dev.ABORT_EVERY_S)
        if dev.PROMPT in s.buf:
            s.read_idle(0.3, 1.0)
            log("at u-boot> prompt (no stock)", "ALERT")
            return True
    log("countdown not aborted", "FATAL")
    return False


def dump_ping(s: console.Session, a: list[str]) -> int:
    for x in a:
        if x.startswith("--mask="):
            log(f"setenv us48_init_mask {x[7:]} -> {' | '.join(run(s, 'setenv us48_init_mask ' + x[7:]).split()[3:])}", "ALERT")
    if "--diag" in a:
        for c in DIAG:
            log(f"{c} -> {' | '.join(run(s, c).split()[2:])}", "ALERT")
    if "--dump" in a:
        for c in DUMPS:
            log(f"{c} -> {' | '.join(run(s, c).split()[2:])}", "ALERT")
    if "--ping" in a:
        out = run(s, f"setenv ipaddr {dev.U2_IP}; setenv serverip 192.168.25.30; ping 192.168.25.30", 20.0)
        log(f"ping -> {' | '.join(out.split())[:400]}", "ALERT")
    return 0


def main() -> int:
    a = sys.argv[1:]
    if not dev._ensure_console() or not console.restart(console.BAUD):
        return 1
    s = console.Session()
    try:
        if "--here" in a:  # board already at the awto-uboot prompt: only dump / ping
            s.write(b"\x03\r\n")  # abort a running TFTP/ping retry loop
            s.expect(dev.U2_PROMPTS, 5.0, "awto-uboot prompt")
            return dump_ping(s, a)
        if "--no-cycle" not in a:
            if not cycle_to_prompt(s):
                return 1
        _n, off, length, _i = dev._slot(["--slot", "uboot"])
        for cmd in ("sf probe 0", f"sf read {dev.LOAD_ADDR} {dev.KERNEL0_OFF + off:x} {length:x}"):
            if dev._uboot_cmd(s, cmd, dev.PROMPT, dev.SF_BUDGET_S, cmd) is None:
                return 1
        if dev._bootm_watch(s, "uboot"):
            return 1
        return dump_ping(s, a)
    except console.Timeout as e:
        log(str(e), "FATAL")
        return 1
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
