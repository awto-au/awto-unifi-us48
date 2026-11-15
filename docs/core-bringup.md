# Core SoC bring-up (#84)

- Order: each item on a2, then a1. Tests are 1-2 s (`scripts/core_test.py <item> [--unit a1|a2]`), one result line each into `tmp/logs/core_test.log`.
- Results go in the table: number + conditions + date. Empty = not run yet.
- Build only so far: nothing below has run on the board.

## Table

| # | Item | Driver | Module | Test | a1 result | a2 result |
|---|---|---|---|---|---|---|
| 1 | Caches L1/L2 | `CACHE_L2X0` (PL310, errata), A9 L1 | built in | `core_test caches`: PL310 id/ctrl/aux + `us48bench lat/bw` | | |
| 2 | CPU clock | `CLK_BCM_HR2` | built in | `core_test clock`: `us48bench spin` vs `--mhz` | | |
| 3 | Timers | `ARM_GLOBAL_TIMER`, TWD, `ARM_TIMER_SP804` | built in | `core_test timers`: tick drift (a1), cyclictest 1 s | | |
| 4 | Interrupts | `ARM_GIC` | built in | `core_test irq`: `/proc/interrupts` delta 1 s; MSI is #54 | | |
| 5 | DMA | `PL330_DMA` | `pl330.ko` | `core_test dma`: IDM reset check, `dmatest.ko` 20 iterations | | |
| 6 | Memory | - | - | `core_test memory`: `memtester 1M 1` (a2 512K), meminfo split | | |
| 7 | RNG | `HW_RANDOM_BCM2835` (nsp layout) | built in | `core_test rng`: `rngtest` on 20000 bits | | |
| 8 | Watchdog | `ARM_SP805_WATCHDOG` | built in | `core_test wdt`: sysfs + VALUE reads, no reset test | | |
| 9 | Flash | `SPI_BCM_QSPI` | built in | `core_test flash`: 1 MiB `/dev/mtd0ro` read (a1 only) | | |
| 10 | Buses | `I2C_BCM_IPROC`, `PINCTRL_IPROC_GPIO` | built in | `core_test buses`: adapter + gpiochip listing only | | |
| - | Self-tests | `TEST_BITMAP`, `LOCK_TORTURE_TEST`, `RCU_TORTURE_TEST`, `TEST_UDELAY` | `test_bitmap`, `locktorture`, `rcutorture`, `torture`, `udelay_test` | `core_test selftest` (debug image) | | |

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
