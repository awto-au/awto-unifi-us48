# Clock / DDR / die-temperature audit (#17, #21, #55)

- Offline re-derivation 2026-11-14 from primary sources only; no board access. Every earlier claim treated as unverified.
- Tags: **C** = confirmed from a cited source or a logged board read, **I** = inferred.
- Sources:
  - `[LX]` `tmp/linux-mainline`: `drivers/clk/bcm/clk-iproc-armpll.c`, `clk-iproc-pll.c`, `drivers/thermal/broadcom/ns-thermal.c`, `arch/arm/boot/dts/broadcom/bcm-ns.dtsi`.
  - `[DL]` D-Link DGS-1210 B1 GPL HR2 U-Boot, extracted to `tmp/dl-armpll/DGS-1210-ME-B1-Series/boot/uboot_bcm_switch/uboot/`: `arch/arm/cpu/armv7/iproc/{armpll,genpll,ddr_init,ddr_restore}.c`, `arch/arm/include/asm/arch-hurricane2/socregs.h`, `board/broadcom/bcm95615x/ddr_init_table.c`.
  - `[RG]` `regdb/hr2_regs.tsv`. `[OB]` `private/reference/openbcm-hurricane2.c`. `[OM]` `/vendors/broadcom/openmdk/db9c67869680/bmd/PKG/chip/bcm56150_a0/bcm56150_a0_bmd_reset.c`.
  - `[DS]` `/vendors/broadcom/bcm56150-datasheet-whycan/56150-DS06-RDSS.pdf` (`pdftotext -layout` line numbers).
  - `[E0]` live register reads 2026-11-14, `tmp/logs/clock_trial.log` (uboot-1 prompt, 400 MHz). `[ST]` stock S-channel write trace `tmp/schan_stock_writes.txt`.

## 1. Bugs in our code (fixed in this commit unless noted)

| # | Where | Severity | Bug | Fix |
|---|---|---|---|---|
| B1 | `uboot-port/board/ubiquiti/us48/board.c:40` (old) | high (status lost, linker data corrupted) | `static int cpu_status = 0` is `.bss` (System.map `0x040390bc`, `__bss_start` = `__rel_dyn_start` = `0x040390b8`). `board_early_init_f` writes it **before relocation**, where `.bss` overlays `.rel.dyn` (`u-boot.lds` `.bss ADDR(.rel.dyn) (OVERLAY)`): it overwrites `r_info` of relocation 0 (vector word `0x04000020`), so that fixup is skipped; BSS is then zeroed, so `board_init` always prints `status=0` and a refused/failed switch (`-22`, `-110`) is never shown. C | `__section(".data")`; rebuilt: `cpu_status` now `d 0x040378a0` |
| B2 | `board.c:105`, `sram_clk.c:172,186`, `scripts/clock_trial.py:126` (old) | medium | Lock poll tests `PLLARMA` bit 28 only, but the same register write sets bit 28 = 1 (`[DL]` armpll.c:103-107 does too). If the bit is writable the poll passes with no lock. `[RG]` has `PLLARM_LOCK_RAW` bit 7; every logged read after a relock shows bits 28 and 7 set (`0x11005081`, `0x11006081`, `0x11007081`, `[E0]`) | poll `bit 28 AND bit 7` (`CCU_PLLARMA_LOCKED`) |
| B3 | `board.c` `us48_arm_hz` | low | ignores bypass (`PLLARMC` b8), lock, `PLLARM_OFFSET.SW_CTL` (b29), `ndiv_frac`, 0 = max divider; `[LX]` recalc handles all | added; pdiv field 3 bits per `[RG]`; 400 MHz gate compares whole MHz (stock frac 1 = +0.4 ppm) |
| B4 | `scripts/clock_trial.py:122` (old) | low | `mw.l 19000c04 00000001` overwrites `PLLARMB`; `[DL]` masks ndiv_frac. Harmless today (reserved bits read 0, `[E0]` `0x00000001`) | read-modify-write |
| B5 | `board.c`, `sram_clk.c`, `us48.h`, Kconfig | n/a (limit) | relock hard-coded to ndiv 80 (1000 MHz) | `ndiv = MHz x 2 / 25`, 1000 <= MHz < 2000, MHz x 2 multiple of 25 (`US48_RELOCK_OK`) |
| D1 | `board.c` / `sram_clk.c` poll counts | doubt, not changed | GO 4096 reads, lock 2000 reads: unmeasured; `[DL]` uses `0x100000` for both. Expiry = rollback to 400, so a too-short count fails safe | measure on the board (uboot-2 build with `US48_CPU_MHZ`) |
| D2 | uboot-1 private A9 WDT | doubt (I) | runs on PERIPHCLK = ARM/2: 30 s at 400 MHz (uboot.md §2) becomes 30 x 400 / MHz (8.6 s at 1400, 6.2 s at 1950). Long U-Boot commands after a switch can trip it | `clock_sweep.py` keeps each U-Boot op to 4 MiB |
| D3 | mainline `clk-iproc-armpll.c:115-118` | note | fid 2 (sys clk) returns mdiv 1 -> reports the VCO rate. `[DL]` table: fid 2 = 100 MHz (armpll.c:26). Only matters if Linux boots on fid 2 (never) | none |

Build: `scripts/build_uboot.py` default config OK, variant `US48_CPU_MHZ=1400` + `US48_SRAM_CLK=y` OK (SRAM code 0x234 B, 0 relocations), logs `tmp/logs/build_uboot_audit_*.out`. Not flashed.

## 2. ARM PLL (CCU, base `0x19000000`), re-derived

| Item | Verified value | Tag / source |
|---|---|---|
| Rate | `25 MHz x (ndiv_int + ndiv_frac / 2^20) / pdiv / mdiv(fid)`; ndiv_int 0 = 1024, pdiv 0 = max, mdiv 0 = 256; bypass `PLLARMC` b8 -> 25 MHz; unlocked -> 0 | C `[LX]` 185-227, 141-173 |
| fid source | `ARM_DIV` (`0x19000e00`) b4 override -> policy = b[3:0], else 0; fid = `POLICY_FREQ` >> 8 x policy & 7; `POLICY_DBG` (`0x19000ec0`) [14:12] active fid wins | C `[LX]` 66-96 |
| `ARM_DIV` live | `0x2012`: override 1, policy **2**, `CORE_CLOCK_OFF` 2 | C `[E0]`, `[RG]` |
| fids | 0 xtal, 2 sys clk (100 MHz `[DL]` row), 6 = ch0 `PLLARMC` [7:0], 7 = ch1 `PLLARMCTL5` [7:0] | C `[LX]` 51-56, `[DL]` armpll.c:25-37 |
| Live dividers | `PLLARMA` `0x11004083` (ndiv 64, pdiv 1, lock, LOCK_RAW, both resets released), `PLLARMB` 1, `PLLARMC` 4, `PLLARMCTL5` 2, `PLLARM_OFFSET` `0x10000000` (OFFSET_MODE 1, SW_CTL 0 -> ndiv from `PLLARMA`) | C `[E0]` |
| `PLLARMA` fields | SOFT_RESETB 0, SOFT_POST_RESETB 1, PWRDWN 3, IDLE_PWRDWN_SW_OVRRIDE 4, FLOCK 5, LOCK_LATCH 6, LOCK_RAW 7, NDIV_INT 17:8, PDIV **26:24 (3 bits)**, LOCK 28 | C `[RG]`; `[LX]` masks pdiv with 0xf |
| Other status | `PLL_DEBUG` `0x19000e10`: LOCK_IGNORE b0, **LOCK_LOST b1**, TIMER_LOCK_EN b2 | C `[RG]` |
| Loop filter | `PLLARMCTRL0` `0x19000c0c` KP 22:19, KI 18:16, KA 15:13 (reset `0x3a4000`: KP 7, KI 2, KA 2); `PLLARMCTRL3` `PLL_CONFIG_CTRL` reset `0x8102000`. `[DL]` never writes them, not even for its 1200 MHz row (VCO 2400) | C `[RG]`, `[DL]` |
| Relock sequence | unlock `0xa5a501` -> all policies fid 2 + b31 -> GO\|GO_AC, poll GO clear -> `PLLARMB` frac -> `PLLARMA` = LOCK\|ndiv<<8\|pdiv<<24\|SOFT_RESETB -> poll lock -> set SOFT_POST_RESETB -> policies fid 7 -> 5 CLKGATE regs `0x303` -> GO, poll -> lock `0` | C `[DL]` armpll.c:62-152. Ours matches except the CLKGATE writes; `0x303` = their `[RG]` reset value, so a no-op unless changed (I) |
| Table | 400/448/500/560/600 on fid 6, 800/896/1000/1100/1120/1200 on fid 7, pdiv 1, mdivs never written. Max VCO **2400** (1200 row, ndiv 96) | C `[DL]` armpll.c:23-38 |
| VCO limit | No ARM-PLL limit in any source. Generic iProc PLL: 700-4000 MHz, loop-filter bands change at 2200 and 3100 MHz (`VCO_HIGH`, `VCO_HIGH_HIGH`) | C `[LX]` clk-iproc-pll.c:54-59, 327-340; applying it to the ARM PLL: I |
| 1400 MHz claim | ndiv 112, VCO 2800: above every Broadcom row (2400), inside the generic 2200-3100 band; ran 5 min soak 2026-11-14 (#17) with reset loop params | C (run, `[E0]` `0x11007083`), I (margin) |
| CPU rating | BCM56150 "Maximum CPU speed 1 GHz"; core 1.0 V +-5 %; TJ 125 C max, 110 C steady state | C `[DS]` p21, 5957-5959, 8471 |

Sweep values (pdiv 1, ch1 mdiv 2, `python3 scripts/clock_sweep.py --plan`): 1400 ndiv 112 / VCO 2800 ... 1500 ndiv 120 / VCO 3000; **1550 (VCO 3100) enters the generic HIGH_HIGH band**; last step below VCO_MAX = 1950 (ndiv 156, VCO 3900). 50 MHz = ndiv + 4.

## 3. Die temperature sensor (#21)

- Block: one PVTMON per chip in the switch TOP block (S-channel, not AXI MMIO). `[OB]` `pvtmon_result_reg[] = { TOP_PVTMON_RESULT_0r }` (openbcm-hurricane2.c:6145-6147): **one per unit**, "x2" in #21 = unit 0 + unit 1. C
- Registers (TOP = SBUS block 16, `[RG]`): `TOP_PVTMON_CTRL_0` `0x02050000`, `TOP_PVTMON_CTRL_1` `0x02050400` (PVTMON_SELECT 29:27, POWERDOWN b30, **RESET_N b31**), `TOP_PVTMON_RESULT_0` `0x02050800` (TEMP_DATA 9:0, PEAK_TEMP_DATA 31:22). C
- Formula: **T [C] = (4180000 - 5556 x TEMP_DATA) / 10000** = 418 - 0.5556 x code. C `[OB]` openbcm-hurricane2.c:6172-6175 (result in 0.1 C); same constants in `[LX]` bcm-ns.dtsi:555 `coefficients = <(-556) 418000>` (m C). Code 680 = 40.2 C, 600 = 84.6 C.
- Init (sensor is in reset after power-on until the switch SDK runs; neither uboot-1 nor our drivers init it): `CTRL_0` = `0x001d2003` (FUNC_MODE_SEL 0, MEASUREMENT_CALLIBRATION 5, BG_ADJ 3), `CTRL_1` = 0 then `0x80000000`. C `[OM]` 154-172, `[ST]` lines 18-19 (both units). Peak reset uses `TOP_SOFT_RESET_REG_2` (shared with PLL resets): skipped.
- Access: S-channel READ_REG header `0x2d000200` (op 0x0b, blk 16, 4 B), WRITE_REG `0x35000200`, address word = offset (OM form, driver-internals.md §1). Use **CMC1** (`+0x32000`) because our drivers own CMC0 (`kernel/us48-cmic/us48_cmic.h:14`). CMC1 SCHAN regs exist C `[RG]`; independent per-CMC operation I.
- Unit 0 CMIC `0x48000000`; unit 1 CMIC = PCIe BAR2 `0x0a000000` (Linux boot logs). Precondition: `CMIC_SBUS_RING_MAP_16_23` (`+0x100a0`) nibble 0 = 4 (stock/OpenMDK `0x00005004`); else TOP is not routed: read it first.
- Linux, read-only (unit 0; unit 1: replace `0x4803` with `0x0a03`, `0x48010` with `0x0a010`):

```
devmem 0x480100a0                       # ring map, low nibble must be 4
devmem 0x4803200c 32 0x2d000200; devmem 0x48032010 32 0x02050400; devmem 0x48032000 32 1
devmem 0x48032000; devmem 0x48032010; devmem 0x48032000 32 0     # CTRL (b1 DONE, b20-22 errors), CTRL_1 (b31 = running)
devmem 0x4803200c 32 0x2d000200; devmem 0x48032010 32 0x02050800; devmem 0x48032000 32 1
devmem 0x48032000; devmem 0x48032010; devmem 0x48032000 32 0     # CTRL, RESULT_0 -> T = 418 - 0.5556 x (v & 0x3ff)
```

- Init if CTRL_1 b31 = 0 (writes, from `[OM]`/`[ST]`): same pattern with header `0x35000200`, address, then data at `+0x14` (`0x48032014`): `0x02050000 <- 0x001d2003`, `0x02050400 <- 0`, `0x02050400 <- 0x80000000`. `scripts/clock_sweep.py` `die_temps_linux()` does read, init-if-needed, read.
- uboot-1 / uboot-2: same sequence with `mw.l 4803200c 2d000200; mw.l 48032010 02050800; mw.l 48032000 1; md.l 48032000 1; md.l 48032010 1; mw.l 48032000 0`. Ring map may be unprogrammed before any switch init (I): read `md.l 480100a0 1` first.
- Linux driver: none usable. `brcm,ns-thermal` (CONFIG_BCM_NS_THERMAL) reads an MMIO PVTMON (`ns-thermal.c:24-36`); HR2's is S-channel only (no PVT/TMON register in the iProc MMIO space of `[RG]`). A hwmon needs an `us48_cmic` S-channel hook (CMC1). C (absence in `[RG]` and mainline), I (design).
- Abort limit for experiments: **99 C** = `[DS]` 110 C steady-state TJ minus 10 %; sensor accuracy unknown.

## 4. DDR (#55)

| Item | Verified value | Tag / source |
|---|---|---|
| DDR clock source | **XGPLL**, not GENPLL: `IPROC_WRAP_IPROC_XGPLL_CTRL_0..4` `0x1803fc1c..2c`, STATUS `0x1803fc30` (LOCK b31). DDR CK = 25 x NDIV_INT (`CTRL_4` [7:0]) x 2 / CH1_MDIV (`CTRL_2` [7:0]); memc = CK / 2 | C `[DL]` ddr_init.c:190-198, socregs.h:117-180, `[RG]` |
| Live | `CTRL_4` `0x78` (NDIV 120, VCO 3000), `CTRL_2` 9 -> CK 666.7 MHz (DDR3-1333), memc 333; `CTRL_0` `0x180f641e` (ch0 30, ch3 100, ch4 15, ch5 24 = reset), `CTRL_1` `0x0e60c000` (pdiv 1, SW_OVWR, resets released) | C `[E0]` |
| Clock table | 333 / 400 / 467 / 533 / **667** (ndiv 120, ch1 9) / **800** (ndiv 128, ch1 8; ch0 32, ch3 80, ch4 16, ch5 24) | C `[DL]` ddr_init_table.c:1409-1419 |
| 800 side effects | ndiv 128 moves the other XGPLL channels: ch5 125 -> 133 MHz, ch3 30 -> 40 MHz; consumers of ch0/3/4/5 unknown | C (arithmetic), I (consumers) |
| Rated | BCM56150 DDR3-1333 (667 MHz CK) max | C `[DS]` p21 |
| SKU default | Broadcom default for 0x8344 (and 0x8394, 0xb151) = **400 MHz**; Ubiquiti overrides to 667 | C `[DL]` ddr_restore.c:26-36 + stock log |
| Controller | Denali `DDR_DENALI_CTL_00..` at `0x18010000`, two frequency sets (F0/F1 fields); PHY = DDR40 at `0x18010800` (control) and `0x18010a00` (word lane 0) | C `[RG]` |
| Timing regs | CTL_05 CASLAT_LIN/WRLAT, CTL_06..12 tRRD/tRC/tRAS/tRP/tWTR/tRTP/tMRD/tMOD, CTL_17 tFAW, CTL_20..22 tRFC/tREF, CTL_210 tRCD/tWR, CTL_44..60 MR0-MR3 data, CTL_82 ROW/COL/BANK_DIFF | C `[RG]` |
| PHY straps | `STRAP_CONTROL/STATUS` `0x180108b0/b8`: MHZ 27:16, JEDEC_TYPE, CHIP_SIZE, CHIP_WIDTH, BUS16, VDDQ; `STRAP_*2` `0x180108b4/bc`: CL, CWL, WR, AL, DDR3. `DDR_S1_IDM_IO_CONTROL_DIRECT` `0x18109408` I_PHY_DDR_MHZ 27:16 (reset `0x190` = 400) | C `[RG]`, `[DL]` ddr_init.c:571, 640-652 |
| Shmoo blob (mtd2 `0xd0000`) | header = `shmoo_sig_t`: `"SHMO"`, word1 = dev_id `0xdb56` (= 56150, ChipcommonA ChipID) \| sku `0x8344` << 16 (CMIC_DEV_REV_ID), word2 = ddr_type 1 (DDR3) \| ddr_clock **667** << 2, checksum = u32 sum of all 178 reg/value words (**recomputed: match `0x58e6c427`**), pairs 89 | C `[DL]` ddr_restore.c:52-61, 381-471; `scripts/shmoo_decode.py` |
| Shmoo pairs | 3 PHY control (`0x830` VDL_OVRIDE_BYTE_CTL, `0x834` BIT_CTL, `0x86c` VREF_DAC_CONTROL DAC0/1 = 32) + 86 word-lane-0 (`0xa00..0xb6c` VDL override steps per byte/bit for RD_EN, W, R_P, R_N; `0xb60` READ_DATA_DLY 3; `0xb64` READ_CONTROL ODT; `0xba0..bb0` pad/drive/preamble). Full table: `tmp/ddr/shmoo_decoded.md` (regenerate). | C |
| Blob validity | signature includes ddr_clock: a stock boot at another DDR clock fails validation, re-runs shmoo and **writes mtd2** (`iproc_save_shmoo_values`) | C `[DL]` ddr_restore.c:427-452, 554-580 |
| DRAM part | not identified (no photo, no log). 256 MiB used, 16-bit bus (`[DL]` HR2 strap comment). Width/density readable from `CTL_82` + `STRAP_STATUS` | open |
| DRAM temperature | none readable: DDR3 has no temperature readout (MR4 refresh-rate readout is LPDDR2/3 only); Denali/PHY expose no sensor field in `[RG]`. Proxy = die PVTMON (§3) | C (absence in `[RG]`), I (JEDEC) |
| DDR "speed sensor" | `DDR_PHY_CONTROL_REGS_PLL_STATUS` `0x18010810`: LOCK b0, CLOCKING_4X b13, **DDR_MHZ 25:14**, LOCK_LOST b26; plus `STRAP_STATUS.MHZ` and XGPLL regs above. Measured bandwidth/latency: `scripts/memtest_arm.c` | C `[RG]` |

Read-only DDR identification (uboot-1, uboot-2, or `devmem` in Linux; none of these has side effects per `[RG]` names):

```
md.l 1803fc1c 5; md.l 1803fc30 1        # XGPLL ctrl 0..4, lock
md.l 18010810 1                         # PHY PLL_STATUS: DDR_MHZ, LOCK, LOCK_LOST
md.l 180108b0 4                         # STRAP_CONTROL/2, STRAP_STATUS/2
md.l 1801084c 1; md.l 18010844 1        # VDL calib status, read-data-delay status
md.l 18010000 1                         # DENALI_CTL_00 DRAM_CLASS
md.l 18010014 6; md.l 18010044 6        # CL/WL, tRRD/tRC/tRAS/tRP/tWTR/tRTP/tMRD/tMOD/tCKE, tFAW, tRFC/tREF
md.l 18010148 1; md.l 18010348 2        # ROW/COL/BANK_DIFF; tRCD/tWR
md.l 180100b0 4                         # MR0..MR3
md.l 18109408 1; md.l 18109500 1        # IDM I_PHY_DDR_MHZ; DDR_TYPE / PHY ready
md.l 18010830 2; md.l 1801086c 1; md.l 18010a00 6e   # compare with the 89 shmoo pairs
```

What a faster DRAM clock needs (none of it done): XGPLL relock to the 800 row (moves ch0/3/4/5 too) with DDR in self-refresh, from SRAM (`sram_clk.c` stack is still in DDR); IDM `I_PHY_DDR_MHZ` / PHY strap MHz = 800; DDR40 PHY PLL relock; Denali timing table for 1600 (`[DL]` `ddr3_init_tab_1600`) checked against the DRAM datasheet (part unknown); shmoo re-run (`[DL]` `shmoo2/shmoo_ddr40.c`); results kept in RAM, never mtd2. Gate: DRAM part and speed grade first (#55).

## 5. Old claim vs verified

| Doc | Old claim | Verified |
|---|---|---|
| ddr.md | `0x8344db56`: low half `0xdb56` unknown | dev_id `0xdb56` = 56150 (ChipID), high half sku 0x8344. C |
| ddr.md | `0x0a6d` unknown | ddr_type 1 \| ddr_clock 667 << 2. C |
| ddr.md | `0x58e6c427` checksum (I), algorithm not traced | u32 sum of reg+value words, recomputed equal. C |
| ddr.md | 667 = "Ubiquiti SKU choice (I)" | Broadcom default for 0x8344 is 400 MHz; Ubiquiti raised it to 667 = BCM56150 DDR3 max. C |
| ddr.md | DDR PLL "in GENPLL (`0x1803fc00..`)" | XGPLL ch1 at `0x1803fc1c..30`. C |
| ddr.md | controller table "up to 800 MHz (I)" | 800 row exists, ndiv 128 ch1 8. C |
| sram.md:27 | "12 B header + 89 pairs" | 20 B header (12 B signature + checksum + count); 0x14 + 89 x 8 = 0x2dc. C |
| clocks.md | `PLLARMA` pdiv 27:24 | 26:24 (`[RG]`); mainline masks 4 bits. C |
| clocks.md | GENPLL "ch5 = 2000/40 = 50 MHz" | live `CTRL2` `0x00080a00`: ch5 mdiv 0, ch4 8 (250), ch3 10 (200); `CTRL1` ch0 10, ch1 0. The log's "GENPLL[5] mdiv=40" does not map to `CTRL2` [31:24]. C (regs), I (log meaning) |
| clocks.md | 1400 "extrapolated, no datasheet limit known" | correct; VCO 2800 > Broadcom max row 2400; generic iProc VCO_MAX 4000. C |
| uboot-cpu-clock.md | "status=-22 on the console" on refusal | never printed before B1 fix. C |
| #21 | "HR2 PVTMON x2" | one PVTMON per chip; x2 = two chips. C |

## 6. Test plan for the board agent (`scripts/clock_sweep.py`)

- Prereq: `python3 scripts/clock_sweep.py --build` (memtest_arm, 42 KB static, musl). `--plan` prints the step table. `US48_HOST=lizard`, OpenWrt image in kernel0 openwrt slot (as `clock_trial.py cpu`).
- Default: `python3 scripts/clock_sweep.py --start 1400 --stop 1999 --step 50 --soak 300`. Log `tmp/logs/clock_sweep.log`, table `tmp/logs/clock_sweep_results.md`.
- Per step:
  1. `dev._session_at_uboot` -> uboot-1 prompt (400 MHz, fresh from reset).
  2. Calibrate U-Boot op budget: time `crc32` of 4 MiB at 400 MHz; budget = 1.25 x measured + console.
  3. References at 400 MHz: `sf read` 1 MiB of the openwrt slot + `crc32`; `crc32` of the same bytes through the BSPI window `0x1c1e0000`.
  4. `sf read` the kernel to `0x01000000`, arm SP805 (46 s), `clock_trial.switch_cpu(mhz)` (gate, fid 2, PLLARMB RMW, `PLLARMA`, lock bits 28 + 7, post-reset, fid 7, verify GO clear + POLICY_DBG fid 7, `PLL_DEBUG`).
  5. U-Boot checks at the new clock (SP805 petted between ops): DDR `0x02000000..0x05ffffff` constant patterns 0/~0/0x55../0xaa.. (`mw.l` + `crc32` per 4 MiB vs host zlib CRC), walking ones (32 x 1 MiB), `cp.l` + `cmp.l` 4 MiB, SRAM 16 KiB two patterns, `sf read` CRC and BSPI-window CRC equal to step 3.
  6. Re-arm SP805, `bootm`; userspace within `dev.USERSPACE_BUDGET_S`.
  7. Linux (ssh): `sched_clock` = MHz / 2; board uptime vs host clock over 10 s within 2 %; `memtest_arm --mib 64` (DDR const/addr/addr-inverse/walk 0+1 with CRC, memset/memcpy/32-bit read/write MB/s, L1/L2/DDR pointer-chase ns; SRAM `0x1b000000` 16 KiB via `/dev/mem` patterns + MB/s, contents restored); `md5` of mtd `u-boot` and `shmoo` vs the verified backup (Linux spi-nor/MSPI path); PCI: `0000:01:00.0` vendor `0x14e4`, `CMIC_DEV_REV_ID` unit 0 `0x48010224`, unit 1 `0x0a010224` not `0xffffffff`; host `ping -c 5` 0 % loss; die temp both units (§3); hwmon max.
  8. Soak (default 300 s): `memtest_arm --mib 16 --no-sram` loop, die + hwmon temps every 15 s.
- Stop (record row, power-cycle to stock 400): any FAIL, die >= 99 C, hwmon >= 70 C (`clock_trial.TEMP_ABORT_C`), VCO >= 4000, U-Boot op past budget, memtest stall 60 s or 3 x first run, no ssh in 600 s.
- Recovery: SP805 resets in <= 46 s on a hang; uboot-1 reprograms 400 MHz every boot; nothing is written to flash.
- After a pass: set `CONFIG_US48_CPU_MHZ` to the chosen value in `uboot-port/configs/us48_defconfig` (B1/B2 fixed builds only), check the `cpu: ... status=2` console line.
- Not covered: core voltage (no control known), DDR clock change (§4 gate), AXI/GENPLL (clocks.md E5).
