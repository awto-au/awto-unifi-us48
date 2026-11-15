# Core SoC bring-up (#84)

- Order: each item on a2, then a1. Tests are 1-2 s (`scripts/core_test.py <item> [--unit a1|a2]`), one result line each into `tmp/logs/core_test.log`.
- Results go in the table: number + conditions + date. Empty = not run yet.
- First board run 2026-11-15: debug image `7.4.0-rc1-dbg+`, RAM-booted. a1 = ARM 1000 MHz. a2 = ARM 100 MHz (a2 corrupts memory above 100 MHz, #79), awto-uboot then `bootz`, DTB with `--periph`; L2 off unless stated.

## Table

| # | Item | Driver | Module | Test | a1 result | a2 result |
|---|---|---|---|---|---|---|
| 1 | Caches L1/L2 | `CACHE_L2X0` (PL310, errata), A9 L1 | built in | `core_test caches`: PL310 id/ctrl/aux + `us48bench lat/bw` | L2 PL310 on (id 0x410000c9, aux 0x4e120001); lat ns 4K 4.9, 32K 9.6, 256K 137, 8M 187; bw MB/s 16K rd/wr/cp 1652/1662/1185, 8M 495/1521/431 | L2 off: lat ns 4K 52, 32K 161, 2M 1520; bw 8M rd/wr/cp 22/254/23. L2 on (`--l2`): lat 2M 1499, bw 8M 55/259/59 (1 of 2 L2-on boots oopsed in the first bw run, #79) |
| 2 | CPU clock | `CLK_BCM_HR2` | built in | `core_test clock`: `us48bench spin` vs `--mhz` | `spin` 976 MHz vs 1000 (0.98) | `spin` 92 MHz vs 100 (0.92) |
| 3 | Timers | `ARM_GLOBAL_TIMER`, TWD, `ARM_TIMER_SP804` | built in | `core_test timers`: tick drift (a1), cyclictest 1 s | clocksource `arm_global_timer`; tick drift -873 ppm (+-1000 ppm jitter, 2 s window: not a drift number); cyclictest n/a: musl `sched_getscheduler` is ENOSYS ("unable to get scheduler parameters") | `arm_global_timer`; cyclictest n/a (musl); drift not measured (DCC poll too coarse) |
| 4 | Interrupts | `ARM_GIC` | built in | `core_test irq`: `/proc/interrupts` delta 1 s; MSI is #54 | `gt` (GIC 27) 103 irq in 1 s (HZ 100 + load) | `gt` 144 irq in 1 s (parse window ~1.4 s) |
| 5 | DMA | `PL330_DMA` | `pl330.ko` | `core_test dma`: IDM reset check, `dmatest.ko` 20 iterations | BLOCKED then FAIL: `RESET_CONTROL` read 0x1 (held in reset); after `--idm-release` the `insmod pl330` + dmatest run hung the board (power-cycled). Not re-run: a manual devmem write was denied by the permission check | not run (`--dmac` DTB needs a1's release of unit 1 IDM) |
| 6 | Memory | - | - | `core_test memory`: `memtester 1M 1` (a2 512K), meminfo split | `memtester 1M x1` rc 0; MemTotal 178260 kB (Low 47188, High 131072) | `memtester 512K x1` rc 0; MemTotal 47732 kB (L2 on: 47728) |
| 7 | RNG | `HW_RANDOM_BCM2835` (nsp layout) | built in | `core_test rng`: `rngtest` on 20000 bits | `18033000.rng`, `rngtest` 8 blocks, 0 fail | `rngtest` 8 blocks, 0 fail |
| 8 | Watchdog | `ARM_SP805_WATCHDOG` | built in | `core_test wdt`: sysfs + VALUE reads, no reset test | `sp805-wdt` registered, `/dev/watchdog0`; VALUE reads 0x1dacc411 -> 0x1ba5ba87 (counting) | VALUE reads 0xffffffff both (not armed: nothing arms SP805 on a2 in Linux boot; awto-uboot a2 also leaves it off) |
| 9 | Flash | `SPI_BCM_QSPI` | built in | `core_test flash`: 1 MiB `/dev/mtd0ro` read (a1 only) | `mtd0` 1 MiB read 2.22 MB/s | n/a (QSPI unit 0 only) |
| 10 | Buses | `I2C_BCM_IPROC`, `PINCTRL_IPROC_GPIO` | built in | `core_test buses`: adapter + gpiochip listing only | 11 I2C adapters, 3 gpiochips | no nodes in the a2 DTS (0 listed) |
| - | Self-tests | `TEST_BITMAP`, `LOCK_TORTURE_TEST`, `RCU_TORTURE_TEST`, `TEST_UDELAY` | `test_bitmap`, `locktorture`, `rcutorture`, `torture`, `udelay_test` | `core_test selftest` (debug image) | `test_bitmap`, `locktorture`, `rcutorture` ok (~2 s each) | not run: the one-line command is ~300 chars, a1 -> ttyA20 DCC input stalled the console (a2 reset to recover) |

## Drivers and DT

- Normal profile: `PL330_DMA=m` only (a module for the swap-without-reboot rule). Debug adds `DMATEST`, `TEST_*`, `*_TORTURE_TEST` as `=m` (`mainline/us48_debug.config`).
- Already present, `=y` in `mainline/us48_defconfig`: QSPI (`SPI_BCM_QSPI`), I2C (`I2C_BCM_IPROC`), SP804, SP805, global timer, PL310, GIC.
- SP805 stays built in: awto-uboot arms it (~46 s) and a module would leave it unfed until the insmod. `WATCHDOG_HANDLE_BOOT_ENABLED=y` makes the watchdog core feed it until userspace opens `/dev/watchdog`, so the busybox feeder is gone from `rc.sh`. The driver default timeout (60 s) applies after probe.
- PL330 (a1 DTS patches 14, 15): `bcm-hr2.dtsi` `dma@20000` had no `apb_pclk`, so the AMBA bus never probed it. Added the 50 MHz APB clock, a `dma` label and `arm,primecell-periphid = <0x00041330>` (no ID read at boot); the board DTS sets `okay`.
- PL330 gate: `dmac_idm.RESET_CONTROL` (0x18114800) resets to 1 (held in reset, regdb); an access while it is 1 stalls the bus. `core_test dma` reads it first and stops with `BLOCKED`; `--idm-release` writes 0. Which boot stage releases it on the board is unmeasured.
- RNG: regdb `CHIPCOMMONB_RNG_*` at 0x18033000 is the iProc/NSP layout (CTRL +0, STATUS +4, DATA +8, FF_THRES +0xc, INT_MASK +0x10), handled by `bcm2835-rng` (compatible `brcm,bcm-nsp-rng`, already in the dtsi, patch 9). `iproc-rng200` wants its FIFO at +0x20: not built, and the #29 comment calling this block RNG200 is incorrect.
- SP804 on a1 is already the DT `ccbtimer0/1`; the clocksource/clockevent choice (global timer rating 300 vs SP804) is what `timers` shows.
- IRQ numbers and PL330 SPI 47-54/63 come from upstream `bcm-hr2.dtsi`/`bcm-nsp.dtsi`: conf I for HR2 (not checked against the SDK).

## a2 (unit 1) nodes

- `mainline/a2/us48-a2.dts`: `apb_clk` (50 MHz), `rng`, `watchdog` (SP805), `timer` (SP804 0), `dma` (PL330). Addresses from hwdb blocks `rng`, `sp805`, `sp804_0`, `dmac` via `A2_*` macros; IRQs and the APB clock copied from unit 0: all conf I on unit 1.
- Off by default (`status = "disabled"`). `scripts/build_a2_linux.py --periph` enables rng/sp805/sp804; `--dmac` enables the PL330 and only after a1 released `dmac_idm.RESET_CONTROL` for unit 1 (a1 reaches it over PCIe, not done here).
- SP805 and PL330 use fixed `arm,primecell-periphid`: no ID read at a2 boot.

## Tools (debug image only)

- `scripts/build_rootfs.py --tools` (called for `--profile debug`): into `/usr/bin` of the debug initramfs: `memtester` 4.7.1, `cyclictest` (rt-tests 2.11, `tools/numa-stub` stands in for libnuma), `tinymembench` (ssvb master a2cf6d7), `us48bench` (`tools/us48bench.c`: lat, bw, spin, tick, rd), `rngtest` (`tools/rngtest.c`, FIPS 140-2; replaces rng-tools which needs autotools + libgcrypt). About 310 KB stripped.
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
