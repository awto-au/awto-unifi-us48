# Core SoC bring-up (#84)

- Order: each item on a2, then a1. Tests are 1-2 s (`scripts/core_test.py <item> [--unit a1|a2]`), one result line each into `tmp/logs/core_test.log`.
- Results go in the table: number + conditions + date. Empty = not run yet.
- First board run 2026-11-15: debug image `7.4.0-rc1-dbg+`, RAM-booted. a1 = ARM 1000 MHz. a2 = ARM 100 MHz (a2 corrupts memory above 100 MHz, #79), awto-uboot then `bootz`, DTB with `--periph`; L2 off unless stated.

## Table

| # | Item | Driver | Module | Test | a1 result | a2 result |
|---|---|---|---|---|---|---|
| 1 | Caches L1/L2 | `CACHE_L2X0` (PL310, errata), A9 L1 | built in | `core_test caches`: PL310 id/ctrl/aux + `us48bench lat/bw` | L2 PL310 on (id 0x410000c9, aux 0x4e120001); lat ns 4K 4.9, 32K 9.6, 256K 137, 8M 187; bw MB/s 16K rd/wr/cp 1652/1662/1185, 8M 495/1521/431 | L2 off: lat ns 4K 52, 32K 161, 2M 1520; bw 8M rd/wr/cp 22/254/23. L2 on (`--l2`): lat 2M 1499, bw 8M 55/259/59 (1 of 2 L2-on boots oopsed in the first bw run, #79) |
| 2 | CPU clock | `CLK_BCM_HR2` | built in | `core_test clock`: `us48bench spin` vs `--mhz` | `spin` 976 MHz vs 1000 (0.98) | `spin` 92 MHz vs 100 (0.92) |
| 3 | Timers | `ARM_GLOBAL_TIMER`, TWD, `ARM_TIMER_SP804` | built in | `core_test timers`: tick drift (a1), cyclictest 1 s | clocksource `arm_global_timer`; tick drift -873 ppm (+-1000 ppm jitter, 2 s window: not a drift number); cyclictest n/a (musl ENOSYS on sched_*scheduler; `tools/rt-tests-musl.patch` now uses the raw syscalls, not yet run on the board) | `arm_global_timer`; cyclictest n/a (musl); drift not measured (DCC poll too coarse) |
| 4 | Interrupts | `ARM_GIC` | built in | `core_test irq`: `/proc/interrupts` delta 1 s; MSI is #54 | `gt` (GIC 27) 103 irq in 1 s (HZ 100 + load) | `gt` 144 irq in 1 s (parse window ~1.4 s) |
| 5 | DMA | `PL330_DMA` + `us48_idm_reset` | `pl330.ko`, `us48_idm_reset.ko` | `core_test dma`: load idm reset module, `pl330` (probe releases the reset via DT `resets`), `dmatest.ko` 20 iterations | first run (2026-11-15): `RESET_CONTROL` read 0x1; manual `devmem` release then `insmod pl330` + dmatest hung the board (power-cycled). Re-run with the reset controller: not yet | not run (`--dmac` DTB needs a1's release of unit 1 IDM) |
| 6 | Memory | - | - | `core_test memory`: `memtester 1M 1` (a2 512K), meminfo split | `memtester 1M x1` rc 0; MemTotal 178260 kB (Low 47188, High 131072) | `memtester 512K x1` rc 0; MemTotal 47732 kB (L2 on: 47728) |
| 7 | RNG | `HW_RANDOM_BCM2835` (nsp layout) | built in | `core_test rng`: `rngtest` on 20000 bits | `18033000.rng`, `rngtest` 8 blocks, 0 fail | `rngtest` 8 blocks, 0 fail |
| 8 | Watchdog | `ARM_SP805_WATCHDOG` | built in | `core_test wdt`: sysfs + VALUE reads, no reset test | `sp805-wdt` registered, `/dev/watchdog0`; VALUE reads 0x1dacc411 -> 0x1ba5ba87 (counting) | VALUE reads 0xffffffff both (not armed: nothing arms SP805 on a2 in Linux boot; awto-uboot a2 also leaves it off) |
| 9 | Flash | `SPI_BCM_QSPI` | built in | `core_test flash`: 1 MiB `/dev/mtd0ro` read (a1 only) | `mtd0` 1 MiB read 2.22 MB/s (1-lane BSPI RAF; DT `spi-rx-bus-width = <4>` added since, not yet run, see Flash read path) | n/a (QSPI unit 0 only) |
| 10 | Buses | `I2C_BCM_IPROC`, `PINCTRL_IPROC_GPIO` | built in | `core_test buses`: adapter + gpiochip listing only | 11 I2C adapters, 3 gpiochips | no nodes in the a2 DTS (0 listed) |
| - | Self-tests | `TEST_BITMAP`, `LOCK_TORTURE_TEST`, `RCU_TORTURE_TEST`, `TEST_UDELAY` | `test_bitmap`, `locktorture`, `rcutorture`, `torture`, `udelay_test` | `core_test selftest` (debug image) | `test_bitmap`, `locktorture`, `rcutorture` ok (~2 s each) | not run: the one-line command is ~300 chars, a1 -> ttyA20 DCC input stalled the console (a2 reset to recover) |

## Memory map of a1 (#84 item 11, `mainline/0018`)

- DDR in the DT: bank 0 `0x00000000` 128 MiB, bank 1 `0x68000000` 128 MiB = 256 MiB. Holes and reservations in the Linux image:

| Region | Size | Kind | Note |
|---|---|---|---|
| `0x04000000..0x07ffffff` `a2@4000000` | 64 MiB | CMA pool, `reusable`, fixed address | movable memory while a2 is off; `us48_a2` takes it whole at start. Was `no-map` (lost to Linux, split lowmem) |
| default CMA `cma=8M@32M-64M` | 8 MiB | CMA (cmdline) | coherent DMA of CMIC/PL330; stays |
| KFENCE pool (debug) | 63 objects x 2 pages | reserved at boot | debug profile only |
| kernel image, memmap, initramfs | per build | reserved | sizes below |
| uboot-2 | none | | `TEXT_BASE 0x04000000` (inside the pool, dead after the hand-over), relocates to `ram_top` 0x70000000 = bank 1, inside the memory node: Linux owns it again. `docs/uboot-second-stage.md` |
| uboot-1 (stock) | none | | own copy at `0x6ffa0000` and the ubnt app at `0x07030000` are dead after the hand-over; no DT reservation for either; ATAGs at `0x8000` ignored (`ARM_ATAG_DTB_COMPAT` off) |
| L2 locked-as-RAM `0x50000000`, SRAM `0x1b000000`, flash `0x1c000000` | | not DDR | SRAM is an `mmio-sram` node (docs/sram.md) |

- Not a hole, by design: uboot-2's CMIC DMA region `0x03000000` is ordinary RAM to Linux.
- Lowmem is now one 128 MiB block (`0..0x08000000`), highmem is bank 1. The pool is `memblock_reserve`d until `cma_activate_area()`, so early allocations (KASAN shadow, memmap) only have the other 64 MiB minus the default CMA.
- Expected `MemTotal` (not measured; first board run fills it in):
  - before (`no-map`, first board run): 178260 kB.
  - a2 off: about +65536 kB (pool) - 512 kB (memmap for it) = ~243300 kB for the same image; the KASAN debug image takes more (shadow 16 MiB, kernel 6.3 MiB larger): estimate ~226000 kB.
  - a2 on: `MemTotal` unchanged (`cma_alloc` does not change totalram); `MemFree` and `CmaFree` drop by 65536 kB.
- a2 side (Linux on a2): its DTB memory node is the 63 MiB carveout minus `a2_shared`; awto-uboot on a2 relocates to `ram_top` 0x0bf00000 inside that node, so it is returned to a2 Linux like on a1 (MemTotal 47732 kB measured with the 1 MiB shared reserve; expected ~48 MB minus kernel/memmap, nothing else held).
- Offline builds (no warnings, DTB W=1 clean, a2 DTB clean): normal zImage 3.75 MB (13 MiB slot ok), debug zImage 8.16 MB, DTB 11 KB, a2 DTB 2.6 KB (2.8 KB with L2).
- KASAN (debug fragment, outline): vmlinux text+data+bss 23.0 MiB (was 16.7 MiB without), zImage 7.8 MiB. Early lowmem need ~23 + 16 (shadow) + ~4 (memmap, page_ext) = ~43 MiB against ~56 MiB (64 - 8 default CMA): fits on paper. Boot result: not run.

## Drivers and DT

- Normal profile: `PL330_DMA=m` only (a module for the swap-without-reboot rule). Debug adds `DMATEST`, `TEST_*`, `*_TORTURE_TEST` as `=m` (`mainline/us48_debug.config`).
- Already present, `=y` in `mainline/us48_defconfig`: QSPI (`SPI_BCM_QSPI`), I2C (`I2C_BCM_IPROC`), SP804, SP805, global timer, PL310, GIC.
- SP805 stays built in: awto-uboot arms it (~46 s) and a module would leave it unfed until the insmod. `WATCHDOG_HANDLE_BOOT_ENABLED=y` makes the watchdog core feed it until userspace opens `/dev/watchdog`, so the busybox feeder is gone from `rc.sh`. The driver default timeout (60 s) applies after probe.
- PL330 (a1 DTS patches 14, 15): `bcm-hr2.dtsi` `dma@20000` had no `apb_pclk`, so the AMBA bus never probed it. Added the 50 MHz APB clock, a `dma` label and `arm,primecell-periphid = <0x00041330>` (no ID read at boot); the board DTS sets `okay`.
- PL330 gate: `dmac_idm.RESET_CONTROL` (0x18114800) resets to 1 (held in reset); an access while it is 1 stalls the bus. Owner: `kernel/us48-idm-reset` (reset controller module, DT `dmac_idm` in the board DTS, patch 16), consumed by `pl330` as `resets = <&dmac_idm 0>; reset-names = "dma"`. pl330 defers until the module is loaded. Which boot stage releases it on the board is unmeasured.
- RNG: regdb `CHIPCOMMONB_RNG_*` at 0x18033000 is the iProc/NSP layout (CTRL +0, STATUS +4, DATA +8, FF_THRES +0xc, INT_MASK +0x10), handled by `bcm2835-rng` (compatible `brcm,bcm-nsp-rng`, already in the dtsi, patch 9). `iproc-rng200` wants its FIFO at +0x20: not built, and the #29 comment calling this block RNG200 is incorrect.
- SP804 on a1 is already the DT `ccbtimer0/1`; the clocksource/clockevent choice (global timer rating 300 vs SP804) is what `timers` shows.
- IRQ numbers and PL330 SPI 47-54/63 come from upstream `bcm-hr2.dtsi`/`bcm-nsp.dtsi`: conf I for HR2 (not checked against the SDK).

## PL330 release sequence (#84 item 5)

- Registers (hwdb `dmac_idm`, base 0x18114000): `IO_CONTROL_DIRECT` +0x408 (reset 0), `RESET_CONTROL` +0x800 (bit 0, reset 1).
- Sequence in `us48_idm_reset` deassert: read-modify-write clear bit 0 of `RESET_CONTROL`; read back until bit 0 = 0 (limit 2 us: one AXI round trip, expected under 1 us, 1.25x rounded up; log + `-ETIMEDOUT` on expiry); settle 900 us (D-Link `udelay(1000)` minus 10 %). Assert = set bit 0 (pl330 remove).
- Module param `clk_en=1` (hypothesis test only, default 0): set `IO_CONTROL_DIRECT` bit 0, read back, settle 900 us, then the sequence above.
- Sources:
  - BISDN `soc/bcm/xgs_iproc/xgs-iproc-idm.c` `xgs_iproc_idm_dmac_reset()` (armel-iproc patch 10-01, HR2 offset 0x14800 = our 0x18114800): `writel(readl(reset) & ~0x1)`, no clock write, no wait; runs before `of_platform_populate`.
  - D-Link HR2 GPL `arch/arm/mach-iproc/board_bu.c`: SDIO `setup_sdio()` writes 0 to `IDM_RESET_CONTROL` only; SATA `config_AHCI()` sets `IO_CONTROL_DIRECT` |= 1, `udelay(1000)`, writes `RESET_CONTROL` 0, `udelay(1000)`. Its PL330 platform device has no IDM write at all (boot stage releases it).
  - Upstream: no iProc IDM reset driver. `reset-simple` would fit the register (bit 0, active high) but has no settle and no compatible for it; NSP/HR2 `dma@20000` has no `resets`. pl330 already takes an optional `dma` reset (`drivers/dma/pl330.c`).
  - regdb names `IO_CONTROL_DIRECT` bit 0 `CLK_GATING_EN`, not a clock enable: the clock-enable cause from the first run is NOT confirmed by any source; BISDN releases the DMAC without touching it.
- Not established: the hang cause. Candidates left: PL330 AXI attributes (`AXUSER_CONFIG`, `CC_n.*_CACHE_CTRL`), DMAC access before the settle, pl330 probe reading registers. The module logs `IO_CONTROL_DIRECT`/`RESET_CONTROL` before and after the release so the last console line locates a hang.

## Flash read path (#20, #84 item 9)

- Linux `spi-bcm-qspi` already reads through BSPI: DT has the `bspi` reg (`has_bspi`), aligned reads of 4 bytes or more use the RAF FIFO in 256-byte chunks (`BSPI_READ_LENGTH`) with one interrupt each; commands, SFDP and short/unaligned reads use MSPI (PIO). `spi-max-frequency = <12500000>` only sets the MSPI SCK (SPBR minimum 8, = 3.125 MHz at the 50 MHz APB): not a read-speed lever.
- BSPI SCK = CRU `QSPI_CLK_SEL` (hwdb `cru.CONTROL`), set by the boot stage; Linux has no driver for it. `core_test flash` should read `devmem 0x1803e000` first.
- Change (patch 17): `spi-rx-bus-width = <4>` on `flash@0`: spi-nor picks 1-1-4 (0x6b, 8 dummy), the BSPI flex mode sends data on 4 lines; programming stays 1-1-1. QE is already set (#20), so `spi-nor` does not write SR. MX25L25635E has no 4-byte opcodes: Linux still enters EN4B as before (hazard in #20 unchanged, flash stays read-only).
- Expected, model not measured: per 256-byte chunk 2088 SCK (1-lane) vs 552 (quad) plus a fixed ~35 us of interrupt/FIFO time (inferred from 2.22 MB/s at 25 MHz). Quad at 25 / 50 / 62.5 MHz: about 4.5 / 5.5 / 5.8 MB/s; 1-lane at 50 MHz about 3.4 MB/s. Beyond that needs fewer interrupts per byte (larger RAF chunk), a driver change.

## a2 (unit 1) nodes

- `mainline/a2/us48-a2.dts`: `apb_clk` (50 MHz), `rng`, `watchdog` (SP805), `timer` (SP804 0), `dma` (PL330). Addresses from hwdb blocks `rng`, `sp805`, `sp804_0`, `dmac` via `A2_*` macros; IRQs and the APB clock copied from unit 0: all conf I on unit 1.
- Off by default (`status = "disabled"`). `scripts/build_a2_linux.py --periph` enables rng/sp805/sp804; `--dmac` enables the PL330 and only after a1 released `dmac_idm.RESET_CONTROL` for unit 1 (a1 reaches it over PCIe, not done here).
- SP805 and PL330 use fixed `arm,primecell-periphid`: no ID read at a2 boot.

## Tools (debug image only)

- `scripts/build_rootfs.py --tools` (called for `--profile debug`): into `/usr/bin` of the debug initramfs: `memtester` 4.7.1, `cyclictest` (rt-tests 2.11, `tools/numa-stub` stands in for libnuma, `tools/rt-tests-musl.patch` maps `sched_{get,set}{scheduler,param}` to raw syscalls: musl returns ENOSYS), `tinymembench` (ssvb master a2cf6d7), `us48bench` (`tools/us48bench.c`: lat, bw, spin, tick, rd), `rngtest` (`tools/rngtest.c`, FIPS 140-2; replaces rng-tools which needs autotools + libgcrypt). About 310 KB stripped.
- In the image, not a side archive: a2 has no network to take an scp, and one image serves both units. The normal profile has none (13 MiB slot).
- `tinymembench` runs about a minute: not part of `core_test`; run by hand when Dan approves.
- Sources pinned by sha256 in the script (`/mnt/1tnvme/toolchains/dl`); built tools cached in `tmp/rootfs/tools-bin`.

## a2 access for `core_test --unit a2`

- Reader on a1: `cat /dev/ttyA20 > /tmp/a2.out &` before `echo start` (docs/a2-linux.md). The runner types the command into `/dev/ttyA20` and polls `/tmp/a2.out` for a marker; tick drift (`timers`) is a1 only.
- Limits (`A1_LIMIT_S` 13, `A2_LIMIT_S` 40, memory 20/60) are estimates until the first board run.

## Bandwidth budget (#84 item 0, a1 ARM 1000 MHz, L2 on, 2026-11-15, debug image, `us48bench`, DDR 667)

| Stage | Measured | Theoretical | Gap / why |
|---|---|---|---|
| DDR read, 8 MiB working set | 495 MB/s | not measured (#55) | single A9 stream, no prefetch tuning |
| DDR write | 1521 MB/s | | write buffer + L2 merge |
| DDR copy | 431 MB/s | | read-bound |
| DDR latency, 8 MiB / 16 MiB | 187 / 202 ns | | L2 (256 KiB) hit 137 ns at 256K, L1 4.9 ns |
| L1/L2-resident (16 KiB) rd/wr/cp | 1652 / 1662 / 1185 MB/s | | CPU-bound |
| PL330 memcpy DDR->DDR | not measured | | dmatest blocked: IDM release then `insmod pl330` hung the board (item 5) |
| CMIC packet-DMA RX pps / MB/s | not measured | | no quick test exists (needs a traffic generator + RX loop, no script) |
| a2 -> host DDR over PCIe (a2 at 1000, #80, 2026-11-15) | cached rd 113 / wr 1061, uncached rd 14 / wr 123 MB/s, 298 ns | PCIe x1 gen2 ~400 MB/s | from #80, not rerun |
| a2 own DDR path at 100 MHz (this run) | L2 off rd/wr/cp 22/254/23 MB/s, 1520 ns; L2 on 55/259/59 MB/s, 1499 ns | | a2 RAM is host DDR over PCIe: every miss crosses it |
