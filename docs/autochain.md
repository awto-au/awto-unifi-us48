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
- awto-uboot needs `sf write` access to `0xc0000` (64 KiB, env only) to re-write or restore the env. Today its window is kernel0 only.

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
- Chosen bootcmd: `sf probe 0 50000000; sf read 0x01000000 0xe0000 0xcdbf0; bootm 0x01000000` (image 842736 B incl. 64 B header).
- Typing the three commands on one line at the prompt loses characters after `sf read` (16 B UART FIFO, CPU busy); irrelevant for env bootcmd.
- Cold power-up (relay ON call = t0, includes ~1 s relay API latency): uboot-1 banner +1.0 s, DDR done +1.2 s, PCIe line +1.65 s, countdown +1.8 s.
- Both cold and warm uboot-1 print "port 0 is not active!!" (#67).

## BLOCKER: env not written (2026-11-15)

- `autochain_boot.py --manual` (cold power-cycle, abort countdown, `sf read` + `bootm`, no env write): awto-uboot (11:50 build) prints `fan: probing unit 1 BAR at 0x08040000` and hangs. Same after a warm reboot from mainline Linux.
  - Cause: no PCIe link to unit 1 (#67). SP805 resets after ~10 s -> uboot-1 again.
- With `bootdelay=0` this is an endless reset loop and no serial escape (see above). Env stays stock until awto-uboot survives a down PCIe link.
- Built env (not written): `scripts/autochain_env.py --base private/backups/20261012T055324Z/mtd1-u-boot-env.bin --size 842736 --hz 50000000`.
  - Live `0xc0000` = backup `mtd1` byte-identical (crc32 `893a81cd`). Format: u32 LE CRC32 over bytes 4..64K, vars, `0xff` fill, no redundant flag.
  - Diff: `bootcmd` and `bootdelay` only; 9 other vars unchanged.

Related: #63, #69, #20, #73, [uboot.md](uboot.md), [uboot-second-stage.md](uboot-second-stage.md).
