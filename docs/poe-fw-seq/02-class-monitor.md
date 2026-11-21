# BCM59111 stock sequencing, cluster 2: class mux schedule, SAT_Pn composition, monitor

- Source: `hwdb/firmware/bcm59111-v21.bin` (v21) and `bcm59111-meraki.bin` (v18). Offline only (#26). Addresses = CPU addresses (base 0x1A00).
- Tags: **C** confirmed by code bytes, **I** inferred, **U** unknown. **C-both** = byte-identical logic in v21 and v18 (normalised diff, `scripts/poe_fw_diff.py` + a hand diff of 0x29EE..0x2EDF, see 'v21 vs v18').
- C code: `poe-fw/src/seq_class.c`, `poe-fw/inc/seq_class.h`; build check `python3 scripts/poe_fw_build.py --seq` (default images unchanged, verified against a clean HEAD build).
- Notation: `A(c)` = XRAM 0x8078+18c, `B(c)` = 0x8079+18c, `F3(c)` = 0x807B+18c, `W(c)` = 0x807C+18c (stride 0x12 = 18). `m` = physical channel = `ch` unless the remap is on (IRAM 0x24 != 0: `map[ch]` = XRAM 0x8125+ch).
- `0x1F74` (inside `0x1F6A`) = `[0x8133] := map(R7)` and returns it. Helper stack: IRAM 0x7C = software arg stack (ch pushed by `DEC 0x7C`), so "ch" below = `@0x7C`.

## 1. Periodic pass order (C, v21 0x4EBA)

- `1B03` offset trim, `37D2` temperature, `34D0` measure, `29EE` class mux, `316B` detect/class sequencer (cluster 1). Same order in v18 (C-both for the mux position).
- `34D0` (0x34E7..0x3518) refreshes 3 words per channel before `29EE` runs, big-endian (AD first, AC second) (C):

| XRAM | word | source |
|---|---|---|
| 0x80C5 + 6c | W4 | `AB = c|4` |
| 0x80C7 + 6c | W8 | `AB = c|8` (the class sample) |
| 0x80C9 + 6c | W0 | `AB = c` (stored, consumer: cluster 4) |

- Consequence: `29EE` never touches AB; its "sample" is the W8 word the same pass captured. The hardware conversion depends on AF (mux). (C)

## 2. Class mux schedule `periodic_dc_mux_scheduler` 0x29EE (v18 0x29E5)

State (XRAM, all C): `0x8103` active channel bits (set by `st_class_gate` 0x41D9 at 0x4222, cleared here), `0x8134` sub-state, `0x8135` tick, `0x8136` channel (rotates), `0x8137` mapped channel, `0x8138` hp flag, `0x8139` code1, `0x813A` code2, `0x813B:C` sample 1 (BE), `0x813D:E` sample 2 (BE).

- Entry: `PSW = bank 2`; `R7 = [0x8103]`; zero -> return (tick and state untouched). Else `[0x8135]++` (u8, wraps), then dispatch on `[0x8134]` (0x2A06..0x2A26). (C)
- IRAM 0x17 in `CJNE A,0x17` at 0x2B3C is **R7 of bank 2** = code2 just returned by `0x4900` (resolves the open item in poe-fw-rewrite.md). (C)
- `hp` (0x8138) = `(HP_EN & bit(m)) && (HP_Pn_MOD(m) & 1)` via `0x4A69` (C; `0x8044` = reg 0x44, `0x8046/4B/50/55` = HP_Pn_MOD).
- `AF` = SFR 0xAF; value written = `ch*4 | v` (ch unmapped). Earlier docs listed AF "+5": the 5 is a sub-state number, not an AF value. AF values are only 0, 1, 2. (C)

| sub | tick | AF written | sample taken | action / next sub |
|---|---|---|---|---|
| 0 select (0x2A29) | reset to 0 | `ch*4\|1` if `A & bit(ch)` | none | active: sub=1, `[0x8137]=map(ch)`, `hp=0x4A69(m)`; inactive: `ch=(ch+1)&3`. Both: `[0x80DD+2*ch]` bits 7:6 := 01 (new ch). (C) |
| 1 (0x2A84) | == 5 | hp && code1==4: `ch*4\|2`; else `ch*4` | s1 = W8(ch) (0xFFFF if AF.7 set), `code1 = 0x4900(s1)` | hp && code1==4: sub=2, `[80DD+2ch]` bits 7:6 := 10. else sub=5, code2 := code1. (C) |
| 2 (0x2AEA) | == 9 (0x09) | `ch*4\|1` | none | sub=3. (C) |
| 3 (0x2B01) | == 14 (0x0E) | code1==code2: `ch*4\|2`; else `ch*4` | s2 = W8(ch) (0xFFFF if AF.7), `code2 = 0x4900(s2)` | equal: sub=4; else sub=5. (C) |
| 4 (0x2B55) | == 17 (0x11) | unchanged (`ch*4\|2`) | none | sub=5. (C) |
| 5 (0x2B69) | >= 10 (0x0A) | see finish | none | finish below. (C) |

- Ticks run on the periodic ISR pass count; period not measured here. Bound (I): `st_class_gate` arms a 20-timer-tick window (0x4234, R5 = 0x14, mask wait_b bit 2 = class timeout) and the longest mux path needs 18 passes (HP two-event) -> pass period <= ~window/18 (about 11 ms at the 10 ms timer tick of docs/poe-fw.md section 17), else the window ends first and `st_class_done` publishes class 0. (I)
- AF.7 readback (0x2A9E, 0x2B1B): never written by firmware (written values <= 0x11), so it is a hardware status bit; set -> sample forced to 0xFFFF -> code 7. Meaning U.
- Reading of the AF values (I): `|1` = class probe on, `|2` = mark; the HP path (hp, code1 == 4) runs a second class event: event 1 ticks 0..5 (AF 1), mark 5..9 (AF 2), event 2 9..14 (AF 1), mark 14..17 (AF 2). That is the 802.3at two-event class 4 sequence. (I)

### Finish (sub 5, 0x2B75..0x2C75)

1. If `hp` and `code1 == 4` and `code2 == 4` (0x2B75..0x2B85):
   - `opmode(m) == 3` (OP_MOD 2 bits of IRAM 0x41 shadow, `0x4C33`) **or** `F3(ch) bit 2` (host power-on request): power up here (0x2BA0..0x2BE7):
     - `HP_EN & (0x10 << m)` set: `[80DE+2ch] |= 0x20`, `F2 |= bit(ch)`, `F1 |= bit(ch)`.
     - clear: `[80DE+2ch] |= 0x20`, `F2 |= bit(ch)`, `F1 |= 0x11 << ch` (v18 differs, see table).
     - `[0x8077+18ch] := 0x0A`, next-state ptr `0x8087+18ch := 0x2F8C` (`st_power_good`); no timer here.
   - else `AF = ch*4` (0x2BE9).
2. If `hp` and not (`code1 == 4 && code2 == 4`): `AF = ch*4` (0x2BEC). If `hp == 0`: AF untouched (already `ch*4`).
3. Common tail (0x2BF6..0x2C75): (C)
   - `phase = [80DD+2ch] >> 6 & 3`; `HP_Pn_SAT(m) := (phase == 2)` (regs 0x49 / 0x4E / 0x53 / 0x58 via `0x4DAA`). So HP_Pn_SAT = 1 iff the two-event branch ran (hp && code1 == 4). (I for meaning)
   - set service flag `0x23.(ch+1)` (ch unmapped).
   - `[80DD+2ch] := ([80DD+2ch] & 0xC7) | ((code2 & 7) << 3)`: **class code = code2** (second sample when two ran, else the first). Bits 7:6 stay at 01 / 10.
   - `B(ch) |= 0x02` = class sample done (consumed by `st_class_done` 0x47E4, which clears it, cancels the class timer `[0x8081+18c] = 0`, calls `0x4658` and arms `st_class_eval` 0x448D with code 9).
   - `[0x8134] = 0`; `[0x8103] &= ~bit(ch)`; `[0x8136] = (ch+1) & 3`.

## 3. Class code conversion `meas_to_byte` 0x4900 (v18 0x49A8, C-both)

- Input R6:R7 = 16-bit word (hi:lo). Compare unsigned, in order, first hit wins:

| word < | code | meaning |
|---|---|---|
| 0x006F (111) | 6 | class 0 (SAT bits 6:4 = 6) |
| 0x00F7 (247) | 1 | class 1 |
| 0x0189 (393) | 2 | class 2 |
| 0x0233 (563) | 3 | class 3 |
| 0x0322 (802) | 4 | class 4 |
| otherwise (incl. 0xFFFF) | 7 | above class 4 / invalid |

- Only callers: 0x2AB3 (sample 1), 0x2B30 (sample 2). Units of the word: U (cluster 4). `class_code()` in meas.c is byte-identical in behaviour. (C)
- Code 0 is never returned by `0x4900`: SAT class bits = 0 means "no class stored" (`st_class_done` timeout path clears bits 5:3, 0x47FC).

## 4. Where the class code lives and how it reaches SAT_Pn

- Per-channel byte `K(c)` = XRAM 0x80DD + 2c ("cls"):

| bits | meaning | writers |
|---|---|---|
| 7:6 | mux phase: 01 selected/idle, 10 two-event branch taken | 0x2A7E, 0x2AD4, 0x3BD6 (detect-good path writes `80DE`, not this byte) |
| 5:3 | class code (0x4900 result) | 0x2C4B (mux finish), clear: 0x47FF (timeout), 0x4AE9 (reset) |
| 2:0 | power state, 2 = powered | 0x30B3 (power good); clear 0x4AE4 (reset) |

- Second byte `L(c)` = 0x80DE + 2c ("ctl"): bit 0 detect good (cleared at 0x1A98, set 0x3BCF), bit 1 ctrl pending (set 0x3130, tested 0x2E9F), bit 2 set at power good (0x309B), bit 5 F2/HP enabled (0x2258, 0x4DDE; consumed as CM scale in 0x34D0), bits 7:6 = 01 after detect good (0x3BD6), v18 bit 3 (0x2BDE). (C writers, I meaning)

### SAT_Pn byte (regs 0x0C + c = XRAM 0x800C + c)

- Composition (C): `SAT_Pn = shadow(m)`, whole byte. `shadow(m)` = IRAM 0x42 + m.

| bits | content | written by |
|---|---|---|
| 3:0 | detect code | `0x37C0`: `shadow = (shadow & 0xF0) \| (IRAM 0x6B & 0x0F)`, IRAM 0x6B = result of `0x3651` from `D(ch)>>4` and the D9..DB words (cluster 1). 4 = good signature, 6 = open circuit. |
| 6:4 | class code | `0x4658`: `shadow = (shadow & 0x8F) \| (([80DD+2m] & 0x38) << 1)` |
| 7 | always 0 | never set (all writers mask it or preserve 0) |

- Publish sites (all copy the shadow, or a constant, to the register):

| addr | where | value written |
|---|---|---|
| 0x1AA8 | detect phase 2 start (`st_detect_wait` 1A60, v18 ~0x3FF8) | `shadow & 0xF0` (detect nibble cleared), then DET event (`0x4E1A`) |
| 0x3B3C | `st_detect_result` (v18 0x3B76) | `shadow` with new detect nibble; DET_CLASS_EVT \|= bit(m), INT 0x08. If MISC_CONF (0x8017) bit 6 set: event only when the nibble changed. Register always written. |
| 0x4658 | class publish (from `st_class_done` 0x47E4 and `st_power_good` 0x2FAA) | `shadow` with new class bits; DET_CLASS_EVT \|= 0x10<<m, INT 0x10. MISC_CONF bit 2: event only when the class changed. |
| 0x4AD7 | `st_port_reset` | `shadow & 0x80` = 0; `K &= 0xC0` |
| 0x2D88 | monitor, disconnect (flags A bit 3) | shadow = 0x06, SAT = 0x06 (open) |
| 0x2DFE | monitor, off requested | shadow = 0, SAT = 0 |
| 0x3EB1.. | push-button off (0x3E5D, regs 0x19 bits 4..7) | shadow = 0, SAT = 0 |

- Resulting values (C): empty port `0x06` = detect code 6 from `0x3651`, no class. Camera: after detect `0x04`, after class `0x44` (class code 4 = bits 6:4). Class 0 PD = `0x64`. Above class 4 = `0x74`.
- INT / event side effect of a SAT change is only raised at the sites above; `SAT_Pn` itself is plain XRAM read by the host. (C)

### SAT_PWR (reg 0x10, XRAM 0x8010)

- Bits 3:0 = enable, bits 7:4 = good (stock naming; observed 0x88 / 0x22 for one powered channel). (C)
- Set: enable `|= bit(m)` in `0x4C62` (called by `st_power_good` 0x2FB2 when `A(c) == 0 && B(c) == 2`), PWR_EVT `|= bit`, INT 0x01. Good `|= 0x10 << m` at 0x3147 after the hardware power-good test passed (`0x5074(m)`, SFR F3 & bit), PWR_EVT `|= 0x10 << m`, INT 0x02. (C)
- Cleared (both nibbles, `&= ~(0x11 << m)`): monitor paths 0x2CBF, 0x2D01, 0x2D62, 0x2E25, `st_power_good` failure 0x3028, reset 0x4AD7 side. (C)

## 5. Monitor `st_monitor` ("periodic_port_monitor") 0x2C79..0x2EDD (v18 0x2CA6..0x2F0A, C-both)

- Role: state handler for state code 0x0B (powered), installed by `st_power_good` (0x30A0..0x30AC: code 0x0B, ptr 0x2C79). Run by the main loop when service flag 0x23.(ch+1) is set (`R7 = ch`). (C)
- No state-pointer exit except through `arm_channel` 0x49FF (`R7 = ch, R4:R5 = ptr, R3 = code`): sets service flag, `[0x8087+18c] = ptr` if R4:R5 != 0, `[0x8077+18c] = R3` if R3 != 0. (C)
- Correction: R3 is a **state code** in `[0x8077+18c]`, not a tick delay: `tick_timers` 0x3C1E only decrements `[0x807E+18c]` and `[0x8081+18c]` (timers loaded by `set_timer` 0x5024: R5 ticks at 0x8081, R2/R3 = mask OR-ed into `W(c)` / `W(c)+1` at expiry). The only reader of `[0x8077+18c]` is 0x34D0 (0x3555: gate CM/VM on `== 0x0B`). (C)
- Pointer constants: fault `0x4D5C`, idle `0x4EEF`, cleanup `0x4F9F`, class eval `0x448D`, power good `0x2F8C`, monitor `0x2C79`.

Event decision order (first match wins; every branch ends at 0x2EDD = `INC 0x7C; RET`):

| # | condition | clears | next state / code | side effects |
|---|---|---|---|---|
| 0 | `A bit 1` (0x2C7E) | A bit 1 | none (continues) | none |
| 1 | `A & 0x41` (0x2C94) | 0x41 | fault 0x4D5C / 0x0C | PWR_EVT `\|= 0x11<<m` (+ shadow copy 0x8003), SAT_PWR `&= ~(0x11<<m)`, TS_EVT `\|= 0x10<<m` (+0x8009), INT `\|= 0x23`, CNT_SHORT (0x8062+4m) **high** nibble ++ (wraps) |
| 2 | `A bit 4` (0x2CEE) | bit 4 | fault 0x4D5C / 0x0D | PWR_EVT `\|= 0x11<<m`, SAT_PWR cleared, FAULT_EVT `\|= bit(ch)` (low nibble, + 0x8007), INT `\|= 0x23`, CNT_SHORT **low** nibble ++ |
| 3 | `A bit 3` (0x2D31) | bit 3 | idle 0x4EEF / 0x02 | FAULT_EVT `\|= 0x10<<m` (+0x8007), PWR_EVT `\|= 0x11<<m`, SAT_PWR cleared, INT `\|= 0x07`, CNT_MPS (0x8063+4m) ++ (byte), shadow = SAT = 0x06 |
| 4 | `(A & 0x20) \| (B & 0x30)` (0x2DA4) | A 0x20, B 0x30 | cleanup 0x4F9F / 0x0E | PWR_EVT, SAT_PWR cleared, INT `\|= 0x03` |
| 5 | else: `opmode(m) == 0` -> `F3 \|= 0x10` (0x2DD4); then `F3 & 0x18` (0x2DE7) | F3 0x18 | cleanup 0x4F9F / 0x0E | `F1 &= ~bit(ch)` (low nibble only), shadow = SAT = 0, PWR_EVT, SAT_PWR cleared, INT `\|= 0x03` |
| 6 | else `SFR F3 & bit(m) == 0` (0x2E3C, `0x5074`) | | counter `[0x8120+ch]++`; at >= 4 (0x2E52 `SUBB` with carry set): clear it, CNT_DENIED (0x8061+4m) ++, fault 0x4D5C / 0x0C | PWR_EVT, SAT_PWR cleared, TS_EVT `\|= 0x10<<m`, INT `\|= 0x23`. Below 4: continue to 7. |
| 7 | `W bit 1` (0x2E91, 2-tick timer) | W bit 1, `L bit 1`, `F3 bit 2` | none | if `L bit 1`: clear it and `B(ch) |= 0x80` (`0x493F`, R5 = 1, SFR B1..B4); `F3 &= ~4`; copy CM word `[0x80F5+4c]` and VM word `[0x80F3+4c]` to regs 0x30+4m..0x33+4m (`0x442B`: CM lo, CM hi, VM lo, VM hi); re-arm timer: `[0x8081+18c] = 2`, mask 0x0200 (`0x501E`) |

- Mixed indexing in stock (C): branch 2 uses unmapped `ch` for FAULT_EVT bit, branches 1/3/4/5/6 use `m`; identical when the remap is off.
- Channel-ISR flag sources (C, `isr_chan0` 0x4255; ch1..3 same code): `CHn_EVENT` (SFR BC..BF) bit 7 -> `A \|= 0x40`; bit 6 -> `B \|= 0x01`; bit 4 -> `B \|= 0x04`; bit 3 -> `A \|= 0x10`; bit 2 -> `A \|= 0x08`; bit 1 with `EV95.1` -> `A \|= 0x01`; bit 0 -> `A \|= 0x80`; ext pin ISR (0x4879) -> `B \|= 0x10`. `A bit 1` and `A bit 5` have no writer in the ISRs (U). Meaning of the event bits: U (I from the actions: ev.7 / ev.1 overload-TCUT, ev.3 current limit, ev.2 disconnect).
- FAULT_EVT low nibble vs TS_EVT bit meanings: I (TS_EVT 7:4 = TCUT per channel, 3:0 = TSTA; seen with INT bits 0x20 / 0x40).
- `st_power_good` failure path (cluster 3, 0x2FD1): `TS_EVT |= bit(m)` (low nibble), CNT_SHORT high nibble ++, INT `\|= 0x41`: consistent with TSTA = start-up timeout. (C action, I meaning)

## 6. Other stock helpers used (C)

| addr | role |
|---|---|
| 0x1F11 / 0x1F6A / 0x1F74 | push ch on the IRAM stack (0x7C), `[0x8133] = map(ch)` |
| 0x1F23 / 0x1F27 / 0x1F3B / 0x1F3E / 0x1FD1 / 0x1FE5 / 0x1FF8 | DPTR = 0x8078 / 0x8078 / 0x807C / 0x807C / 0x807A / 0x807B / 0x807E + 18*idx |
| 0x1F5D / 0x1FA3 | DPTR = 0x80DE + 2*idx (A = L) / 0x80DD + 2*idx (A = K) |
| 0x2086 / 0x2085 / 0x1F93 | DPTR = 0x8077 / 0x8087 + 18*idx (state code / pointer) |
| 0x2061, 0x2070, 0x2076, 0x2077 | `[0x8002] \|= 0x11<<m`, `[0x8003] =` same, returns `[0x8010]` |
| 0x22C8 / 0x20F3 | `[0x8008] =` / `[0x8009] =` A (TS_EVT pair); `0x20F3` also `[0x8000] \|= 0x23`, INT line, returns CNT_SHORT |
| 0x4A69 | hp flag; 0x4C33 / 0x4B0D OP_MOD of m (`IRAM 0x41 >> 2m & 3`, v18 0x4C73 / 0x4B7D) |
| 0x4BD3 | class code of ch for limit tables; code 4 returns 0 if `hp == 0` (cluster 3). v18 has no such helper |
| 0x4C03 / 0x4C0B | tables (limit byte per class code `F0 48 84 F0 CC F0 F0 F0`; F2 enable `00 00 00 00 01 00 00 00`); v18 0x4C43 / 0x4C4B, same bytes |
| 0x4C23 / 0x4C2B / 0x4C2F | `01 02 04 08` / `10 20 40 80` / `11 22 44 88`; v18 0x4C63 / 0x4C6B / 0x4C6F |

## 7. v21 vs v18 (Meraki)

Same logic (C-both), v18 address:

| function | v21 | v18 |
|---|---|---|
| mux `periodic_dc_mux_scheduler` | 0x29EE | 0x29E5 (+ helper 0x2C9A) |
| monitor | 0x2C79 | 0x2CA6 |
| class decode (thresholds 0x6F 0xF7 0x189 0x233 0x322, codes 6 1 2 3 4 7) | 0x4900 | 0x49A8 |
| SAT low nibble `3651` / detect result | 0x3651 / 0x3B3C | 0x368B / 0x3B76 |
| class publish `0x4658` | 0x4658 | 0x46B6 |
| `st_class_done` | 0x47E4 | 0x4842 |
| `st_class_eval` | 0x448D | 0x454A |
| `st_port_reset` | 0x4AD7 | 0x4B47 |
| `st_detect_wait` (SAT clear at phase 2) | 0x1A60 | ~0x3FF8 |
| hp flag / op mode / map / PG status | 0x4A69 / 0x4C33 / 0x50E0 / 0x5074 | 0x4AD9 / 0x4C73 / 0x5132 / 0x50C6 |
| arm / set_timer / monitor timer | 0x49FF / 0x5024 / 0x501E | 0x4AA7 / (same shape) / 0x5061 |
| `st_fault_shutdown` / `st_idle` / `st_disconnect_cleanup` | 0x4D5C / 0x4EEF / 0x4F9F | 0x4DC4 / 0x4F32 / 0x4FE2 |
| CM/VM write `442B`; `34D0` | 0x442B; 0x34D0 | 0x44E8; 0x350A |
| `st_power_good` SAT_PWR enable (`4C62`) and good bit | 0x4C62 | 0x4CA2 |

Schedule constants identical: sub-states 0..5, tick compares 5 / 9 / 14 / 17 / >=10, AF values 0 / 1 / 2, AF.7 -> 0xFFFF, HP_Pn_SAT rule, class bits `<<3`, flag `B |= 2`, all monitor branch masks (0x41, 0x10, 0x08, 0x20 / 0x30, 0x18), counters, code values 0x0C 0x0D 0x0E 0x02 0x0A 0x0B, PG count 4, 2-tick timer, SAT constants 0x06 and 0.

Differences (firmware-specific):

| item | v21 | v18 |
|---|---|---|
| mux sub 0, hp flag | `hp = 0x4A69(m)` | `LCALL 0x2C9A` (opmode(ch) == 3?) first: **auto mode forces hp = 0** (0x2A4D..0x2A5D): no two-event class in auto. v21 adds `DEFAULT_AF_AUTO_MODE` (tag string): auto mode runs the two-event class and powers from the mux (I) |
| finish, power path, hp set | `F2\|=bit, F1\|=bit, L\|=0x20` | same |
| finish, power path, hp clear | `L \|= 0x20`, `F2 \|= bit`, `F1 \|= 0x11<<ch` | `F2 &= ~bit`, `F1 \|= 0x11<<ch`, `L \|= 0x08`, `[0x8081+18c] = 8`, mask `0x0100` (wait_a bit 0 after 8 ticks) (0x2BC6..0x2BF4) |
| tables / pointers | 0x4C23 0x4C2B 0x4C2F; ptr 0x4D5C 0x4EEF 0x4F9F | 0x4C63 0x4C6B 0x4C6F; 0x4DC4 0x4F32 0x4FE2 (relocation only) |
| `st_class_gate` | `CLR EA` around `[0x8103] \|=` | no EA protection; also relocation |
| `st_power_good` | 204 B | 306 B: extra branch on `B bit 0` and `(K >> 3) & 0x1F` bit 0 (0x3056, 0x3092), `4BD3` replaced by inline code (cluster 3, not decoded here) |
| FW_REV | 0x15 | 0x12 |

- The SAT_Pn, SAT_PWR, thresholds and monitor logic need no per-variant switch; only the auto-mode hp rule and the non-HP power path differ. `seq_class.c` takes `-DPOE_FW_SEQ_V18` for those two (default = v21).
- Other images in `hwdb/firmware/`: `us48-tsc.bin` is not a BCM59111 image; `bcm59111-awto*.bin` are ours. Not compared.

## 8. Deviations of `chan.c` (rewrite) found while decoding (I)

- Stock `arm_channel` R3 is a state code, not a delay in ticks (section 5); the stock wait comes from `set_timer`. `chan.c` `arm(ch, st, dly)` conflates them.
- Stock flags A bit 3 (disconnect) goes to idle 0x4EEF / code 2 with FAULT_EVT high nibble, MPS counter and SAT = 0x06; `chan.c` routes disconnect through ST_DISCONNECT (cleanup) and never writes SAT = 0x06 on disconnect.
- Stock empty-port SAT 0x06 comes from detect code 6 (and the disconnect path); class nibble is only written after a class sample: `chan.c` `ST_CLASS_DONE` writes the class from a single sample at an unspecified time.
- Stock power-up from the mux (auto, class 4, hp) skips `st_power_request` and goes to `st_power_good` directly with F1 / F2 already set.

## 9. Open questions (U) phrased as simulator experiments

1. With W8 = 0x0180 for channel 0 (class 2 range), HP_EN = 0, OP_MOD = 3, `[0x8103] = 1`, run 0x29EE: expect sub 1 at tick 5, finish at tick 10, `K(0) bits 5:3 = 2`, `B(0) bit 1` set, AF = 0 at the end. Does the stock image publish `SAT_P0 = 0x24` after `st_class_done`?
2. HP_EN = 1, HP_P1_MOD = 1, W8 = 0x0400 held constant (class 4): expect AF 1, 2, 1, 2 on ticks 0, 5, 9, 14 and `HP_P1_SAT = 1`. With OP_MOD = 3 does `F1 = 0x01` (HP) and `F2 = 0x01` appear and the next state become 0x2F8C?
3. Same with W8 changing from 0x0400 to 0x0100 between ticks 5 and 14: expect code1 4, code2 1, finish via sub 5 with class code 1, `HP_P1_SAT = 1` (phase 10), no power-up. Confirm.
4. AF read-back bit 7: if the simulator returns 0x80 for AF reads, expect sample 0xFFFF and class 7 (SAT bits 6:4 = 7). What drives AF.7 on hardware (U)?
5. Monitor: raise `A(0) = 0x08` in state 0x0B: expect state ptr 0x4EEF, code 2, FAULT_EVT 0x10, SAT_P0 = 0x06, CNT_MPS +1, SAT_PWR bits for ch0 cleared.
6. Monitor with SFR F3 bit 0 = 0 for 4 consecutive runs: expect CNT_DENIED +1 and fault state on the 4th run only; with F3 bit 0 = 1 expect `[0x8120] = 0`? (stock never resets it on a good pass: confirm the counter keeps its value).
7. Which hardware condition sets `A bit 1` (acknowledged by the monitor) and `A bit 5`: no ISR code writes them; does any simulator stimulus of CHn_EVENT bits produce them?
8. Pass period of the periodic ISR (cluster 5): does the 18-pass HP schedule finish inside the 20-timer-tick class window (`st_class_gate` timer 0x14)? Measure passes per timer tick in the simulator.
9. v18 vs v21 auto mode: OP_MOD = 3, HP_EN = 1, class 4 PD: v21 expected to run AF 1, 2, 1, 2 and power up from the mux; v18 expected to skip the second event (hp = 0) and finish at tick 10 without power-up. Confirm by running both images.
10. Detect code 6 vs open: with all `D(n)` readings giving the open-circuit result, does `0x3651` always return 6 (SAT 0x06), or can 9 (mapped to 6 at 0x37B9) / other codes appear? (cluster 1 owns the D9..DB words).
