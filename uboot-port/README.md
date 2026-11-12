# uboot-port: second-stage mainline U-Boot (issue #46, milestones M1, M2, read-only exam commands)

Design: [docs/uboot-second-stage.md](../docs/uboot-second-stage.md). M1 = serial prompt, no network (verified on hardware). M2 + exam commands: build only, not yet run on the board. M1 verified on the board 2026-11-12: `us48-uboot>` prompt, `bdinfo` (2 banks, reloc 0x6ffbe000), `sleep 25` no reset, `reset` via DMU returns to stock U-Boot.

## Layout
- `arch/arm/dts/us48-uboot.dts`: standalone DT (ns16550a UART1, A9 global timer, SP805, DMU syscon-reboot, 2 RAM banks).
- `board/ubiquiti/us48/`, `include/configs/us48.h`, `configs/us48_defconfig`: board.
- `patches/0001-arm-add-TARGET_US48.patch`: Kconfig hooks into upstream `arch/arm/Kconfig` (`--save-patches` rewrites it).
- `cmd/bcm_cmic.c`: read-only `cmic r`, `schan r`, `smem r`, `miim r|scan` over the shared core.
- Shared core: `common/bcm-cmic/` (single source), staged read-only into the tree's `lib/bcm-cmic/` by `scripts/sync_bcm_cmic.py` (U-Boot copy lives in `lib/`: `drivers/net/` is not built with `NO_NET`).
- Patches: 0001 arch Kconfig, 0002 `lib/` hook, 0003 `cmd/Makefile`, 0004 `cmd/sf.c` (only `sf probe`/`sf read` run; write, erase, update, protect refuse).
- Everything else is copied into the tree by the build script.

## Build
- `python3 scripts/build_uboot.py` (`--clean`, `--save-patches`, `--jobs N`); log `tmp/logs/build_uboot.log`.
- Tree: `tmp/u-boot-v2026.07` = detached worktree of tag `v2026.07` from the store `tmp/u-boot-store` (gitignored symlink; `$US48_UBOOT_STORE`). Reset to the tag every build.
- Output: `tmp/uboot/us48-uboot-chain.bin` = legacy uImage (ARM, Linux, kernel, uncompressed) around `u-boot-dtb.bin`, built by `scripts/mkuimage.py`.

## QSPI (M2)
| Item | Value | Source |
|---|---|---|
| iproc_qspi node | mspi `0x18027200`/0x184, bspi `0x18027000`/0x124, bspi_raf `0x18027100`/0x24 | `private/reference/bcm-hr2.dtsi` qspi@27200; raf = bspi + 0x100 (Linux spi-bcm-qspi layout) |
| Flash | MX25L25635E, 32 MiB, cs 0, mode 3 | `mainline/0007` board DTS |
| Speed | rated 104 MHz read, board allows ?, we run 12.5 MHz | copy of the Linux DTS; gap unmeasured |
| Write guard | `sf` refuses all but probe/read; `UNLOCK_ALL`, `LOCK`, `SF_TEST`, `CMD_SPI` off | never write u-boot, bootsel `0x1ee0000`, kernel1, EEPROM |

## Addresses
| Item | Value |
|---|---|
| uImage load = entry = `TEXT_BASE` | `0x04000000` |
| Image in RAM before `bootm` | `0x01000000` |
| Init stack top | `0x03f00000` |
| Banks | `0x00000000` 128 MiB, `0x68000000` 128 MiB (as the Linux DT) |
| Relocation target (`ram_top`) | `0x70000000` |
| Console | UART1 `0x18000400`, 115200 8N1, byte registers (as Linux) |

## M2 / exam commands at our prompt (coordinator, hardware; after the M1 steps below)
- `sf probe 0` (expect MX25L25635E, 32 MiB), `sf read 0x01100000 0x1ff0000 6`, `md.b 0x01100000 6` = EEPROM MAC.
- `sf write ...` / `sf erase ...` must print "disabled in this build".
- `schan r 2 0x12200` (UMAC_REV_ID, unit 0): `ack 0x30e00200`, `[0] = 0x00030601`.
- `cmic r 0x31000`, `smem r <blk> <addr> <words>`, `miim scan int 0` (front 1 GPHY = bus 0 phy 9).

## Test from the stock prompt (coordinator, hardware)
1. Stock U-Boot prompt. `loady 0x01000000 460800`, send `tmp/uboot/us48-uboot-chain.bin` (YMODEM; 192 KiB at about 9.9 KiB/s = 20 s).
2. Arm the SP805 as `dev.py` does before every bootm: `mw.l 18039c00 1acce551`, `mw.l 18039000 1dcd6500`, `mw.l 18039008 3`.
3. `bootm 0x01000000`. Expect the stock "Starting kernel" lines, then `U-Boot 2026.07 ... us48-<sha>` and `us48-uboot> ` at 115200 (stock's console was at 460800: switch the host side back to 115200).
4. At our prompt:
   - `md.l 0x18000400 4`, `bdinfo` (two banks, `relocaddr` just below `0x70000000`).
   - `wdt dev` / `wdt list`: SP805 `Started ... (10s timeout)` is printed at boot; the cyclic feeds it every 1 s.
   - `sleep 25`: unit must not reset (checks that the feeding works).
   - `reset`: DMU restart, unit reboots to stock.
5. Loading a kernel: `loady 0x01000000`, `bootm 0x01000000` (legacy uImage, load/entry `0x18000`).

## If it is silent after "Starting kernel"
- Our entry is `_start` at `0x04000000`; the stock handoff leaves MMU/caches off, r0 = 0.
- No UART output but the unit stays up: UART register layout. The DT uses Linux's (`reg-shift` 0, 1-byte accesses); try `reg-shift = <2>; reg-io-width = <4>;`.
- Reset after about 10-20 s: SP805 not fed, check `Started` line, apb clock (`clocks` of the watchdog node).
