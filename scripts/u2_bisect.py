#!/usr/bin/env python3
"""u2_bisect — cold board, then apply the dropped stock write groups one at a time (hr2x) and check CPU RX after each.

Usage: scripts/u2_bisect.py [--no-cycle] [--here] [--groups 0,1,2,...]   (default groups: cumulative, order below)
- Per step: `hr2x <1 << g>` (cumulative), one `ping` of the host (3 s budget, Ctrl-C if it does not return),
  then CMIC ch1 RXPKT (0x31488) and IPIPE HOL_DROP (blk 10 0x44005100). Pass for a step = ping alive or RXPKT > 0.
- Group ids = gen_bcm_hr2_init.py XGROUPS: 0 cpu_cos_map 1 egr_pri_cng_map 2 source_trunk_map 3 ipipe_mem 4 epipe_mem
  5 ser 6 gport12 7 xlport 8 other.
- Ping budget: 4 s (first ping 30 s incl. cold init) = ARP + ICMP round trip on a link that is already up (<10 ms) plus the first-ping GPHY AN/link wait (~3 s).
  Expiry = Ctrl-C, step counted as RX fail. Log: tmp/logs/u2_bisect.log
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dev  # noqa: E402
import u2_cold  # noqa: E402
from dev import console, log  # noqa: E402

ORDER = [0, 1, 2, 3, 4, 5, 7, 6, 8]
NAMES = ["cpu_cos_map", "egr_pri_cng_map", "source_trunk_map", "ipipe_mem", "epipe_mem", "ser", "gport12", "xlport", "other"]
HOST = "192.168.25.30"
PING_S = 4.0
FIRST_PING_S = 30.0   # first ping runs the cold init replay (24.5k S-channel ops, ~5-10 s) before AN/ARP


def probe(s, tag: str, budget: float = PING_S) -> bool:
    out = u2_cold.run(s, f"ping {HOST}", budget)
    alive = "is alive" in out
    if not any(p in s.buf for p in dev.U2_PROMPTS):
        s.write(b"\x03\r\n")
        s.expect(dev.U2_PROMPTS, 5.0, "prompt after Ctrl-C")
    rx = u2_cold.run(s, "cmic r 0x31488 1")
    hol = u2_cold.run(s, "schan r 10 0x44005100 2")
    rxv = rx.split("=")[-1].split()[0] if "=" in rx else "?"
    log(f"[{tag}] ping alive={alive} RXPKT={rxv} HOL_DROP={' '.join(hol.split()[4:9])}", "ALERT")
    return alive or (rxv not in ("0x00000000", "?"))


def main() -> int:
    a = sys.argv[1:]
    groups = ORDER
    if "--groups" in a:
        groups = [int(x) for x in a[a.index("--groups") + 1].split(",")]
    if not dev._ensure_console() or not console.restart(console.BAUD):
        return 1
    s = console.Session()
    try:
        if "--here" in a:
            s.write(b"\x03\r\n")
            s.expect(dev.U2_PROMPTS, 5.0, "awto-uboot prompt")
        else:
            if "--no-cycle" not in a and not u2_cold.cycle_to_prompt(s):
                return 1
            _n, off, length, _i = dev._slot(["--slot", "uboot"])
            for cmd in ("sf probe 0", f"sf read {dev.LOAD_ADDR} {dev.KERNEL0_OFF + off:x} {length:x}"):
                if dev._uboot_cmd(s, cmd, dev.PROMPT, dev.SF_BUDGET_S, cmd) is None:
                    return 1
            if dev._bootm_watch(s, "uboot"):
                return 1
        u2_cold.run(s, f"setenv ipaddr {dev.U2_IP}; setenv serverip {HOST}")
        if probe(s, "baseline xmask=0", FIRST_PING_S):
            log("baseline already receives", "ALERT")
        mask = 0
        for g in groups:
            mask |= 1 << g
            r = u2_cold.run(s, f"hr2x {1 << g}", 15.0)
            log(f"hr2x group {g} {NAMES[g]}: {' '.join(r.split()[1:])[:120]}", "ALERT")
            if probe(s, f"+{NAMES[g]} cumulative mask 0x{mask:x}"):
                log(f"RX works after group {g} {NAMES[g]} (cumulative mask 0x{mask:x})", "ALERT")
                return 0
        log("RX still 0 with all groups", "FATAL")
        return 2
    except console.Timeout as e:
        log(str(e), "FATAL")
        return 1
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
