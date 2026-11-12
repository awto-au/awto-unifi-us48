# uboot-port: second-stage mainline U-Boot (issue #46, milestone M1)

Design: [docs/uboot-second-stage.md](../docs/uboot-second-stage.md). M1 = serial prompt, no network, no QSPI.

## Layout
- `arch/arm/dts/us48-uboot.dts`: standalone DT (ns16550a UART1, A9 global timer, SP805, DMU syscon-reboot, 2 RAM banks).
- `board/ubiquiti/us48/`, `include/configs/us48.h`, `configs/us48_defconfig`: board.
- `patches/0001-arm-add-TARGET_US48.patch`: Kconfig hooks into upstream `arch/arm/Kconfig` (`--save-patches` rewrites it).
- Everything else is copied into the tree by the build script.

## Build
- `python3 scripts/build_uboot.py` (`--clean`, `--save-patches`, `--jobs N`); log `tmp/logs/build_uboot.log`.
- Tree: `tmp/u-boot-v2026.07` = detached worktree of tag `v2026.07` from the store `tmp/u-boot-store` (gitignored symlink; `$US48_UBOOT_STORE`). Reset to the tag every build.
- Output: `tmp/uboot/us48-uboot-chain.bin` = legacy uImage (ARM, Linux, kernel, uncompressed) around `u-boot-dtb.bin`, built by `scripts/mkuimage.py`.

## Addresses
| Item | Value |
|---|---|
| uImage load = entry = `TEXT_BASE` | `0x04000000` |
| Image in RAM before `bootm` | `0x01000000` |
| Init stack top | `0x03f00000` |
| Banks | `0x00000000` 128 MiB, `0x68000000` 128 MiB (as the Linux DT) |
| Relocation target (`ram_top`) | `0x70000000` |
| Console | UART1 `0x18000400`, 115200 8N1, byte registers (as Linux) |

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
- Everything is untested on the board.
