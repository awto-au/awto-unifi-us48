# uboot-2 CPU clock (#17)

**UNTESTED on hardware.** Build only (400, 800, 1000 all compile; logs `tmp/logs/build_uboot_cpuclk*.out`). Sequences = [clocks.md](clocks.md) E1 / E2.

## What changed (`uboot-port/`)
- `board/ubiquiti/us48/board.c`:
  - `us48_arm_hz()`: 25 MHz x ndiv / pdiv / mdiv of the active fid (`POLICY_DBG` 14:12; fid 6 = `PLLARMC` ch0, fid 7 = `PLLARMCTL5` ch1, fid 0 = 25 MHz, fid 2 = unknown -> 0).
  - `us48_periph_clk` clk driver: rate = ARM/2 from the live regs; 0 falls back to `CFG_SYS_HZ_CLOCK` (200 MHz).
  - `board_early_init_f()`: the CPU switch. Runs before `timer_init`, so the global timer reads the new rate at probe.
  - `board_init()`: prints `cpu: US48_CPU_MHZ=.. status=.. ARM=.. MHz PERIPHCLK=.. MHz`.
- `us48-uboot.dts`: `periph_clk` is `awto,us48-periph-clk` (was fixed 200 MHz). `apb_clk` stays fixed 50 MHz.
- `us48.h`: `CFG_SYS_HZ_CLOCK` is a fallback only; `#error` unless `US48_CPU_MHZ` is 400/800/1000.
- `us48_defconfig`: `CONFIG_BOARD_EARLY_INIT_F=y`, `CONFIG_BOARD_INIT=y`, `CONFIG_US48_CPU_MHZ=400`.

## Config macro
- `CONFIG_US48_CPU_MHZ` (Kconfig `US48_CPU_MHZ`): 400 = no change (default), 800 = E1 (fid 7, no relock), 1000 = E2 (relock ndiv 80).
- Gate (E0), checked before any write: ch1 mdiv == 2, active fid == 6, computed ARM == 400 MHz. Fail -> no write, stays 400, `status=-22` on the console.
- Relock failure (GO or PLL lock poll expires): policy back to fid 6 (VCO restored to 1600 first), `status=-110`.
- SP805: armed for 10 s at the start of a switch only if not already armed (dev.py arms it first). APB unchanged, so `SP805_LOAD` in `dev.py` is still right.

## Enable
- Set `CONFIG_US48_CPU_MHZ=800` or `1000` in `uboot-port/configs/us48_defconfig`, `python3 scripts/build_uboot.py`, then `dev.py uboot-chain` as usual.
- Expect on the console: `cpu: US48_CPU_MHZ=1000 status=2 ARM=1000 MHz PERIPHCLK=500 MHz`.

## Test later (dev.py)
- At `uboot-2>`: `md.l 19000ec0 1` bits 14:12 = 7; `md.l 19000c00 1`; `sleep 5` and compare with wall clock (udelay check); `crc32` of a RAM block twice.
- Then the clocks.md E1 Linux pass criteria (`sched_clock: 64 bits at 500MHz` at 1000), thermal log, 5 then 20 power cycles.
- Order: 800 first, then 1000.

## Other consumers of the rate
- Linux reads the PLL regs (`clk-hr2`), so the clock tree follows. Kernel timer/`sched_clock` rate changes accordingly.
- uboot-1 private A9 WDT: stopped at `bootm` to uboot-2, not an issue here.
- SP805, UART (APB 50 MHz), QSPI (`CONFIG_SF_DEFAULT_SPEED`, BSPI clock): not ARM-derived, unchanged.
- Ethernet/CMIC driver delays use `udelay`, now correct at any ARM rate.

## Risks
- 1000 MHz: relock, no core voltage margin (clocks.md). 800: low.
- Poll limits are read counts (no timer before `timer_init`): 4096 reads for GO (expected ~10 us), 2000 for PLL lock (expected tens of us); both I, unmeasured.
- Hang with no SP805 armed: covered by the 10 s arm; SP805 reset returns to stock U-Boot at 400.
- `PLLARM_OFFSET` bit 29 (SW offset mode) is not checked by `us48_arm_hz()`.
