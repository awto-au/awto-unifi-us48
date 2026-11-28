# Auto-chain: uboot-1 → awto-uboot on every power-on (#63)

Decision: Dan, 2026-11-15. Replaces the kernel1-head plan. Stock stays intact in kernel1.

## Flash layout used

| Offset | Size | Partition | Role here |
|---|---|---|---|
| `0x0000000` | 768 KiB | `u-boot` | uboot-1 (352 KiB) + ubnt app (85 KiB, magic `87564523` at `0x5814c`) + 331 KiB `0xff` (backup 2026-10-12) |
| `0x00c0000` | 64 KiB | `u-boot-env` | **the only write**: `bootcmd`, `bootdelay` changed |
| `0x00d0000` | 64 KiB | `shmoo` | untouched |
| `0x00e0000` | 1 MiB | `kernel0` head | awto-uboot uImage (822 KiB, load = entry `0x04000000`) |
| `0x01e0000` | 14 MiB | rest of `kernel0` | test kernels, scratch dumps |
| `0x0fe0000` | 15.06 MiB | `kernel1` | stock 7.5.15, untouched |
| `0x1ee0000` | 64 KiB | (end of `kernel1`) | bootsel record, untouched |
| `0x1ef0000` / `0x1ff0000` | 1 MiB / 64 KiB | `cfg` / `EEPROM` | untouched |

## Env change

- `bootdelay=0`: no countdown and no abort check (docs/uboot.md §3).
- `bootcmd=sf probe 0 <hz>; sf read 0x01000000 0xe0000 <exact image size>; bootm 0x01000000`
- Keep every other variable byte-for-byte.
  - `ubntappinit` must stay non-empty. Empty → `go ${ubntaddr} uappinit` + reset path = boot loop (docs/uboot.md §3).
- `verify` stays unset, so `bootm` checks the data CRC (Dan's choice: recovery over a ~1 s saving).
- Built on the host from the backed-up `mtd1`. CRC32 recomputed, written to `0xc0000` only, read back and compared with the host image.
- Not a `saveenv`: uboot-1 would also persist runtime vars such as `ubntaddr`.

## What no longer runs

- Stock `bootcmd` chain: `uappinit` → `ureset_button` → `urescue` → `uwrite` → `ubntboot`.
  - No reset-button erase of `cfg` and env at power-on. So **the reset button is no longer a recovery path**.
  - No A/B slot pick, no `ubnt_*` bootargs. The bootsel record is never read or written.
- Still runs: the ubnt app copy into RAM (`0x67030000`, loader `0x1c01e33c`, ~320 KiB). It is unconditional and has no env switch.

## Failure → result → recovery

| Failure | uboot-1 does | Recovery |
|---|---|---|
| awto-uboot image corrupt (data/header CRC) | `bootm` fails → `u-boot>` prompt, WDT petted by getc/putc | serial: `sf read` + `bootm` a good image, or rewrite the image from stock |
| env CRC bad | default env (stock `bootcmd`, `bootdelay` 3) and rewrites `mtd1` with it (seen in `scripts/uboot1_emu.py`) → stock boots | re-write our env |
| awto-uboot boots but is broken (hang after jump) | `bootm` stopped the uboot-1 WDT; awto-uboot arms SP805 (10 s) → reset → same path | serial at awto-uboot if it reaches the prompt; else JTAG / SPI clip |
| want stock | — | RAM only: `scripts/stock_ramboot.py` (`us48stock` variant, below), or restore the default env |

- **No serial escape at uboot-1 with `bootdelay=0`.** Every recovery other than JTAG goes through a working awto-uboot or a failed `bootm`.
- awto-uboot has `sf write` access to `0xc0000` (64 KiB, env only) to re-write or restore the env.

## Speed levers (measure each stage on the first auto boot)

- `bootdelay` 3 → 0: −3 s vs stock default.
- `sf read` exact size instead of `0xd0000`.
- `sf probe 0 <hz>`: uboot-1's MSPI clock is APB/(2 × SPBR), clamped by `SPBR_MIN` (docs/clocks.md QSPI row). A higher `hz` may be clamped. Measure.
- Alternative: `bootm 0x1c0e0000` straight from the BSPI XIP window. `bootm` then copies via BSPI, which may be faster than MSPI `sf read`. Measure both.
- Not done: `verify=n` (a corrupt image would hang, recoverable only with JTAG).

## Measured 2026-11-15 (uboot-1 prompt, `scripts/autochain_time.py`, 20 ms host tick, 2 reps, image CRC `bf43ffdd` OK in all)

| Form | probe | `sf read` |
|---|---|---|
| `sf probe 0`, read `0xd0000` | 0.02 s | 0.181 s |
| `sf probe 0`, read exact `0xcdbf0` | 0.02 s | 0.181 s |
| `sf probe 0 12500000` | 0.02 s | 0.341 s |
| `sf probe 0 25000000` | 0.02 s | 0.341 s |
| `sf probe 0 50000000` | 0.02 s | 0.181 s |

- 12.5 and 25 MHz give the same time (clamp); default = 50 MHz. Exact size saves nothing measurable.
- `bootm` from RAM: -> "Starting kernel" 0.51-0.54 s (verify 0.22 s, load 0.25 s). Path total: 0.02 + 0.18 + 0.52 = ~0.72 s.
- `bootm 0x1c0e0000` (XIP window covers `0xe0000`): -> "Starting kernel" 0.80 s (verify 0.38 s, load 0.38 s). Slower; not used.
- Chosen bootcmd: `sf probe 0 50000000; sf read 0x01000000 0xe0000 0xce4f8; bootm 0x01000000` (image 845048 B incl. 64 B header).
- Typing the three commands on one line at the prompt loses characters after `sf read` (16 B UART FIFO, CPU busy); irrelevant for env bootcmd.
- Cold power-up (relay ON call = t0, includes ~1 s relay API latency): uboot-1 banner +1.0 s, DDR done +1.2 s, PCIe line +1.65 s, countdown +1.8 s.
- Both cold and warm uboot-1 print "port 0 is not active!!" (#67).

## Env in place (2026-11-15, #63, #76)

- Written from awto-uboot: TFTP (ge1) -> RAM, `sf erase 0xc0000 0x10000`, `sf write`, read-back crc32 `ee76b962` = host image. Live env before the write = backup `mtd1` (crc32 `893a81cd`).
- awto-uboot `sf` window (patch 0004): kernel0 `0xe0000..0xfe0000` and exactly `0xc0000` + `0x10000` (env). Nothing else.
- Image in the slot: `tmp/uboot/us48-uboot-chain.bin`, 845048 B incl. header, crc32 `87ab6737`. Env built with `--size 845048 --hz 50000000`; rebuild the env whenever the image size changes.
- Diff vs backup: `bootcmd`, `bootdelay` only (9 other vars unchanged).
- Written env image only: `scripts/autochain_env.py` output (this doc); no other env content (Dan 2026-11-15).
- Recovery from the awto-uboot prompt: `tftpboot 0x01000000 us48-env-restore.bin` (backup `mtd1`, `autochain_env.py --restore`), `sf erase 0xc0000 0x10000`, `sf write 0x01000000 0xc0000 0x10000`.

## Timings, power-cycle without a manual step (`scripts/autochain_boot.py`, t0 = relay ON call, 2026-11-15, image 845048 B)

| Stage | Boot 1 | Boot 2 |
|---|---|---|
| uboot-1 banner | +1.02 s | +1.03 s |
| DDR done | +1.23 | +1.23 |
| uboot-1 PCIe line | +1.63 | +1.65 |
| countdown line (`bootdelay` 0) | +1.79 | +1.79 |
| bootm verify | +1.99 | +2.00 |
| bootm start | +2.47 | +2.48 |
| awto-uboot banner | +2.51 | +2.52 |
| `pcie:` PERST# cycled / link up | +2.67 / `10210 us` | +2.68 / `10211 us` |
| fans ok, temperatures | +2.82 | +2.82 |
| `awto-uboot>` prompt | +2.92 | +2.92 |

- `autochain_boot.py --manual` (abort countdown, typed commands) and `--warm` (`reset` at the prompt, no relay) exist for testing without the env; image size is read from the built image.

## 2 MiB slot (Dan 2026-11-15)

- kernel0 layout: awto-uboot slot `0xe0000..0x2dffff` (2 MiB), kernel slot `0x2e0000..0xfdffff` (13 MiB). dev.py `SLOTS`.
- Env rewritten 2026-11-15 13:41 with `scripts/u2_flash_env.py --expect-live ee76b962`: `sf read 0x01000000 0xe0000 0x200000`, crc32 `5e0fbaf4`, read-back OK. `bootm` checks only the header length, so image growth up to 2 MiB needs no env change.
- Cold power-cycle ×2: prompt at +3.31 s (was +2.92 s with the exact-size read; the extra 1.2 MiB read costs ~0.4 s).
- The kernel at the old offset `0x1e0000` is now inside the uboot slot: re-flash mainline to `0x2e0000` before booting it from flash.

## Stock from RAM (#78, Dan 2026-11-16)

- `scripts/build_uboot.py --stock` -> `tmp/uboot/us48-uboot-stock.bin` (`CONFIG_US48_STOCKBOOT` + LZMA, command `us48stock` in `uboot-port/cmd/us48boot.c`). Never flashed (not the slot image); off by default.
- `us48stock`: `sf read` kernel1 (`0xfe0000`, header + data, read only), stock's bootargs (docs/uboot.md section 4), `machid bb8`, `bi_boot_params` 0x8000 (without it stock gets no cmdline and builds a 4-partition MTD map), SP805 off (stock has no feeder), `bootm`.
- `scripts/stock_ramboot.py [--cycle|--reboot|--reset|--here]`: catch the awto-uboot window, TFTP the variant, crc check, `bootm`, catch its window (1 s autoboot), read bootsel `0x1ee0000` + env `0xc0000` (crc32 `7c56c6ae`, `5e0fbaf4`), `us48stock`. Stock ready (`switchdrvr_ready`) ~1 min after the kernel starts.
- Bootsel record and env read-back after stock ran twice: unchanged. Stock wrote its own `cfg` (cfgmtd) as always.
- Clean-control variant: `build_uboot.py --stock --stock-no-u1` (`CONFIG_US48_STOCK_NO_U1`) -> `us48-uboot-stock-nou1.bin`, `stock_ramboot.py --no-u1`. The variant skips PCIe/PERST#, the unit 1 CMIC probe and all fan frames; the flashed awto-uboot that TFTPs it still does them first (uboot slot is not changed). Control result: #78.
- `stock_wait_ready.py` (init timeline), `stock_clean_control.py` (sw1 hold + bounces). `switchdrvr_ready` + `.poe_ready` exist ~55 s after the kernel starts; CPU stays ~50 % sirq (traffic), so no idle signal.
- Power cycle returns to the normal chain. Stock's ssh: `ubnt@192.168.25.206`.

Related: #63, #69, #20, #73, [uboot.md](uboot.md), [uboot-second-stage.md](uboot-second-stage.md).

## Autoboot to Linux (#63, #73, 2026-11-15)

- awto-uboot `CONFIG_BOOTDELAY=1`, `CONFIG_BOOTCOMMAND="us48boot"` (`uboot-port/cmd/us48boot.c`): `sf probe`, read the 64 B uImage header at `0x2e0000`, check magic + header CRC + length (<= 13 MiB - 64), read exactly header + data, `bootm` (data CRC checked). Any failure prints the reason and stays at the prompt; no fallback.
- Flashed: uboot slot = `tmp/uboot/us48-uboot-chain.bin` (us48-41da525, crc32 incl. header `048c24ea`, 852696 B, = us48-health `ubcrc`), kernel slot = `us48-mainline-normal.bin` (2026-11-19: commit 75c82ba, read-back crc `4d0f1078`, 4661107 B, #139/#35). Env untouched.
- Catching the 1 s window: `dev.py _listen` sends a space every 20 ms from the `awto-uboot 20` banner until the prompt; uboot-1's own `autoboot:  0` line is not treated as a window.
- `autochain_boot.py --full`: relay power-cycle, timestamps every stage, host-side ping + TCP :22 probes, ends 5 s after the shell prompt (or at ssh).

Cold power-cycle x2, t0 = relay ON call (normal profile, quiet off, a1 at 1000 MHz):

| Stage | Boot 1 | Boot 2 |
|---|---|---|
| uboot-1 banner | +1.07 s | +1.04 s |
| DDR done | +1.27 | +1.25 |
| uboot-1 PCIe line | +1.67 | +1.67 |
| countdown line (bootdelay 0) | +1.83 | +1.83 |
| bootm verify / start (2 MiB slot read by the env first) | +2.27 / +2.76 | +2.27 / +2.75 |
| awto-uboot banner | +2.80 | +2.79 |
| awto-uboot cpu / pcie lines | +2.88 | +2.85 / +2.87 |
| us48boot header ok (includes the 1 s bootdelay) | +4.08 | +4.08 |
| us48boot image read (3.7 MB `sf read`) | +4.57 | +4.54 |
| kernel start (data CRC + copy + `Booting Linux`) | +4.85 | +4.85 |
| kernel init done | +6.25 | +6.25 |
| userspace (`rc.sh`) | +6.27 | +6.29 |
| `us48-net: done` | +6.80 | +6.80 |
| shell prompt | +6.86 | +6.86 |
| ping / ssh (2026-11-15 18:03, normal profile, fixed init) | +20.6 s | +20.6 s |

- Stock first stage chain: power-on to shell was ~140-220 s on stock, ~32 s from flash earlier today (debug image, no autoboot).
- No kernel initcall gap above 0.16 s (dmesg deltas): kernel init is spread, nothing to cut.
- `sf probe` clock 12.5 / 25 / 50 MHz: identical `sf read` time (0.482 s for 3.7 MB, crc `6dfd3b22`); `crc32` over 3.7 MB with caches = 0.060 s, so skipping the bootm data CRC saves under 0.1 s (kept).
- `cp.l` from the BSPI window `0x1c2e0000`: 0.321 s for the same bytes (crc identical) vs 0.482 s: -0.16 s, built (`tmp/us48boot_xip.c`) but not flashed (host to board network down at the time, TFTP flash path needs it).
- Remaining waits: uboot-1 to awto-uboot 2.8 s (fixed: env reads the whole 2 MiB slot, uboot-1 verify), bootdelay 1.0 s (fixed by the 1 s window decision), kernel 1.4 s, userspace 0.6 s.
- Network after a cold flash boot (#34): the stock trace's pipe-reset DONE polls were dropped from the replay, so the next range started while the previous one ran. With I/D caches on (#75) the S-channel ops are fast enough to overlap: L2X only half cleared (entries >= 0x2000 NAK on read), `L2_ENTRY_PARITY_STATUS` nonzero, L2 insert NAK in awto-uboot and Linux.
  - Fix: `bcm_cmic_hr2_init.c` polls DONE (bit 18, 5 ms) after each nonzero write to `ING_HW_RESET_CONTROL_2` / `EGR_HW_RESET_CONTROL_1`.
  - The Linux `us48_cmic` unit 0 probe runs `bcm_hr2_u0_cold_init` (guarded on the SBUS ring map) + `bcm_hr2_dma_init` (RX credit pulse after the EP reset), then the normal bring-up. A chip awto-uboot already initialised is left alone.
  - Board 2026-11-16 (#34, gaps cut, normal profile, cold x2): `us48-net: done` +8.52 / +8.65, ping+ssh +14.7 / +15.2. `L2X` readable at 0, 0x1fff, 0x2000, 0x3fff, 0x7fff, parity regs and `ING_HW_RESET_CONTROL_2` 0 (`scripts/chip_health.py`). awto-uboot TFTP 8.5 MB crc matches host.
  - Warm path (awto-uboot ping/TFTP, then `us48boot`) FAILS: ge1 up, RX counts, TX stuck (see mainline.md Module reload).
- Board 2026-11-16 (f4ce402, quiet fan rule, a2 cpu_mhz 1600; #88): normal profile; cold x2: ssh port +13.6 / +14.0 s, awto-uboot fan level 0 on both groups, Linux pwm1 128.

## Reset recovery with the CMIC DMA running (#167, #159)

- Bench 2026-11-27 (normal image, unit 0 + unit 1 DMA interrupts, `dma_irq=0` gives the same): an SP805 reset of a running Linux (feeder killed) or a kernel panic (`panic=5`) loops: uboot-1 -> awto-uboot -> `pcie: unit 1 ...` -> stall in `us48_unit1_cmic_arm()` -> awto-uboot's 10 s SP805 -> uboot-1. Only a relay power cycle clears it (same signature as the a2 note in `docs/a2-linux.md`).
- Cause: the unit 1 RX DMA (RELOAD ring) still running at the reset. Same SP805 reset after `rmmod us48_cmic_xe`, or with only unit 1 `DMA_CTRL[1]` EN cleared, boots back to Linux. Linux `reboot` is fine (driver shutdown).
- Fix in Linux: `us48_cmic` panic notifier clears DMA_EN on both units (priority max). Panic test (throwaway module calling `panic()`, unit 1 RX running): without the notifier loops, with it recovers.
- Not covered: a hard kernel hang (no panic, no notifier) still wedges at the SP805 reset.
- `us48-wdt-guard` (S96): reboots through the clean path after 3 bad checks (dropbear, ge1, a temperature) once uptime >= 300 s; `touch /tmp/wdt-guard-off` pauses it.
