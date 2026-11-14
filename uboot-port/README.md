# uboot-port: second-stage mainline U-Boot (issue #46, milestones M1, M2, M3 net, read-only exam commands)

Design: [docs/uboot-second-stage.md](../docs/uboot-second-stage.md). M1 = serial prompt (verified on hardware). M3 = Ethernet on xe1 (build only). M2 + exam commands: build only, not yet run on the board. M1 verified on the board 2026-11-12: `awto-uboot>` prompt, `bdinfo` (2 banks, reloc 0x6ffbe000), `sleep 25` no reset, `reset` via DMU returns to stock U-Boot.

## Layout
- `arch/arm/dts/us48-uboot.dts`: standalone DT (ns16550a UART1, A9 global timer, SP805, DMU syscon-reboot, 2 RAM banks).
- `board/ubiquiti/us48/`, `include/configs/us48.h`, `configs/us48_defconfig`: board.
- `patches/0001-arm-add-TARGET_US48.patch`: Kconfig hooks into upstream `arch/arm/Kconfig` (`--save-patches` rewrites it).
- `cmd/bcm_cmic.c`: read-only `cmic r`, `schan r`, `smem r`, `miim r|scan` over the shared core.
- Shared core: `common/bcm-cmic/` (single source), staged read-only into the tree's `lib/bcm-cmic/` by `scripts/sync_bcm_cmic.py` (U-Boot copy lives in `lib/`; the driver in `drivers/net/` is wired by patch 0005).
- Patches: 0001 arch Kconfig, 0002 `lib/` hook, 0003 `cmd/Makefile`, 0005 `drivers/net/Makefile`, 0004 `cmd/sf.c` (only `sf probe`/`sf read` run; write, erase, update, protect refuse).
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
3. `bootm 0x01000000`. Expect the stock "Starting kernel" lines, then `awto-uboot 2026.07 ... us48-<sha>` and `awto-uboot> ` at 115200 (stock's console was at 460800: switch the host side back to 115200).
4. At our prompt:
   - `md.l 0x18000400 4`, `bdinfo` (two banks, `relocaddr` just below `0x70000000`).
   - `wdt dev` / `wdt list`: SP805 `Started ... (10s timeout)` is printed at boot; the cyclic feeds it every 1 s.
   - `sleep 25`: unit must not reset (checks that the feeding works).
   - `reset`: DMU restart, unit reboots to stock.
5. Loading a kernel: `loady 0x01000000`, `bootm 0x01000000` (legacy uImage, load/entry `0x18000`).

## M3 as built: Ethernet on unit 1 xe1 (front 50, logical port 27) (build only, not yet run on the board)
- Unit 1, not the unit-0 front-1 plan above: the Linux driver (`kernel/us48-cmic/us48_cmic.c`) already pings on this port.
- Chip programming = shared core `common/bcm-cmic/bcm_cmic_hr2.c` (xe1 TSC/XLMAC enable, `EGR_ENABLE`, `EPC_LINK_BMAP`, private VLAN 48, `PORT_VID`, `CML_FLAGS` 0, static L2X entry, DMA init/abort, RX ring, 2-DCB TX). Linux refactor onto it is later.
- S-channel now waits for DONE only (error bits checked after); backstop 10 ms.
- Driver `uboot-port/drivers/net/us48_cmic_eth.c` (UCLASS_ETH, polled), DT node `ethernet@8040000` (`ubnt,us48-cmic-eth`), wired by `patches/0005`.
- Fixed addresses: CMIC `0x08040000` (PCIe BAR2, enumerated by stock; no PCI driver); DMA region `0x03000000`: RX DCBs `+0x0`, TX DCBs `+0x1000`, TX buf `+0x2000`, RX bufs (16 x 2 KiB) `+0x10000`. Below the chip's `0x08000000` window, outside relocated U-Boot (`0x6ffbe000`).
- Chip is touched on the first `start()` only (not at boot/probe): `dma_init` aborts stock's CMC0 ch0/ch1, then xe1 bring-up (skipped if XLMAC already enabled), L2 add, RX arm.
- `stop()` (also before `bootm`): abort both channels, L2 delete. VLAN 1 untouched (HiGig links to unit 0).
- Env defaults (defconfig, `USE_IPADDR` etc.): `ipaddr=192.168.25.207`, `netmask=255.255.255.0`, `gatewayip=192.168.25.254`, `serverip=192.168.25.30`; `ethaddr=02:75:73:34:38:50` (`include/configs/us48.h`). Build-time override: `US48_IPADDR` / `US48_SERVERIP` / `US48_GATEWAYIP`; run time: `setenv`.
- Link wait: none. sw1 STP can take 30+ s; `ping`/`dhcp`/`tftpboot` retries cover it.
- Timeouts: S-channel 10 ms, TX completion 2 ms (abort + log), DMA abort 10 ms, MIIM 65 us.
- `sf` stays read-only; the net driver never calls `sf probe`.

### M3 hardware test (coordinator)
1. `dev.py boot-flash --slot uboot` (or the `loady` + `bootm` chain), prompt `awto-uboot>`.
2. `ping 192.168.25.11` (expect `is alive`, first tries may time out while STP converges).
3. `tftpboot 0x01000000 us48-initramfs.bin` (host serves `192.168.25.30`; override with `setenv serverip`).
4. `iminfo 0x01000000` (header + data CRC OK), then `bootm 0x01000000` as before.
5. On failure: `cmic r 0x31140 4` (DMA_CTRL/STAT), `smem r 10 0x04000000+27 8` (PORT_TAB[27]), report the log lines `bcm-cmic:` / `us48-eth:`.

## If it is silent after "Starting kernel"
- Our entry is `_start` at `0x04000000`; the stock handoff leaves MMU/caches off, r0 = 0.
- No UART output but the unit stays up: UART register layout. The DT uses Linux's (`reg-shift` 0, 1-byte accesses); try `reg-shift = <2>; reg-io-width = <4>;`.
- Reset after about 10-20 s: SP805 not fed, check `Started` line, apb clock (`clocks` of the watchdog node).
