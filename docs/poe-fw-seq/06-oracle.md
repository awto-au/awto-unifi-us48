# 06 Stock image as oracle (v21 and Meraki v18)

Offline experiment: the stock images run with a modelled analog front end; what they publish and write is ground truth for the rewrite ([poe-fw-rewrite.md](../poe-fw-rewrite.md)). Tool: `scripts/poe_fw_oracle.py` (log `tmp/logs/poe_fw_oracle.log`), core `scripts/mcs51.py`. Tags: C = observed on the run, both images unless stated; I = inferred model; U = unknown.

## 1. Setup

- Engine: pure-Python MCS-51 core with SFR / XRAM hooks (C). ucsim_51 -t RL6449 is not usable for the model: one console round trip is ~40 ms, a tick needs ~500 SFR accesses (AB / AC / AD / B5 / B7 / E9..EF), and the math unit E9..EF is a plain register there.
- Core check (`--xcheck`): stock boot + 20 ticks of timer-1 ISR + one main-loop pass, plain SFRs, ucsim harness of `poe_fw_sim.py` vs the core: IRAM 0x10..0x7F and XRAM 0x8000..0x8140 identical, v21 and v18 (C, ran).
- Harness fixes taken from `poe_fw_sim.py`: ROM 0x03..0xFF = RETI, ROM strap pointer 0x1900 -> RET, DEV_ID 0x8043 = 0xC0, boot = hook slot 0 to the main-loop poll, interrupts off. Ports P0..P3 = 0xFF (ucsim default; real strap unknown, U).
- Per-image entry points (hook slots read from the image): v21 poll 0x230D, loop 0x3D1A, host write table 0x26EE, detect classifier 0x3651; v18 poll 0x2304, loop 0x3D54, table 0x26E5, classifier 0x368B.
- Interrupts are injected at the main-loop top: per 10 ms tick timer-1 ISR (hook 8) once, periodic ISR (hook 3) `--pt` times (default 1), channel ISR (hooks 4..7) when a modelled hardware event is due, then 2 main-loop passes. ACC / B / DPTR / PSW saved around each injection.
- Host state like the driver after probe: 0x01 = 0x66, 0x13 = 0x0F, 0x14 = 0xFF, 0x44 = 0x0F, per channel `HP_Pn_MOD` (0x46+5c) = 0x01, `OV_CUT` (0x47+5c) = 0xE2, `CL_FBC` (0x48+5c) = 0xC0, OP_MOD auto for the channel under test. Camera channel = 1 (regs 0x4C / 0x4E, as the board capture); ch0 also run.
- Writes to host registers go through the stock write handler table (direct calls, not the I2C ISR).

## 2. Analog input model (reusable by the rewrite's simulator stub)

| Item | Model | Tag |
|---|---|---|
| `AB` (0xAB) | index latch. Reads of `AC` (lo) / `AD` (hi) return word[AB]; writes to `AC` / `AD` store into word[AB] (trim write-back) | C |
| word[0x2C] | temperature sensor. TEMP reg 0x2C = (word + IRAM 0x4E:4F offset) >> 1; offset 0x10 after the boot calibration with the stubbed ADC: word 0x130 -> TEMP 0xA0 (board 0xA0..0xA7) | C/I |
| word[0x30] | supply voltage. 0x2E/0x2F = 11 * clamp(raw), raw from word[0x30]: 0x0A87 -> 0x2E/2F ~ 0x22FA (52 V, board value); word 0x0800 -> 0x1A15; clamp 0x3FF | C |
| word[0x30] low | supply raw (IRAM 0x5E:5F) < 0x2AA (word below ~0x08D0, ~44 V) and SUP_EVT.4 clear: main loop (v21 0x3D66) sets SUP_EVT |= 0x80 and powers every powered channel down (0x3D92 -> 0x4CBA). With the default word 0x0130 for AB 0x30 stock sheds power for the first 3 passes at boot | C |
| word[c\|8], c = 0..3 (AB = 8..11) | before power: class probe sample (read at periodic-ISR tick 5 and 14 of the class window, AF = 4c+1 / 4c+2, `0x29EE`); after power: CM. Class code from the word (section 4) | C |
| word[c\|4] (AB = 4..7) | VM after power (VM = word * 11); before power: detect sample (only the legacy probe 0x316B reads it) | I |
| word[0x24..0x2B] | offset-trim accumulators (0x1B03, `B5` select 2c+1 / 2c+9, `B7` / `B6` ADC bytes): constants, read-back only | I |
| `AF` | plain latch, bit 7 reads 0 (bit 7 set = invalid sample, word forced to 0xFFFF) | C |
| `D1..D4` | detect result: `D(c) >> 4` = code (section 3), low nibble 0. Written 1 = start, 2 = timeout stop (written only when the 50-tick window expires) | C |
| `D9` / `DA:DB` | D9 = 4c + k selects sample word k (0..3) of channel c, data = (DA << 8) \| DB. Read by the classifier only when `D >> 4 == 0xF` | C |
| `BC..BF` | per-channel event latch, write-1-clear; reads feed channel ISR (hook 4+c). Events table in section 6 | C |
| `F3` | per-channel power-good status, read once on the BC bit 4 event (0x5074) and polled by the monitor each pass | C |
| `F1` | power enable, level semantics: low nibble bit c = channel c on (firmware clears with ANL to cut), high nibble = pair bit. Model: bit set -> BC bit 4 event after `pg_latency` ticks, F3 bit set | I |
| `F2` | plain register (0x02 / 0x20.. per channel at HP power-up): no power effect in the model | I |
| `E9..EF` | math unit: write EE = 16x16 -> 32 multiply into E9..EC; write EF = 0x20 \| n = 32-bit logical shift right by n | C (as `poe-fw.md` §14; reproduces TEMP / 0x2E / 0x2F) |

## 3. (a) Detect: result code -> SAT_Pn (open port = 0x06)

- `D(c) >> 4` is the detect nibble, published to `SAT_Pn` low nibble by `st_detect_result`. 4 = good, 6 = open (C). No ADC word enters: open port with class / detect words 0x000..0xFFF all give SAT 0x06, no class start, no power, re-detect every 63 ticks (D start at ticks 2, 65, 128, ..), invalid counter 0x60+4c b0 incremented per attempt (C, both images).
- Pass-through: code 0..3, 5..8, A..E publish the same nibble; 9 -> 6; 4 -> class + power chain (section 4); 0xF -> sample-word classifier (below). Codes other than 4: no class sample, no `F1` write, no D5 / D6 write.
- Good detect restarts every 76 ticks when class code = 7 (above class 4: 0x74, no power).
- Classifier on `D >> 4 == 0xF` (v21 0x3651 / v18 0x368B), four words W0..W3 (D9 = 4c..4c+3), grid of 525 inputs, W3 = 0x100, 8 steps each for W0-W1 / W1-W2 / W2-W3 plus threshold probes, results identical on both builds:

| Condition (signed 16 bit) | Result nibble |
|---|---|
| W2 - W3 >= 0x5C, or W2 >= 0x355 | 6 (open) |
| else W1 - W3 >= 0x96, or W0 - W2 >= 0x96 | 5 |
| else if W0-W2, W1-W3 and W0-W3 all > 0x55 | 2 |
| else 3 if W0 - W3 < 3 * (W0 - W1) + 10, otherwise 2 | 3 / 2 |
| override to 4 (good): `HP_Pn_MOD & 4` != 0 and W0 > W1 > W2 > W3 and W0 - W3 >= 0x6E (replaces 2 / 3 / 5 / 6 whenever the ordering holds) | 4 |

- With the driver value `HP_Pn_MOD` = 0x01 the classifier never returns 4 (grid histogram {0:1, 1:1, 2:34, 3:27, 4:1, 5:199, 6:262}; the single 4 is the pass-through test row); with 0x05: {2:23, 3:21, 4:350, 5:67, 6:63}. So on a stock-configured chip a good PD must come from the hardware code `D >> 4 == 4`, not from the words (C inference from the grid; the thresholds are code facts, see cluster 4 for 0x3651).
- Smallest good example found, MOD 0x05: W = 190 170 150 100. Chain check (D = 0xF0 + these words, MOD 0x05): SAT_P1 0x44, power, OV_CUT 0x11 (C).

## 4. (b) Class code and limits after power-up (detect code 4, 120 ticks, camera channel 1)

Class word = word[AB 8+c] (any AF). Thresholds from `0x4900`. Limits written at the power-good event (section 5).

| Class word | SAT_P1 | A2 (limit SFR) | OV_CUT 0x4C | ILIM 0x4D | HP_SAT 0x4E | F2 | SAT_PWR | Note |
|---|---|---|---|---|---|---|---|---|
| 0x000..0x06E | 0x64 | F0 | 0x54 | 0x80 | 00 | 00 | 0x22 | class 0 |
| 0x06F..0x0F6 | 0x14 | 48 | 0x46 | 0x80 | 00 | 00 | 0x22 | class 1 |
| 0x0F7..0x188 | 0x24 | 84 | 0x4B | 0x80 | 00 | 00 | 0x22 | class 2 |
| 0x189..0x232 | 0x34 | F0 | 0x54 | 0x80 | 00 | 00 | 0x22 | class 3 |
| 0x233..0x321 | **0x44** | **CC** | **0x11** | 0xC0 (unchanged) | **01** | 02 | **0x22** | class 4 (HP path: 0x4C / 0x4E = 0x11 / 0x01 as the board capture) |
| >= 0x322 | 0x74 | F0 | 0xE2 | 0xC0 | 00 | 00 | 0 | code 7, no power, re-detect after 76 ticks |

- Class 0..3 overwrite OV_CUT / ILIM (0x47+5c / 0x48+5c) with the table values; class 4 writes OV_CUT = 0x11 and HP_SAT = 0x01 and leaves ILIM (C).
- Board comparison: stock camera port after power-up reads SAT_Pn 0x44, SAT_PWR 0x22 (ch1), reg 0x4C = 0x11, reg 0x4E = 0x01, unpowered 0xE2 / 0x00 (poe-fw-rewrite.md Phase 3). The oracle reproduces all four values with detect code 4 + class word 0x233..0x321 + power-good event (C: run; board value from the capture).
- The reg pair the capture called "ICUT" and "+2" is `HP_P(c+1)_OV_CUT` (0x47+5c) and `HP_P(c+1)_SAT` (0x49+5c): ch idx1 = 0x4C / 0x4E; idx3 = 0x56 / 0x58 (C, regs tsv).
- No D5 / D6 write and no 0x316B probe on the whole chain (C): the class result comes from the 0x29EE mux samples.

## 5. (b)/(c)/(d) Power-up sequence of a class-4 camera, ch1 (key SFR writes; ticks of 10 ms with `--pt 1`)

v21 (v18 differs only where noted):

| Tick | Write / event | Meaning |
|---|---|---|
| 1 | C9 = ~DIS_SEN_EN (0xF2 with ch1 set), B2 = 00 then 04, S95 = 0x0C, BD = 0xFF | OP_MOD write: port reset, gate, clear event latches |
| 2 | D2 = 01 | detect start (state `st_detect_start`) |
| 3 | event BD bit 6 (modelled), D9 = 4..7 | detect complete; classifier reads the 4 words (D >> 4 = 4: pass-through) |
| 3..22 | none | class window: the 0x29EE mux samples word[AB 9], AF = 5 (tick 5) and 6 (tick 14 of the window), ~20 ticks |
| 23 | F1 = 0x22 (F2 = 0x02 v21) | power on (`st_power_on`); SAT_P1 0x44, SAT_PWR = 0x02 (enable), PWR_EVT 0x02, HP_SAT 0x4E = 01 |
| 25 | event BD bit 4 (+F3 bit 1) modelled `pg_latency` = 2 after F1 | power good |
| 25 | C9 = 0xF0, **A2 = 0xCC**, B2 = 04 then 00, F2 = 02, host 0x4C = 0x11, SAT_PWR = 0x22 | limits applied, gate released, SAT_PWR good nibble |
| 27 | B2 = 0x80 | control bit 7 set (trigger of the auto-mode power state, I) |

- v18 identical sequence with the post-power-good block (C9 = F0, A = CC, B = 04/00, host 0x4C) 6 ticks later (tick 31 vs 25), and F2 written 0x00 at tick 23 instead of 0x02. Final state identical (C: run, `power_trace`).
- Power good appears only through the hardware event BC bit 4 with the F3 bit set (C, 0x2F8C -> 0x3053 -> 0x5074 reads `F3 & mask`). Variants (class-4 camera, 400 ticks):

| Variant | v21 end state | v18 end state |
|---|---|---|
| event +2, F3 set | SAT_PWR 0x22, A2 CC, OV_CUT 0x11 | same |
| event +10 / +60, F3 set | same as +2 | SAT_PWR 0x20, counter 0x66 = 0x10 (v18 flags a late event) |
| event +2, F3 clear (power-up failed) | SAT_PWR 0x02 (no good nibble), limits not written (A2 F0, OV_CUT E2), F1 stays 0x22 | same, F2 = 00 |
| no event | SAT_PWR 0x02, limits not written, F1 stays 0x22 (no timeout in 400 ticks) | same |

- Stock never times out a missing power-good event (C, 400 ticks): the channel stays enabled with SAT_PWR 0x02. A rewrite that needs a timeout must add its own (not stock behaviour).
- Monitor: every pass `0x2E38` reads `F3 & mask`; three consecutive passes with the bit clear -> `st_fault_shutdown` (counter 0x61+4c, SAT unchanged, `F1` cleared) (C). If F2 is modelled as a power-clear register the camera is shut at tick 33 (v21) / 39 (v18): the model keeps F2 plain.

## 6. (e) Disconnect / overload / analog reactions (steady powered class-4 port, then one event; 150 ticks)

Rows identical v21 vs v18 in 14 / 14 on ch1 and on ch0 (ch0 run: same table, power-up one tick earlier: F1 at tick 22, limits A1 at 24 / 30).

| Input | Result (ch1) |
|---|---|
| BC bit 0, 1 (+S95 bit 1), 4, 5, 6 | no change: state stays 0x44 / SAT_PWR 0x22 (these bits do not reach a shutdown in the powered state) |
| BC bit 2 | disconnect: counter 0x67 (= `0x60+4c+3`, "MPS absent") +1, host 0x06 = 0x20 (ch1), INT 0x00 = 0x1F; F1 cleared 61 ticks after the event (60-tick idle), the port is then re-detected and re-powered (SAT_PWR 0x22 again at 150 ticks) |
| BC bit 3 | fault: counter 0x66 (`0x60+4c+2`) = 1, host 0x06 = 0x02, F1 cleared 1 tick after the event, SAT_PWR 0, port stays off for the 150 ticks |
| BC bit 7 | fault: counter 0x66 = 0x10, host 0x08 = 0x20, F1 cleared 1 tick after, SAT_PWR 0, port off for the 150 ticks |
| CM word 0x000 or 0xFFF after power | no reaction: stock does not compare CM against a limit in software (C, 150 ticks) |
| supply word 0x0400 (low) | F1 cleared 1 tick later, SAT_PWR 0 (UVLO, section 2); SUP_EVT already 0x80 |
| temperature word 0x600 (hot) | same shutdown pattern (F1 cleared 1 tick later, SAT_PWR 0); cause (SUP gate via the 0x5E:5F word) I |

- Disconnect, overcurrent and short are hardware events (BC bits); the analog words never trigger them in the image (C). The rewrite's stub must raise BC.2 for a removed PD and BC.3 / BC.7 for faults.
- The BC bit -> meaning mapping is from the channel ISR 0x4255 (flags written) plus the effect seen: bit 6 = detect complete, bit 4 = power good, bit 2 = disconnect, bit 3 / 7 = fault classes (I for names, C for effects); bits 0, 1 (+S95), 5 have no effect in the powered steady state.

## 7. SFR accesses per tick (steady camera, v21 == v18, 60 ticks)

- Reads: AB 0x00..0x03 / 0x24..0x2C / 0x30 / 0x04..0x0B once or twice per tick each (offset trim, temperature, supply, VM / CM), AE 4 per tick (trim ready), D7 2 total, D(c+1) once per detect, DA / DB 4 each per classification.
- Writes per tick (counts over 60 ticks): AB 2040, AC 480, AD 480 (trim write-back), B5 960, EF 575, IP 124 (periodic ISR priority toggles), TCON 120, TL1 60, TH1 60, IE 84, F8 5 (INT line), F1 6, F2 8, C9 3, B2 8, BD 3, D2 1, D9 4, AF 4, S95 1, A2 1 (v18 identical counts except IE 76, EF 569).
- Boot writes (identical): S93 0x2F, B1..B4 00, A1..A4 F0, C9 0x0F, F1 / F2 00, E5..E8 04, S96 FF, TMOD 0x10, TL1 F4, TH1 E5, IE 0x0C, DD 0xB0.

## 8. Timing sensitivity and v21 / v18 agreement

- Periodic ISR calls per tick 1, 2, 3, 5: same published result and same write order in 4 / 4 (C); only the tick position of the class window ends shifts (the class samples count periodic calls, U which real rate: not determined by this experiment).
- Agreement of the two builds on identical analog input (ch1): open_port 6 / 6, det_codes 16 / 16, detect classifier 525 / 525 for MOD 01 and MOD 05, detect_chain 9 / 9, class_words 12 / 12, reactions 14 / 14, timing 4 / 4 rows equal (same on ch0). Differences: power_good 1 / 5 rows (late event / missing F2 write: v18 counter 0x66 = 0x10 and SAT_PWR 0x20 for events > 2 ticks after power-on), power_trace (v18 programs the limits 6 ticks later, F2 write differs). Final published state of the nominal camera identical (C).

## 9. Not modelled / limits

- Real analog behaviour: detect code D and the sample words are inputs, not derived from a cable / PD model; the thresholds above are the image's own decisions on those inputs (C), not a statement about PD resistance.
- Event timing: BC bit 6 (detect done) modelled 1 tick after D = 1, BC bit 4 2 ticks after F1. Latency range accepted by stock: detect event must arrive before the 50-tick window ends (else D = 2 and idle 60 ticks); power-good event any time (C). Real hardware latency U.
- Periodic ISR rate and main-loop pass rate U (`--pt`, `--passes`); one 10 ms tick assumed (poe-fw.md §17).
- ROM (< 0x1A00) absent: start-up glue, I2C slave, FW CRC not run (host writes call the stock write handlers directly).
- Interrupt controller: no nesting / priority (IP toggles ignored), injection only at the main-loop top.
- `E9..EF` math unit timing (NOP waits) not modelled: results immediate (the image waits with NOPs anyway).
- Unused / not reached: legacy probe 0x316B (needs `D >> 4 != 4` and HP bit 1 per cluster 1; not exercised: D5 / D6 never written), 0x1B03 trim loop convergence, I2C ISR (hook 1), host watchdog, hook 2 external event ISR.
- `F2` semantic unresolved (U): treated as a plain register; if it is a power-clear the stock sequence would shut the port, which contradicts the board (port stays powered).
- Class 4 power-up with CM word 0 shows no fault in the model; board CM 0x0139 / 0x0333 (raw) are not needed for the published result.
