# Boot straight into awto-uboot (uboot-2) without uboot-1

Question: can a warm reset land in uboot-2 instead of uboot-1, as a step to retiring uboot-1? Issue: see #63; context #46, #53, #20.

- Tags: **C** = confirmed (source/dump/log cited), **I** = inferred. **UNTESTED** = plan only.
- Names: uboot-1 = stock U-Boot at flash 0; uboot-2 = awto-uboot (slot flash 0xE0000) ([uboot-second-stage.md](uboot-second-stage.md)).
- Boot ROM dump: `/vendors/broadcom/hr2-bootrom/` (read from unit 1, same die as unit 0; not in this repo, Broadcom material). Addresses below are ROM offsets only.

## Answer

- **Cold boot: not possible without writing flash offset 0.** The ROM has no alternate image, flag, offset or fallback; it jumps to the word at `0xffff0400` (= `0x1c000000`, flash offset 0). C (ROM dump)
- **Warm reset: possible in principle, needs one experiment.** `0xffff0400` is the ROM's "SKU-ROM LUT" word; Broadcom/mainline kernels write that LUT region on NS/HR2. If it is writable on HR2 and survives the chosen reset, uboot-2 can set it before `reset` and the ROM jumps to our code. I (UNTESTED)
- **No reset at all ("self-restart")**: re-entering uboot-2 from DDR (`go` to a fresh copy) works today in principle but proves nothing about removing uboot-1. I

## 1. What the ROM does (C, ROM dump, unit 1, identical die)

| ROM addr | Action |
|---|---|
| `ffff0000` | vectors: `b ffff0020`, then `ldr pc,[ffff0404..041c]` = `0x1c000004..1c` (exceptions go to flash) |
| `ffff0020` | MPIDR & 3 != 0 (secondary) → store own PC at `0xffff042c`, `wfe`, `ldr pc,[0xffff042c]` (spin until released) |
| `ffff0040` | CPU0: `ldm [ffff0420]` → writes `0` to AXI `0x1a000000` (AXI interconnect, purpose unknown; I: boot remap off) |
| `ffff005c` | reads `ROM_S0_IDM_IO_STATUS` `0x1811a500` & 7 |
| `ffff0078..c8` | if == 7: PAXB config-indirect writes (`0x18012120/24`: regs 0x800/0x2800 ← `0x10600300`, 0x80c/0x280c ← `0x00090020`), then spin: store PC at `0xffff0430`, `ldr pc,[0xffff0430]` → waits for a host to write an entry address (PCIe-boot mailbox) |
| `ffff00dc` | else: `ldr pc,[0xffff0400]` → `0x1c000000` |
| `ffff1000..` | interconnect enumeration table (slaves: flash `0x1c000000`/64 MiB, ROM `0xfffd0000`/192 KiB, SRAM, DDR, PCIe…) |

- Total ROM code is ~0xe0 bytes: no QSPI init, no image header check, no OTP read, no flash offset parameter. C
- The two spin loops only work if `0xffff042c`/`0xffff0430` are writable: the ROM region `0xffff0400..` is a RAM/register overlay, not mask ROM. C (code), I (writability on HR2)
- Mainline writes the same region: `bcm4708.dtsi:39` `secondary-boot-reg = <0xffff0400>`, `bcm-nsp-ax.dtsi:9` `<0xffff042c>`, `arch/arm/mach-bcm/platsmp.c:99-123` `nsp_write_lut()` writes the entry ("SKU-ROM LUT"). D-Link HR2 3.6.5 `plat-iproc/platsmp.c:34-44`: LUT offset 0x400 for NS and HR2 (#53). C (other SoCs), I (HR2)
- a2 (unit 1) idles at PC sample `0xffff00d8` = the PCIe mailbox spin above, so its entry word is `0xffff0430`, not `0x42c` (`tmp/bootrom/a2_boot.py` polled `0x42c`; explains "a2 did not take the path" in [a2.md](a2.md)). I

### Live reads, unit 0, 2026-11-14 (devmem, mainline 7.4.0-rc1, after a uboot-2/Linux soft reboot)

| Addr | Value | Meaning |
|---|---|---|
| `0xffff0400` / `0404` | `0x1c000000` / `0x1c000004` | CPU0 boot target / undef vector (as ROM dump) |
| `0xffff0420/24/28` | `0x1a000000`, `0x18130e00`, `0` | ROM parameters |
| `0xffff042c..043c` | `0` | mailboxes unused on unit 0 |
| `0x1811a500` ROM_S0_IDM_IO_STATUS | `0` | BOOT_DEV 0 → flash path |
| `0x1803f028` STRAPS_CAPTURED | `0x102c` | BOOT_DEV 0, PCIE_MODE 1 (RC) |
| `0x1803f02c` STRAPS_SW_OVERRIDE | `0x402` | override off |
| `0x1803f018` IPROC_RESET_REASON | `0x2` | IPROC_SOFT_RESET: last reset = DMU soft reset (uboot-2 / Linux `reboot`). Answers "last-reset reason" (gaps.md #9) |
| `0x1803f014` CRU_RESET_REASON | `0` | |
| `0x1803f020` WATCHDOG_RESET_MODE | `0` | 2-bit scope selector for watchdog reset; encodings unknown |
| `0x1803f200` DMU_CRU_RESET | `3` | SW_RESET_N b0, IPROC_RESET_N b1 |
| `0x1803e000` CRU_CONTROL | `0x32` | ARM_RST_N b4 = 1, QSPI_CLK_SEL = 1 |
| `0x19000f08` A9_CORE_SOFT_RSTN | `3` | core reset bits (unlock `0x19000f00` = `0xa5a5<<8 \| 1`, per NS `iproc_cru.c`) |
| `0x18027038` BSPI_FLASH_UPPER_ADDR_BYTE | `0` | only flash address translation register; upper byte only, no offset remap for the low 24 bits |

## 2. Reset paths today

| Path | Mechanism | Result |
|---|---|---|
| Linux `reboot` (mainline, OpenWrt) | `syscon-reboot` DMU `0x1803f200` clear b1 (IPROC_RESET_N) | ROM → uboot-1 banner ~5 s later. C (DTS `bcm53344-ubnt-unifi-us-48-500w.dts:31-36`; `openwrt-port.md:40`) |
| uboot-2 `reset` | `SYSRESET_SYSCON`, same register | stock banner. C (`tmp/logs/console/us48-20261113-132714.log:3639-3642`) |
| uboot-2 data abort | `Resetting CPU ...` | stock banner, PCIe LINKSTA 0x9012. C (same log :4981-5013) |
| SP805 expiry (`0x18039000`, armed by dev.py ~46 s) | scope by WATCHDOG_RESET_MODE (0) | returns to uboot-1 at 400 MHz. C (`clocks.md:66,71`) |
| uboot-1 `reset` | NULL handler | do not use. C (uboot.md §3) |

What survives the DMU soft reset:
- `IPROC_PCIE_CLK_CTRL` survives (PERST stays asserted into the next boot; patch 952). C (`openwrt/0001-*.patch` ~436-446)
- `IPROC_RESET_REASON` records it. C
- DDR contents: DRAM is not powered down, but the controller is reset and re-trained by uboot-1; retention not measured. I
- SRAM `0x1b000000`: random at power-on; across a soft reset unknown. I
- LUT `0xffff0400`: unknown. **This is the deciding question.**
- Straps: re-captured or kept: unknown; SW_OVERRIDE changes `ROM_S0_IDM_IO_STATUS` live (a2.md). C (override), I (across reset)

## 3. Interception options (ranked)

| # | Option | Status | Notes |
|---|---|---|---|
| 1 | **LUT `0xffff0400` ← our entry, then DMU soft reset** | I, UNTESTED | Only knob the ROM reads for CPU0. Target must be valid post-reset: SRAM stub, or XIP code in flash (`0x1c0e0000+`, our slot). Fails safe if the LUT resets to default (normal uboot-1 boot) |
| 2 | Core-only reset (`0x19000f08` b0, or SP805 with a core-scope WATCHDOG_RESET_MODE) + LUT | I, UNTESTED | Keeps DDR controller/PLLs/PCIe up → target can be uboot-2 in DDR, no DDR init needed. Unknown whether core0 self-releases; SP805 (outside the A9) backs it up |
| 3 | Strap override BOOT_DEV=7 + reset → ROM waits on `0xffff0430` | I | Needs someone else to write the mailbox (only a2 over PCIe, and a2 cannot reach unit-0 AXI as far as known). Hangs otherwise. Not useful |
| 4 | BSPI remap so `0x1c000000` shows flash 0xE0000 | not available | Only `BSPI_FLASH_UPPER_ADDR_BYTE` (b31:24); no low-offset translation. C (regdb). ROM does not init BSPI, so any change resets anyway (I) |
| 5 | Self-restart without reset (`go` fresh uboot-2 copy in DDR) | I | Works like `bootm` today; skips uboot-1 only for this restart; peripherals not re-reset |
| 6 | SRAM image picked by the ROM | not available | ROM has no SRAM/UART/USB boot path; only flash (via LUT) or PCIe mailbox. C |
| 7 | OTP-selected boot source | not found | ROM reads no OTP. C |

## 4. What uboot-1 does that we depend on

| Item | uboot-1 | uboot-2 today | Needed for self-boot |
|---|---|---|---|
| L2-as-RAM pre-DDR stack | `lowlevel_init` 0x1c0004a8 locks 128 KiB at 0x50000000. C | none (SKIP_LOWLEVEL_INIT) | yes, or SRAM 16 KiB stack |
| ARM PLL / GENPLL / AXI 200 / APB 50 MHz | `iproc_config_armpll(400)`, `genpll(4)`. C (log), I (args) | optional CPU switch (#17), reads live regs | yes (or rely on reset defaults: unknown values) |
| DDR3 init + shmoo restore | `FUN_1c003548`, 89 pairs from mtd2 `0xd0000`, 667 MHz, 256 MiB. C ([ddr.md](ddr.md)) | none | yes; read mtd2 read-only, never re-tune-and-write |
| QSPI/BSPI + CRU_CONTROL QSPI_CLK_SEL | set. C | `iproc_qspi`, `sf` read-only | flash must be in 3-byte mode at every reset (#20 hazard) |
| PCIe RC init + unit-1 enumeration (BAR2 0x08040000) | `soc_pcie_hw_init`. C | none: uses stock BARs; network (`us48_cmic_eth`) depends on it | yes (RC driver + PERST, link) |
| UART1 115200 | set. C | DM serial | yes (clock source) |
| GPIO 6/7 LED | set. C | none | cosmetic |
| A9 private WDT 30 s | armed, stopped by `bootm`. C | SP805 | SP805 early |
| PoE, fans, switch, PHY resets | not touched. C | not touched | none |

## 5. Plan (ranked, safest first)

Gate for any write to the `u-boot` region (flash 0..0xbffff): Dan's explicit approval, a fresh verified full-flash backup (`scripts/backup.py`, `sha256sum -c`), JTAG recovery proven on this unit. Not done; not requested here.

| Step | Experiment | Writes | Risk / recovery |
|---|---|---|---|
| E0 | Read-only: LUT, straps, reset reason (done above) | none | none |
| E1 | uboot-2: `mw.l 0xffff0438 <pattern>`, read back, restore 0; then same on `0xffff0400` with readback and immediate restore. Proves the LUT is writable on HR2 | on-chip register only | a reset while `0x400` is wrong → board stuck → `dev.py power-cycle` (I: POR restores `0x1c000000`) |
| E2 | SRAM stub test: stub at `0x1b000000` writes `!` to UART1, bumps a counter in SRAM, then `ldr pc,=0x1c000000` (chains to uboot-1). Set LUT = `0x1b000000`, `reset` (DMU IPROC_RESET_N). Pass = `!` before the stock banner | SRAM + LUT | LUT reset → plain stock boot (answer: no). SRAM lost → garbage executes, no watchdog after reset → power-cycle |
| E2b | Same with SW_RESET_N (b0), SP805 expiry at each WATCHDOG_RESET_MODE value, and core-only reset `0x19000f08` | same | core reset may not self-release → SP805 armed first |
| E3 | Stub prints a DDR marker written before reset (only meaningful for core-only reset) | SRAM + LUT | as E2 |
| E4 | awto-SPL (XIP, PIC, linked for `0x1c0e0000` in our kernel0 slot): L2-as-RAM, PLLs, DDR from mtd2 shmoo (read-only), load uboot-2. LUT → SPL; `reset` = uboot-1 never runs on warm reboot | kernel0 slot only (allowed) | SPL hang → power-cycle → uboot-1 cold boot restores everything |
| E5 | uboot-2 PCIe RC driver (iProc PAXB) so networking does not need uboot-1's enumeration | none | none |
| E6 | **Gated**: SPL at flash 0 (replace uboot-1). Cold boot without vendor code | `u-boot` region | brick without JTAG; gate above |

- E1-E3 need no flash writes and no change to uboot-1. Do E1 first: if `0xffff0400` is read-only on HR2, options 1-2 are dead and only E6 remains.
- Timeouts for the stub/SPL follow CLAUDE.md (derive from UART/DDR arithmetic, log op + limit + elapsed).

## Open

- Writability of `0xffff0400` on HR2 and its reset domain (E1, E2).
- WATCHDOG_RESET_MODE encodings; core-only reset self-release.
- What AXI `0x1a000000` ← 0 does in the ROM.
- PLL/clock reset defaults (needed if SPL skips PLL setup).
