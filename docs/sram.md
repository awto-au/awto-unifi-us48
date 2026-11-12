# On-chip memory (SRAM, L2-as-RAM, ROM) — BCM53344 / BCM53346

Tags: C = code or measurement with source, I = inferred. Static analysis by agent from stock U-Boot (`tmp/ghidra/out/uboot/`) and Broadcom/halmartin `u-boot-ms225` sources; probes from uboot-2 2026-11-13.
Related: [uboot.md](uboot.md), [uboot-second-stage.md](uboot-second-stage.md), [hardware.md](hardware.md), unit-1 A9 question in issue #53.

## Unit 0 (BCM53344, host SoC), addresses as seen by its A9

### Real SRAM at 0x1b000000
- Present: writes stick, contents at power-on are random (uninitialised), so stock U-Boot does not rely on a clean image there. C (uboot-2 `mw.l`/`md.l` 0x1b000000, +0x1000, +0x3000)
- Size: 16 KiB = 0x1b000000..0x1b003fff. 0x1b003ff0/0x1b003ffc read back what was written, 0x1b002000 does not alias 0x1b000000, a read at 0x1b004000 raises a bus fault (board resets, harmless). C (uboot-2 probes 2026-11-13)
- Broadcom's non-L2 HR2 U-Boot config places the stack here (`CONFIG_PHYS_SDRAM_0 0x1b000000`, `CONFIG_SYS_STACK_SIZE 0x3000`) and the DDR shmoo parameter pointer (`DDR_SHMOO_PARAM_MEM_PTR 0x1b000000`). C (halmartin/u-boot-ms225 `hurricane2.h`, `ddr_bist.h`)
- Stock U-Boot's only use: DDR-tune state block, about 0x2a0 bytes (`FUN_1c00c2d4` @0x1c00c2d4, words +0x0, +0x4, +0x298 = 0x50000000, handlers index up to word 0x96). Only while the DDR tune runs (flash shmoo validation fails or user answers Ctrl-C). C
- No other reference to 0x1b000000..0x1b0fffff in the stock U-Boot image or the D-Link 3.6.5 `mach-iproc` tree. C
- Stock boot page table (TTBR0 0x1c040000): section 0x1b0 = 0x1b002c02, 1 MiB, TEX=010 C=B=0 AP=3 XN=0 -> device/uncached, executable. We can map it ourselves (cacheable) at runtime. C (decode I)
- Not listed in stock `/proc/iomem` (System RAM only 0x00000000-0x07ffffff and 0x68000000-0x6fffffff); stock Linux gets ATAGs with one MEM tag, no DT node, nothing reserves it. C
- No SMP/spin-table/PSCI mailbox (single core), no DMA descriptors (CMIC DMA uses host RAM). C
- Free after boot: all 16 KiB, once DDR is up (I). Mainline use: an `mmio-sram` DT node, `reg = <0x1b000000 0x4000>`.

### L2 cache locked as RAM at 0x50000000 (128 KiB)
- This is the PL310 (L2C @0x19022000) locked by line, not a memory block. C
- Stock `lowlevel_init` @0x1c0004a8: 8 ways x 16 KiB, fills 0x50000000..0x50020000 with 0xA5A5A5A5 while L2C+0x950 lockdown-by-line is on (@0x1c000630, cleared @0x1c000658); initial SP 0x5001eff0, gd (0x78 bytes) at the top. Same sequence in Broadcom `hurricane2/lowlevel_init.S`. C
- Pre-DDR stack and gd are the only occupants. After relocation to DRAM (0x6ffa0000) the rest is free, but stock disables PL310 at the kernel handoff and Linux `l2x0` init invalidates by way, so assume contents are lost; usable again only by redoing the lock sequence. I

### Boot flow (stock U-Boot = uboot-1)
- Flash is XIP at 0x1c000000 (BSPI window); uboot-1 is linked at 0x1c000000, flash offset 0 is its vector table, no stage is copied into SRAM. C
- `_start` -> caches/MMU/L2 init -> lock L2 RAM -> `board_init_f` -> DDR init `FUN_1c003548` and tune `FUN_1c00c2d4`, all running from flash. The DDR assembly at 0x1c000660 is a dead emulation-only block. C
- Shmoo data: read in place from 0x1c0d0000 (partition 0xd0000), 0x2dc bytes = 12 B header + 89 (register, value) pairs replayed as register writes (`FUN_1c002abc`); a 64 KiB memtest at 0x60000000 follows. C

### Boot ROM
- Base and size unknown; the A9 resets to 0x0. Interconnect/IDM wrapper registers exist (`AXIIC_ROM_S0` 0x1a008008, `ROM_S0_IDM_*` 0x1811a400..0x1811a90c, `AXIIC_SRAM_S0` 0x1a015008, `SRAM_S0_IDM_*` 0x18120400..0x18120808) but are wrappers, not bases. C (`tmp/regdb/hr2_regs.tsv`)
- The ROM is not in flash. No public dump found for HR2 (searched GitHub; Northstar/BCM4708 only has U-Boot docs). UNVR (Annapurna Alpine) is a different SoC; `awto-unvr` decompiled its flash-resident preboot/S2 loader, not a mask ROM.

## Unit 1 (BCM53346)
- Own copy of the same blocks behind its PCIe endpoint; the host reaches any AXI address through `PAXB_IMAP0_7` (BAR0+0x2c1c, host-writable) and the 4 KiB window at BAR0+0x7000 (OpenMDK `shbde_iproc.c`:255-311). `IMAP0_1` (BAR0+0x2c04) ignores writes. C (probes, #53)
- Unit 1 has the same SRAM: 0x1b000000 (window page 0x1b000001) and 0x1b003000 (page 0x1b003001) read random power-on contents and accept writes through the BAR0+0x7000 window, so the host can load code into it over PCIe. Size assumed 16 KiB like unit 0; 0x1b004000 not probed (a fault there would abort the host). C (probe 2026-11-13, #53)
- Unit 1's A9 is out of reset and powered (same CRU/OTP/IDM state as unit 0); its entry mechanism is not known. See #53.
- BAR4 16 MB windows of both functions read zero and ignore writes (not RAM). C

## a2 (unit 1 A9) runs code from SRAM (2026-11-13, #53)
- Debug block of a2 at AXI 0x19010000 is readable and authorised: DIDR 0x35137041, `AUTHSTATUS` 0xff, CoreSight CIDR valid. a2 is out of reset and idles in the boot ROM (PC sample 0xffff00d8) with BOOT_DEV 0. C
- Method that works: load a program into unit-1 SRAM (0x1b000000) via the host window, unlock (LAR 0xC5ACCE55 @+0xfb0), set DSCR.HDBGen (b14), DRCR HRQ -> halted, DSCR.ITRen (b13), `DTRRX` = entry, ITR `mrc p14,0,r0,c0,c5,0` (0xee100e15) then `mov pc,r0` (0xe1a0f000), clear ITRen, DRCR RRQ|CSE. Result: marker 0xa2a2a2a2 written and counter incrementing in SRAM, PC sample 0x1b00000c. C (`scripts/a2_force_pc.py`, `tools/a2_test.S`)
- Undo: pulse `CRU_CONTROL` (unit 1 0x1803e000) bit 4 low then high; a2 returns to the ROM. C
- Not yet: any I/O from a2, DDR for it (none known), a2 doing useful work; SRAM is 16 KiB.
