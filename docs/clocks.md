# Clocks: rated max vs what runs (#17, #20, #55)

- Rule: run at rated max unless a reason is recorded (global). Datasheet limits are challengeable; test them RAM-only.
- Tags: **C** = confirmed (source/log cited), **I** = inferred. **UNTESTED** = never run on the board.
- Offline research 2026-11-14; nothing here was run on the board.
- DDR (XGPLL, shmoo): see [ddr.md](ddr.md). Not repeated here.
- Sources:
  - `[UB]` stock boot log `tmp/logs/console/us48-20261014-083748.log` (facts: [stock-bootlog-facts.md](stock-bootlog-facts.md)).
  - `[DL]` D-Link DGS-1210/ME B1 GPL, HR2 U-Boot, `private/reference/DGS-1210-ME-B1-Series_GPLSourceCode.tar.gz`, under `boot/uboot_bcm_switch/uboot/`: `arch/arm/cpu/armv7/iproc/{armpll,genpll}.c`, `board/broadcom/bcm95615x/bcm95615x.c`, `drivers/spi/iproc_qspi.c`, `arch/arm/include/asm/arch-hurricane2/socregs.h`.
  - `[LX]` `tmp/linux-mainline`: `drivers/clk/bcm/clk-iproc-armpll.c`, `clk-hr2.c`, `arch/arm/boot/dts/broadcom/bcm-hr2.dtsi`.
  - `[OM]` OpenBCM `private/reference/openbcm-hurricane2.c`; `[MX]` `sources/macronix-MX25L25635E-v1.3.pdf`; `[RG]` `regdb/hr2_regs.tsv`.

## Clock tree (what is known)

| Node | Value | Tag |
|---|---|---|
| Crystal | 25 MHz (`osc`) | C `[LX]` dtsi |
| ARM PLL (CCU `0x19000000`) | VCO 25 x 64 = 1600 MHz; fid 6 = ch0 = 400 MHz (`arm_clk=400MHz`) | C `[UB]:478` + `[DL]` armpll.c table; mdiv 4 I |
| A9 PERIPHCLK (global timer, TWD) | ARM/2 = 200 MHz | C `[UB]:478`, `[LX]` `periph_clk` fixed-factor /2 |
| GENPLL (`0x1803fc00`) | VCO 25 x 80 = 2000 MHz | C `[UB]` `GENPLL[5] mdiv=40 rate=2000000000`, `[DL]` genpll.c |
| AXI | GENPLL ch3 = 2000/10 = 200 MHz | C `[UB]:478`, `[DL]` genpll.c mode 4 |
| APB | AXI/4 = 50 MHz (UART, SP805, SP804, I2C, QSPI SPBR) | C `[UB]:478`, `[DL]` bcm95615x.c comment |
| GENPLL ch5 | 2000/40 = 50 MHz, role unknown | C value, I role |
| QSPI/BSPI | `CRU_CONTROL` `0x1803e000` bits[2:1] `QSPI_CLK_SEL` | C `[RG]`, `[DL]` iproc_qspi.c |
| Switch core | `TOP_CORE_PLL`, VCO 3375 (I, derived): mdiv 40 = 84.375, 25 = 135, 135 = 25 MHz | C `[OM]` 4060-4129 for mdiv 25/135; VCO I |
| PCIe | Gen2 x1, `LNKSTA 0x9012`, `LNKCAP 0x5c12` | C `[UB]` |

## Per-domain table

| Domain | Rated max | Board allows | We run | Why the gap | How to change | Risk | Safe RAM-only test |
|---|---|---|---|---|---|---|---|
| **CPU (A9, 1 core)** | no BCM53344 datasheet in repo. `[DL]` table has 400/448/500/560/600/800/896/1000/1100/1120/1200; b15x SKUs of the same die boot at 1000 (C `[DL]` bcm95615x.c). Rated for this SKU: unknown (I) | core rail = resistor-set buck, no voltage control seen (I); heat: CPU share small next to 2 switch cores, 6 PHYs, 12 PSEs (I) | 400 MHz, fid 6 (C `[UB]`); OpenWrt reads the same (C `[LX]`) | SKU policy in Broadcom U-Boot: dev IDs 0x8342-47/0x8393/94 forced to 400 (C `[DL]` bcm95615x.c). Not a measured limit (I) | CCU `POLICY_FREQ` fid 6 -> 7 (800, no relock), or relock to 1000. Sequences: E1, E2 below | 800: low (power-cycle or SP805 reset restores 400). 1000: medium (relock, no voltage margin) | E0 then E1 at the U-Boot prompt before `bootm` |
| **AXI / APB** | `[DL]` genpll.c: AXI 100/200/250/285/400 MHz; b15x comment AXI 400, APB 100 (C) | unknown (I) | AXI 200, APB 50 (C `[UB]`) | SKU policy (I) | GENPLL ch3 mdiv (CTRL2 [15:8]) + load strobe, sequence E5 | high: APB moves UART divisor (console garbage), SP805 timeout (halves at 100 MHz), SP804, I2C timing, MSPI SPBR; DT `apb_clk` fixed 50 MHz must follow | none cheap; only E5 with console loss accepted, uboot-2 RAM only |
| **A9 PERIPHCLK** | = ARM/2 | | 200 MHz | follows ARM | none, derived. Consumers: global timer (uboot-2 `CFG_SYS_HZ_CLOCK 200000000` hardcoded, `uboot-port/include/configs/us48.h:9`, C), uboot-1 A9 private WDT (30 s = 0x0165A0BC at 781250 Hz, assumes 200 MHz, C uboot.md §2) | after ARM 800: uboot-2 `udelay` and timeouts run 2x short (I); uboot-1 private WDT expires in ~15 s but is petted by getc/putc and stopped at `bootm` (C uboot.md §2); Linux reads the rate from the PLL registers (C `[LX]`) | uboot-2 now derives the timer rate from the PLL regs and can switch early (`US48_CPU_MHZ`, [uboot-cpu-clock.md](uboot-cpu-clock.md), UNTESTED); the hardcoded constant is only a fallback |
| **QSPI flash read (BSPI)** | MX25L25635E: READ 50, FAST_READ 80 (8 dummy), 2READ/DREAD 70, 4READ/QREAD 70, 4PP 20 MHz (C `[MX]` AC table) | `QSPI_CLK_SEL` options: 62.5 / 50 / 31.25 MHz (C `[DL]` iproc_qspi.c:229-238). Quad lines wired: unknown (#20) | stock: 50 MHz, 1 lane, 3-byte address (C `[UB]` `qspi_iproc`). uboot-2 reads ~1.8 MB/s (C, #20 comment 2026-11-14) | inherited default, no reason (#20) | `CRU_CONTROL` bits[2:1] = 3 (62.5 MHz); quad: BSPI `bits_per_cycle` + QE bit (#20) | medium: stock U-Boot executes XIP from the BSPI window `0x1c000000` (I: U-Boot text at `0x1c0025e4` = flash base + offset) so a bad clock hangs it. Do it from RAM-resident uboot-2 only. If 62.5 MHz uses opcode 03h it exceeds the 50 MHz READ rating (I) | E3 |
| **QSPI MSPI (`sf`, spi-nor in Linux)** | SPBR >= 8 (`SPBR_MIN` "for safety", C `[DL]`); SCK = APB/(2 x SPBR) = 3.125 MHz at SPBR 8 (I, base = APB per `[DL]` iproc_qspi.c:286) | flash 50 MHz | OpenWrt DT `spi-max-frequency = <12500000>`, no `clocks`: driver assumes 27 MHz base (C, #20) so the real SCK is unknown (I) | missing `clocks` in DT | DT `clocks` = 50 MHz APB; lower SPBR_MIN only after a scope | low (read-only partitions) | Linux RAM boot, md5 vs backup (#20) |
| **PCIe to unit 1 (BCM53346)** | Gen2 5 GT/s x1 (C `LNKCAP 0x5c12`) | same | `LNKSTA 0x9012` = 5 GT/s x1 (C `[UB]`) | none | n/a | n/a | none. Open: MPS. BDE logs `Payload size exceeds device capability (256)` (C `[UB]`), Linux MPS 128 I; tune in DT/`pci=pcie_bus_perf` later |
| **I2C (CMIC SMBus `0x48000000`)** | iProc IP + PCA9548 400 kHz (C board.md, I NXP spec); LM75 400 kHz (I); SFP EEPROM A0h/A2h 100 kHz (I) | BCM59111 limit unknown | 100 kHz, `MODE_400` bit 31 = 0, TIMING `0x19003200` (C board.md live read); DT `clock-frequency = <100000>`, polled | SFP EEPROMs on the shared mux tree | DT `clock-frequency = <400000>` (driver rewrites TIMING) or CONFIG bit 31 | low-medium: SFP channels 0-3 must stay 100 kHz; PoE/temp channels 4-6 only (I) | E4 |
| **MIIM MDC (CMIC)** | IEEE 802.3 2.5 MHz; octal PHY (B50282) limit unknown (no datasheet in repo, I) | | `CMIC_RATE_ADJUST` DIVISOR 40, DIVIDEND 1: docs say 5 MHz (C `[OM]`, driver-internals.md), #22 says 2.5 MHz (I). Conflict: read the register + scope | SDK default | `CMIC_RATE_ADJUST` `0x48011000`, `_INT_MDIO` `0x48011004` ([31:16] DIVIDEND, [15:0] DIVISOR) | low | read-only first |
| **MSPI board IO (fans, LEDs, PoE LEDs)** | 74HC595-class, tens of MHz (I) | | SDK SPBR 8, mode 3 (C board.md); our `fan_frame.py` assumes ~1 MHz (I) | no driver, low rate is irrelevant | not worth it | none | none |
| **UART1** | 3.125 MBd (APB/16) (C 8250 `UART clock rate 50000000`); MAX3321 250 kbps | 460800 works, 921600 fails (C uboot.md §8) | 115200 | U-Boot fixed | n/a | n/a | none |
| **SerDes / XLPORT / HiGig** | line rates fixed by protocol: 10.3125G SFP+, 1G SFP, 13G HiGig2 between chips (C board.md); TSC ref 156.25 MHz (C driver-internals.md) | | at spec | no gap by design | not tunable | n/a | none |
| **Switch core** | SDK options: 84.375 native, 135 (any TSC port > 1G), 25 (low power) (C `[OM]`); lower mdiv unvalidated (I) | | not initialised by us (I); stock sets it in switchdrvr | no driver yet | `TOP_MISC_CONTROL_1.CMIC_TO_CORE_PLL_LOAD=1`, `TOP_CORE_PLL_CTRL4.MSTR_CH0_MDIV` (0x2039000), `TOP_CORE_PLL_CTRL2.LOAD_EN_CH0/1` (0x2038800) (C driver-internals.md) | n/a until driver; no forwarding need beyond line rate (I) | none |
| **DDR** | see [ddr.md](ddr.md) | | 667 MHz | | | | |

## Register map (ARM PLL / CCU, base `0x19000000`)

| Reg | Addr | Fields | Tag |
|---|---|---|---|
| `WR_ACCESS` | `0x19000000` | write `0xa5a501` to unlock, 0 to lock | C `[DL]` armpll.c |
| `POLICY_FREQ` | `0x19000008` | policy0..3 fid at bits 0/8/16/24 (3 bit), `priv_access_mode` bit 31 | C `[DL]` socregs.h |
| `POLICY_CTL` | `0x1900000c` | GO bit 0, GO_AC bit 1; GO self-clears when switched | C |
| `PLLARMA` | `0x19000c00` | soft_resetb 0, soft_post_resetb 1, ndiv_int 17:8, pdiv 27:24, lock 28 | C |
| `PLLARMB` | `0x19000c04` | ndiv_frac 19:0 | C |
| `PLLARMC` | `0x19000c08` | ch0 mdiv 7:0, bypass bit 8 | C `[LX]` |
| `PLLARMCTL5` | `0x19000c20` | ch1 mdiv 7:0 | C `[LX]` |
| `PLLARM_OFFSET` | `0x19000c24` | SW offset mode bit 29 (changes ndiv if set) | C `[LX]` |
| `ARM_DIV` | `0x19000e00` | policy override bit 4 | C `[LX]` |
| `POLICY_DBG` | `0x19000ec0` | active fid bits 14:12 | C `[LX]` |
| `CORE0/1_CLKGATE`, `ARM_SWITCH`, `ARM_PERIPH`, `APB0` | `0x19000200/204/210/300/400` | `[DL]` writes `0x303` after a relock | C |

- fids: 0 = 25 MHz crystal, 2 = sys clk, 6 = ch0 ("slow"), 7 = ch1 ("fast"). C `[LX]`
- `[DL]` rows, all pdiv 1: 400 = ndiv 64 fid 6; 800 = ndiv 64 fid 7; 1000 = ndiv 80 fid 7; 500 = ndiv 80 fid 6. mdivs are never written by armpll.c. I: ch0 mdiv 4, ch1 mdiv 2 (1600/4 = 400, 1600/2 = 800, 2000/2 = 1000 all consistent).
- Frequency = 25 x ndiv / pdiv / mdiv(active fid). Risk: if ch1 mdiv reads 1, fid 7 = 1600 MHz. The E1 gate checks it.
- Linux `clk-hr2` is recalc-only (no `set_rate`) and honours the active fid, so a pre-kernel change shows correctly in the clock tree. C `[LX]`
- U-Boot (uboot-1) re-runs `iproc_config_armpll(400)` + `iproc_config_genpll(4)` every boot (C `[DL]` bcm95615x.c `board_early_init_f`; the stock call `0x1c0025e4(400)`, I same function, uboot.md §5). So a watchdog reset always returns to 400.

## Watchdog during every experiment

- uboot-2 and `dev.py` arm the SP805 at `0x18039000` before `bootm`: load `50_000_000 * 23`, IRQ at ~23 s, reset at ~46 s (C `scripts/dev.py:591-603`). APB is unchanged by E1-E4, so the 46 s is unchanged.
- The ARM clock does not clock the SP805 (APB). A hung CPU at 800 resets the board in ~46 s once armed; U-Boot then restores 400. C (SP805 reset verified, #11), I (full recovery).
- Order: arm SP805 first, then the clock `mw` lines, then verify (`md`), then `bootm`. Send as one batch; the whole thing must finish well inside 23 s.
- Before the SP805 is armed (prompt phase) the uboot-1 private A9 WDT (30 s at 200 MHz, ~15 s at 400 MHz PERIPHCLK) covers a hang. C uboot.md §2

## Experiments (all UNTESTED, RAM-only, no flash writes, no `saveenv`)

Ranked: lowest risk, highest value first.

### E0. Read-only dump (no risk)

UNTESTED. At any U-Boot prompt (or `devmem` from OpenWrt):

```
md.l 19000000 4
md.l 19000c00 3
md.l 19000c20 2
md.l 19000e00 1
md.l 19000ec0 1
md.l 1803fc00 4
md.l 1803fc18 1
md.l 1803fc1c 5
md.l 1803e000 1
md.l 1803e010 1
```

Expected if the I rows hold: `0x19000008` = `0x86060606`; `0x19000c00` = `0x11004003` (lock, pdiv 1, ndiv 64, post+soft reset released); `0x19000c08` low byte 4; `0x19000c20` low byte 2; `0x19000ec0` bits 14:12 = 6; `0x1803fc04` NDIV_INT = 80. Record the values in this file (replace the "I" with "C"). `0x1803e000` bits[2:1] tell the BSPI clock (E3).

### E1. CPU 400 -> 800 MHz (fid 6 -> 7, no relock)

Gate: only run if `md.l 19000c20 1` low byte == 2 and `0x19000ec0` bits 14:12 == 6. Otherwise stop.

UNTESTED, to paste at the **uboot-1 prompt right before the Linux `bootm`** (uboot-2 hardcodes a 200 MHz timer, see table):

```
mw.l 18039c00 1acce551
mw.l 18039000 448b9b80
mw.l 18039008 3
mw.l 19000000 a5a501
mw.l 19000008 87070707
mw.l 1900000c 3
md.l 1900000c 1
md.l 19000ec0 1
mw.l 19000000 0
bootm 0x01000000
```

- `0x448b9b80` = 50_000_000 x 23 (matches `dev.py` `SP805_LOAD`; `dev.py` itself arms with this, so prefer `dev.py`'s own arm + `send` of the CCU lines).
- Policy word: `0x80000000 | 7 << 24 | 7 << 16 | 7 << 8 | 7` = `0x87070707`. `POLICY_CTL` `3` = GO | GO_AC.
- Pass condition at the prompt: `md.l 1900000c` bit 0 = 0 (GO cleared). If still 1 on the first read, re-read once; if still 1 after ~1 s, do not `bootm`: power-cycle. Expiry rationale: the CCU switch takes a few hundred reference cycles (I); `[DL]` polls 0x100000 reads; a human-latency read is already orders longer.
- `md.l 19000ec0` bits 14:12 must read 7 before `bootm`.
- Back out without power-cycling (CPU alive):

```
mw.l 19000000 a5a501
mw.l 19000008 86060606
mw.l 1900000c 3
mw.l 19000000 0
```

- Linux pass criteria (#17): `sched_clock: 64 bits at 400MHz` (was `64 bits at 200MHz`, C `tmp/logs/console/us48-20261113-132714.log:3738`), `Calibrating delay` ~2x, clean initramfs unpack, 10 min CPU load + `md5sum` loop, fand temperature log (TEMP-2) flat, n = 5 power cycles then n = 20.
- Fail modes to log: hang before `bootm` (SP805 reset in ~46 s), early oops/lock-up after `Starting kernel` (wrong rate or marginal silicon), silent corruption (md5 loop mismatch).

### E3. QSPI BSPI clock 50 -> 62.5 MHz (boot-time lever, #20 / #34)

UNTESTED. From RAM-resident uboot-2 only (stock XIP U-Boot would execute from the BSPI window; I). Read current: `md.l 1803e000 1` (bits[2:1]). Let `V` = that value.

```
md.l 1803e010 1                     (bit 0 QSPI_CLK_READY)
mw.l 1803e000 <(V & ~6) | 6>         (62.5 MHz; [DL] sel 3 = 0x6)
md.l 1803e010 1                     (bit 0 must read 1)
crc32 1c0e0000 100000                (kernel0 first 1 MiB through BSPI window; compare against the value at 50 MHz)
```

- Do the `crc32` at the old clock first and compare; repeat 10 x. Any mismatch -> write `V` back.
- Window address `0x1c0e0000` = flash base `0x1c000000` + kernel0 offset `0xe0000` (I: base from U-Boot text at `0x1c0025e4`).
- Option values: `0x2` = 50, `0x4` = 31.25, `0x6` = 62.5 MHz (C `[DL]` iproc_qspi.c). `0x0` = lower/default (I).
- Quad read (4 x 70 MHz) needs the QE bit and a wiring check first: plan in #20.

### E4. I2C 100 -> 400 kHz (PoE/temperature channels only)

UNTESTED. Linux RAM boot: DT `clock-frequency = <400000>` on `i2c@48000000`, then only touch mux channels 4-6 (BCM59111 `0x28-0x2d`, LM75 `0x48-0x4b`), never 0-3 (SFP). Pass: `i2cdetect` of those channels stable over 1000 reads, no NACK. Gain: ~4x PoE/temperature poll rate; value only once the PoE driver polls heavily.

### E2. CPU 1000 MHz (relock; only after E1 passes thermal and stress)

UNTESTED. Same prompt/SP805 order as E1. Replicates `[DL]` armpll.c for the 1000 row (ndiv 80, pdiv 1, fid 7). Replace `<B>` = (`PLLARMB` read value & `0xfff00000`) | 1.

```
mw.l 19000000 a5a501
mw.l 19000008 82020202      (policies -> fid 2, sys clk; CPU slows to ~100 MHz)
mw.l 1900000c 3
md.l 1900000c 1             (GO clear)
mw.l 19000c04 <B>
mw.l 19000c00 11005001      (lock bit | ndiv 80 << 8 | pdiv 1 << 24 | soft_resetb)
md.l 19000c00 1             (bit 28 = lock; I: bit may be RO status. The write above sets it, re-read to see the real state)
mw.l 19000c00 11005003      (soft_post_resetb)
mw.l 19000008 87070707
mw.l 1900000c 3
md.l 1900000c 1             (GO clear)
md.l 19000ec0 1             (fid 7)
mw.l 19000000 0
```

- Expected clock: 25 x 80 / 1 / ch1 mdiv 2 = 1000 MHz (needs the E0 mdiv read to be 2).
- Back out: same as E1, plus fid 6 needs the VCO back at 1600: `mw.l 19000c00 11004003` after setting fid 2. Simplest: SP805 reset or power-cycle (uboot-1 reprograms 400).
- `[DL]` also writes `0x303` to the five CLKGATE regs after relock (table above); not needed for a switch between already-enabled policies (I).

### E5. AXI / APB (document only)

- GENPLL row (`[DL]` genpll.c): ndiv 80 (`CTRL1` [9:0]), ch3 mdiv in `CTRL2` [15:8]: 5 = 400, 7 = 285, 8 = 250, 10 = 200 (now), 20 = 100 MHz AXI; ch4 mdiv `CTRL2` [23:16] = 8. Sequence in `[DL]`: clear `CTRL3` [21:16] load-enables, set `CTRL0` bit 28 FAST_LOCK, write NDIV and MDIV, set `CTRL3` bit 28 (`SW_TO_GEN_PLL_LOAD`), write load-enable `0x18` to `CTRL3` [21:16], poll `STATUS` (`0x1803fc18`) bit 0 lock.
- Not worth doing before a benchmark shows AXI-bound work. Side effects listed in the table; also changes the console baud and SP805 period.
- Ask first: DDR AXI port clock interplay (see [ddr.md](ddr.md)).

## Ranked list (also posted on #17)

| # | Experiment | Gain | Risk |
|---|---|---|---|
| 1 | E0 read-only dump | confirms every I above | none |
| 2 | E3 BSPI 50 -> 62.5 MHz from uboot-2 | boot read +25% | low (crc compare) |
| 3 | E1 CPU 800 MHz via fid 7 | 2x CPU | low-medium |
| 4 | #20 quad read + DT `clocks` | 4-5x flash read | medium (QE, wiring) |
| 5 | E4 I2C 400 kHz (channels 4-6) | poll rate | low-medium |
| 6 | E2 CPU 1000 MHz relock | 2.5x CPU | medium |
| 7 | MDC rate check | PHY access speed | low |
| 8 | E5 AXI/APB | memory/IO bandwidth | high |

## Open

- Real CCU values (E0).
- BCM53344 rated CPU clock: no datasheet in the repo.
- Core voltage and heat headroom: unknown; measure temperature during E1.
- MDC 5 vs 2.5 MHz conflict (see table).
- Whether the BSPI clock select resets with SP805 reset (decides E3 recovery path).

## Online sources (2026-11-14)

- Public Broadcom product briefs (53346-PB102, BCM5344x page) state no clock, DDR or QSPI figure: only "Arm A9" / "high-performance Arm A9 CPU". Broadcom publishes no BCM53344/46 datasheet (NDA).
- The BCM56150 data sheet leaked on a community mirror (marked BROADCOM CONFIDENTIAL; stored `/vendors/broadcom/bcm56150-datasheet-whycan/`, manifest in `/vendors/broadcom/README.md`). BCM56150 is the HR2 die; BCM5334x are cut-down SKUs (I: same die, per `[DL]` SKU table).
- `[DS]` = `56150-DS06-R` (2014-12-11), line refs are `pdftotext -layout` lines.

| Source | What it says | Tag |
|---|---|---|
| `[DS]` p1, p21 | "1 GHz ARM Cortex-A9 single-core"; "Maximum CPU speed 1 GHz"; 32 KB I$/D$ 4-way, 128 KB L2, 128-entry TLB; 40 nm | C (BCM56150 SKU only) |
| `[DS]` p21 | DDR: 16-bit DDR2-800 MT/s (400 MHz clk, 1.8 V) or DDR3-1333 MT/s (667 MHz clk, 1.5 V); up to 2 GB, 8 Gb parts. Our 667 MHz = DDR3 max (see ddr.md) | C |
| `[DS]` p21 | PCIe 2.0 (Gen2) x1; 100 MHz HCSL refclk (in and out), +-50 ppm | C |
| `[DS]` Table 49/50, 3565 | QSPI BSPI max 62.5 MHz (reset default 25 MHz via `CRU_CONTROL.QSPI_CLK_SEL`, selectable 25 / 31.25 / 50 / 62.5); XIP yes; 4-byte addressing yes; MSPI max 15.625 MHz. Confirms `[DL]` 62.5 option as in-spec. Flash-side limit still MX25L25635E (50 MHz READ) | C |
| `[DS]` 7534-7800 | Refclks: XTAL 25 MHz; XG PLL 25 MHz; WarpCore LC PLL1 25 or 156.25 MHz; QSGMII LC PLL0 25 MHz; EXT_QS2 125 MHz; TS/BroadSync 12.8/20/25/32 MHz. LED_CLK 5 MHz | C |
| `[DS]` | No switch-core clock figure, no per-SKU table, no devID list, no AXI/APB figure (grep MHz/GHz: 120 lines read). Core 1.0 V +-5 %. | C (absence in this document) |
| t1600g-28ts_gpl `hurricane2.h` + `bcm95615x.c` ([hodgesds](https://github.com/hodgesds/t1600g-28ts_gpl), `ldk/3.4.5_RC3/bootloader/uboot-2012.10/`) | `IPROC_ARM_CLK 1000000000`, AXI 400 MHz, APB 100 MHz for default (b15x) SKU. devIDs 0x8342/43/44/46/47/0x8393/0x8394 -> `iproc_config_armpll(400)` + `iproc_config_genpll(4)` (AXI 200). Same 400 policy as `[DL]` | C |
| [clayface/U-boot-MX64](https://github.com/clayface/U-boot-MX64-20190430_MX64), [halmartin/u-boot-ms225](https://github.com/halmartin/u-boot-ms225), same file | Identical SKU split (ms225 also lists 0xb151 in the DRAM-size test). Same lineage as `[DL]`: not independent confirmation | C (code), I (as evidence) |
| T1600G / MX64 / MS225 comment | "Wolfhound/Deerhound" = 0x834x / 0x839x parts forced to 400; "default SKU = b15x, ARM 1 GHz". 0xb151 = DeerHound SVK (BCM56151) | C |

Conclusions

- SKU policy (devIDs 0x8342-47/0x8393/94 -> 400 MHz) is confirmed in 4 U-Boot trees; all share one Broadcom code base, so it proves the SDK policy, not a silicon limit.
- No source states a fuse or silicon limit for BCM5334x. Only datasheet statement is BCM56150 = 1 GHz max (C). BCM5334x rating: I, 1 GHz die, 400 MHz by software policy.
- No third-party boot log or review found stating a BCM5334x CPU clock other than 400 (ours). Search by exact phrase returned nothing; relaxed queries only returned the briefs, the datasheet mirror and our own issues. OpenWrt forum post (BCM53346 POEPLUS) did not load.
- Open: eCosCentric BCM56150 ref BSP (`doc.ecoscentric.com/ref/arm-bcm56150-ref-setup.html`) exists; its HAL source may hold clock settings (not read).
