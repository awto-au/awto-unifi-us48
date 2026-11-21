# BCM59111 PSE firmware: interrupt map, timer structure, real-time scale (cluster 5)

Offline only (no board). Images: v21 `hwdb/firmware/bcm59111-v21.bin` (primary), Meraki v18 `bcm59111-meraki.bin` (cross-check). CPU base 0x1A00.
Tags: **C** = proven by code bytes (address cited). **I** = inferred. **U** = unknown.
Tools: `scripts/poe_fw_dis.py` (listings `tmp/poe-fw/<image>.asm`), `scripts/poe_fw_isr_cycles.py [--meraki] [--irq N]` (ISR cost + interrupt-driven sim run, log `tmp/logs/poe_fw_isr_cycles.log`).
Background: [../poe-fw.md](../poe-fw.md) sections 3, 8, 12, 17; [../poe-fw-rewrite.md](../poe-fw-rewrite.md) "Phase 3 status".

## 1. Interrupt-related SFR writes in the whole image (C)

Complete list (v21; Meraki has the identical set at shifted addresses, section 7):

| SFR / bit | Where (v21) | Effect |
|---|---|---|
| `ORL TMOD,#0x10` | 0x5083 | timer 1 mode 1 (16-bit). Low nibble (timer 0) never written |
| `TL1=0xF4, TH1=0xE5`, `SETB TCON.6` (TR1) | 0x5086..0x508E; again in the ISR 0x4FD2..0x4FDD | reload 0xE5F4 = 6668 counts |
| `SETB IE.3` (ET1) | 0x508C | timer 1 interrupt on |
| `SETB IE.2` (EX1) | 0x50BF, after `SFR 0x93 = (P2 & 7) \| 0x28` (0x50B7) | external interrupt 1 on |
| `SETB EA` / `CLR EA` | 80 sites, critical sections (e.g. 0x1ABF, 0x1FCC, 0x1FE0, 0x2064, 0x218D) | no explicit EA = 1 in the start-up path (0x3CE3..0x3D17); the first `SETB EA` of a CLR/SETB pair also sets it (I) |
| `SETB IP.5, .4, .3, .2` | 0x3A24..0x3A2A (init_hw_channels tail) | priority bits set at init |
| `SETB IP.1` | 0x41D4 (tail of meas_init 0x415C) | priority bit set at init |
| `SETB T2CON.6` (EXF2, bit 0xCE) | 0x3A22 | once, never cleared by the image |
| `SETB T2CON.5` (RCLK, bit 0xCD) | 0x41D6 (tail of meas_init) | once |
| `CLR IP.n` at hook entry, `SETB IP.n` at exit | slot 3: 0x4EBF / 0x4ED0 (IP.1); slot 4: 0x4255 / 0x42CA (IP.2); slot 5: 0x40DD / 0x4159 (IP.3); slot 6: 0x42CD / 0x4342 (IP.4); slot 7: 0x4345 / 0x43BA (IP.5) | slots 1, 2, 8 never touch IP |

Never written by the image (C): IE bits 0, 1, 4, 5, 6 (EX0, ET0, ES0, ET2, ES1); TCON.4 (TR0), TH0/TL0; T2CON TR2/TF2/C-T/CP-RL; TH2/TL2/RCAP2; EIE/EIP/EICON/EXIF; CKCON (`CLR 0x8E` is the bit TCON.6, not CKCON). Timer 0, timer 2 and every interrupt enable except ET1 / EX1 / EA are therefore configured by the ROM or by reset values (C: absence; I: ROM does it).

## 2. Interrupt map

DW8051 databook v3.2a (`/vendors/realtek/analysis/rl6449-dw8051/DW8051-databook.txt` tables 3-21, 3-22): INT0 0x03 IE.0 IP.0; T0 0x0B IE.1 IP.1; INT1 0x13 IE.2 IP.2; T1 0x1B IE.3 IP.3; S0 0x23 IE.4 IP.4; T2 0x2B IE.5 IP.5 (TF2 and EXF2 both request); int2..5 and WDT via EIE.

| Slot | v21 / v18 hook | Bank | What the hook does (C) | Hardware source | Period |
|---|---|---|---|---|---|
| 0 | 0x3CE3 / 0x3D1D | 0 | start-up, then main loop | ROM calls it | once |
| 1 | 0x3F10 / 0x3F4A | 1 (PSW 0x08) | I2C slave ISR, SFR DD / F5 / F6 / F7 | I2C core (I) | per bus event |
| 2 | 0x4879 / 0x4921 | 3 (0x18) | reads XRAM 0x8017, sets all four service flags, `SFR 0x93 = 0x3F` | **U**; EX1 (enabled by the image beside `SFR 0x93`) is the only external source the image turns on: candidate (I) | event |
| 3 | 0x4EBA / 0x4EFD | 2 (0x10) | `CLR IP.1`; calls 0x1B03, 0x37D2, 0x34D0, 0x29EE, 0x316B (v18: 0x1B03, 0x380C, 0x350A, 0x29E5, 0x31A4); `SETB IP.1` | **U** (section 3) | **U** (section 4) |
| 4..7 | 0x4255 0x40DD 0x42CD 0x4345 / 0x422E 0x2F0B 0x42A6 0x431E | none (A, DPTR only) | `CLR IP.2..5`, set service flag 0x23.(c+1), read / write-1-clear SFR BC..BF (0x95 on ch0), update channel block 0x8078 + 0x12c, `SETB IP.n` | **U** (per-channel PSE event lines, I) | event |
| 8 | 0x4FCD / 0x5010 | 3 (0x18) | `CLR TR1; TL1=0xF4; TH1=0xE5; LCALL tick_timers (0x3C1E / 0x3C58); SETB TR1` | timer 1 overflow (the hook reloads and restarts timer 1) | 6668 counts |
| 9..14 | RET stubs | | | unused | |

- Hook 8 runs on the board with the rewrite: its state machine ran detect five times (counter 0x64 = 5, poe-fw-rewrite.md "Board result"), driven only by timer-1 ticks (I).
- The hook-table order is not the 8052 vector order (timer 1 is slot 8, not 4): the slot to vector map lives in the ROM (U). Provable from the image alone: hook addresses, the IE / IP / TCON / TMOD writes above, register banks.
- Banks (C): slots 2 and 8 share bank 3, slot 3 owns bank 2, slot 1 owns bank 1, main loop bank 0. Sharing bank 3 means slots 2 and 8 cannot preempt each other: same priority level (I).
- The IP clear / set bracket (C) cannot change the priority of the interrupt in service on an 8051 (priority is latched at vectoring), so its meaning is **U**: (a) the ROM wrapper uses the bits as software masks, (b) a nested-interrupt scheme through a ROM trick. Slot 5 toggles IP.3 (= PT1) while timer 1 is slot 8: the IP bit numbers are not the 8052 per-vector priorities of the same names (I).
- Armed state (C): after init IP.1..IP.5 = 1, T2CON.6 / .5 = 1. Stock arms IP.1 and RCLK as the last act of meas_init (0x41D4 / 0x41D6), after the calibration reads. **The rewrite's `hal_meas_calib` does not set IP.1 or T2CON.5** (`poe-fw/src/hal_analog.c`: only `T2CON_6` and `IP_5..IP_2` in `hal_sfr_init`) and its hook 3 is empty: whether slot 3 fires at all on the board is untested (U).

## 3. What raises slot 3 (U, candidates)

- H1 timer 0 overflow: slot 3's bit is IP.1 = PT0 in the 8052 layout; TMOD low nibble / TR0 / ET0 are never touched by the image, so a ROM-owned timer 0 is possible. Against: slot 5 shows the IP numbers do not follow the vector names.
- H2 timer 2 / EXF2: the image sets EXF2 (a software-set EXF2 requests the T2 interrupt on an 8052) and RCLK, never clears EXF2; a ROM T2 wrapper would have to clear it. Against: nothing links EXF2 to slot 3 specifically (the SETBs sit in init_hw_channels and in meas_init).
- H3 ROM-synthesised from another source (a ROM counter dividing timer 1 or 0).
- Holds for all: the hook is a plain function (no RETI) and costs about 6000 machine cycles (section 4), so the source cannot be faster than that.

## 4. Period of slot 3 (U) and bounds (I)

Cost of one call, simulator (`ucsim_51 -t RL6449`, stock image, analog SFRs plain registers, 2026-11-21, `poe_fw_isr_cycles.py`; MOVX at base 2 cycles, CKCON stretch not modelled: real silicon +~5 %, I):

| Item | Cycles (4 clk each) | Insns |
|---|---|---|
| hook 3, idle (no channel active) | 6029 | 3005 |
| hook 3 with DC scheduler / sequencer states set | 6066..6723 | |
| 0x1B03 trim / 0x34D0 convert / 0x37D2 temp / 0x29EE / 0x316B (idle) | 3229 / 2413 / 309 / 26 / 27 | |
| hook 8 tick, no timers armed | 445 | 168 |

- Lower bound: period >= 24116 clk / f_cpu; 3.0 ms at 8 MHz with zero main-loop time.
- Sim, interrupt-driven stock run, stand-in T0 period 36000 clk: ~67 % of time in hook 3 (`--irq 3000`).
- Class mux (C, section 6) against IEEE 802.3at (I; AF = 1 class probe and AF = 2 mark are inferred from the 2-event sequence, which only runs when the first sample decodes to class 4, 0x2AC3 `CJNE A,#4`): mark events last 4 calls (call 5 -> 9, 14 -> 18); 802.3at mark event 6..12 ms (recalled, not re-verified) => **period 1.5..3.0 ms**; class events 5 calls (>= 6 ms => >= 1.2 ms).
- Period 1.5..3.0 ms with the cost above gives f_cpu >= 8 MHz at zero headroom, >= 16..32 MHz for >= 50 % headroom.

## 5. Clock derivation

Facts (C): databook: 4 clk per machine cycle; timers count clk/12 unless CKCON T1M (never written) is 1. The sim does the same (timer 1 mode 1 at 12 clk per count, checked with an ISR-reload loop 2026-11-21). Timer-1 tick = 6668 counts = **80016 clk = 20004 machine cycles**; tick = 80016 / f_cpu (core-independent: a 12-clk core would change only CPU-side times, 3x).

| f_cpu | tick T | one hook-3 call (24116 clk) |
|---|---|---|
| 4 MHz | 20.0 ms | 6.0 ms |
| 8 MHz | 10.0 ms | 3.0 ms |
| 12 MHz | 6.67 ms | 2.0 ms |
| 16 MHz | 5.0 ms | 1.5 ms |
| 24 MHz | 3.33 ms | 1.0 ms |
| 32 MHz | 2.5 ms | 0.75 ms |

Previous estimate (poe-fw.md section 17): T = 10 ms, 8 MHz, from two 802.3 anchors (detect start 50 T = Tdet 500 ms, cleanup 30 T = Tmpdo 300 ms). Both mappings are inferred (the 30 T timer is not known to be the Tmpdo timer).

Result: **f_cpu is not pinned.** Two inferences disagree at the margin:
- timer-1 anchors: T = 10 ms, f = 8 MHz (I, low);
- hook-3 cost + class mux mapping: f >= 16 MHz for sane headroom (I, low). At 8 MHz the period would be >= ~4 ms (30 % headroom) and a mark event 16 ms (> 12 ms).
Plausible range 8..32 MHz (T = 10..2.5 ms). Deciding experiments: section 9 items 2 and 5.
Third bound: I2C read-byte path 139 cycles = 556 clk (poe-fw.md section 12) plus ROM wrapper: 69.5 us at 8 MHz, 17.4 us at 32 MHz versus 22.5 us per byte at 400 kHz. Without SCL stretching f >= 24.7 MHz; with stretching no bound (U).

## 6. Timed constants

Units: **T** = timer-1 tick (hook 8), **P** = one hook-3 call. ms: T at 10 / 5 / 3.33 ms (f = 8 / 16 / 24 MHz); P at 1.5 / 2 / 3 ms (section 4 range, I). Values C, meanings I.

| Constant | Address | Unit | Count | ms (T 10 / 5 / 3.33) |
|---|---|---|---|---|
| detect start timer (`R5 = 0x32`) | 0x4639 | T | 50 | 500 / 250 / 167 |
| idle re-arm (`0x3C`) | 0x4EFA | T | 60 | 600 / 300 / 200 |
| class gate (`0x14`) | 0x4234 | T | 20 | 200 / 100 / 67 |
| power-up hold (`0x14`) | 0x4FBE | T | 20 | 200 / 100 / 67 |
| disconnect cleanup (`0x1E`) | 0x4FAA | T | 30 | 300 / 150 / 100 |
| fault cool-down (`0x4B`) | 0x4D76 | T | 75 | 750 / 375 / 250 |
| class hardware start (`0x64`) | 0x49F1 | T | 100 | 1000 / 500 / 333 |
| retry waits (`0xC8`, `0xFA`) | 0x1F87, 0x4BB4, 0x3BFE | T | 200, 250 | 2000, 2500 / 1000, 1250 / 667, 833 |
| arm delays (R3 via 0x49FF): shutdown 2, detect arm 3, result 4, ok-wait 5, class gate 8, power request 10, invalid 15, retry 16 | poe-fw.md section 11 (not re-verified) | T | 2..16 | 20..160 / 10..80 / 7..53 |
| sequencer end hand-off timer (`[0x8081 + 18c] = 0x14`) | 0x332B | T | 20 | 200 / 100 / 67 |

| Constant | Address | Unit | Count | ms (P 1.5 / 2 / 3) |
|---|---|---|---|---|
| class mux sample 1 | 0x2A84 (`XRL #5`) | P | call 5 | 7.5 / 10 / 15 |
| class mux AF back to 1 (2nd event) | 0x2AEA (`XRL #9`) | P | call 9 | 13.5 / 18 / 27 |
| class mux sample 2 | 0x2B01 (`XRL #0x0E`) | P | call 14 | 21 / 28 / 42 |
| class mux state 5 | 0x2B55 (`XRL #0x11`); state 5 acts at count >= 0x0A (0x2B69) | P | call 17 | 25.5 / 34 / 51 |
| class event / mark event length (I) | | P | 5 / 4 | 7.5 / 6, 10 / 8, 15 / 12 |
| trim 0x1B03 (IRAM 0x25 + c) settle | 0x1EC0 (`SETB C; SUBB #0x14`: counts 1..20) | P | 20 | 30 / 40 / 60 |
| trim accumulate then `>>3` | 0x1ED9 (`SUBB #0x1B`: reset at count 0x1C) | P | counts 21..28 = 8 samples; cycle 28 | 42 / 56 / 84 |
| sequencer state 4 settle (counter A) | 0x3210 (`SETB C; SUBB #0x1E`: done at count 31) | P | 31 | 46.5 / 62 / 93 |
| sequencer state 3 | 0x31EB..0x31FE | P | 1 (state := 4 at once; sample >= 0x2B -> state 0x0D) | |
| 0x2A, 0x28, 0x48, 0x35, 0x41, 0x0C, 0x0A, 0x02 in 0x316B | | | ADC sample thresholds, not times | |

Corrections to poe-fw.md / poe-fw-rewrite.md (C):
- "0x2A" is an ADC threshold (sample >= 0x2B -> probe lost, state 0x0D); no state-3 wait loop (0x31EB sets state 4 first).
- 0x1B03: settle 20 calls (counts 1..20), accumulate counts 21..28 = 8 samples (matches `>>3`), not 20 / 7.
- AF writes in 0x29EE are `ch*4 | {1, 2}` and `ch*4`; 0x2205 / 0x2180 store the state number (1, 3, 5) and return `ch*4`.
- "Timer word 0x5F:0x5E vs 0x02AA" (0x3D67) is a temperature-derived word (written 0x3919, read 0x3624), not a time base.
- Channel arbitration: 0x3DA6 (once per main-loop pass) picks the channel in 0x8076 round-robin when no channel owns the probe (0x80C0 bits 7:4 = 0); 0x316B services only that channel, so probes never overlap.

802.3 windows used (I, recalled): Tdet <= 500 ms, Tpon <= 400 ms, Tmpdo 300..400 ms, class event 6..75 ms, mark event 6..12 ms. At T = 10 ms: detect 500 ms (edge), power-up 200 ms, cleanup 300 ms (edge); at T = 5 ms cleanup 150 ms (below Tmpdo).

## 7. v21 versus v18 (Meraki)

| Item | v21 | v18 | Same? |
|---|---|---|---|
| hook table roles | section 2 | hooks 0x3D1D 0x3F4A 0x4921 0x4EFD 0x422E 0x2F0B 0x42A6 0x431E 0x5010, stubs 0x526E..0x5273 | roles identical; stub addresses differ |
| TMOD, TL1 / TH1 | 0x5083, 0xF4 / 0xE5, ISR 0x4FD2 | 0x50D5, 0xF4 / 0xE5, ISR 0x5017 | identical |
| IE writes | IE.3 0x508C, IE.2 0x50BF | IE.3 0x50DE, IE.2 0x5111 | identical |
| IP / T2CON arm, hook brackets | section 1 | 0x3A5C..0x3A64, 0x4229 / 0x422B, slot 3 0x4F02 / 0x4F13, 0x422E / 0x42A3, 0x2F0B / 0x2F87, 0x42A6 / 0x431B, 0x431E / 0x4393 | identical |
| slot 3 call order | 1B03 37D2 34D0 29EE 316B | 1B03 380C 350A 29E5 31A4 | identical order |
| class mux compares | 5, 9, 0x0E, 0x11, >= 0x0A | 0x2A87, 0x2AED, 0x2B04, 0x2B58, 0x2B6D: same | identical |
| trim counters | 0x14, 0x1B | 0x1EC0, 0x1ED9 | identical |
| sequencer thresholds | 0x2A 0x1E 0x28 0x48 0x35 0x41 0x0C | same, at 0x3230.. | identical |
| timer R5 values | 0x14 0x1E 0x32 0x3C 0x4B 0x64 0xC8 0xFA | all present | identical set |
| hook 3 idle, hook 8 (sim cycles) | 6029, 445 | 6029, 445 | identical |
| per-function cycles | 3229 / 309 / 2413 / 26 / 27 | same | identical |

Every interrupt, timer and counter constant is identical: the unit and scale problem belongs to the platform (ROM, silicon, clock), not the build. The v18 / v21 pair gives no independent cross-check of the period or clock (nothing differs).

## 8. Rewrite hook change (POE_FW_SEQ, description only; no code changed)

No startup.s change needed: `_hook_periodic` already has the stock bracket (bank 2, `CLR IP.1` ... `SETB IP.1`). Under a new define `POE_FW_SEQ` (default off, images unchanged):
- `hal_meas_calib` end: `IP_1 = 1; T2CON_5 = 1` (new `__sbit` at 0xB9 / 0xCD), as stock 0x41D4 / 0x41D6.
- `hal_periodic` (hook 3 body): ported chain in stock order (trim, temperature, convert, mux scheduler, sequencer); P counters live there.
- `chan_tick` stops setting `meas_due` (no second run from the main loop).
- Arbitration 0x3DA6 stays in the main loop.
Probe build (bench only, separate define): hook 3 and hook 8 each increment a 16-bit counter in unused plain-XRAM host registers (check hostregs.c; 0x22 / 0x23 / 0x45 are not used by the code), and the `IP_1` / `T2CON_5` arm is a build switch.

## 9. Open questions (U) as experiments

Board items need the awto image on a PoE chip; MCU-side registers only, host-visible over I2C.
1. Does slot 3 fire? Probe build with and without `IP_1 = 1; T2CON_5 = 1`; read the hook-3 counter twice, 5 s apart. Zero in both: the ROM does not start the source, the image must (items 3, 4).
2. Period and tick: counters of hooks 3 and 8, delta over a host-timed interval of >= 10 s, sampled every 1 s (16-bit counters wrap in 2.5 s at P = 2 ms). T = dt / d8, so f_cpu = 80016 / T; P = dt / d3. Settles sections 4 and 5.
3. Source of slot 3 if item 1 is zero: variant A starts timer 0 (`TMOD |= 0x01`, `ET0 = 1`, `TR0 = 1`), variant B sets EXF2 / TF2 by software again after init; see which moves the counter.
4. Back-to-back refire (level-like source): counter rate close to 1 / (cost + wrapper) = <= 3 ms at 8 MHz.
5. f_cpu from the host, stock firmware, read only: time a long auto-increment block read (`0x00..0x7F`) at 400 kHz and 100 kHz, many repeats. Per-byte time = max(22.5 us bus, ISR time) when SCL is stretched: 8 MHz gives 69.5 us plus wrapper (about 9 ms per 128 B); 32 MHz gives the bus-bound 2.9 ms.
6. Sources of slots 2 and 4..7: probe counters in each hook, PD plugged / unplugged.
7. Mark / class interval on the wire (scope on the port): the AF = 2 interval of 4 calls gives P directly; not MCU-only.
8. Sim (no board): interrupt-driven stock run works (`poe_fw_isr_cycles.py --irq N`: ROM vector stubs at 0x0B / 0x1B, T0 stand-in). For the full detect / class chain over time it still needs (a) a timer source at P, (b) time-varying analog stubs for D1..D4, AE, AC / AD, B5..B7, DD (ucsim has no PSE peripheral: inject with SFR event breakpoints), (c) `-X` set to the candidate f_cpu. Limits: MOVX stretch / wait states not modelled (awto-sdcc docs/dual-dptr.md); default xtal 11.0592 MHz (clk counts do not depend on it).

## 10. Probe images (experiments 1..4, section 9)

Built by `scripts/poe_fw_build.py` from the `-nopower` base with `-DPOE_FW_PROBE` (normal images byte-identical with the defines off, checked against a rebuild of the base). Sim: `scripts/poe_fw_probe_sim.py` (48 checks: counters, carry, arming, probeC re-arm, host regs equal to `-nopower` outside the probe regs). Reader: `scripts/poe_fw_probe_read.py` (`--selftest` covers wrap, torn pairs, aliasing).

| Image | Arming IP.1 + T2CON.5 (stock 0x41D4 / 0x41D6) | Timer-2 re-arm | Answers |
|---|---|---|---|
| `bcm59111-awto-probeA.bin` | yes | no | does slot 3 fire with the stock arming |
| `bcm59111-awto-probeB.bin` | no | no | does slot 3 fire without it |
| `bcm59111-awto-probeC.bin` | yes | EXF2 + TF2 set every 50 hook-8 ticks | does a software T2 request raise slot 3 |

- Hook 3 body is the counter only (no measurement work, so the period is not distorted by its cost). TEMP / CM / VM still run from the main loop as in `-nopower`. IRAM unchanged (counters live in host regs).
- Registers (lo, hi; plain XRAM, no handler, not clear-on-read; rows in `hwdb/firmware/bcm59111-regs.tsv`, source `awto-probe`): hook 3 `0x22` / `0x23`, hook 8 `0x2D` / `0x40`, hook 2 `0x45` / `0x4F`, slot 4 `0x54` / `0x59`, slot 5 `0x5A` / `0x5B`, slot 6 `0x5C` / `0x5D`, slot 7 `0x1C` / `0x1D`. Zeroed at start-up. Slots 4..7 count in trampolines (`poe-fw/src/probe/probe.s`, hook table via `poe-fw/src/probe/chanhooks.inc`); normal builds include `poe-fw/src/normal/chanhooks.inc`.
- Status `0x16`: bits 7:4 variant (1 A, 2 B, 3 C), bit 0 hook 3 body entered since the last re-arm (cleared by the probeC re-arm, set by every hook-3 call), bit 1 arming done at build time, bit 2 probeC re-arm done at least once.
- Torn reads: counters are not updated atomically. The host reads hi, lo, hi; when the two hi differ, lo < 0x80 means the carry came first (use the second hi). The reader does this.
- Risk (probeC, and A / B if the ROM wrapper never clears the request): a request that stays pending re-enters its hook back to back and starves the main loop. The I2C hook has its own bank and the watchdog poke path is the main loop's: if the chip stops answering sensibly, the driver's host watchdog expires, which is the intended failure signal. Reload clears it.
- Interpretation: rate of hook 8 gives T and f_cpu = 80016 / T. Hook 3 counter rate gives P (period). Zero hook-3 count in A and B: the ROM does not start the source. Count in A but not in B: the arming bits gate it. Count in C only: the software request raises it (rate then is every 50 ticks, `hook 3 per tick` = 0.02, not a period). Counter equal to hook 3 back-to-back rate = about 24116 clk / f_cpu + wrapper: a stuck request.

Board steps (chip PoE-07 per the DT patch 0026: mux channel 5 (PoE card 1), address 0x28; the kernel bus number must be read from `/sys/bus/i2c/devices`, it is not recorded in docs):
1. Stage the image: `tmp/poe-fw-override` line `PoE-07=<path>/hwdb/firmware/bcm59111-awto-probeA.bin` (untracked, see poe-fw-rewrite.md "Bench loading"); build the mainline image, boot with all ports shut, no PD change, driver arms and pokes the host watchdog as for `-nopower`.
2. Confirm: dmesg `firmware rev 0xa1 ... firmware loaded` for PoE-07; the chip is `<bus>-0028` (find the bus with `ls /sys/bus/i2c/devices | grep -- -0028`).
3. `python3 scripts/poe_fw_probe_read.py --chip <bus>-0028 --seconds 12` (read only). Log `tmp/logs/poe_fw_probe.log`.
4. Repeat with probeB, then probeC (each is a fresh load of the chip: reset / cold chip as for any image change). Camera on ch1 may stay; ports stay shut.
5. Check afterwards: SAT / TEMP regs as for `-nopower`, host watchdog still armed (reg 0x21 = 0x82).
