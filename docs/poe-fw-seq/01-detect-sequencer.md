# BCM59111 v21: per-channel probe sequencer (0x316B), cluster 1

Static analysis of `hwdb/firmware/bcm59111-v21.bin` (CPU base 0x1A00), listing `tmp/poe-fw/bcm59111-v21.asm` (`scripts/poe_fw_dis.py`). No board access.
Tags: **C** confirmed by the code bytes (address cited), **I** inferred, **U** unknown. Port: `poe-fw/src/seq_detect.c`, `poe-fw/inc/seq_detect.h` (`-DPOE_FW_SEQ`, off by default).

## 1. Findings that change the plan

- **0x316B is not the standard detect sequencer.** It runs only for the *legacy-powerup probe* (D5 / D6 class-probe hardware), reached from 0x3B3C only when the detect code is not 4 AND `HP_Pn_MOD & 0x02` is set. C (0x3BBD..0x3C16, 0x4D83, 0x3C08, 0x4F31, 0x4E14, 0x49E4)
  - Only writer of an 0x80C0 request bit: 0x4DF5 `st_chan_enable` (0x4DF8..0x4E0B). Only place 0x4DF5 is armed: 0x4F31 (`st_detect_ok_wait` 0x4F23). Only place 0x4F23 is armed: 0x3C08 (detect code != 4, `HP_Pn_MOD & 2` != 0). C
  - Detect code 4 (good signature) arms 0x41D9 `st_class_gate` (0x3BE7..0x3BED), which uses `0x8103` + 0x29EE class mux, never 0x80C0. C
  - `HP_Pn_MOD` default 0x01 (regs.tsv 0x46 / 0x4B / 0x50 / 0x55): bit 1 clear, so with defaults the sequencer is never started. C (default), I (bit 1 = legacy / non-standard powerup; feature string `LEGACY_POWERUP`, `bcm59111-protocol.tsv` 0x1C mode 1 legacy)
- **The missing piece for the detect nibble is 0x3651, not 0x316B.** 0x3651 (cluster 4) reads D9 = 4c + {0..3} with DA:DB words, and when the raw nibble `D(c) >> 4` is 0x0F classifies the signature into code 1..6 / 9->6; raw codes != 0xF pass through. Result goes to the low nibble of IRAM 0x42+c. C (0x3651..0x37C7, 0x36DA, 0x37B4..0x37C6)
- Sample used by the sequencer is `word(AB = c | 4) >> 2`: that word is the port VM source (`VM = word(c|4) * 11`, docs/poe-fw-rewrite.md "Ported"), so X is a port-voltage sample, about 257 mV per count with the 64 mV/count VM scale. C (reads 0x34E9..0x3519), I (voltage, scale)
- Thresholds read as voltage (I): 0x2A ~ 10.8 V, 0x28 ~ 10.3 V, 0x35 ~ 13.6 V, 0x48 ~ 18.5 V; baseline tolerance 6 ~ 1.5 V. A ramp from the detect voltage (< 10 V) to the class window (15.5..20.5 V).
- The sequencer writes no host register and no SAT_Pn. Its only hardware effects are SFR D6 (0x3299, and via 0x227C D5 / D6 = 0). C
- After it ends, 0x3A30 evaluates the record: success arms 0x4F3D -> 0x3FBE (`st_power_request`), failure arms 0x509E (retry, 16 ticks). C (0x3B1D, 0x3B30)

## 2. Call graph and handshake (who feeds the sequencer)

| Step | Function | Addr | Effect | Tag |
|---|---|---|---|---|
| 1 | `st_detect_result` | 0x3B3C | D(c) >> 4 -> 0x3651 -> IRAM 0x42+c low nibble; code == 4 -> W1 = (W1\|1)&0x3F\|0x40, timer1 0x25 / mask 0x0080, arm 0x41D9 (8); else `4D83(c, 2)` != 0 -> timer2 0xFA ticks, wait_b mask 0x10, arm 0x4F23 (5); else arm 0x48BE (0x0F) | C |
| 2 | `st_detect_ok_wait` | 0x4F23 | wait_b bit 4 (0x807D+18c... via 0x1F35/0x2006), clear it, arm 0x4DF5 delay 0 | C |
| 3 | `st_chan_enable` | 0x4DF5 | `0x80C0 \|= bit(c)` (request, low nibble), arm 0x49BD | C |
| 4 | `chan_event_latch` | 0x3DA6 / 0x3E10 | if no owner (`0x80C0 & 0xF0 == 0`, `0x80C4 & 0xF0 == 0`): if `0x80C0 & bit(rr)` -> `0x80C0 = (0x80C0 \| 0x10<<rr) & ~bit(rr)`, flags_b (0x8079+18c) \|= 0x40, `arm_channel(c, 0, 0)`; else `0x8076 = (0x8076 + 1) & 3`. While an owner exists 0x8076 is not advanced | C (0x3DAC..0x3E5B) |
| 5 | `st_class_hw_start` | 0x49BD | if flags_b.6: clear it; `D5 \|= bit(c)`, `D6 \|= bit(c)` (0x49D5, 0x49D7), `state = 1` (0x49DD), arm ptr 0x3A30, `set_timer(c, 100 ticks, wait_a mask 0x08)` (0x49ED..0x49F3) | C |
| 6 | `meas_convert` tail | 0x34D0 (0x35EC..0x3616) | per pass, per channel: state == 1 -> state = 2, delay byte (0x8077+18c) = 6, counter A, baseline, +5, +6 = 0 (counter B kept) | C |
| 7 | **`periodic_detcls_sequencer`** | 0x316B | this document | C |
| 8 | `st_class_wait` | 0x3A30 | timer expiry (wait_a bit 3): evaluate, section 8 | C |

- `c` of 0x316B = XRAM 0x8076 (`rr_chan`), the single probe owner: 0x80C0 bits 7:4 are non-zero only for it, and 0x3DAC refuses to advance 0x8076 while an owner exists. C
- Timer tick unit (10 ms) and the periodic ISR pass period: cluster 5, not measured here. All counts below are in passes of the periodic ISR. U (period)

## 3. Memory map

### 3.1 Probe record, XRAM `0x8104 + 7c` (7 bytes per channel)

| Off | Addr c=0 | Name (ours) | Written by | Meaning | Tag |
|---|---|---|---|---|---|
| +0 | 0x8104 | `state` | 0x49DD (=1), 0x35FA (1->2), every state handler | state machine, section 5 | C |
| +1 | 0x8105 | `cnt_a` | 0x3208 (state 4: ++), 0x3248 (states 6 / 7: =0), 0x32DF (8 / 9: ++), 0x3606 (=0) | state 4: ramp-call count; 8 / 9: hold-call count | C |
| +2 | 0x8106 | `cnt_b` | 0x3228 (states 4 / 5: =0), 0x324E (6 / 7: ++) | number of passes spent in states 6 / 7; target of cnt_a in 8 / 9; read by 0x3A9A | C |
| +3 | 0x8107 | `sample` | 0x3195 | `(u8)((u16 BE at 0x80C5+6c) >> 2)` each pass | C |
| +4 | 0x8108 | `base` | 0x3234 (=sample, states 4 / 5), 0x32D7 (=sample at 6 / 7 -> 8 / 9), 0x360B (=0) | baseline for the "lost" test and the rise | C |
| +5 | 0x8109 | `rise_a` | 0x3269 (`sample - base` in 6 / 7), 0x3611 (=0) | rise during settle; read by 0x3A7E, 0x3AA7, 0x3AB5, 0x3ACD | C |
| +6 | 0x810A | `rise_b` | 0x3308 (`sample - base` at 8 / 9 -> A / B), 0x3616 (=0) | rise during hold; read by 0x3A75 | C |

Address helpers (all C, return DPTR): 0x1FB0 (c = @0x8076 -> R7, then 0x1FB5), 0x1FB3 (c = @DPTR), 0x1FB5 (+0), 0x20B0 / 0x20B5 (+1), 0x20E3 / 0x20E5 (+2), 0x2017 / 0x2018 (+3), 0x20D3 / 0x20D4 / 0x20D5 (+4), 0x203E (+5, 0x2039 loads c from the soft stack first), 0x2134 / 0x2135 (+6). 0x20E3 / 0x20D3 / 0x2029 start with `MOVX @DPTR,A` (store, then fall through to the next pointer calc).

### 3.2 Other state touched

| Item | Addr | Use | Tag |
|---|---|---|---|
| `rr_chan` | XRAM 0x8076 | c, the probe owner | C |
| `ev_chan_mask` | XRAM 0x80C0 | bits 7:4 owner active (gate 0x3174), bits 3:0 request (set by 0x4DF5); 0x227C clears 7:4 for all channels | C |
| VM source word | XRAM 0x80C5 + 6c (BE, hi first) | `(AD << 8) \| AC` at AB = c\|4, written by 0x34D0 (0x34E9..0x351A: AC stored first, AD OR-ed into the high byte at 0x2213 / 0x21D3) | C |
| delay byte | XRAM 0x8077 + 18c | arm_channel R3 target (0x4A28..0x4A2F); sequencer end writes 7 (0x3326..0x3328); 0x34D0 writes 6; 0x3A54 compares with 7 (not 7: call 0x227C); 0x3552 compares 0x0B | C. Consumer as a delay: not found (the tick timers use 0x807E / 0x8081): U |
| timer2 ticks | XRAM 0x8081 + 18c | sequencer end sets 0x14 (0x332B..0x332D); expiry ORs 0x8082:0x8083 into wait_a / wait_b (0x807C / 0x807D + 18c) and raises the service flag (0x3C85..0x3CD5) | C |
| timer1 ticks | XRAM 0x807E + 18c, mask 0x807F:0x8080 | not touched by the sequencer | C |
| PSW | 0x10 (bank 2) | 0x316B pushes PSW (0x316B) and selects bank 2 (0x316D); pops at 0x3331 | C |
| SFR D6 | 0xD6 | `= (c << 6) \| 0x20` (0x3292..0x3299, plain store) | C, meaning U |
| SFR D5, D6 | 0xD5, 0xD6 | `= 0` at the end (0x227C) | C |
| W0 / W1 | XRAM 0x80DD + 2c / 0x80DE + 2c | config / result bytes read by 0x3A30, section 8 | C |

## 4. Functions

| Addr | Name | Role | In | Out / side effects |
|---|---|---|---|---|
| 0x316B | `periodic_detcls_sequencer` | per-pass step for the probe owner | XRAM 0x80C0, 0x8076, 0x80C5+6c, record | updates the record, D6, may end the probe (section 6); PSW bank 2 |
| 0x317B..0x3195 | sample | `X = (u8)(w >> 2)`, w = BE16 at 0x80C5+6c | R5 = c | rec.sample |
| 0x3196..0x31BD | lost test | `X + 6 < base` (9-bit sum vs 8-bit base) -> state = 0x0D (0x31B4..0x31BD) | | rec.state |
| 0x31BE..0x31C5 | dispatch | `keil_CCASE` 0x43FF on `state`, table at 0x31C5, entries `{hi, lo, value}`, `00 00 33 11` = end / default 0x3311 | A = state, R7 = c | |
| 0x31E1 | state 2 handler | `state = 3` | R7 = c | |
| 0x31EB | state 3 handler | `state = 4`, then `X > 0x2A` -> 0x32C4 `state = 0x0D` | | |
| 0x3205 | state 4 handler | `cnt_a++`; `cnt_a >= 0x1F` -> `state = 5`, D6 select (0x3292); else falls into 0x321F | | D6 |
| 0x321F | state 5 handler (and tail of 4) | `cnt_b = 0`, `base = X`, `X > 0x28` -> `state += 2` (0x3309 / 0x330A) | | |
| 0x3245 | states 6 / 7 handler | `cnt_a = 0`, `cnt_b++`, `d = (u8)(X - base)`, `d >= 0x48` -> 0x32C3 lost; `rise_a = d`; 6: 0x3273..0x32AC; 7: 0x32AE..0x32C1; advance 0x32CD (`base = X`, `state += 2`) | | D6 (state 6 restart) |
| 0x32DB | states 8 / 9 handler | `cnt_a++`; `cnt_a == cnt_b` -> `rise_b = (u8)(X - base)`, `state += 2` | | |
| 0x32C4 | fail tail | `state = 0x0D` | A = c | |
| 0x3292 | D6 select | `D6 = ((c << 6) & 0xC0) \| 0x20` (SWAP A, RLC, RLC, ANL 0xC0, ORL 0x20; carry only reaches masked bits) | A = c | D6 |
| 0x3311 | tail | `state >= 0x0A`: `delay(c) = 7`, `timer2(c) = 0x14`, 0x227C (D5 = D6 = 0, `0x80C0 &= 0x0F`) | | |
| 0x3331 | exit | POP PSW, RET | | |
| 0x227C | `detcls_227C` | release the probe: D6 = D5 = 0, `0x80C0 &= 0x0F` | | also called by 0x3A59 |
| 0x22D8 / 0x22D9 | helper | `R7 = A (=@DPTR)`, `R6 = A = @0x8076` (c) | | |
| 0x43FF | `keil_CCASE` | switch helper | | |
| 0x2126 | helper | DPTR = 0x80C5 + 6c | | |

Boundary (not ported here): 0x34D0 conversion and word cache (cluster 4), 0x1B03 offset trim (4), 0x29EE class mux (2), 0x3651 detect scaling (4), tick timers 0x3C1E and the periodic ISR period (5), 0x3334 / 0x30EF limits (3), 0x2F8C power good (3).

## 5. State machine (record `state`), C unless noted

Every pass (before the dispatch): `X = sample`; if `X + 6 < base` then `state = 0x0D` (0x319F..0x31BD). The dispatch then sees the new state. Counters in passes.

| State | Handler | Condition (in order) | Action | Next |
|---|---|---|---|---|
| 0 idle | none | not in table | default 0x3311 | 0 |
| 1 armed | none (0x49DD) | not in table; 0x34D0 converts it before 0x316B runs | | 2 (by 0x34D0) |
| 2 start | 0x31E1 | always | none | 3 |
| 3 pre | 0x31EB | `X >= 0x2B` | none | 0x0D (lost) |
| 3 | 0x31EB | `X <= 0x2A` | none | 4 |
| 4 ramp A | 0x3205 | `cnt_a + 1 >= 0x1F` | `cnt_a++`; `D6 = 0x20 \| c << 6` | 5 |
| 4 | 0x3205 | `cnt_a + 1 < 0x1F`, then like state 5 | `cnt_a++`; `cnt_b = 0`; `base = X` | `X >= 0x29`: 6, else 4 |
| 5 ramp B | 0x321F | `X >= 0x29` | `cnt_b = 0`; `base = X` | 7 |
| 5 | 0x321F | `X <= 0x28` | `cnt_b = 0`; `base = X` | 5 |
| 6 settle A | 0x3245 | `(u8)(X - base) >= 0x48` (includes X < base) | `cnt_a = 0`; `cnt_b++` | 0x0D |
| 6 | 0x3245 | `cnt_b == 0x0A` after the increment and `d < 0x0C` | `cnt_a = 0`; `cnt_b++`; `rise_a = d`; `D6 = 0x20 \| c << 6` | 5 |
| 6 | 0x3245 | else `X >= 0x36` and `cnt_b >= 2` | `rise_a = d`; `base = X` | 8 |
| 6 | 0x3245 | else | `rise_a = d` | 6 |
| 7 settle B | 0x3245 | `d >= 0x48` | as state 6 | 0x0D |
| 7 | 0x3245 | `X >= 0x36` or `cnt_b >= 0x41` | `rise_a = d`; `base = X` | 9 |
| 7 | 0x3245 | else | `rise_a = d` | 7 |
| 8 hold A / 9 hold B | 0x32DB | `cnt_a + 1 != cnt_b` | `cnt_a++` | unchanged |
| 8 / 9 | 0x32DB | `cnt_a + 1 == cnt_b` | `cnt_a++`; `rise_b = (u8)(X - base)` | 0x0A / 0x0B |
| 0x0A / 0x0B done | none | not in table, `state >= 0x0A` | end (below) | stays |
| 0x0D lost | none | not in table, `state >= 0x0A` | end (below) | stays |

End (0x3311, any state `>= 0x0A`, same pass): `delay(c) = 7`; `timer2(c) = 0x14`; `D6 = D5 = 0`; `0x80C0 &= 0x0F`. Later passes return at 0x3178 (no owner). C

- State 6 versus 7: 6 is entered from state 4, 7 from state 5 (`state + 2`). State 6 has the `cnt_b == 10` restart and needs `cnt_b >= 2`; state 7 has no restart, a `cnt_b >= 0x41` exit. C (0x3273..0x32C1)
- The restart (state 6 -> 5) rewrites D6 (0x3286..0x3299) without changing D5. C
- In states 4 / 5 `base` follows `X` every pass, so the test `X + 6 < base` only fires on a drop of more than 6 counts between two passes. C
- In states 6 / 7 `base` stays fixed (set at the transition), so `rise_a` is the rise since entry; the lost test then fires when `X < base - 6`, and the 0x3245 `d >= 0x48` test fires for any `X < base` (wrap) or `X >= base + 0x48`. C
- The probe ends only through `state >= 0x0A`; otherwise 0x3A30 (timer2 = 100 ticks from 0x49ED) calls 0x227C when the delay byte is not 7 (0x3A51..0x3A59). C

## 6. Thresholds and constants

| Const | Addr | Compared with | Meaning | Tag |
|---|---|---|---|---|
| 6 | 0x319F | `X + 6` vs `base` | lost tolerance (9-bit sum) | C |
| 0x2A | 0x31F7 | `X` > 0x2A (state 3) | start voltage too high | C; I (~10.8 V) |
| 0x1E | 0x3211 | `cnt_a` >= 0x1F (state 4) | ramp timeout, restart via D6 | C |
| 0x28 | 0x323B | `X` > 0x28 (4 / 5) | ramp above ~10.3 V: leave | C; I |
| 0x48 | 0x325C | `d` >= 0x48 (6 / 7) | rise window upper bound / any decrease | C |
| 0x0A | 0x3279 | `cnt_b == 10` (state 6) | settle check count | C |
| 0x0C | 0x3282 | `rise_a < 0x0C` (state 6, cnt_b == 10) | too small a rise: restart | C |
| 0x35 | 0x32A3, 0x32B3 | `X >= 0x36` (6 / 7) | settle voltage reached (~13.9 V) | C; I |
| 2 | 0x32A8 | `cnt_b >= 2` (state 6) | minimum settle passes | C |
| 0x41 | 0x32BD | `cnt_b >= 0x41` (state 7) | settle timeout | C |
| 7, 0x14 | 0x3326, 0x332B | end values | delay byte, timer2 ticks | C |
| 0x20, 0xC0 | 0x3295..0x3297 | D6 | select value | C |
| 0x4C23 / 0x4C2B tables | 0x3E1D / 0x3E2B | `bit(c)` 1 / 2 / 4 / 8, `0x10 << c` | owner masks | C |

## 7. What is published

- The sequencer publishes nothing to SAT_Pn (0x0C + c). C (no store to 0x800C..0x800F or IRAM 0x42..0x45 in 0x316B..0x3333)
- Detect nibble (SAT_Pn bits 3:0): IRAM 0x42+c low nibble, written by 0x3651 (0x37BC..0x37C6) from the raw `D(c) >> 4` (0x3B6F..0x3B7B) and the four-point words; published by 0x3B3C (0x3B7E..0x3BBA, via 0x2059 -> 0x8000 + 0x0C + c, 0x4E1A raise). C (data flow), I (publish branch details of 0x3B7E..0x3BBA)
- Class nibble (SAT_Pn bits 6:4): `(W0 & 0x38) << 1` merged into IRAM 0x42+c bits 6:4 by 0x4658, published the same way; W0 bits 5:3 are written by 0x29EE (cluster 2); 0x47E4 clears them (0x47FC..0x4801) and calls 0x4658. C
- 0x3A30 success clears W0 bits 5:3 (0x3AFF) without calling 0x4658: the class nibble is not updated in the legacy path. C (0x3AF6..0x3B01), I (consequence)
- SAT_PWR, F1 / F2: not touched by the sequencer; the power-up request comes from 0x4F3D -> 0x3FBE. C

## 8. Consumer: `st_class_wait` 0x3A30 (adjacent, decision part ported as `seq_eval`)

Runs from the main loop when the service flag is set and `wait_a (0x807C + 18c)` bit 3 is set (timer2 expiry). Notation: `W1 = XRAM[0x80DE + 2c]`, `W0 = XRAM[0x80DD + 2c]`, `P = rise_a`, `Q = rise_b`, `B = cnt_b`.

| Step | Addr | Action | Tag |
|---|---|---|---|
| 1 | 0x3A3A..0x3A4C | wait_a bit 3 clear -> return; else clear it | C |
| 2 | 0x3A4D..0x3A59 | `delay(0x8076) != 7` -> `0x227C` (release; end not reached) | C |
| 3 | 0x3A5C..0x3A66 | `W1 &= 0x3F` | C |
| 4 | 0x3A67..0x3A6E | `state == 0x0D` -> skip steps 5 / 6 | C |
| 5 | 0x3A70..0x3A95 | `diff = abs(P - Q)` | C |
| 6 | 0x3A95..0x3AC5 | valid (`W1 \|= 0x80`) iff: `B == 2`: `P < 0x14`; `B != 2`: `B < 0x41`, or `B >= 0x41` and `P >= 0x16` | C |
| 7 | 0x3ACD..0x3ADE | `diff >= (P >> 1)` -> `W1 &= 0x3F` (valid cleared) | C |
| 8 | 0x3AE1..0x3AEF | success iff `W1 >> 6 & 3 == 2` | C |
| success | 0x3AF6..0x3B24 | `W1 \|= 1`; `W0 &= 0xC7`; `set_timer(c, 0x20 ticks, wait_b mask 0x40)`; timer1 `0x28 ticks`, mask `0x0080` (0x5065); ptr = 0x4F3D | C |
| failure | 0x3B26..0x3B34 | `W1 &= 0xFE`; `arm_channel(c, 0x509E, 0x10)` | C |

- 0x4F3D waits wait_b bit 6 then arms 0x3FBE (`st_power_request`) with delay 0. C (0x4F3D..0x4F51)
- `W1` bits 7:6: 0x3BCC..0x3BD8 sets `01` at a good detect, 0x3A5C clears them, step 6 sets bit 7. C

## 9. Open questions (a simulator experiment can answer)

1. With `HP_P1_MOD = 0x03`, raw `D1 >> 4 = 6` (open) and `D1 >> 4 = 1`, does the stock image take 0x3C08 (4F23) and start the probe? (bit 1 meaning, I)
2. With `word(c|4)` constant 0 for 0x80 passes: the stock `state` reaches 5 after 31 passes in state 4, `D6 = 0x20 \| c << 6` is written once, `0x3A30` finds `delay != 7`, calls 0x227C and arms 0x509E. Confirm.
3. With `word(c|4) >> 2` ramping 0x00 -> 0x30 -> 0x3A in steps: which pass reaches state 8 / 9 / 0x0A, and what `rise_a`, `rise_b`, `cnt_b` are at 0x3A30 (predicted: section 5)?
4. With `word(c|4) >> 2 = 0x2B` at the first pass in state 3: state 0x0D, probe released, 0x3A30 fails (predicted).
5. With a constant `X >= 0x36` from state 6: end at `cnt_b = 2` -> state 8, hold 2 passes, `rise_b = 0`: does 0x3A30 succeed or fail (`P = 0`, `diff = 0 >= 0`: predicted fail)? Which `P` / `Q` / `B` combination does succeed (predicted: `B != 2`, `B < 0x41`, `diff < P >> 1`, e.g. P = 0x20, Q = 0x20 + 5)?
6. What does the hardware do with D6 `= 0x20 \| c << 6` versus `D6 \|= bit(c)` (0x49D7)? Sim: log D5 / D6 writes during the whole probe and compare with the sequence in section 5.
7. What is the periodic ISR pass period (the `cnt_a` / `cnt_b` limits are in passes)? Sim: count passes between two 0x3A30 runs for a fixed `delay`.
8. Is `HP_Pn_MOD & 0x02` what 0x4D83(c, 2) reads for all four channels (0x8046 / 0x804B / 0x8050 / 0x8055)? C for the addresses, I for the register meaning.
9. Where is the 0x8077 + 18c byte consumed as a delay? Sim: write 7 / 6 / 0x0A there and watch which state handler runs and when.
10. Does 0x3651 (not this cluster) return code 4 for a camera with `D1 >> 4 = 0x0F` and four-point words, and which words (D9 = 4c..4c+3 via DA:DB) does it need? This decides the standard detect path.

## 10. Variants: v21 vs Meraki v18 (`hwdb/firmware/bcm59111-meraki.bin`, listing `tmp/poe-fw/bcm59111-meraki.asm`)

Method: normalised instruction streams compared (code addresses masked, XRAM addresses and all 8-bit immediates kept) with difflib; helper bodies compared head to RET. Tag **C-both** = identical logic and constants in both images.

- 0x316B..0x3333 (457 B) vs v18 0x31A4..0x336C (457 B): identical instruction stream; the only byte differences are code addresses (59 bytes: LCALL / LJMP / table targets, uniform +0x39). C-both
- All thresholds, state values, XRAM addresses (0x8076, 0x80C0, 0x8104.., 0x8077, 0x8081), D6 formula and the `0x31C5` table values (`02..09`, default) are the same. C-both
- No firmware-specific parameter differs in this cluster: the "v21 vs v18" parameter table is empty. C-both
- Consequence: every C / I statement in sections 1..8 that is about this cluster's logic holds for both images; the I items that remain I are the hardware meanings (voltage, legacy mode, D6), not the code.

| v21 | v18 | Role | Compared |
|---|---|---|---|
| 0x316B | 0x31A4 | `periodic_detcls_sequencer` | identical |
| 0x31C5 | 0x31FE | CCASE table (`31 E1 02`.. -> `32 1A 02`.. ; default 0x3311 -> 0x334A) | values identical, targets +0x39 |
| 0x31E1 / 0x31EB / 0x3205 / 0x321F / 0x3245 / 0x32DB / 0x3311 | 0x321A / 0x3224 / 0x323E / 0x3258 / 0x327E / 0x3314 / 0x334A | state handlers, tail | identical |
| 0x1FB0 / 0x1FB5 | 0x1FB0 / 0x1FB5 | record +0 pointers | identical (same address) |
| 0x20B0 / 0x20B5, 0x20E3 / 0x20E5, 0x2017 / 0x2018, 0x20D3..0x20D5, 0x203E, 0x2135 | 0x20B2 / 0x20B7, 0x20E5 / 0x20E7, 0x202A / 0x202B, 0x20D5..0x20D7, 0x2051, 0x2147 | record +1..+6 pointers (strides 7, offsets 5, 6, 7, 8, 9, 0xA) | identical |
| 0x2126, 0x2086, 0x2029, 0x227C, 0x22D8, 0x43FF | 0x2138, 0x2099, 0x2009, 0x227F, 0x22D6, 0x4450 | 0x80C5+6c, delay byte, timer2, release, helper, CCASE | identical |
| 0x34D0 (0x35EC..0x3616 conversion 1 -> 2) | 0x350A (0x3626..) | caller side | identical |
| 0x3A30 `st_class_wait` | 0x3A6A | consumer (all thresholds 0x14, 0x15, 0x41, `>>1`) | identical; success ptr 0x4F3D -> 0x4F80, failure ptr 0x509E -> 0x50F0, delay 0x10 same |
| 0x3B3C `st_detect_result` | 0x3B76 | code 4 -> class gate; else `4D83(c, 2)` -> timer 0xFA / ptr 0x4F23 (v18 0x4F66, delay 5) else 0x48BE (v18 0x4966, delay 0x0F) | identical |
| 0x4D83 | 0x4DEB | `HP_Pn_MOD` (0x8046 / 0x804B / 0x8050 / 0x8055) `& R5` | identical |
| 0x4F23 / 0x4DF5 / 0x49BD | 0x4F66 / 0x4E38 / 0x4A65 | ok-wait, request, class hw start (timer 100, ptr 0x3A30 -> 0x3A6A) | identical |
| 0x3DA6 `chan_event_latch` | 0x3DE0 | owner grant | identical |
| 0x3651, 0x4658, 0x41D9, 0x448D | 0x368B, 0x46B6, 0x4396, 0x454A | detect scaling, class publish, gate, eval | identical (only handler-address constants shift) |
| 0x5024 / 0x5065 / 0x2199 | 0x5067 / 0x50B7 / 0x219B | `set_timer`, timer1, timer helper | called with identical arguments (0x64, 0x20 / 0x40, 0x28, 0xFA / 0x10) |

- Upgrades to C-both: sections 1 (legacy-only reach of the sequencer), 2 (handshake), 4..6 (functions, state machine, thresholds), 8 (consumer). Section 7 (what is published): C-both for the sequencer and 0x3A30; the 0x4658 publish path is identical.
- Open: other v21 / v18 differences from docs/poe-fw.md section 16 (`st_power_good`, `st_port_cfg`, limit code) are outside this cluster; v18 behaviour for them is not covered here.
