# DDR3: stored shmoo, boot behaviour, clock headroom (#55, #17)

- Tags: **C** = confirmed (log / flash dump / disassembly cited), **I** = inferred.
- Sources: stock boot logs `tmp/logs/console/us48-20261013-181009.log`, `...20261014-120052.log`; flash backup `private/backups/20261012T055324Z/full-flash.bin`; [uboot.md](uboot.md) §5, [sram.md](sram.md), [uboot-second-stage.md](uboot-second-stage.md), [hardware.md](hardware.md).
- Nothing here was run on the board; flash analysis is offline from the backup.

## Boot sequence (uboot-1 = stock)

| Step | Fact | Tag |
|---|---|---|
| Log | `SKU ID = 0x8344`, `DDR type: DDR3`, `MEMC 0 DDR speed = 667MHz`, `Validate Shmoo parameters stored in flash ..... OK`, `Press Ctrl-C to run Shmoo ..... skipped`, `Restoring Shmoo parameters from flash ..... done`, `Running simple memory test ..... OK`, `DDR Tune Completed`, `DRAM: 256 MiB` | C |
| Default path | **restore, not re-run**: validate blob in `mtd2`, replay it as register writes, 64 KiB memtest at `0x60000000` | C (log; `FUN_1c002abc`, sram.md) |
| Re-run shmoo | only on Ctrl-C at the prompt, or when validation fails | C |
| After a run | U-Boot writes the result to `mtd2` (`Writing Shmoo values into flash` @0x1c003218) | C (string), I (exact trigger) |
| Where it runs | from QSPI XIP, `board_init_f`: DDR init `FUN_1c003548`, tune `FUN_1c00c2d4`; state block ~0x2a0 B in SRAM `0x1b000000` only while tuning | C |
| DRAM size | hard-coded 256 MiB (`gd->ram_size`), not probed | C |
| Cache | `WARNING: Caches not enabled` = stock U-Boot proper runs D-cache off; handoff to `bootm` leaves caches off, MMU off | C (log), I (cause) |

## What uboot-2 inherits

- uboot-2 is entered by stock `bootm` (legacy uImage holding `u-boot.bin`) after the full stock boot (uboot-second-stage.md "Entry mechanism"). C
- DDR is already trained and running at 667 MHz, 256 MiB at `0x60000000` (low 128 MiB aliased at `0x0`). C
- uboot-2 builds with `CONFIG_SKIP_LOWLEVEL_INIT`: no PLL, no DDR init, no shmoo, no `mtd2` access. C (uboot-second-stage.md)
- So uboot-2 cannot corrupt `mtd2`; only a stock boot with Ctrl-C or an invalid blob rewrites it. C
- uboot-2 enables its own caches (stock hands over with them off): `WARNING: Caches not enabled` does not limit uboot-2 or Linux. I

## Stored parameters: `mtd2` `shmoo`

- Flash `0xd0000`, 64 KiB partition; **0x2dc (732) bytes used**, rest `0xff`. C (backup, offline dump)
- Not in env, not in OTP/EEPROM: the only copy is `mtd2`. C (no other reader found in stock U-Boot disassembly; I for OTP)
- Layout, little-endian words. C

| Offset | Value | Meaning |
|---|---|---|
| 0x00 | `0x4f4d4853` | ASCII `SHMO` magic |
| 0x04 | `0x8344db56` | `0x8344` = SKU ID from the log; low half `0xdb56` unknown (I: board/DDR config id) |
| 0x08 | `0x0a6d` | unknown (I: version or length field) |
| 0x0c | `0x58e6c427` | I: checksum over the pairs (algorithm not traced) |
| 0x10 | `89` | pair count |
| 0x14 | 89 x {reg, value} | replayed as 32-bit register writes |

- Total = 0x14 + 89 x 8 = 0x2dc. C
- Targets are all `0x18010xxx`: `0x18010830/834/86c`, then `0x18010a00..0x18010b6c` (per-lane/per-bit delay and VREF-style PHY registers, by address range), tail `0x18010ba0..bb0`. C (addresses), I (field meaning)
- Dump of the pairs: `tmp/ddr/shmoo_dump.txt` (gitignored, regenerate from the backup).
- The blob is the trained PHY state for **667 MHz**; it is not valid at another clock (I). A DDR clock change needs a fresh training.
- Public reference for the algorithm: Broadcom HR2 U-Boot (`halmartin/u-boot-ms225`: `hurricane2.h`, `ddr_bist.h`, `DDR_SHMOO_PARAM_MEM_PTR 0x1b000000`); D-Link DGS-1210 B1 GPL HR2 U-Boot (`ddr_init.c`). See [prior-art.md](prior-art.md), [sram.md](sram.md). C (listed), tuning code not yet read for #55.

## DDR3: rated max | board allows | we run | why gap

| Item | Rated max | Board allows | We run | Why the gap |
|---|---|---|---|---|
| Controller / PHY (HR2) | table up to 800 MHz = DDR3-1600 (I: D-Link `ddr_init.c` per #17) | unknown | 667 MHz = DDR3-1333 (`MEMC 0 DDR speed`) | Ubiquiti SKU choice (I) |
| DRAM device | unknown: part marking not read (needs board photo) | unknown | 256 MiB total, one 16-bit die assumed (I) | no part ID => no rated speed grade |
| Bus width / ranks | n/a | n/a | 16-bit, 1 rank (I, from 256 MiB) | unconfirmed |
| Shmoo | n/a | n/a | restored from flash, no re-tune | stock default path |

- Gap is unexplained until the DRAM part and routing are known: a gap with no reason is a bug (#17 rule), so this stays open.
- Upside is bounded: 667 -> 800 MHz = +20 % DRAM bandwidth. The A9 is 400 MHz and the stock AXI 200 MHz (#17), so DDR is unlikely to be the bottleneck for forwarding (I).

## Cheapest safe experiment (RAM-only, no flash writes)

0. **Read-only identification** (no risk): board photo for the DRAM marking (speed grade, x16, 4 Gb); from uboot-2 `md` the DDR PLL / MEMC / PHY registers at `0x18010000..` and the DDR PLL block in GENPLL (`0x1803fc00..`); record in this file with date. Compare live PHY registers against the 89 stored pairs (they should match). Pass criterion: all 89 equal.
1. **Re-train at 667 without touching flash**: SRAM-resident code in uboot-2 (16 KiB at `0x1b000000`, sram.md) running the Broadcom shmoo against a RAM buffer instead of `mtd2`; compare result with the stored 89 pairs. Gives the real margin per lane (window width) at the current clock. Needs the code running from SRAM (DRAM is being reconfigured) and its own stack; no stock U-Boot rerun.
2. **Raise the clock**: same SRAM code reprograms the DDR PLL to 733, then 800 MHz, re-trains, runs `ddr_bist` over 256 MiB, then returns to uboot-2. Failure mode is a hang or reset, recovered by power cycle; flash untouched, next stock boot restores the 667 MHz blob (validation `OK`). Pass: BIST clean for the whole range and a 20-cycle repeat at 733, before 800.
- **Stock Ctrl-C shmoo = cheapest step 1 (667 baseline).** Writes `mtd2` on completion (C string; trigger I), allowed: `shmoo` is not a protected region and the blob is in the backup. Save its printed per-lane windows to compare with step 2. Restore the old blob from the backup if the new one boots worse. Tunes at 667 only (I: uboot-1 sets the DDR clock from its table), so steps 1 and 2 at 733/800 still need the SRAM code.
- Do not start step 2 until step 0 names the DRAM part and its speed grade. 800 MHz on a part rated DDR3-1333 is a rating exceed, not a gap.
- Before any run that could touch `mtd2`: confirm `private/backups/20261012T055324Z/full-flash.bin` (contains the verified 0x2dc blob).
- Timeouts for the SRAM code (per CLAUDE.md): derive from DRAM cycle arithmetic, log op + limit + elapsed on expiry; not specified here.

## Open

- DRAM part number, speed grade, width (board photo).
- Checksum algorithm and the unknown header words (`0xdb56`, `0x0a6d`).
- Field map of `0x18010a00..b6c` (read the Broadcom `ddr_init`/shmoo source).
- Whether a stock boot with invalid blob rewrites `mtd2` on every boot or once (disassembly of @0x1c003218 caller).
