#!/usr/bin/env python3
"""Cycle cost of the stock BCM59111 periodic ISR (hook 3, 0x4EBA) and tick ISR (hook 8, 0x4FCD) on ucsim_51 -t RL6449 (docs/poe-fw-seq/05-timing.md).

Usage: poe_fw_isr_cycles.py [--meraki] [--irq T0_COUNTS]
  --meraki: Meraki v18 image (hwdb/firmware/bcm59111-meraki.bin, addresses of its hook 3 / hook 8 and chain functions).
  --irq N: also runs the stock image interrupt-driven: ROM vector stubs (0x0B -> hook 3, 0x1B -> hook 8, PUSH ACC/B/DPL/DPH, RETI),
  timer 0 mode 1 reloaded to N counts (12 clk each) in its stub as the stand-in periodic source (the real source is unknown),
  timer 1 as stock; logs the share of simulated time spent in ISRs. Shows the simulator can run the stock ISR chain.
  Boots hwdb/firmware/bcm59111-v21.bin to the main loop (poe_fw_sim.boot: ROM absent, analog SFRs plain registers), then calls
  each hook with XRAM state forced per scenario and reads the simulated clock (`state`, 4 clk per machine cycle) before / after.
  The analog stubs return constants, so SFR-polling paths run their minimum; scenarios are lower bounds for those paths.
  Output: clks and machine cycles per scenario; time = cycles * 4 / f_cpu. Log: tmp/logs/poe_fw_isr_cycles.log.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402
import poe_fw_sim as S  # noqa: E402

LOG = S.ROOT / "tmp/logs/poe_fw_isr_cycles.log"
HOOK_PERIODIC = 0x4EBA
HOOK_TICK = 0x4FCD
POLL = S.STOCK_POLL
CHAIN = ((0x1B03, "meas_cur_offset_trim"), (0x37D2, "meas_temp_calib"), (0x34D0, "meas_convert_cm_vm_temp"),
         (0x29EE, "periodic_dc_mux_scheduler"), (0x316B, "periodic_detcls_sequencer"))


def log(msg):
    print(msg)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(msg + "\n")


def clks(sim):
    """(clks, instructions, memory reads, memory writes) from `state`."""
    t = sim.c("state")
    m = re.search(r"Total time since last reset= [0-9.]+ sec \((\d+) clks\)", t)
    i = re.search(r"Inst= (\d+) Fetch= \d+ Read= (\d+) Write= (\d+)", t)
    return int(m.group(1)), int(i.group(1)), int(i.group(2)), int(i.group(3))


def measure(sim, target, setup):
    sim.batch(setup)
    t0 = clks(sim)
    sim.call(target)
    t1 = clks(sim)
    return tuple(b - a for a, b in zip(t0, t1))


def irq_run(sim, n):
    """Interrupt-driven run: stand-in ROM vectors, T0 = periodic source with n counts, T1 = tick (stock reload)."""
    rl = 65536 - n

    def wrap(addr, hook, pre):
        b = pre + [0xC0, 0xE0, 0xC0, 0xF0, 0xC0, 0x82, 0xC0, 0x83, 0x12, hook >> 8, hook & 255,
                   0xD0, 0x83, 0xD0, 0x82, 0xD0, 0xF0, 0xD0, 0xE0, 0x32]
        sim.c(f"set mem rom 0x{addr:x} " + " ".join(f"0x{x:x}" for x in b))

    wrap(0x100, HOOK_PERIODIC, [0x75, 0x8C, rl >> 8, 0x75, 0x8A, rl & 255])
    wrap(0x120, HOOK_TICK, [])
    sim.batch(["set mem rom 0x0b 0x02 0x01 0x00", "set mem rom 0x1b 0x02 0x01 0x20", "set mem sfr 0x89 0x11",
               f"set mem sfr 0x8c 0x{rl >> 8:x}", f"set mem sfr 0x8a 0x{rl & 255:x}", "set mem sfr 0x8b 0xf4", "set mem sfr 0x8d 0xe5",
               "delete 1 2", "set mem xram 0x8103 1", "set mem sfr 0x88 0x50", "set mem sfr 0xa8 0x8a"])
    sim.c("step 1500000")
    t = sim.c("state")
    m = re.search(r"Total time since last reset= [0-9.]+ sec \((\d+) clks\)\s+Time in isr = [0-9.]+ sec \((\d+) clks\)", t)
    tot, isr = int(m.group(1)), int(m.group(2))
    log(f"irq run: T0 period {n * 12} clks: ISR share {100 * isr / tot:.1f} % of {tot} clks")


def main():
    help_guard(__doc__)
    LOG.write_text("")
    global HOOK_PERIODIC, HOOK_TICK, POLL, CHAIN
    img = S.FW / "bcm59111-v21.bin"
    if "--meraki" in sys.argv:
        img = S.FW / "bcm59111-meraki.bin"
        HOOK_PERIODIC, HOOK_TICK, POLL = 0x4EFD, 0x5010, 0x2304
        CHAIN = ((0x1B03, "meas_cur_offset_trim"), (0x380C, "meas_temp_calib"), (0x350A, "meas_convert_cm_vm_temp"),
                 (0x29E5, "periodic_dc_mux_scheduler"), (0x31A4, "periodic_detcls_sequencer"))
    sim = S.Sim(img)
    try:
        if not S.boot(sim, POLL):
            log("FAIL boot did not reach the main loop")
            return 1
        base = [f"set mem xram 0x8103 0", "set mem xram 0x80c0 0"]
        log(f"image {img.name}; stack/trap overhead of the call harness is included (a few clks)")
        rows = [("periodic, idle (0x8103 = 0, 0x80C0 = 0)", HOOK_PERIODIC, base),
                ("tick (hook 8), no timers armed", HOOK_TICK, base)]
        # DC scheduler sub-states 0x8134 = 1..5 with channel 0 active (0x8103 = 1) and tick counter 0x8135 forced
        for st, tk in ((1, 4), (1, 5), (2, 8), (3, 13), (4, 16), (5, 9), (5, 10), (0, 0)):
            rows.append((f"periodic, DC scheduler state {st} tick {tk} -> {tk + 1} (0x8103 = 1)", HOOK_PERIODIC,
                         ["set mem xram 0x8103 1", f"set mem xram 0x8134 {st}", f"set mem xram 0x8135 {tk}", "set mem xram 0x80c0 0"]))
        # detect / class sequencer 0x316B: record 0x8104 + 7c states with 0x80C0 bit 4 (channel 0 active)
        for st in (2, 3, 4, 5, 6, 8, 0x0A, 0x0D):
            rows.append((f"periodic, sequencer record state {st:#04x} channel 0 (0x80C0 = 0x10)", HOOK_PERIODIC,
                         ["set mem xram 0x8103 0", "set mem xram 0x80c0 0x10", "set mem xram 0x8076 0", f"set mem xram 0x8104 {st}",
                          "set mem xram 0x8105 0", "set mem xram 0x8106 0"]))
        for fn, nm in CHAIN:
            rows.append((f"function {fn:#06x} {nm}, idle", fn, base))
        for name, tgt, setup in rows:
            n, ins, rd, wr = measure(sim, tgt, setup)
            log(f"{name}: {n} clks = {n / 4:.0f} cycles, {ins} insns, reads {rd}, writes {wr}")
        if "--irq" in sys.argv:
            irq_run(sim, int(sys.argv[sys.argv.index("--irq") + 1]))
    finally:
        sim.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
