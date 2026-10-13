# U-Boot `usw-v1.1.6.122-gd2310f3e` (US-48-500W)

Static RE for the first RAM boot (issue #1). Offline only; nothing here was run on the device.

- **C** = confirmed in disassembly (function@address / string / constant cited). **I** = inferred.
- Image: PART `u-boot` of 7.5.15, 447118 B. Flash backup `mtd0` (taken after the 7.5.15 upgrade) is byte-identical over those 447118 B (C) — this is the U-Boot on the device.
- Backup `mtd1` env = default env below (CRC valid) (C).
- Addresses `0x1c......` = link address of U-Boot proper (QSPI XIP window). App addresses `0x6703....` = ubnt app run address.
- Tools: `scripts/uboot_dis.py` (annotated disassembly; the image is `-fpic`, strings are reached as `ldr; add pc`, plain literal scans find nothing), `scripts/carve_ubntapp.py`, `scripts/ghidra_export.py --only ubntapp`.

## Result summary

| # | Finding | Impact on first boot |
|---|---|---|
| 1 | **No Ethernet driver linked.** `eth_register`@0x1c032380 has no callers; `board_eth_init`@0x1c034dd0 and `cpu_eth_init`@0x1c031eb4 are `return -1` stubs | `tftpboot` cannot work ("No ethernet found."). Load the image over serial (`loady`/`loadb`) |
| 2 | **ATAGs are written at `0x8000`** (`bi_boot_params`, set @0x1c034c94), after the kernel copy | uImage load/entry `0x8000` overwrites the first ~0x140 B of zImage. Use load/entry `0x18000` (stock uses it) |
| 3 | `bootm` = magic + header CRC + data CRC only. No RSA in U-Boot proper | none |
| 4 | A 30 s watchdog is armed at boot, stopped by `bootm` right before the kernel jump | none if `bootm` reaches "Starting kernel" |
| 5 | Any key aborts autoboot (3 s) | none |

## 1. Signature checks

| Item | Evidence | Status |
|---|---|---|
| `bootm` handler = `do_bootm`@0x1c00e8d8 → `bootm_start/find_os`@0x1c00e218 | cmd table entry @0x1c05541c | C |
| Checks in `bootm_find_os`: magic `0x27051956` (@0x1c00e354), header CRC (`image_check_hcrc`@0x1c01c294), data CRC (`image_check_dcrc`@0x1c01c2e4), arch==ARM(2), type kernel/standalone/kernel_noload/multi/flat | strings "Bad Magic Number", "Bad Header Checksum", "Bad Data CRC", "Unsupported Architecture" | C |
| Data CRC runs unless env `verify` starts with `n` (unset → verified) | `getenv_yesno`@0x1c01c3e0 | C |
| No RSA / libtomcrypt / `FATAL:Image is not RSA signed` reference in U-Boot proper (< 0x5814c). Only reference to the app region (`0x6703....`) in U-Boot proper is the app loader@0x1c01e33c | scan for movt `0x6703` and words in `0x67030000..0x67090000`: only @0x1c01e370, @0x1c01e3b4 | C |
| RSA code is in the ubnt app: `FATAL:Image is not RSA signed..Aborting` @app 0x67043088 used by header/signature routine @0x670317dc ("UBNT"/"GEOS"/"OPEN" container magics, "Signature authentication failed"). Nothing in the app calls it (no BL/B caller; no `uwrite` path reaches it) | app disassembly | C (no caller found), I (dead code in this build) |
| `uwrite` handler@0x67032fa4 = `return 0` | app disassembly | C |
| `urescue` U-Boot command (table@0x1c055764) handler@0x1c0136c4 = `return 0` (stub) | | C |
| `ubntboot` app command@0x67032dec: picks slot (`force_bootsel` env), builds `bootargs` (adds `ubnt_active_slot`, `ubntbootid`, `ubnt_current_slot`), prints "FATAL: No parition is good." if none. Only runs from `bootcmd` | | C |
| Unused helper@0x1c034e90 in U-Boot proper loads `kernel0`/`kernel1` to `loadaddr`, CRC-checks ("Verifying '%s' parition:"). No callers/pointers | | C |
| Plain `bootm 0x01000000` after a serial load triggers none of the app code | | C |

## 2. Watchdog

| Item | Evidence | Status |
|---|---|---|
| Cortex-A9 private watchdog, regs `0x19020620` load, `+0x628` control, `+0x634` disable | `wdt` cmd@0x1c002868 (table@0x1c0553c8) | C |
| **Armed automatically** in `board_init_r`@0x1c00d278 → `wdt_arm`@0x1c0027d8: load `0x0165A0BC` = 30 s × 781250 Hz (periph clk / 256), control `0xFF0B` = prescaler 255, WD mode (reset), auto-reload, enable | | C |
| 781250 Hz tick assumes 200 MHz PERIPHCLK (clock string: `arm_periph_clk`) | | I |
| Pet = `wdt_pet`@0x1c002808 (reload from saved value). Called from serial getc/putc (@0x1c024a04/0x1c024a1c), `udelay`@0x1c02ad1c, `memmove_wd`@0x1c01c504, memory cmds, hush loop, CRC loops, TFTP loop | callers scan | C |
| So the prompt stays alive; a long `loady` stays alive (getc pets) | | C (path), I (no gap > 30 s) |
| **`bootm` stops it**: `do_bootm_linux`@0x1c00d4ac calls `wdt_stop`@0x1c00282c (writes disable keys `0x12345678`, `0x87654321` to `+0x634`, control=0, clears saved reload) at 0x1c00d4fc, before "Starting kernel ..." | | C |
| OpenWrt will not reboot after boot unless `bootm` fails after arming; no need to pet. If stuck in U-Boot idle in a non-polling state, board resets after 30 s | | C |

## 3. Reaching the prompt

| Item | Evidence | Status |
|---|---|---|
| `abortboot` in `main_loop`@0x1c01dba8: `bootdelay` env (3), counts down 1 s steps of 100 × 10 ms `udelay`; **any received character** aborts (`tstc` then `getc`, no compare, no `CONFIG_AUTOBOOT_KEYED`) → prompt `u-boot> ` (string@0x1c04cc64) | | C |
| `bootdelay` < 0 → no wait; 0 → no abort check. Env default is 3 | | C |
| No silent console, no stdin/stdout/`silent` handling, no boot lock | strings | C |
| Console = NS16550 UART1 `0x18000400`, FCR=7, divisor from `gd->baudrate` (env `baudrate`, default 115200) | `serial_init`@0x1c024a64/0x1c024950 | C |
| `ubntappinit` is **not** run before the prompt while env `ubntappinit` is non-empty. If missing/empty, `board_init_r` runs `go ${ubntaddr} uappinit` then `reset`-path (@0x1c00d2f4) | | C |
| `bootcmd` sequence (only after the delay): `uappinit` → `ureset_button` → `urescue` (no-op) → `uwrite` (no-op) → `ubntboot`, then `ubntboot` env = `bootm 0x01000000` | | C |
| `ureset_button`@0x67032fac: GPIO 11 (CCA bit 11, reset button, active low) polled every 50 ms; **held ≥ ~3 s** → erases flash `0x1ef0000` len 1 MiB (cfg) and `0xc0000` len 64 KiB (u-boot-env), prints "reset button pressed: clearing cfg partition!" | | C |
| It runs only via `bootcmd`/`go`. Interrupting autoboot with a key avoids it | | C |
| Do not hold the reset button at power-on and do not let `bootcmd` run | | — |
| `tftpsrv` (table@0x1c05577c, handler@0x1c013a94) is the TFTP server command; not called automatically. Moot: no NIC | | C |
| Commands present: `wdt go bootm coninfo echo exit help itest fsinfo ls fsload license loady loadb mw/md/mm/nm/cp/cmp/crc32/mtest/loop/mwc/mdc base sleep mtdparts chpart ping dhcp urescue tftpsrv tftpboot bootp run setenv printenv env saveenv pci sf source sspi true false test version showvar`. **No** `bdinfo`, `fdt`, `reset` working (table entry@0x1c0553e4 has a NULL handler; avoid), `nand`, `mii`, `usb` | cmd table 0x1c0553c8..0x1c05595c | C |

## 4. `bootm` handoff

| Item | Evidence | Status |
|---|---|---|
| Legacy uImage with load ≠ data address is **copied** (`memmove_wd`, 64 KiB chunks, comp none) to the load address; "XIP" only if equal. Prints "Loading Kernel Image ... OK" | `load_image`@0x1c00e018 | C |
| Overlap: if load < image address and load_end > image address → return -2; for a legacy single image this is non-fatal (continues). Forward copy with dest < src is safe. Keep image at `0x01000000` and payload < 0xFF8000 B, or load the image higher | 0x1c00ea3c..0x1c00eb18 | C |
| **ATAGs only, no FDT.** No `fdt` command, no libfdt fixup; `fdtcontroladdr` is only a gd field | | C |
| ATAG list built by `boot_prep_linux`@0x1c00d538: CORE (pagesize 0x1000), CMDLINE = env `bootargs` + " ubootver=usw-v1.1.6.122-gd2310f3e", one MEM tag (start `0x60000000`, size `ram_size`), INITRD2 if a ramdisk arg is given, NONE | | C |
| **ATAG address = `bd->bi_boot_params` = `0x8000`** (store@0x1c034c94). Written after the kernel is copied, ≈0x140 B (CORE 20 B + CMDLINE ≈ 0x110 B + MEM 16 B + NONE 8 B) | | C |
| ⇒ a uImage with load/entry `0x8000` has its first bytes overwritten by ATAGs before entry → crash/hang. Stock kernel uImage uses `0x18000` | | C (collision), I (symptom) |
| Machine ID r1 = `0xBB8` (3000), `bd->bi_arch_number` @0x1c034c8c; env `machid` (hex) overrides ("Using machid 0x%lx from environment") | | C |
| r0 = 0, r1 = machine ID, r2 = `0x8000` (ATAG pointer), entry = uImage entry (`blx`@0x1c00d520) | | C |
| With appended DTB and `ARM_ATAG_DTB_COMPAT` off, the kernel ignores r2 ATAGs and uses the DTB (DT `bootargs`, `memory` node win) | kernel behaviour | I |
| State at entry: IRQ+FIQ masked, I-cache off + invalidated, D-cache flushed and off, MMU off (`cleanup_before_linux`@0x1c00d0c8: `dcache_disable` clears M and C; `wdt_stop` just before), PL310 L2 disabled ("Disabling outer cache"@0x1c00d8c4). SVC mode | | C |
| `bootm` option `verify`, `autostart`, `bootm_low/size/mapsize` are standard | | C |
| Stock-image header: ih_os=5 Linux, arch ARM(2), type kernel(2), comp none. The built `us48-initramfs.bin` has these but load/entry `0x8000` | built image header | C |

## 5. Hardware state left by U-Boot

| Item | State | Evidence | Status |
|---|---|---|---|
| UART | UART1 `0x18000400`, 115200 8N1, FIFO enabled | `serial_init`@0x1c024a64 | C |
| CPU clock | `0x1c0025e4(400)` MHz for BCM53342/3/4 (device ID `0x8342..0x8344` read at `0x48010224`) | @0x1c034ccc | I (arg = MHz) |
| DDR | trained by U-Boot (shmoo params from `mtd2`, re-saved to flash when invalid: `Writing Shmoo values into flash` @0x1c003218). **U-Boot can write `mtd2`** at boot | 0x1c003548, 0x1c003218 | C |
| DRAM size | **hard-coded 256 MiB** (`gd->ram_size = 0x10000000` @0x1c034cc4), `bi_dram[0]` = `0x60000000`/256 MiB (@0x1c00d20c) | | C |
| L2/PL310 | disabled at handoff | | C |
| CCA GPIO | only bits 6 and 7 driven (system LED; init to 0, OUTEN set); no other GPIO writes. Bit 9 (BCM59111 reset), bits 0/1/8 (PSE IRQs), bit 11 (reset button) untouched in U-Boot proper | GPIO setter@0x1c01f680 callers: pins 6, 7 only | C |
| PoE | not initialised by U-Boot (no I2C master code, no BCM59111 strings) | | C |
| Fans / MSPI board IO / CPLD | not touched: no CMIC GP-pin or MSPI board-IO writes. CMIC (`0x48010000`) is only read for the device ID. Fans stay at power-on default; OpenWrt has no fan control — watch temperature | movt `0x4801` scan | C |
| QSPI | MSPI/BSPI `0x18027000..` used for flash; `spi1.0` | `spi_setup_slave`@0x1c025020 | C |
| PCIe | `Done PCI initialization` path exists (`pci` command, RC init @0x1c024084); not required for boot | | I (runs at boot) |
| Ethernet | **none**. `Net:` line prints, then "Board Net Initialization Failed" / "No ethernet found." (@0x1c032690, @0x1c0326a4). `ethaddr`, `ipaddr`, `serverip` are unused. No AMAC/GMAC driver; `phy_read/phy_write`@0x1c023c64 are CCB-MDIO helpers for PCIe SerDes | | C |
| Switch fabric | not initialised | | C |

## 6. Memory map seen by U-Boot

DDR is one 256 MiB block at `0x60000000..0x6fffffff`; the low 128 MiB is also visible at `0x00000000..0x07ffffff` (I: matches stock `mem=128M@0x0 mem=128M@0x68000000`; check with the alias test below).

| Region | Contents | Evidence |
|---|---|---|
| `0x00000000..0x07ffffff` | low alias, cacheable in U-Boot's page table (C: table maps sections 0x000-0x07f cacheable, 0x080-0x1df device, 0x1e0-0x1ff cacheable (flash window), 0x200-0xfff device) | `dcache_enable`@0x1c00dc9c |
| `0x00008000` | ATAG list (~0x140 B) | §4 |
| `0x00018000` | recommended uImage load/entry | §4 |
| `0x01000000` | `loadaddr` (and `bootm 0x01000000` in env) | env |
| `0x07030000..0x0707ffff` (alias of `0x67030000`) | ubnt app copy: U-Boot reads `0x50000` B from flash offset `0x5814c` (end of relocated U-Boot) into `0x67030000` every boot, magic `0x87564523` checked, sets env `ubntaddr=67030020` | loader@0x1c01e33c |
| `0x68000000..0x6fffffff` | upper bank | |
| `0x6ff4ff68..0x6ff4ffff` approx. | `gd`/`bd` copy (relocated) | @0x1c00d3f8 |
| `0x6ff50000..0x6ff9ffff` | malloc heap (0x50000) | `mem_malloc_init(dest-0x50000, 0x50000)`@0x1c00d268 |
| `0x6ffa0000..0x6fffffff` | relocated U-Boot (`relocaddr` = (0x70000000 − 0x5ff8c) & ~0x3fff), bss; stack just below gd | `board_init_f`@0x1c00d358, `relocate_code`@0x1c0000a0 |

- Relocation: position-independent copy of the first `0x5814c` B + `R_ARM_RELATIVE` fixups (1278 entries at `0x5595c`) to `0x6ffa0000`; the app is not part of it (C).
- Safe load buffer for the uImage: `0x01000000 .. 0x07030000` (≈ 96 MiB) in the low alias. U-Boot's own data is all in the upper bank (C).
- The ubnt app copy (`0x07030000..0x0707ffff` alias) is not needed for a manual boot; it can be clobbered once `go` is not used (I: nothing calls it after `bootm`).
- Page-table note: `0x6xxxxxxx` is mapped device/uncached, so U-Boot itself runs uncached (C, performance only).

## 7. A/B slot selection (bootsel v2)

Static: ubnt app (`ubntboot` 0x67032dec, selector 0x67030afc). Live: `ubntbox bootsel status`, stock 7.5.15, 2026-10-13.

- Record: flash `0x1ee0000` (last 64 KiB of `kernel1`, mtd4 + `0xf00000`), 0x30 B. Magics v1 `0xa34de82b` / v2 `0xa34de82c`. Fields: bootsel, active, backup, active/backup bootcount, max_retry (3), slot_state a/b, force_fallback, hcrc a/b (env `ubnt_hcrc_a/_b`). C.
- Live: active = kernel1 (7.5.15), backup = kernel0 (6.3.13), counts 0, both GOOD, ff 0. C.
- Selection: `force_bootsel` env overrides. Else ff = 1 → swap roles, clear ff. Else active bootcount ≥ max_retry → swap roles. Chosen slot must pass uImage magic + hcrc + dcrc, else the other slot. C.
- **U-Boot never increments bootcount** ("BS: skip bootcount increment (runtime-only policy)" 0x67031044). The OS does it; stock resets via `ubntbox bootsel mark-success` (syswrapper.sh). C.
  - → No hardware fallback: a slot that boots but never reaches userspace stays selected on every power-up. Only serial or that OS can switch back.
- Stock tool: `ubntbox bootsel {init,mark-success,force-fallback [-n],reset-counts,set-bootcount <active|backup> N,status}`. FW auto-rollback (`rc.auto_rollback_watchdog`) is compiled out in 7.5.x (`auto-capable` rc 1). C.
- A swap to a slot whose image passes CRC needs nothing else: OpenWrt uImage (load 0x18000) in kernel0 + `bootsel force-fallback` boots it. I: untested.

## 8. Console speed

Measured 2026-10-13, PL2303 USB-RS232 cable → front RJ45 (MAX3321), `scripts/dev.py baud-sweep`.

| | Rated max | Board allows | We run | Why the gap |
|---|---|---|---|---|
| Console / `loady` | MAX3321: 250 kbps (datasheet) | 460800 clean `loady` handshake both ways, 921600 fails | 115200 interactive, **460800 for `loady`** | 921600: no reply at 921600 / 1041667 / 892857 / 781250 (transceiver slew, I) |

- `setenv baudrate N` does nothing in this U-Boot (no "press ENTER", stays 115200). C.
- `loady [off] [baud]` switches: "press ENTER" at 115200 → ENTER at N → transfer → "press ESC" at N → ESC at 115200. C.
- A failed speed leaves U-Boot waiting for ENTER at that speed → power-cycle (nothing saved).
- Bare Enter at `u-boot>` repeats the last command (could be `loady`/`bootm`): probe with `echo`.
- 115200 `loady` of 5.6 MB: 9.9 KiB/s ≈ 9.5 min.

## Hazards

| Command / condition | Effect |
|---|---|
| `saveenv` | writes `mtd1` (forbidden by project rules) |
| `sf write/erase/update`, `mtdparts`+`chpart` writes, `go ${ubntaddr} uclearenv/uclearcal/uclearcfg/usetmac/usetbid/usetbrev/usetsshkey/usaveenv` | app commands that erase/write flash (`go` table at app 0x67030020) |
| `run bootcmd` / `run ubntappinit` | hits `ureset_button` (button state) and the stock boot path |
| `reset` | table handler is NULL: do not use |
| Missing the 3 s window | stock boot proceeds (harmless; reboot and retry) |
| Boot with reset button held ≥ 3 s and `bootcmd` allowed to run | erases `cfg` and `u-boot-env` |

## First boot procedure

Serial 115200 8N1 on UART1 (header location: see [hardware.md](hardware.md)). Do not use TFTP: there is no NIC in this U-Boot.

1. Power on; **press any key within 3 s** of "Hit any key to stop autoboot". Expect `u-boot> `.
2. Read only: `version`, `printenv` (default env as in §3), `coninfo`, `help`. No `saveenv`.
3. RAM alias check (scratch RAM, safe): `mw.l 0x01000000 0xa5a5a5a5` then `md.l 0x61000000 1`. Same value ⇒ alias confirmed. Different ⇒ stop and re-read §6.
4. Build the uImage with **load = entry = `0x00018000`** (not `0x8000`): fix the image rule in `openwrt/0001-*.patch` first.
5. Send the image over serial: `loady 0x01000000` then ymodem-send `us48-initramfs.bin` (5.4 MB ≈ 8–10 min at 115200). Optional faster rate `loady 0x01000000 <baud>` then switch the host terminal (divisor from the UART clock, I: 921600 unverified). `loadb` (kermit) is the alternative.
   - Expected: "## Total Size = 0x... Bytes". Size must equal the file size.
6. `bootm 0x01000000`. Watch for, in order:
   1. `## Booting kernel from Legacy Image at 01000000 ...`, image name `ARM OpenWrt Linux-6.18.55`
   2. `Verifying Checksum ... OK` (data CRC; a transfer error shows `Bad Data CRC`)
   3. `Loading Kernel Image ... OK`
   4. `Starting kernel ...` (watchdog stopped here)
   5. kernel/earlycon output on UART1 (DT `bootargs` has `earlycon`)
7. Silence after "Starting kernel ...": check load address first (ATAG overwrite), then DTB/`earlycon`/uart1 in the DTS, then that the zImage decompressor reaches `Uncompressing Linux`.
8. Temperature: fans are not controlled by U-Boot or OpenWrt; power off if the box gets hot.
9. Recovery: power-cycle. Nothing is written to flash by this procedure.

## Open items

- Real PERIPHCLK (watchdog tick) and UART input clock: not measured.
- Is a missing NIC the intended state? Stock 7.5.15 never uses U-Boot networking. A later flash-resident network boot needs a NIC driver (none in this image).
- Reserve size `0x5ff8c` used for `relocaddr` (word @0x4c of the image) exceeds the relocated copy (`0x5814c`); purpose not traced (harmless).
