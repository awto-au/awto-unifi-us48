# awto-uboot (uboot-2): live design (#46, #63)

- Mainline U-Boot v2026.07, chained from stock uboot-1, TFTP + `sf` for test kernels. Build/layout/M3 hardware test: [uboot-port/README.md](../uboot-port/README.md). Boot chain + flash env: [autochain.md](autochain.md). Stock U-Boot facts: [uboot.md](uboot.md).
- The original 2026-11-12 plan (unit 0 front 1 NIC, planned `dev.py uboot-chain`/`ramload`/`uboot_test.py`, `gen_bcm_names.py`, host test) is retired: `debris/docs/uboot-second-stage-plan.md`. Built instead: NIC on unit 1 xe1 (front 50).

## Entry and handoff
- uboot-1 `bootm` of a legacy uImage (Linux/ARM/kernel, load = entry = `TEXT_BASE` `0x04000000`) holding `u-boot-dtb.bin`; `scripts/mkuimage.py` wraps it. Stock checks magic + hcrc + dcrc only.
- `bootm` hands over clean: IRQ/FIQ off, caches + MMU off, L2 off, SVC, r0 = 0, r1 = 3000, r2 = `0x8000` (ATAGs, ignored). `TEXT_BASE` stays clear of `0x8000..0x8200`.
- `CONFIG_SKIP_LOWLEVEL_INIT`: stock already did DDR + CPU clock. Own page table + I/D cache on in `enable_caches()`, L2 off (#17, #75).
- Banks `0x0` 128 MiB + `0x68000000` 128 MiB; relocates to `ram_top` `0x70000000`. Addresses table: uboot-port/README.md.
- `CONFIG_ENV_IS_NOWHERE`; `saveenv` unavailable. Flash writes: `sf` window only (patch 0004), see autochain.md.

## Watchdog
- SP805 `0x18039000`, 50 MHz clock, 10 s timeout (`CONFIG_WATCHDOG_TIMEOUT_MSECS`), running at entry (armed by `dev.py` before `bootm`, or by awto-uboot itself).
- Fed by `sp805_wdt` + `schedule()` cyclic every 1 s, also inside flash wait loops. Linux `sp805-wdt` takes over. `reset` = DMU restart.
- Stock's A9 private watchdog (`0x19020620`) is stopped by uboot-1 `bootm`.

## Network driver
- `uboot-port/drivers/net/us48_cmic_eth.c`: UCLASS_ETH, polled, `NET_LEGACY`. Unit 1 xe1 (front 50, logical 27), CMIC via PCIe BAR2 `0x08040000` (enumerated by stock, no PCI driver).
- Chip is touched on the first `start()` only; `stop()` (also before `bootm`) aborts DMA and deletes the L2 entry.
- Why not unit 0 front 1: GPORT0 MIB stayed 0 with PHY link up (driver-internals.md section 12); unit 1 already pinged from Linux.
- Waits: S-channel 10 ms, TX completion 2 ms, DMA abort 10 ms, MIIM 65 us; expiry = log + fail.
- DMA window `0x03000000` (layout in uboot-port/README.md); RX flush after arm (#104).

### Gap after DMA off (#104)
- A gap of tens of s with DMA off (`sf erase/write`, `sleep 45`) queues 30-60 frames in the chip CPU queue; the 32-slot RX ring overflows at arm and the first ARP/ping is late or lost.
- Fix: `us48_rx_flush` takes frames until 50 us quiet (backstop 2 ms, logged), then re-arms. `sf erase|write` (patch 0004) also calls `us48_net_quiesce()` (CPU port out of the private VLAN; resumes at next start or `remove`).
- Not the cause: SP805, link/PHY state. CPU egress off (`EGR_ENABLE[0]`) does not help. Test knobs: env `us48_quiesce`, `us48_flush` (default 1); `us48quiesce on|off|stat`. Repro: `scripts/u2_net_probe.py --gap 40`, `scripts/u2_sf_repro.py`. Counts: #104.

## Shared core `common/bcm-cmic/`
- One source for U-Boot and the Linux driver (`kernel/us48-cmic/us48_cmic_core.c` is glue; reference history).
- `scripts/sync_bcm_cmic.py stage uboot` copies it byte-identical and read-only into the U-Boot tree's `lib/bcm-cmic/`; `check` fails on drift. Never edit the staged copy; no `#ifdef __UBOOT__` / `__KERNEL__` in the core.
- Why not symlinks or a submodule: the trees are disposable worktrees; a symlink dangles and dirties `git status`.
- Core files: `bcm_cmic_schan.c` (S-channel), `_miim.c`, `bcm_cmic_hr2*.c` (chip init from the stock trace replay, ports, DMA, mem/reg tables, generated `*_data.h` / `*tab.h` by `scripts/gen_bcm_hr2_init*.py`, `gen_hr2_regtab.py`, `gen_hr2_memtab.py`).
- OS contract: native `readl/writel`, `udelay`, `pr_*`, `cpu_to_le32`; per-unit optional S-channel lock hook (Linux); DMA memory is the caller's.
- Examination commands `cmic`, `schan`, `smem`, `miim` (`uboot-port/cmd/bcm_cmic.c`, read-only) and `us48boot` / `us48stock` (`cmd/us48boot.c`).

## Not copied from the sibling ports
- Persisted env, `saveenv`, bootsel: forbidden here (CLAUDE.md). The power-up chain is autochain.md (env `bootcmd` written once).
- UNVR `go`-chain, DDR/SPL; st1008f `rtk` commands. Siblings: https://github.com/awto-au/awto-tl-st1008f/blob/main/docs/UBOOT-MAINLINE.md
