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
| want stock | — | from awto-uboot: `sf read` kernel1 + `bootm`, or restore the default env |

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

Related: #63, #69, #20, #73, [uboot.md](uboot.md), [uboot-second-stage.md](uboot-second-stage.md).
