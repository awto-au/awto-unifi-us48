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
| 5 | DMA | `PL330_DMA` + `us48_idm_reset` | `pl330.ko`, `us48_idm_reset.ko` | `core_test dma`: load idm reset module, `pl330` (probe releases the reset via DT `resets`), `dmatest.ko` 20 iterations | 2026-11-16 (normal image, kernel0): `release:`/`released:` lines, `Loaded driver for PL330 DMAC-41330` (8 chan, 16 events), 8 channels x 100 x 256 KiB copies 0 failures; 20 verified copies x 8 chan (16 KiB) 0 failures || not run (`--dmac` DTB needs a1's release of unit 1 IDM) |
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
- KASAN (debug fragment, outline): vmlinux text+data+bss 23.0 MiB (was 16.7 MiB without), zImage 7.8 MiB. Early lowmem need ~23 + 16 (shadow) + ~4 (memmap, page_ext) = ~43 MiB against ~56 MiB (64 - 8 default CMA): did not fit as a 3G/1G kernel (OOM); the debug profile uses `VMSPLIT_1G` + SPARSEMEM, all 256 MiB lowmem, boots (docs/mainline.md Profiles; `MemTotal` 192980 kB, `CmaTotal` 73728 kB, board 2026-11-16).

## Drivers and DT

- Normal profile: `PL330_DMA=m` only (a module for the swap-without-reboot rule). Debug adds `DMATEST`, `TEST_*`, `*_TORTURE_TEST` as `=m` (`mainline/us48_debug.config`).
- Already present, `=y` in `mainline/us48_defconfig`: QSPI (`SPI_BCM_QSPI`), I2C (`I2C_BCM_IPROC`), SP804, SP805, global timer, PL310, GIC.
- SP805 stays built in: awto-uboot arms it (~46 s) and a module would leave it unfed until the insmod. `WATCHDOG_HANDLE_BOOT_ENABLED=y` makes the watchdog core feed it until userspace opens `/dev/watchdog`, until `S01us48-wdt` starts busybox `watchdog -T 30 -t 5` (#159): userspace owns the SP805 from then on, a stopped or hung feeder resets the board 30 s later (bench 2026-11-23: SIGSTOP at 19:58:08, reset banner 19:58:37). `CONFIG_WATCHDOG_OPEN_TIMEOUT=60` stops the kernel feeding if nothing opened it within 60 s; cmdline `panic=5` reboots 5 s after a panic (#160).
- PL330 (a1 DTS patches 14, 15): `bcm-hr2.dtsi` `dma@20000` had no `apb_pclk`, so the AMBA bus never probed it. Added the 50 MHz APB clock, a `dma` label and `arm,primecell-periphid = <0x00041330>` (no ID read at boot); the board DTS sets `okay`.
- PL330 gate: `dmac_idm.RESET_CONTROL` (0x18114800) resets to 1 (held in reset); an access while it is 1 aborts (imprecise external abort). Owner: `kernel/us48-idm-reset` (reset controller module, DT `dmac_idm`, patch 16), consumed by `pl330` as `resets = <&dmac_idm 0>; reset-names = "dma"`. Release = clear bit 0 only; `IO_CONTROL_DIRECT` is 0xc003f003 at boot (bit 0 already set), no write needed.
- Root cause of the imprecise abort (#84): `CONFIG_RESET_CONTROLLER` was off, so `devm_reset_control_get_optional()` is a stub returning NULL and pl330 never called the deassert; its first register read (`CRD`, +0xe14, in `pl330_add`) hit the DMAC in reset. Fix: `CONFIG_RESET_CONTROLLER=y` in `mainline/us48_defconfig`. The debug profile never showed it only because its panic came earlier (IRQs).
- PL330 IRQ lines: event N is on GIC 85+N = SPI 53+N, board-measured (dmatest single channel = event 0 counts on GIC 85 only; GIC 84 never fires). SPI 52 is not an event line; with SPI 52-59, event 7 (8th channel) had no line and timed out (patch 20 now 53-60, abort 69 by the D-Link ids 85..100 events + 101 abort, abort line unmeasured).
- RNG: regdb `CHIPCOMMONB_RNG_*` at 0x18033000 is the iProc/NSP layout (CTRL +0, STATUS +4, DATA +8, FF_THRES +0xc, INT_MASK +0x10), handled by `bcm2835-rng` (compatible `brcm,bcm-nsp-rng`, already in the dtsi, patch 9). `iproc-rng200` wants its FIFO at +0x20: not built, and the #29 comment calling this block RNG200 is incorrect.
- SP804 on a1 is already the DT `ccbtimer0/1`; the clocksource/clockevent choice (global timer rating 300 vs SP804) is what `timers` shows.
- Tick (#84 item 6): the base had `HZ_PERIODIC`, `HZ_100`, no `HIGH_RES_TIMERS` (cyclictest 1000 us: min 4248 / avg 9192 / max 10477 us, 10 ms tick granularity, debug image 2026-11-15). Base now `NO_HZ_IDLE` + `HIGH_RES_TIMERS` (selects `TICK_ONESHOT`; `arm_global_timer` is a per-CPU oneshot clockevent, no broadcast device needed on one CPU). `HZ`: normal 1000 (rated max; 1 ms jiffy timers, cost one tick IRQ per ms when busy), debug 250 (checks make each tick dearer). Also affects the 1 ms hrtimer poll in us48_cmic (#56).
- PL330 IRQs (patch 20): SPI 52-59 events, 68 abort. Upstream 47-54/63 were copied from NSP.
  - D-Link GPL `plat-iproc/include/mach/irqs.h`: GIC id = SPI + 33 (NSP: UART0 id 118 = SPI 85, DMAC id 80 = SPI 47, both match upstream NSP).
  - HR2 matches the `CONFIG_MACH_HX4`/`KT2` branch at both ends of the DMAC: UART0 SPI 91, NAND RBB SPI 74 (both in `bcm-hr2.dtsi`). That branch gives DMAC id 85 = SPI 52 (16 event lines), abort id 101 = SPI 68.
  - SPI 47-50 are IDM error lines `DS_1..DS_4`, 51 is the DDR controller: not DMAC lines. 63 is NAND RD_MISS. Conf I (HX4 branch used for HR2; no HR2 branch in the header).

## PL330 release sequence (#84 item 5)

- Registers (hwdb `dmac_idm`, base 0x18114000): `IO_CONTROL_DIRECT` +0x408 (reset 0), `RESET_CONTROL` +0x800 (bit 0, reset 1).
- Sequence in `us48_idm_reset` deassert: read-modify-write clear bit 0 of `RESET_CONTROL`; read back until bit 0 = 0 (limit 2 us: one AXI round trip, expected under 1 us, 1.25x rounded up; log + `-ETIMEDOUT` on expiry); settle 900 us (D-Link `udelay(1000)` minus 10 %). Assert = set bit 0 (pl330 remove).
- Module param `clk_en=1` (hypothesis test only, default 0): set `IO_CONTROL_DIRECT` bit 0, read back, settle 900 us, then the sequence above.
- Sources:
  - BISDN `soc/bcm/xgs_iproc/xgs-iproc-idm.c` `xgs_iproc_idm_dmac_reset()` (armel-iproc patch 10-01, HR2 offset 0x14800 = our 0x18114800): `writel(readl(reset) & ~0x1)`, no clock write, no wait; runs before `of_platform_populate`.
  - D-Link HR2 GPL `arch/arm/mach-iproc/board_bu.c`: SDIO `setup_sdio()` writes 0 to `IDM_RESET_CONTROL` only; SATA `config_AHCI()` sets `IO_CONTROL_DIRECT` |= 1, `udelay(1000)`, writes `RESET_CONTROL` 0, `udelay(1000)`. Its PL330 platform device has no IDM write at all (boot stage releases it).
  - Upstream: no iProc IDM reset driver. `reset-simple` would fit the register (bit 0, active high) but has no settle and no compatible for it; NSP/HR2 `dma@20000` has no `resets`. pl330 already takes an optional `dma` reset (`drivers/dma/pl330.c`).
  - regdb names `IO_CONTROL_DIRECT` bit 0 `CLK_GATING_EN`, not a clock enable: the clock-enable cause from the first run is NOT confirmed by any source; BISDN releases the DMAC without touching it.
- `dmatest` is not in the normal image: build `drivers/dma/dmatest.c` out of tree against `tmp/mainline/build-normal` (`obj-m += dmatest.o`, `make M=...`) and scp it; the debug image panics at boot with KASAN (#84 item 11). Run: `insmod dmatest.ko max_channels=8 threads_per_chan=1 iterations=100 test_buf_size=262144 norandom=1 noverify=1 timeout=500 wait=1 run=1`; max_channels=N starts N+1 channels.
- Earlier panic (IRQ before `pl330_add`): fixed by patches 20/21; history in #84.
- Read path without /dev/mem: debugfs `/sys/kernel/debug/us48_idm_reset/regs` (read only), used by `core_test dma`.

## Flash read path (#20, #84 item 9)

- Linux `spi-bcm-qspi` already reads through BSPI: DT has the `bspi` reg (`has_bspi`), aligned reads of 4 bytes or more use the RAF FIFO in 256-byte chunks (`BSPI_READ_LENGTH`) with one interrupt each; commands, SFDP and short/unaligned reads use MSPI (PIO). `spi-max-frequency = <12500000>` only sets the MSPI SCK (SPBR minimum 8, = 3.125 MHz at the 50 MHz APB): not a read-speed lever.
- BSPI SCK = CRU `QSPI_CLK_SEL` (hwdb `cru.CONTROL`), set by the boot stage; Linux has no driver for it. `core_test flash` should read `devmem 0x1803e000` first.
- Change (patch 17): `spi-rx-bus-width = <4>` on `flash@0`: spi-nor picks 1-1-4 (0x6b, 8 dummy), the BSPI flex mode sends data on 4 lines; programming stays 1-1-1. QE is already set (#20), so `spi-nor` does not write SR. MX25L25635E has no 4-byte opcodes: Linux still enters EN4B as before (hazard in #20 unchanged, flash stays read-only).
- Board run 2026-11-15 (debug image, `core_test flash`): spi-nor params `read 0x6c, 8 dummy, 1S-1S-4S`, `4B_OPCODES` flag set (id `c2 20 19`; the 4-byte-opcode statement above is wrong for this part), program `0x12`, erase `0x21`; 1 MiB read of `/dev/mtd0ro` 2.92 MB/s vs 2.22 MB/s 1-lane; `CRU_CONTROL` 0x32 (QSPI_CLK_SEL bits 2:1 = 1). No `INVALID COMBINATION` / BSPI timeout in dmesg. Read only: no write or erase issued. Md5 against a 1-lane read not done.
- Expected, model not measured: per 256-byte chunk 2088 SCK (1-lane) vs 552 (quad) plus a fixed ~35 us of interrupt/FIFO time (inferred from 2.22 MB/s at 25 MHz). Quad at 25 / 50 / 62.5 MHz: about 4.5 / 5.5 / 5.8 MB/s; 1-lane at 50 MHz about 3.4 MB/s. Beyond that needs fewer interrupts per byte (larger RAF chunk), a driver change.

## a2 (unit 1) nodes

- `mainline/a2/us48-a2.dts`: `apb_clk` (50 MHz), `rng`, `watchdog` (SP805), `timer` (SP804 0), `dma` (PL330). Addresses from hwdb blocks `rng`, `sp805`, `sp804_0`, `dmac` via `A2_*` macros; IRQs and the APB clock copied from unit 0: all conf I on unit 1.
- Off by default (`status = "disabled"`). `scripts/build_a2_linux.py --periph` enables rng/sp805/sp804; `--dmac` enables the PL330 and only after a1 released `dmac_idm.RESET_CONTROL` for unit 1 (a1 reaches it over PCIe, not done here).
- SP805 and PL330 use fixed `arm,primecell-periphid`: no ID read at a2 boot.

## Tools (debug image only)

- `scripts/build_rootfs.py --tools` (called for `--profile debug`): into `/usr/bin` of the debug initramfs: `memtester` 4.7.1, `cyclictest` (rt-tests 2.11, `tools/numa-stub` stands in for libnuma, `tools/rt-tests-musl.patch` maps `sched_{get,set}{scheduler,param}` to raw syscalls: musl returns ENOSYS), `tinymembench` (ssvb master a2cf6d7), `us48bench` (`tools/us48bench.c`: lat, bw, spin, tick, rd), `rngtest` (`tools/rngtest.c`, FIPS 140-2; replaces rng-tools which needs autotools + libgcrypt). About 310 KB stripped.
- In the image, not a side archive: a2 has no network to take an scp, and one image serves both units. The normal profile has none (13 MiB slot).
- Board run 2026-11-15 (debug image: lockdep etc., a1, idle, `cyclictest -t1 -p80 -i1000 -l1000 -m`): min 4248 / avg 9192 / max 10477 us, tick drift 200 ppm (+-1000 ms/s window jitter), clocksource `arm_global_timer`. Latency far above the 1000 us interval: not a PREEMPT figure, re-run on the normal profile.
- `tinymembench` runs about a minute: not part of `core_test`; run by hand when Dan approves.
- Sources pinned by sha256 in the script (`/mnt/1tnvme/toolchains/dl`); built tools cached in `tmp/rootfs/tools-bin`.

## a2 access for `core_test --unit a2`

- Reader on a1: `cat /dev/ttyA20 > /tmp/a2.out &` before `echo start` (docs/a2-linux.md). The runner types the command into `/dev/ttyA20` and polls `/tmp/a2.out` for a marker; tick drift (`timers`) is a1 only.
- Limits (`A1_LIMIT_S` 13, `A2_LIMIT_S` 40, memory 20/60) are estimates until the first board run.

## Bandwidth budget (#84 item 0, a1 ARM 1000 MHz, L2 on, 2026-11-15 debug image `us48bench`; PL330/CPU-copy rows 2026-11-16 normal image at the commit of this change, DDR 667)

| Stage | Measured | Theoretical | Gap / why |
|---|---|---|---|
| DDR read, 8 MiB working set | 495 MB/s | not measured (#55) | single A9 stream, no prefetch tuning |
| DDR write | 1521 MB/s | | write buffer + L2 merge |
| DDR copy | 431 MB/s | | read-bound |
| DDR latency, 8 MiB / 16 MiB | 187 / 202 ns | | L2 (256 KiB) hit 137 ns at 256K, L1 4.9 ns |
| L1/L2-resident (16 KiB) rd/wr/cp | 1652 / 1662 / 1185 MB/s | | CPU-bound |
| PL330 memcpy DDR->DDR, 1 channel, 256 KiB copies | 96 MB/s | not measured | `dmatest` `noverify=1 norandom=1`, per-copy time only |
| PL330 memcpy, 8 channels concurrent (aggregate) | ~315 MB/s (8 x 37-41) | | 4 ch 5 x 64 = 320 MB/s: saturates at 4-5 channels; 0.78x CPU copy (406) |
| CPU memcpy, same image, 256 KiB / 8 MiB | 451 / 406 MB/s | | `us48bench bw` cp, userspace, L2 on |
| CMIC packet-DMA RX pps / MB/s | not measured | | no quick test exists (needs a traffic generator + RX loop, no script) |
| a2 -> host DDR over PCIe (a2 at 1000, #80, 2026-11-15) | cached rd 113 / wr 1061, uncached rd 14 / wr 123 MB/s, 298 ns | PCIe x1 gen2 ~400 MB/s | from #80, not rerun |
| a2 own DDR path at 100 MHz (this run) | L2 off rd/wr/cp 22/254/23 MB/s, 1520 ns; L2 on 55/259/59 MB/s, 1499 ns | | a2 RAM is host DDR over PCIe: every miss crosses it |
| a2 Linux own RAM, 1000 MHz, L2 off (2026-11-16, `us48bench lat/bw` run on a2, debug a1) | lat 4K 6.7, 32K 570, 64K 1239, 2M 1530 ns; rd/wr/cp 16K 816/553/178, 256K 22/233/23, 8M 23/229/22 MB/s | | every L1 miss is a PCIe round trip |
| a2 Linux own RAM, 1000 MHz, L2 on (PL310 128 KiB, aux 0x4e120001, same session) | lat 4K 4.4, 32K 5.6, 64K 34, 128K 411, 256K 1120, 2M 1260 ns; rd/wr/cp 16K 2110/961/675, 256K 71/275/67, 8M 67/254/66 MB/s | | reads x3 (22 -> 67 MB/s), 64 KiB working set at L2 latency; writes unchanged |
| a2 Linux own RAM, 1000 MHz, L2 off (2026-11-16, 3c820ff, a1 normal image cold boot from kernel0, `scripts/a2_bw.py`) | rd/wr/cp 16K 378/361/166, 256K 20/224/22, 8M 23/230/23 MB/s | | same as the debug-a1 row: a1 profile does not change the a2 path |
| a2 Linux own RAM, 1000 MHz, L2 on (same session and image) | rd/wr/cp 16K 2041/534/293, 256K 67/264/67, 8M 67/254/66 MB/s | | 8M reads x2.9; vnet and halt rows in a2-linux.md "a2 L2" |
| a2 Linux own RAM, 1600 MHz, L2 on (2026-11-16, f4ce402, a1 normal image cold boot from kernel0, `scripts/a2_bw.py`, #85, #88) | rd/wr/cp 16K 3332/647/597, 256K 69/248/67, 8M 67/256/66 MB/s | | 8M equal to 1000 MHz: memory-bound (PCIe round trip); 16K reads x1.6 with the core clock; vnet rows in a2-linux.md "a2 L2" |

## RX_FREE_ARID_COUNT cost (#79, #88; a1 ARM 1000 MHz, L2 on; 2026-11-16, base 8224728 + rx_arid default 0)

- Unit 0 `RX_DEBUG_CONTROL_0` [1:0] (reset 3 = `0x1f`; value N = register `0x1c|N`) set with `devmem 0x1801210c` (a2 stopped) or `us48_a2 rx_arid=N` (a2 running Linux, 1000 MHz, DCC poll on, idle shell, `paxb_order=1`). `scripts/rx_arid_cost.py`, normal image, median of 3, each test 1-2 s.
- TCP: 10 MiB both ways over ge1, host python timing (rx = host to board `nc -l`, tx = board `dd | nc`). PL330: `dmatest` 8 ch x 100 x 256 KiB `noverify=1 norandom=1`, summed KB/s.

| rx_arid | a2 | TCP rx MB/s | TCP tx MB/s | PL330 MB/s |
|---|---|---|---|---|
| 3 | stopped | 63.5 | 19.6 | 303 |
| 2 | stopped | 44.2 | 19.6 | 314 |
| 1 | stopped | 63.8 | 19.5 | 311 |
| 0 | stopped | 64.1 | 19.6 | 312 |
| 3 | Linux | 65.9 | 19.0 | 305 |
| 2 | Linux | 69.9 | 19.1 | 307 |
| 1 | Linux | 63.6 | 19.1 | 297 |
| 0 | Linux | 59.9 | 18.9 | 299 |

- TCP rx reps scatter 44-72 MB/s inside every row (44.2 recurs: not a trend); tx is CPU-bound in `dd | nc` (19-20 MB/s); PL330 within +-5 %. No cost seen from rx_arid on unit 0 packet DMA or PL330 (PL330 is on the unit 0 AXI, not behind PAXB RX).
- a2 boot to shell (`/init` shell answers a marker over DCC) 2.26-2.27 s at rx_arid 3, 1, 0 (one start 2.47 s at 2).
- Unit 1 path: a2 `bustest` firmware at 1000 MHz, AXI 200, `paxb_order=1`, a1 idle, debug image, median of 3 passes (`scripts/a2_bus_arid.py`; results page read via `/dev/mem`, ticks at ARM/2 = 500 MHz):

| rx_arid | cached rd / wr / cpy MB/s | uncached rd / wr MB/s | uncached lat / line fill ns |
|---|---|---|---|
| 3 | 29.6 / 274.3 / 28.7 | 3.9 / 66.6 | 1003 / 1085 |
| 2 | 29.3 / 274.5 / 28.8 | 3.9 / 66.6 | 1004 / 1085 |
| 1 | 29.3 / 274.3 / 28.8 | 3.9 / 66.6 | 1005 / 1085 |
| 0 | 29.3 / 274.5 / 28.8 | 3.9 / 66.5 | 1011 / 1085 |

- `paxb_order` 0 vs 1: identical (29.3 / 274.5). 400 MHz = 1000 MHz (latency-bound). a2 issues one miss at a time, so serialising inbound reads costs it nothing here.
- The #80 row above (cached rd 113 MB/s) is not reproduced: now 29.3 at AXI 200 (module default `axi_mhz` 200; unit 1 power-on is 400).
- `us48_bustest` takes the a2 pool from `us48_a2` (`us48_a2_region_get`/`_put`, symbol_get; #89): `pcie_harness.py smoke` a2 rows pass again (debug image RAM-booted, 2026-11-16).

## a2 bustest vs unit 1 AXI clock (#80, #89; 2026-11-16)

- Conditions: debug a1 image (RAM boot), a2 `us48-a2-bustest.elf` at 1000 MHz (ticks = ARM/2 = 500 MHz), `paxb_order=1`, `rx_arid=0`, a1 idle, no DCC. `scripts/a2_bus_arid.py --values 0 --axi 200 400 --reps 3`, median of 3 passes after a cold one. Unit 1 powers on at AXI 400, module default `axi_mhz` 200.

| axi_mhz | cached rd / wr / cpy MB/s | uncached rd / wr MB/s | uncached lat / line fill ns |
|---|---|---|---|
| 200 | 29.3 / 274.4 / 28.8 | 3.9 / 66.5 | 1008 / 1085 |
| 400 | 32.7 / 274.4 / 32.3 | 4.3 / 66.5 | 937 / 990 |

- AXI 400 = +12 % on read-bound rows, writes unchanged; the 113 MB/s row of the first #80 run is not reproduced at either clock (cause open).
- Stale-read check at AXI 400 (`a2_cap.py LINUX 1000 --axi 400 --skip --limit-ms 6000`, a2 Linux normal zImage, a1 idle): 1 fault in 18 runs (first run of the set: wild PC 0x2dc4 in svc mode, DFSR 0x180c, not the maple-tree DFAR 0x4 signature); AXI 200 control: 0/4.
