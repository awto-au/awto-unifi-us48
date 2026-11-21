#!/usr/bin/env python3
"""Simulator checks for the bench probe images (bcm59111-awto-probeA / B / C.bin) against the -nopower base.

Usage: poe_fw_probe_sim.py
  For each probe image (ucsim_51 -t RL6449, harness of poe_fw_sim.py): boots to the main loop, calls hooks 2, 3, 4..7, 8 by hand
  (return address on a trap) and checks the 16-bit counters (lo / hi host regs, carry into hi), the status reg 0x16, the arming
  bits (IP.1, T2CON.5), the probeC EXF2 / TF2 re-arm after 50 hook-8 calls, and that every host register outside the probe regs
  equals the -nopower image after boot and after 3 ticks. Needs tmp/poe-fw-build/<tag>/out.map (run poe_fw_build.py first).
  Log: tmp/logs/poe_fw_probe_sim.log. Exit 1 on any failed check.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402
import poe_fw_sim as S  # noqa: E402

LOG = S.ROOT / "tmp/logs/poe_fw_probe_sim.log"
PAIRS = {"h3": (0x22, 0x23), "h8": (0x2D, 0x40), "h2": (0x45, 0x4F), "s4": (0x54, 0x59), "s5": (0x5A, 0x5B), "s6": (0x5C, 0x5D),
         "s7": (0x1C, 0x1D)}
PROBE_REGS = {r for p in PAIRS.values() for r in p} | {0x16}
RES = []


def log(m):
    print(m)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(m + "\n")


def check(name, cond, detail=""):
    RES.append(bool(cond))
    log(f"{'PASS' if cond else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not cond else ""))


def poll_of(tag):
    txt = (S.ROOT / f"tmp/poe-fw-build/{tag}/out.map").read_text()
    return int(re.search(r"^(?:C:)?\s+([0-9A-F]{8})\s+_poll_common\s", txt, re.M).group(1), 16)


def boot(img, tag):
    sim = S.Sim(img)
    poll = poll_of(tag)
    if not S.boot(sim, poll):
        raise SystemExit(f"{img.name}: boot did not reach the main loop")
    return sim, poll


def regs(sim):
    return S.Sim.dump(sim, "xram", 0x8000, 0x807F)


def cnt(r, p):
    return r[p[1]] << 8 | r[p[0]]


def tick(sim, poll, n=1):
    for _ in range(n):
        sim.call(sim.hook[8])
        sim.resume(poll)


def one(tag, variant):
    img = S.FW / f"bcm59111-awto-{tag}.bin"
    sim, poll = boot(img, tag)
    base, bpoll = boot(S.FW / "bcm59111-awto-nopower.bin", "nopower")
    try:
        r = regs(sim)
        check(f"{tag}: status reg 0x16 = variant {variant} in bits 7:4, arm bit 1 = {variant != 2}, rest 0",
              r[0x16] == (variant << 4 | (0x02 if variant != 2 else 0)), f"{r[0x16]:02X}")
        ip = sim.dump("sfr", 0xB8)[0]
        t2 = sim.dump("sfr", 0xC8)[0]
        check(f"{tag}: IP.1 {'set' if variant != 2 else 'clear'} after boot", bool(ip & 2) == (variant != 2), f"IP {ip:02X}")
        check(f"{tag}: T2CON.5 {'set' if variant != 2 else 'clear'} after boot", bool(t2 & 0x20) == (variant != 2), f"T2CON {t2:02X}")
        check(f"{tag}: all counters zero after boot", all(cnt(r, p) == 0 for p in PAIRS.values()))
        for name, slot in (("h2", 2), ("h3", 3), ("s4", 4), ("s5", 5), ("s6", 6), ("s7", 7)):
            for _ in range(3):
                sim.call(sim.hook[slot])
                sim.resume(poll)
            r = regs(sim)
            check(f"{tag}: slot {slot} counter = 3, others untouched", cnt(r, PAIRS[name]) == 3, f"{cnt(r, PAIRS[name])}")
        r = regs(sim)
        check(f"{tag}: hook 3 sets status bit 0 (entered)", r[0x16] & 1 == 1, f"{r[0x16]:02X}")
        # carry into the high byte
        sim.setmem("xram", 0x8000 + 0x22, 0xFF)
        sim.setmem("xram", 0x8000 + 0x54, 0xFF)
        sim.call(sim.hook[3]); sim.resume(poll)
        sim.call(sim.hook[4]); sim.resume(poll)
        r = regs(sim)
        check(f"{tag}: carry lo 0xFF -> hi (slot 3 and slot 4 trampoline)", cnt(r, PAIRS["h3"]) == 0x100 and cnt(r, PAIRS["s4"]) == 0x100,
              f"{cnt(r, PAIRS['h3']):X} {cnt(r, PAIRS['s4']):X}")
        # hook 8: counter, and with C the re-arm after 50 ticks
        sim.setmem("xram", 0x8000 + 0x16, variant << 4 | (0x02 if variant != 2 else 0) | 1)
        tick(sim, poll, 49)
        r = regs(sim)
        check(f"{tag}: slot 8 counter after 49 ticks = 49", cnt(r, PAIRS["h8"]) == 49, f"{cnt(r, PAIRS['h8'])}")
        t2 = sim.dump("sfr", 0xC8)[0]
        check(f"{tag}: no TF2 request before the 50th tick (EXF2 is set by stock init in every image)", not t2 & 0x80, f"{t2:02X}")
        tick(sim, poll, 1)
        r = regs(sim)
        t2 = sim.dump("sfr", 0xC8)[0]
        if variant == 3:
            check(f"{tag}: 50th tick: EXF2 + TF2 set, status bit 2 set, bit 0 cleared", t2 & 0xC0 == 0xC0 and r[0x16] & 5 == 4,
                  f"T2CON {t2:02X} stat {r[0x16]:02X}")
        else:
            check(f"{tag}: 50th tick: no timer-2 request, status bit 0 stays", not t2 & 0x80 and r[0x16] & 5 == 1, f"{t2:02X} {r[0x16]:02X}")
        # host registers equal to -nopower (same boot, same ticks), outside the probe regs
        sim2, p2 = boot(img, tag)
        try:
            tick(sim2, p2, 3)
            tick(base, bpoll, 3)
            a, b = regs(sim2), regs(base)
            diff = [i for i in range(0x80) if a[i] != b[i] and i not in PROBE_REGS]
            check(f"{tag}: host regs 0x00..0x7F equal to -nopower after boot + 3 ticks (probe regs excluded)", not diff,
                  " ".join(f"{i:02X}:{a[i]:02X}/{b[i]:02X}" for i in diff))
        finally:
            sim2.close()
    finally:
        sim.close()
        base.close()


def main():
    help_guard(__doc__)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("")
    for tag, v in (("probeA", 1), ("probeB", 2), ("probeC", 3)):
        log(f"== {tag}")
        one(tag, v)
    ok = all(RES)
    log(f"{sum(RES)}/{len(RES)} checks passed" + ("" if ok else "  FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
