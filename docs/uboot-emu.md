# uboot-1 host emulator (#69 phase 1)

`scripts/uboot1_emu.py`: runs the vendor U-Boot (`usw-v1.1.6.122`) from reset under Unicorn (Cortex-A9 model), hooks every device access. Offline; no board contact.

- Inputs (from `private/`, never committed): newest `private/backups/*/full-flash.bin` (mapped at 0x1c000000, 32 MiB), `tmp/ghidra/out/uboot/*.funcs.tsv` (names; docs `name@0x1c...` override `FUN_`), `tmp/logs/clock_trial.log` (live `md.l` dumps -> register defaults).
- Run: `python3 scripts/uboot1_emu.py --budget 80000000 --cmd version --cmd help ...` (needs `unicorn`, `capstone`; repo `.venv`). ~3 min for the full boot + 11 commands.
- Outputs: `tmp/logs/uboot1_emu.log` (events, console; use `grep -a`), `tmp/uboot1_emu.tsv` (seq, pc, func, R/W, addr, size, value, flag), `tmp/uboot1_emu_report.md` (coverage, writes per phase, polls, unmapped, no-default reads, flash writes, functions not run).
- Memory map: flash XIP 0x1c000000, SRAM 0x1b000000 (16 KiB), L2-as-RAM 0x50000000 (128 KiB, boot stack 0x5001eff0), DDR 0x60000000 (256 MiB) + 128 MiB alias at 0, vectors 0xffff0000 (`b .`). MMIO: 0x18000000-0x1803ffff, 0x18100000-0x181fffff (IDM), 0x19000000-0x190fffff, 0x48000000-0x480fffff. Other pages are auto-mapped as logged UNMAPPED+UNKNOWN.
- Memory map should move to `scripts/hwdb.py` (#72) once it covers these ranges.

## Device model
- Register store, reads return last write, else default (clock_trial dumps), else 0 (flagged `DEF0`, listed in report).
- UART1 0x18000400: byte regs, LSR = THRE|TEMT (+DR when input queued); TX -> console text. A space is injected after "autoboot" appears; `--cmd` lines are sent at each `u-boot> ` prompt; stop when none left.
- A9 global timer 0x19020200/204: counter += `--timer-step` per read (udelay).
- MSPI 0x18027200..: flat TXRAM-word model feeding a byte-stream MX25L25635E (JEDEC id, RDSR=0, READ). Writes/erases are logged `FLASH-WRITE`, never applied.
- Polls: same (pc, addr) read > `--poll-n` (16) times; the next <= 10 instructions (tst/and/cmp/ubfx/lsl, stack spill/reload, branch) are evaluated for candidate values; the first that leaves the loop is returned and logged `POLL`. Loops that exit unaided are untouched.

## Phase 1 result (80 M insn budget, 2026-11-15)
- Boots to `u-boot> ` and answers `version help printenv coninfo md.l mw.l crc32 sf pci echo`. Banner, DDR type/speed, relocation to 0x6ffa0000, env, PCIe, L2, clock banner printed.
- Gaps (not modelled, each a phase-2 item):
  - DDR PHY PLL lock poll after the PHY reset is not auto-satisfied (timer-counted loop): "DDR phy calibration failed"; the shmoo/training body never runs.
  - MSPI queue semantics: env read gets bad CRC (default env saved: 1 erase + 256 page programs at 0xc0000, not applied); shmoo-save probe reads the JEDEC id at a different slot ("Unsupported manufacturer 00"); ubnt app copy reads magic 0 ("UBNT APP Magic mismatch").
  - IDM reset registers (0x18100000+) and DDR/PHY (0x18010a00..) have no real behaviour; PCIe port reads 0 ("End-Point mode - ignored").
- Coverage 165 / 513 text functions (32.2%; Ghidra entries past 0x1c048000 are data, dropped). Device writes: IDM 24, PLL/clock 34, UART 9, GPIO 4, WDT 157, QSPI ~116k (MSPI queue traffic), PCIe 33, A9-priv/SCU/L2 162, DDR/PHY 30, other 7; CMIC read-only (DEV id).
- Auto-satisfied polls (7): 0x1902277c @0x1c0005d8 (PL310), 0x1900000c @0x1c00266c/0x1c002748 (CCU PLL lock), 0x1803fc30 @0x1c002cdc (GENPLL lock), 0x18010800 @0x1c003600 (DDR PHY up), 0x18027114 @0x6ffc4e14/0x6ffc4f00 (BSPI busy).
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
  - env: bad CRC -> erase 0xc0000 (cmd d8) + 256 x 0x02 pages @ spi path (emulated count), `saveenv` command.
  - shmoo: "Writing Shmoo values into flash" @0x1c003218 when mtd2 invalid (0xd0000, 0x2dc B).
  - ubnt app (not run): `ureset_button` erases 0x1ef0000 (1 MiB cfg) and 0xc0000; `uclear*`, `usetmac/tbid/brev/sshkey`, `usaveenv`.
  - On a2 every one hits a missing flash: patch `spi_flash_probe` @0x1c025234 to fail cleanly and keep the default env (`env_relocate` default path), or stub it.
- Watchdog / resets
  - `wdt_arm` @0x1c0027d8 (A9 private WDT 0x19020620 load 0x0165a0bc, 0x19020628 = 0xff0b reset mode) armed from `board_init_r` (first write @0x6ffa27fc); `wdt_pet` @0x1c002808 from getc/putc/udelay; `wdt_stop` @0x1c00282c (keys to 0x19020634). On a2 a reset-mode private WDT resets only that core (I) - verify; else set WD mode off.
  - `reset` command table entry @0x1c0553e4 = NULL handler; `ubntappinit` empty -> `reset` path @0x1c00d2f4. No SP805 (0x18039000) or DMU/CRU reset write seen in the run.
- Identification reads
  - DEV ID 0x18000000 (@0x1c003550, @0x1c034d28), SKU 0x48010224 (@0x1c00357c: 0x8342-0x8344 selects clocks); GPIO 0x18000064/68 `setter` (LED bits 6,7). On a2 these may read unit-1 or shared values: check.
- Not exercised yet (need phase 2): DDR shmoo/training body, `bootm`/`do_bootm` (0x1c00e8d8), cleanup_before_linux, ubnt app, tftp/net (no NIC).
