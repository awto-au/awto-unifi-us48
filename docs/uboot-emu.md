# uboot-1 host emulator (#69 phase 1)

`scripts/uboot1_emu.py`: runs the vendor U-Boot (`usw-v1.1.6.122`) from reset under Unicorn (Cortex-A9 model), hooks every device access. Offline; no board contact.

- Inputs (from `private/`, never committed): newest `private/backups/*/full-flash.bin` (mapped at 0x1c000000, 32 MiB), `tmp/ghidra/out/uboot/*.funcs.tsv` (names; docs `name@0x1c...` override `FUN_`), `tmp/logs/clock_trial.log` (live `md.l` dumps -> register defaults).
- Run: `python3 scripts/uboot1_emu.py --budget 150000000 --cmd version --cmd help ...` (needs `unicorn`, `capstone`; system `python3` has them). ~5 min for the full boot (incl. DDR shmoo) + 11 commands.
- `--overlay FILE@OFFSET`: overlay a file into the flash model (emulator copy only). `--trace-mspi`: log every MSPI queue run. `--timer-poll-n` (3): reads before a timer-bounded poll is satisfied.
- Auto-chain run: `--budget 500000000 --overlay tmp/uboot/us48-uboot-chain.bin@0xe0000 --cmd 'sf probe 0 50000000' --cmd 'sf read 0x01000000 0xe0000 <size>' --cmd 'bootm 0x01000000'` (~25 min).
- Outputs: `tmp/logs/uboot1_emu.log` (events, console; use `grep -a`), `tmp/uboot1_emu.tsv` (seq, pc, func, R/W, addr, size, value, flag), `tmp/uboot1_emu_report.md` (coverage, writes per phase with hwdb names, polls, unmapped, no-default reads, flash writes, functions not run), `tmp/uboot1_emu_ddr_writes.tsv` (ordered DDR controller/PHY + DDR IDM writes with hwdb names: the #55 DDR init reference).
- Memory map and register names: `scripts/hwdb.py` (#72). RAM/flash/bootrom from regions `ddr`, `ddr_alias`, `l2_lockram`, `sram`, `flash_xip`, `bootrom`; MMIO from `kind=device` regions (overlaps split) plus `cmic0`. Other pages are auto-mapped as logged UNMAPPED+UNKNOWN.
- hwdb gaps seen: IDM wrappers past 0x18120000 (0x18120900/0x18121900/0x18123900/0x18132900/0x18141900 writes, 0x18131408 read), BSPI/RAF register offsets (local constants `RAF_*`, `BSPI_UPPER`, `MSPI_CS` from FUN_1c024d10 / FUN_1c025234).

## Device model
- Register store, reads return last write, else default (clock_trial dumps), else 0 (flagged `DEF0`, listed in report).
- UART1 0x18000400: byte regs, LSR = THRE|TEMT (+DR when input queued); TX -> console text. A space is injected after "autoboot" appears; `--cmd` lines are sent at each `u-boot> ` prompt; stop when none left.
- A9 global timer 0x19020200/204: counter += `--timer-step` per read (udelay).
- MSPI queue (FUN_1c025234): SPE runs slots NEWQP..ENDQP; slot i = TX words 0x10+2i/0x11+2i, RX words 0x30+2i/0x31+2i, CDRAM bit 7 keeps CS. SPCR0_MSB BITS 0 = 16-bit slots (2 bytes), 8 = 8-bit slots (byte in the MSB TX word, response in both RX words). CS = write to MSPI+0x180 (1 assert / 0 deassert). Feeds a byte-stream MX25L25635E: JEDEC `c2 20 19`, RDSR=0, READ/FAST_READ. Writes/erases are logged `FLASH-WRITE`, never applied.
- BSPI RAF (FUN_1c024d10, `sf read` and env/shmoo/app reads): START_ADDR/NUM_WORDS, CTRL=1 loads a FIFO from the image (address = BSPI+0x38 top byte | START), STATUS bit1 = empty, bit0 = busy (0).
- DDR PHY status defaults: 0x1801084c = 3 (VDL calibration idle|locked).
- Polls: same (pc, addr, caller) read > `--poll-n` (16) times (> `--timer-poll-n` when the global timer is read between two reads: the loop is timer-bounded and would time out first; caller = LR when the load is a leaf accessor, e.g. the DDR PHY reg reader 0x1c0062e4); the next <= 10 instructions (tst/and/cmp/ubfx/lsl, stack spill/reload, branch) are evaluated for candidate values; the first that leaves the loop is returned and logged `POLL`. Leaf-accessor timer loops use the direction-flip rule (a value that flips the first branch exits). `KNOWN_POLLS` pins two the heuristic cannot decide (backward exit branch). Loops that exit unaided are untouched.

## Phase 1 result (2026-11-15)
- Standard run (150 M insn, 11 commands): boots to `u-boot> `, DDR PHY PLL/ZQ/VDL calibration and the whole shmoo (RD_EN, RD_DQS, WR_DQ) run; env read has a valid CRC (no default-env save); ubnt app copy reads magic 87564523; `version help printenv coninfo md.l mw.l crc32 sf pci echo` answer.
- Auto-chain run (500 M insn): `sf probe 0 50000000`, `sf read 0x01000000 0xe0000 0x17e8f8` (tmp/uboot/us48-uboot-chain.bin overlaid), `bootm 0x01000000`: "Verifying Checksum ... OK", "Loading Kernel Image ... OK", "Starting kernel ..."; awto-uboot runs to its `awto-uboot>` prompt (PCIe/unit 1 absent as modelled, fans fail-safe, WDT started). Run stops on budget, not on a fault.
- Coverage (functions run / 513 text functions; Ghidra entries past 0x1c048000 are data, dropped): 165 (32.2%) before, 185 (36.1%) standard run, 207 (40.4%) auto-chain run.
- Regcheck (`uboot1_emu_regcheck.py`, distinct MMIO addresses): before 396 = 339 reg + 49 block + 8 region; standard run 641 = 634 reg + 7 region; chain run 669 = 662 reg + 7 region; none = 0 in all. Remaining region-only: 0x18010b18.. (DDR PHY gap).
- Writes per phase (standard run, distinct regs / writes): IDM 22/24, PLL/clock 25/34, UART 4/9, GPIO 2/4, WDT 2/670, QSPI 64/1576 (was ~116k), PCIe 2/33, A9-priv/SCU/L2 158/162, DDR/PHY 309/141669 (was 6/30), other 2/7.
- DDR writes (`tmp/uboot1_emu_ddr_writes.tsv`): 141674 writes, 314 distinct regs. Per writer: FUN_1c005fa0 96000 (BIST pattern ctl in the shmoo loops, `DDR_BIST*`), FUN_1c006a14 45391 (PHY reg write helper: shmoo VDL overrides, PHY setup), FUN_1c002d48 212 (DRAM mode registers / ddr_ctl), FUN_1c007814 35, FUN_1c003548 10 (PHY straps 0x180108b0/b4, IDM). By block: `sdk_iproc` DDR regs 134984, `ddr_ctl` 214, `ddr_phy` 61, rest unnamed PHY (0x18010d60/0x18010d90..da4).
- Auto-satisfied polls (flagged): 0x1902277c @0x1c0005d8 (PL310), 0x1900000c @0x1c00266c/0x1c002748 (CCU PLL lock), 0x1803fc30 @0x1c002cdc (GENPLL lock), 0x18010800 @0x1c003600 (PHY revision/up), 0x18010164 bit8 @0x1c0036a8 (ddr_ctl.CTL), 0x18010810 bit0 caller 0x1c0072d4 (PHY PLL lock, `KNOWN_POLLS`), 0x1801083c bit 0x400000 callers 0x1c0073bc/0x1c007990 (ZQ cal done, `KNOWN_POLLS` / flip), 0x18010460 @0x1c005f94 caller 0x1c007ebc (DDR BIST status, x6400, flip rule), 0x1902277c/0x190227fc @0x6ffad78c/c8 (PL310 in `cleanup_before_linux`). BSPI busy polls are gone (RAF model).
- Modelled, not real: shmoo passes on perfect RAM (BIST status forced), so VDL/override values are placeholders; "Validate Shmoo parameters stored in flash ... failed" (backup mtd2 not valid) -> shmoo save = erase 0xd0000 + 3 page programs, logged, not applied.
- Gaps: IDM wrappers past 0x18120000 unmapped (hwdb gap); PCIe port reads 0 ("End-Point mode - ignored"); `Net: Board Net Initialization Failed` (no NIC).
- Counts and the poll list: `tmp/uboot1_emu_report.md` (regenerate; not committed).

## Patch list to run uboot-1 on a2 (unit 1 A9), from the run
Unit 1 has no flash; AXI 0x60000000+ is a dead DDR controller. Every item below is an absolute assumption in the image (PC = image address; after relocation add 0x6ffa0000-0x1c000000).
- XIP / flash fetch
  - Entry 0x1c000000 (vector table + literals 0x1c000020..3c): code must be loaded into a2-reachable RAM and linked/offset-patched (image is `-fpic`, so mostly a load-address change; literal base = `sub r0,pc` @0x1c0000b0).
  - `relocate_code` @0x1c0000a0: copies 0x5814c B from the XIP source, applies 1278 R_ARM_RELATIVE fixups (table 0x5595c, loop @0x1c0000fc-0x1c000148). Source address = running PC; fine if loaded elsewhere.
  - Flash reads at runtime: env (`CONFIG_ENV` 0xc0000, via spi_flash_probe @0x1c025234 path), ubnt app loader @0x1c01e33c (flash 0x5814c, 0x50000 B -> 0x67030000, movt @0x1c01e370/0x1c01e3b4), shmoo mtd2 read (0xd0000), BSPI window 0x1c1e0000 etc. All fail on a2 (no QSPI): env falls back to default (writes it back, see below), app absent.
- Stack, gd, relocation, heap (all assume DDR at 0x60000000)
  - Boot stack literal 0x5001eff0 @0x1c000090 (L2-as-RAM; needs PL310 lock sequence @0x1c0005c8-0x1c000630 or another RAM) and gd = same address @0x1c00d35c/0x1c00d364.
  - `relocaddr` = (0x60000000 + gd->ram_size - reserve) & ~0x3fff: `add r4,#0x60000000` @0x1c00d3e8, ram_size hard-coded 0x10000000 @0x1c034cc4; result 0x6ffa0000 (emulated). Heap = reloc-0x50000 (0x6ff50000..0x6ff9ffff) @0x1c00d3f8; relocated gd/bd 0x6ff4ff68; SVC sp 0x6ff4df4c (emulated r8/sp); tlb_addr gd+0x4c in the same area.
  - `bi_dram[0]` start 0x60000000 @0x1c00d210, size @0x1c00d220; `bi_boot_params` 0x8000 @0x1c034c94; ubnt app dest 0x67030000.
  - Needs a2 RAM window with >= ~0x60000 B (image+bss) + 0x50000 heap + stack; or shrink heap (0x50000 literal @0x1c00d3f8) and set `relocaddr`.
- Page table (built @0x1c00dc9c `dcache_enable`, table at gd->tlb_addr)
  - Sections 0x000-0x07f = cacheable DDR alias (desc 0x...4c0a), 0x080-0x1df device (0x2c02), 0x1e0-0x1ff cacheable flash window, 0x200-0xfff device, so 0x6xxxxxxx is device/uncached. The 128 MiB low alias and 0x1c0 flash cacheability assume unit-0 layout; "Caches not enabled" in the emulator because SCTLR.M is already 0.
- DDR / PLL / IDM writes that must NOT run on a2 (would retune or reset a1's live DDR)
  - `FUN_1c002c0c` @0x1c002c0c: GENPLL 0x1803fc1c/24/20/2c (DDR PLL NDIV 0x78, CTRL_2 9) + lock poll @0x1c002cdc.
  - `FUN_1c003548` @0x1c003548: DDR init (PHY straps 0x180108b0/b4, IDM 0x18109800/0x1810a800/0x18109408, PHY 0x18010a00..0x18010bb0 shmoo regs, status poll 0x18010800 @0x1c003600).
  - `FUN_1c002290`/`FUN_1c002244` (GENPLL 0x1803fc00-0x1803fc28), `FUN_1c0025e4` (ARM PLL/CCU 0x19000000-0x19000c04 + 0x19000200..0x19000400 clock enables, writes @0x1c002640-0x1c002734): CCU shared with unit 0? unknown, treat as questionable.
  - IDM reset sweep `FUN_1c034b20` @0x6ffd4bc4-0x6ffd4c30 (relocated; image 0x1c034b20): writes 0x033a to 0x181xx900 for 17 wrappers (0x18108900, 0x18107900, 0x18109900 ... 0x18141900) plus `FUN_1c034c40` CCU 0x19004000/3000/7000: can reset blocks a1 is using.
  - Pre-DDR: `0x1c0005d0` 0x1902277c, PL310 setup 0x19022xxx (unit-local, likely fine).
- Flash writes
  - env: only on a bad CRC (not taken with the backup env): erase 0xc0000 (cmd d8) + 256 x 0x02 pages, and the `saveenv` command.
  - shmoo: "Writing Shmoo values into flash" @0x1c003218 when mtd2 invalid (0xd0000, 0x2dc B).
  - ubnt app (not run): `ureset_button` erases 0x1ef0000 (1 MiB cfg) and 0xc0000; `uclear*`, `usetmac/tbid/brev/sshkey`, `usaveenv`.
  - On a2 every one hits a missing flash: patch `spi_flash_probe` @0x1c025234 to fail cleanly and keep the default env (`env_relocate` default path), or stub it.
- Watchdog / resets
  - `wdt_arm` @0x1c0027d8 (A9 private WDT 0x19020620 load 0x0165a0bc, 0x19020628 = 0xff0b reset mode) armed from `board_init_r` (first write @0x6ffa27fc); `wdt_pet` @0x1c002808 from getc/putc/udelay; `wdt_stop` @0x1c00282c (keys to 0x19020634). On a2 a reset-mode private WDT resets only that core (I) - verify; else set WD mode off.
  - `reset` command table entry @0x1c0553e4 = NULL handler; `ubntappinit` empty -> `reset` path @0x1c00d2f4. No SP805 (0x18039000) or DMU/CRU reset write seen in the run.
- Identification reads
  - DEV ID 0x18000000 (@0x1c003550, @0x1c034d28), SKU 0x48010224 (@0x1c00357c: 0x8342-0x8344 selects clocks); GPIO 0x18000064/68 `setter` (LED bits 6,7). On a2 these may read unit-1 or shared values: check.
- Exercised now: DDR shmoo/training body, `do_bootm` (0x1c00e8d8) to the jump, `cleanup_before_linux` (PL310 invalidate polls), ubnt app copy. Not exercised: ubnt app commands, tftp/net (no NIC).
