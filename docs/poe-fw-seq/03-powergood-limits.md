# BCM59111 stock firmware: power-up, power good, limits, fault / disconnect (cluster 3 of 6)

Offline, no board (#26). Images: v21 `hwdb/firmware/bcm59111-v21.bin` (Ubiquiti, what the US-48 runs), v18 `bcm59111-meraki.bin`. CPU addresses (base 0x1A00). Code: `poe-fw/src/seq_power.c` (+ `.h`, `hal_analog_seq3.c`), not wired, `-DSEQ_V18` = v18 variant. Tools: `scripts/poe_fw_dis.py`, `scripts/poe_fw_asm_range.py` (listing range), `scripts/poe_fw_seq_map.py` (v21 -> v18 function map, `tmp/poe-fw/seq_map.tsv`).

- Tags: **C** confirmed by code bytes (address cited), **C-both** identical bytes in v21 and v18, **I** inferred meaning, **U** unknown (section 9).
- Tick = 10 ms (I, docs/poe-fw.md section 17). All delays below are ticks of `tick_timers` 0x3C1E.

## 0. Corrections to earlier docs (all C)

- `arm_channel` R3 is **not a delay**. It is stored at XRAM `0x8077 + 0x12 ch` and never counted down (`tick_timers` touches only +0x7E and +0x81 per channel). Readers: 0x3555 (`== 0x0B`), 0x3A54 (`== 7`). It is a phase code. docs/poe-fw.md sections 11 / 17 "arm delays" are phase codes; real delays are `set_timer` / timer A only.
- The 20-tick timer at 0x4FB7 (`st_poweron_hold`) is **not** a power-up hold: it is the pause after a denied power request (semi-auto, no host request within 37 ticks), then `st_port_cfg`. The rewrite's "power-up hold 20 ticks" has no stock equivalent in v21.
- `st_power_good` 0x2F8C is not a delay state: it waits for a hardware event and reads SFR F3. 0x30C3..0x3126 are the middle of 0x2F8C (Ghidra split it into "host_30EF"). 0x316B starts right after its `RET` at 0x3168.
- F2 is not "power enable clear": it is a per-channel HP-mode bit (I), set before F1 (section 5). B(ch).bit2 is set in `st_port_cfg` and cleared at the end of 0x2F8C (the auto trigger, section 5).

## 1. Per-channel block (XRAM `0x8077 + 0x12 ch`, ch 0..3; C unless noted)

| Off | Addr ch0 | Name | Use |
|---|---|---|---|
| +0 | 0x8077 | code | phase code written by `arm_channel` R3 (C) |
| +1 | 0x8078 | ev_hi | channel ISR flags (below) |
| +2 | 0x8079 | ev_lo | channel ISR flags (below) |
| +3 / +4 | 0x807A / 0x807B | pend_a / pend_b | pend_b: 0x01 detect req (cleared 0x4601), 0x02 class req (0x41D9), 0x04 power-on req (0x3FD9), 0x08 power-off req, 0x10 host shutdown (set 0x2DDD); setters = push-button regs / monitor (I) |
| +5 / +6 | 0x807C / 0x807D | wa / wb | wait flags, ORed by timer expiry |
| +7 / +8 / +9 | 0x807E / 0x807F / 0x8080 | timer A cnt / mask -> wa / mask -> wb | 0x3C1E expiry ORs masks into wa / wb, sets service flag |
| +0xA / +0xB / +0xC | 0x8081 / 0x8082 / 0x8083 | timer B cnt / mask -> wa / mask -> wb | `set_timer` 0x5024 (R5 ticks, R2 -> wa, R3 -> wb) |
| +0x10 / +0x11 | 0x8087 / 0x8088 | state pointer | `arm_channel` R4:R5; run by main loop 0x3D1A -> `run_state` 0x50AB when service flag 0x23.(ch+1) set |
| - | 0x80DD + 2 ch | cls | bits 2:0 phase (2 after power good, 5 after fault), 5:3 class code, 7:6 probe field (1 probing, 2 class 4 seen at first sample, 0x29EE) |
| - | 0x80DE + 2 ch | flg | 0x20 HP / F2 on, 0x04 powered, 0x02 power good seen, 0x01 cleared by 0x1F4C (class event); v18 0x08 hold |
| - | 0x8120 + ch | pg_miss | consecutive monitor passes with F3 bit clear |
| - | IRAM 0x41 | op shadow | OP_MOD copy, 2 bit / channel, refreshed ONLY by 0x2EE0 and 0x44ED (0x482F); read by 0x4C33 |
| - | IRAM 0x42 + ch | sat shadow | SAT_Pn copy: bits 3:0 detect, 6:4 class |

Channel ISR (`isr_chan<c>` hook 4..7, 0x4255 for ch0) decodes SFR BC+ch (write-1-clear) into flags (C-both, 0x422E in v18):

| SFR BC bit | Flag set | Used by |
|---|---|---|
| 7 | ev_hi 0x40 | 0x2F8C fault, monitor overload (I: overload / ICUT trip) |
| 6 | ev_lo 0x01 | 0x4601 detect request / grant (I) |
| 4 | ev_lo 0x04 | **0x2F8C: hardware power event** |
| 3 | ev_hi 0x10 | monitor 0x2CEE branch (short, I) |
| 2 | ev_hi 0x08 | monitor 0x2D2F branch (disconnect, I) |
| 1 (and EV95.1) | ev_hi 0x01 | 0x2F8C fault, monitor overload |
| 0 | ev_hi 0x80 | 0x2F8C fault |
| any | service flag 0x23.(ch+1) | main loop |

Other writers: class mux 0x29EE ORs ev_lo 0x02 (0x2C4D); chan_event_latch 0x3DA6 ORs ev_lo 0x40. Sources of ev_hi 0x20 and ev_lo 0x10 / 0x20 not found (U).

## 2. Function inventory (v21 address / v18 address)

| v21 | v18 | Name | Role | Inputs | Outputs / side effects | v18 |
|---|---|---|---|---|---|---|
| 0x49FF | 0x4AA7 | arm_channel | set service flag; state := R4:R5 if != 0; code := R3 if != 0 | R7 ch, R4:R5, R3 | block +0, +0x10/+0x11 | C-both |
| 0x5024 | 0x5067 | set_timer | timer B | R7 ch, R5 ticks, R2 -> wa, R3 -> wb | +0xA..+0xC | C-both |
| 0x5065 | 0x50B7 | timer A set | cnt := R5, mask wb 0x80 | R7, R5 | +7..+9 | C-both |
| 0x3C1E | 0x3C58 | tick_timers | decrement timers A, B; expiry ORs masks | - | wa, wb, service flags | C-both |
| 0x4AD7 | 0x4B47 | st_port_reset | power off (0x2143), cls &= 0xC0, flg &= ~4, SAT shadow &= 0x80, SAT_Pn := shadow | ch | F2, F1; arms 0x2EE0 | C-both |
| 0x2EE0 | 0x1A60 | st_port_shutdown | clear ev, wa/wb, timer A; refresh op shadow; clear FAULT_EVT / TS_EVT bits of this channel (0x11 << ch) and INT 0x04 / 0x20 / 0x40 when empty | ch | host regs 0x06..0x09, INT; arms cfg or clears pend (mode 0) | C-both |
| 0x44ED | 0x447C | st_port_cfg | power off, clear ev / wa / wb / timer B, ack HW events (0x4CE4), op shadow, B.bit7 := 0, B.bit2 := 1, C9 |= bit, B.5:4 := 0, F2 off, flg &= ~0x26, CM / VM regs := 0 | ch | SFRs B, C9, F1, F2, BC, EV95; arms 0x454A | v18 also flg &= ~0x08 (0x44CD) |
| 0x454A | 0x45AA | st_mode_select | mode 0 -> reset; mode 1 with request -> power request; else detect arm | ch | pend_b &= ~8 if no request | C-both |
| 0x3FBE | 0x4092 | st_power_request | see section 4 | ch | arms 0x4A31, 0x4AD7, 0x4EEF, 0x4FB7; 0x4C62 | C-both (constants relocated) |
| 0x4C62 | 0x4CA2 | pwr_enable_evt | SAT_PWR |= bit(ch), PWR_EVT |= bit(ch), INT.0 | ch | host regs | C-both |
| 0x4A31 | 0x48D7 | st_power_on | **power-up**: F2 then F1 (section 5) | ch | F2, F1; arms 0x2F8C | differs |
| 0x2F8C | 0x2F8C | st_power_good | section 6 | ch | many | differs |
| 0x4D5C | 0x4DC4 | st_fault_shutdown | F1 &= ~bit(ch) only; cls := (cls & F8) \| 5; timer B 75 ticks wb 0x20; arms 0x4E9D | ch | F1 | C-both |
| 0x4E9D | 0x4EE0 | st_fault_cooldown | wb.5 -> arm idle (code 2) | ch | - | C-both |
| 0x4F9F | 0x4FE2 | st_disconnect_cleanup | timer A cancel; timer B 30 ticks wa 0x40; arms 0x4E60 | ch | - | C-both |
| 0x4E60 | 0x4EA3 | st_wait_b6 | wa.6 -> arm idle (code 2) | ch | - | C-both |
| 0x4EEF | 0x4F32 | st_idle | timer B 60 ticks wa 0x80; state := 0x4E80 | ch | - | C-both |
| 0x4E80 | 0x4EC3 | st_wait_b7 | wa.7 -> arm 0x44ED (code 2) | ch | - | C-both |
| 0x4FB7 | 0x4FFA | st_poweron_hold | timer B 20 ticks wa 0x80; state := 0x4E80 | ch | - | C-both |
| 0x5091 / 0x509E | 0x50E3 / 0x50F0 | st_retry_wait_a / b | timer B 200 ticks wb 0x02; state := 0x4F09 / 0x4BA2 | ch | - | C-both |
| 0x4F09 | 0x4F4C | st_wait_b1 | wb.1 -> arm idle (code 2) | ch | - | C-both |
| 0x4BA2 | (v18 0x4C12) | st_wait_b3 | wb.1 -> timer B 200 wb 0x08; wb.3 -> arm 0x5091 | ch | - | C-both |
| 0x4F3D | 0x4F80 (I, JNB ACC.6 at 0x4F85) | st_wait_b6b | wb.6 -> arm 0x3FBE (code 0) | ch | - | I (not byte-checked in v18) |
| 0x48BE | 0x4966 | st_detect_invalid | counter 0x8060+4c ++; (MS_BO_EN bit and SAT detect != 6) -> 0x5091 code 0x10 else idle | ch | - | C-both |
| 0x3334 | 0x336D | calc_limit | section 7 | ch | R7 = limit byte | differs (class lookup) |
| 0x4B71 | 0x4BE1 | update_chan_limit | A(ch) := calc_limit(ch) (SFR A1..A4) | ch | A | C-both |
| 0x4ED5 | 0x4F18 | set_chan_sfr_a | A(ch) := R5 | ch, R5 | A | C-both |
| 0x46FC | 0x475A | class defaults | host ICUT := 0x4C13[class], ILIM := 0x4C1B[class] | ch, class | host regs 0x47+5c / 0x48+5c | C-both |
| 0x4BD3 | (none) | class code | (cls >> 3) & 7; class 4 -> 0 unless HP allowed (0x4A69) | ch | R7 | v21 only HP check |
| 0x45A7 | 0x4607 | hp_enabled_default | auto: 0x4C0B[class]; else ILIM(ch) != 0x80 | ch | R7 | differs (class code) |
| 0x4A69 | 0x4AD9 | hp_allowed | HP_EN bit(ch) and HP_MOD(ch).0 | ch | R7 | C-both |
| 0x5055 | 0x5098 | F2 update | 0x8129 := hp_default; 0x4DD0 | ch | F2, flg.5 | C-both |
| 0x4DD0 | 0x4D4E | F2 write | R5 != 0: F2 \|= bit(ch), flg \|= 0x20; else F2 &= ~bit, flg &= ~0x20 (EA off) | ch, R5 | F2 | C-both (v18 inline table) |
| 0x493F | 0x49E7 | ctrl bit7 | R5 != 0: B(ch) \|= 0x80 else &= 0x7F | ch, R5 | B1..B4 | C-both |
| 0x497E | 0x4A26 | ctrl bit2 | R5 != 0: B(ch) &= 0xFB (**trigger**) else \|= 0x04 | ch, R5 | B1..B4 | C-both |
| 0x4AA1 | 0x4B11 | ctrl field | B(ch) &= 0xCF; B(ch) \|= (R5 << 4) & 0xF0 | ch, R5 | B1..B4 | C-both |
| 0x4FF6 | 0x5039 | gate | R5 != 0: C9 &= ~bit(ch) (sense on) else C9 \|= bit | ch, R5 | C9 | C-both |
| 0x5074 | 0x50C6 | pg read | (SFR F3 & bit(ch)) != 0 | ch | R7 | C-both |
| 0x2143 | 0x2155 | power off | EA off; F2 &= ~bit(ch); F1 &= ~(0x11 << ch); EA on | ch | F2, F1 | C-both |
| 0x4658 | 0x46B6 | publish class | shadow bits 6:4 := cls bits 5:3; SAT_Pn := shadow (+ MISC_CONF.2 compare / event 0x4E3D) | ch | SAT_Pn | C-both |
| 0x482F | 0x488D | apply op mode | op shadow field := host OP_MOD field | ch | IRAM 0x41 | C-both |
| 0x4C33 | 0x4C73 | opmode | shadow field | ch | R7 | C-both |
| 0x2C79 | 0x2CA6 | monitor | cluster 2; arming interface in section 3 | ch | - | C-both (one dead `MOV R0`) |
| 0x41D9 / 0x4601 / 0x46AB / 0x3B3C / 0x448D / 0x47E4 / 0x4DF5 / 0x4F23 | 0x4396 / 0x465F / 0x4709 / 0x3B76 / 0x454A / (not located) / 0x4E38 / 0x4F66 | class gate / detect start / detect arm / detect result / class eval / class done / chan enable / detect ok wait | clusters 1 / 2; only the hand-over to this cluster is used here | - | - | C-both except 0x41D9 (EA bracket around 0x8103 RMW missing in v18, same logic) |

## 3. Channel state machine

Handlers are entered by the main loop whenever the service flag is set (ISR event, timer expiry, `arm_channel`). Waiting states test their wait flag and return.

| State (addr) | Condition | Action | Next state | Timer / code | 802.3at window implemented |
|---|---|---|---|---|---|
| port_reset 4AD7 | entry | power off (F2, F1 both nibbles), clear class / phase / SAT_Pn | port_shutdown 2EE0 | code unchanged | - |
| port_shutdown 2EE0 | entry | clear events, op shadow := OP_MOD, clear fault / TS bits | port_cfg 44ED, code 2 (mode != 0); mode 0: stays, clears pend | - | - |
| port_cfg 44ED | entry | power off, ack HW events, B.7 := 0, **B.2 := 1**, C9 bit := 1, B.5:4 := 0, F2 off, CM / VM := 0 | mode_select 454A | - | - |
| mode_select 454A | mode 0 | - | port_reset, code 1 | - | - |
| | mode 1 and pend_b.2 | - | power_request 3FBE, code 0x0A | - | - |
| | else | - | detect_arm 46AB, code 3 (cluster 1) | - | - |
| detect good (3B3C, cluster 1) | detect nibble 4 | timer A 37 ticks -> wb.7; class gate | class_gate 41D9, code 8 | 37 ticks | request window (I) |
| class_gate 41D9 (cluster 2) | class off, or mode 1 without class req | - | power_request 3FBE, code 0x0A | - | - |
| | else | 0x8103 \|= bit; timer B 20 ticks wb 0x04 | class_done 47E4 | 20 ticks | Tclass measure window (I) |
| power_request 3FBE | mode 0 | - | port_reset, code 1 | - | - |
| | mode 1, no pend_b.2 | - | idle 4EEF, code 2 | - | - |
| | mode 2, no pend_b.2, wb.7 set | wb.7 := 0, PWR_DENIED counter ++ | poweron_hold 4FB7, code 2 | - | - |
| | mode 2, no pend_b.2, wb.7 clear | wait | stays | - | - |
| | mode 1 / 2 with pend_b.2, or mode 3 | timer A := 0 (mode 2, 3); pend_b.2 := 0; **0x4C62: SAT_PWR enable bit, PWR_EVT, INT.0** | power_on 4A31, code 0x0A | - | - |
| power_on 4A31 | entry | F2 := hp_default, then **F1 \|= mask (power on)** | power_good 2F8C | none (v21), v18: 8 ticks wa.0 | Tpon (<= 400 ms): enforced by hardware, no firmware timer in v21 (U) |
| power_good 2F8C | ev_hi == 0 and ev_lo == 2 | pend_b.2 := 0; publish class; 0x4C62 | stays | - | - |
| | ev_hi & 0xF1 | ev_hi &= 0x0E; code 0x0C; reg 0x62 hi nibble ++; TS_EVT low bit, SAT_PWR enable + good := 0, PWR_EVT both, INT 0x41 | fault 4D5C (pointer only, then service) | - | inrush / start-up overload (I) |
| | ev_lo.2, F3 bit clear | ev_lo.2 := 0, timer A := 0, pg_miss := 0; ev_hi \|= 1 | next pass takes the fault row | - | - |
| | ev_lo.2, F3 bit set | section 6: limits, **B.2 := 0**, SAT_PWR good, PWR_EVT, INT.1 | monitor 2C79, code 0x0B | timer B 2 ticks wa.1 | power good |
| monitor 2C79 (cluster 2) | ev_hi & 0x41 | ev_hi &= 0xBE; reg 0x62 hi nibble ++; TS_EVT hi bit; SAT_PWR := 0; PWR_EVT; INT 0x23 | fault 4D5C, code 0x0C | - | overload (ICUT, tOVLD) in hardware (I) |
| | ev_hi.4 | ev_hi &= 0xEF; FAULT_EVT low bit; reg 0x62 lo nibble ++ | fault 4D5C, code 0x0D | - | short (I) |
| | ev_hi.3 | FAULT_EVT bit 0x10 << ch (disconnect), MPS_ABSENT counter ++, SAT_Pn := 6; INT 0x07 | idle 4EEF, code 2 | - | Tmpdo / disconnect (hardware detects, I) |
| | ev_hi.5 or ev_lo & 0x30 | SAT_PWR := 0, PWR_EVT, INT 0x03 | disconnect_cleanup 4F9F, code 0x0E | - | - |
| | host OP_MOD field == 0, or pend_b & 0x18 | **F1 &= ~bit(ch)**, SAT_Pn := 0, SAT_PWR := 0, PWR_EVT, INT 0x03 | disconnect_cleanup 4F9F, code 0x0E | - | host power off |
| | else F3 bit clear | pg_miss ++; at >= 4: reg 0x61 (PWR_DENIED) ++, TS_EVT hi bit, SAT_PWR := 0, INT 0x23 | fault 4D5C, code 0x0C | - | power-good loss (I) |
| | else wa.1 and flg.1 | flg.1 := 0, **B.7 := 1**, CM / VM publish, timer B 2 ticks wa.1 | stays | 2 ticks | - |
| fault 4D5C | entry | **F1 &= ~bit(ch)** (low nibble only), cls phase := 5 | fault_cooldown 4E9D | timer B 75 ticks wb.5 | 750 ms cool-down |
| fault_cooldown 4E9D | wb.5 | - | idle 4EEF, code 2 | - | - |
| idle 4EEF | entry | - | wait_a7 4E80 | timer B 60 ticks wa.7 | 600 ms |
| wait_a7 4E80 | wa.7 | - | port_cfg 44ED, code 2 | - | power off + re-detect |
| disconnect_cleanup 4F9F | entry | timer A := 0 | wait_a6 4E60 | timer B 30 ticks wa.6 | Tmpdo 300 ms (I) |
| wait_a6 4E60 | wa.6 | - | idle 4EEF, code 2 | - | - |
| poweron_hold 4FB7 | entry | - | wait_a7 4E80 | timer B 20 ticks wa.7 | denial pause |
| detect_invalid 48BE | entry | invalid counter ++ | retry_a 5091 code 0x10 (MS_BO_EN bit and detect != 6) else idle code 2 | - | - |
| retry_a 5091 / b 509E | entry | - | wait_b1 4F09 / wait_b3 4BA2 | timer B 200 ticks wb.1 | re-detect spacing 2 s |
| wait_b1 4F09 | wb.1 | - | idle 4EEF, code 2 | - | - |
| wait_b3 4BA2 | wb.1 | timer B 200 ticks wb.3 | stays | 200 | - |
| | wb.3 | - | retry_a 5091, code 0 | - | second 2 s spacing |

Totals (v21, ticks of 10 ms, I for the tick): fault -> power off immediately (F1 low bit), then 75 + 60 = 135 ticks (1.35 s) until `port_cfg`; then mode_select -> detect_arm -> detect (cluster 1). Disconnect via monitor ev_hi.5 path: 30 + 60 = 90 ticks to `port_cfg`. Disconnect via ev_hi.3: 60 ticks. Invalid signature with MS_BO_EN: 200 + 60 ticks.
Where hardware power-off on disconnect (ev_hi.3 / ev_hi.5 paths) happens is U: no F1 / F2 write on those two paths until `port_cfg` runs 2143 (C). Host-requested off and fault clear F1 explicitly (C).

## 4. st_power_request (0x3FBE) conditions

mode from the op shadow (0x4C33): 0 shutdown, 1 manual, 2 semi-auto, 3 auto (C). Powering requires: auto, or (mode 1 / 2 and pend_b.2). Manual without request goes idle. Semi-auto without request waits for the 37-tick timer A set at detect good (0x3BDE..0x3BE6, C), then denies (counter 0x61 ++, 20-tick pause, re-run from `port_cfg`). v18 identical (C-both).

## 5. Power-up SFR sequence (safety)

**The write that turns on the port power is `ORL F1, mask` (SFR 0xF1).** v21 sites: 0x4A52 (state `st_power_on`, all classes) and 0x2BBB / 0x2BCF (class mux 0x29EE, class-4 path, cluster 2). v18: 0x48F7 / 0x4904 and 0x2BC2 / 0x2BD4. No other write ORs F1 (C, grep of all `ORL 0xF1`). That F1 is the FET enable is I (evidence: raised on every power-up path, cleared on fault / host-off, SAT_PWR enable bit set just before, hardware power-good event follows).

Route B (state machine, every class), driver config HP_EN = 0x0F (HP_EN high nibble 0):

1. `port_cfg` (before): F1 = F2 = 0 (0x2143, order F2 then F1), A(ch) unchanged (init 0xF0, 0x39FE), B(ch): bit7 = 0, bit2 = **1**, bits 5:4 = 0, C9 bit = 1 (sense gate off) (C).
2. `power_request` 0x4C62: SAT_PWR |= bit(ch) (enable status), PWR_EVT, INT.0. No analog write.
3. `power_on` 0x4A5E -> 0x5055 -> 0x4DD0: **F2 := hp_default** (auto: class 4 -> 1, else 0; else ILIM != 0x80). EA off inside.
4. 0x4A52: EA off, **F1 |= (HP_EN high bit of ch) ? bit(ch) : 0x11 << ch**, EA on (driver config -> both nibbles). **54 V on.**
5. Next service pass `power_good` 0x2F8C waits (no timer, no write) until channel event BC.4 sets ev_lo.2.
6. 0x2F8C on ev_lo.2: reads SFR F3 bit(ch); clear -> fault path. Set -> C9 &= ~bit (if DIS_SEN_EN bit), then auto: F2 := 0x4C0B[class], A(ch) := 0x4C03[class], B.5:4 := 0, host ICUT / ILIM := 0x4C13 / 0x4C1B[class]; non-auto: F2 := hp_default, B.5:4 := 0, A(ch) := calc_limit; then **B(ch) &= ~0x04 (auto trigger, 0x3126)**, SAT_PWR |= 0x10 << ch, PWR_EVT, INT.1, timer 2 ticks.
7. ~2 ticks later the monitor sets B(ch).7 (0x2EAF).

So the limit byte A(ch) is written AFTER power good, not before F1. During start-up A(ch) holds its earlier value (init 0xF0 = the class 0 / 3 table value, the largest entry). Hardware inrush control is U.

Route A (class mux 0x29EE, class-4 PD, HP allowed, mode 3 or pend_b.2, two equal class samples at ticks 5 and 14): F2 |= bit (0x2258), F1 |= (HP_EN high bit ? bit : 0x11 << ch), code 0x0A, state := 0x2F8C (C, 0x2B87..0x2BF6). The board's class-4 camera (evidence: ICUT 0x11, SAT_PWR 0x88 / 0x22) is consistent with route A or B: both end in section 6 step with class 4.

Evidence match (stock, class-4 camera): ICUT reg (0x4C ch1, 0x56 ch3) := 0x11 = 0x4C13[4], ILIM := 0xC0 = 0x4C1B[4] (written 0x46FC, C); HP_Pn_SAT (0x4E, 0x58) = 1 written by 0x2C12 (cluster 2) when cls bits 7:6 == 2 (class 4 seen at first sample) (C code, I meaning); SAT_PWR 0x22 / 0x88 = enable bit (0x4C62) + good bit (0x2F8C step 6). Unpowered channels keep the driver's 0xE2 / 0x00 because 0x46FC and 0x4DAA run only on a powered path (C).

## 6. power_good (0x2F8C) detail

Order of host-visible writes in step 6 (C): C9; F2; A; B.5:4; ICUT / ILIM; B.2 := 0; PWR_EVT (and shadow 0x8003); SAT_PWR good bit; INT.1; INT pin if INT_MASK.1 and MISC_CONF.7; timer. Fault branch order: reg 0x62 hi nibble; TS_EVT (+ shadow 0x8009) low bit; SAT_PWR (enable and good) cleared; PWR_EVT both bits; INT |= 0x41 (PWR_EN, TSTA); INT pin. After the fault branch the function continues to the ev_lo.2 test in the same pass (C, 0x3050 falls into 0x3053); both can be taken in one pass (U: whether that happens in practice).

## 7. Limits: ICUT / ILIM / HP regs -> A1..A4

Per channel c (stride 5): HP_MOD 0x46+5c, ICUT 0x47+5c (`HP_Pn_OV_CUT`), ILIM 0x48+5c (`HP_Pn_CL_FBC`), HP status 0x49+5c (written by firmware). A1..A4 = SFR 0xA1 + c, byte.

calc_limit 0x3334 (C, arithmetic; v18 0x336D C-both except class lookup):

| mode | ILIM == 0x80 | ICUT bit 6 | v = ICUT[5:0] clamp | A = |
|---|---|---|---|---|
| 3 (auto) | - | - | - | 0x4C03[class code] |
| other | yes | 0 | 10 | 24 v (0x3496) |
| other | yes | 1 | 21 | 12 v (0x34AF) |
| other | no | 0 | 21 | 12 v |
| other | no | 1 | 42 | 6 v (0x3478) |

Tables (C-both, v18 0x4C43 / 4C4B / 4C53 / 4C5B): index = class code 0..7 (SAT_Pn encoding: 1..4 = class 1..4, 6 = class 0, 7 = above class 4).

| Table | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
|---|---|---|---|---|---|---|---|---|
| A limit 0x4C03 | F0 | 48 | 84 | F0 | CC | F0 | F0 | F0 |
| F2 enable 0x4C0B | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| ICUT default 0x4C13 | 54 | 46 | 4B | 54 | 11 | 54 | 54 | 54 |
| ILIM default 0x4C1B | 80 | 80 | 80 | 80 | C0 | 80 | 80 | 80 |

Consistency check (C): `calc_limit(ICUT default, ILIM default)` reproduces the A table for every class (0x54 = bit6, v 20, ILIM 0x80: 12 x 20 = 0xF0; 0x46: 12 x 6 = 0x48; 0x4B: 12 x 11 = 0x84; 0x11 with ILIM 0xC0: 12 x 17 = 0xCC). The driver cold values ICUT 0xE2 (v = 34, bit 6) with ILIM 0xC0 give 6 x 34 = 0xCC, the same as class 4.
Unit of A in mA: U (no datasheet value; board experiment: read back current at known load or hardware limit trip).
Host writes while powered (poll_common 0x2607..0x2693, C): ICUT dirty flag (0x21.5, 0x21.6, 0x21.7, 0x22.0) with SAT_PWR good bit set -> A := calc_limit; ILIM dirty flag (0x21.1..0x21.4) with good set -> F2 := hp_default, A := calc_limit. Channel index comes from XRAM 0x80BF / 0x80C1 / 0x80C2 / 0x80C3 (I: constants 0..3; 0x80C0 is the event mask).
The F-registers: F2 = hp_default (class 4 in auto, or ILIM != 0x80 otherwise) (C); F1 mask = HP_EN high-nibble bit ? low bit : both (C). Meaning of F2 / F1 nibbles U.

## 8. v21 vs v18

Function-level map `tmp/poe-fw/seq_map.tsv` (normalisation masks state constants and table addresses; tables and constants were then compared by hand). 38 of the 52 mapped entries are byte-identical after masking; the 14 others were diffed by hand (this table). State pointers: v21 -> v18 constants differ only by address; same graph.

| Item | v21 | v18 | Follow for US-48 |
|---|---|---|---|
| st_power_on F2 | F2 := hp_default before F1, always (0x4A5E) | F2 written only in the HP_EN-high branch (0x48EB); common branch leaves F2 = 0 | v21 |
| st_power_on hold | none; event-driven | common branch: flg.3 := 1, timer B 8 ticks wa.0 (0x4912) | v21 |
| 0x2F8C hold handling | ev_lo.2 acts at once | extra branch: wa.0 set -> clear, flg.3 := 0, ev_lo \|= 4 (0x305C..0x3074); ev_lo.2 ignored while flg.3 set (0x3092..0x309E) | v21 |
| Class-4 route A (0x29EE / 0x29E5) | F2 |= bit, F1 \|= pair (0x2BBB / 0x2BCF); no hold | HP_EN-high: F2 \|= bit, F1 \|= bit; else F2 &= ~bit, F1 \|= pair, flg.3, timer B 8 ticks wa.0 (0x2BC6..0x2BF4) | v21 |
| Class code for limits / F2 | 0x4BD3: class 4 -> 0 unless HP allowed | raw (cls >> 3) & 7 (0x2217, 0x3118) | v21 |
| st_port_cfg | - | also flg &= ~0x08 | n/a (v21 has no flg.3) |
| st_class_gate | EA bracket around 0x8103 RMW | none | same logic |
| Monitor 0x2C79 | - | identical (one dead instruction) | same |
| Limit tables, calc_limit, fault / disconnect handlers, timers, codes | - | identical | same |
| `DEFAULT_AF_AUTO_MODE`, FW_REV | v21 | not present | v21 |

Strong evidence from equality (C-both): all timers (37, 20, 2, 30, 60, 75, 200 ticks), codes, tables, calc_limit arithmetic, F1 / F2 / B / C9 / A access order, fault / disconnect arming. The two images differ only in HOW power good is awaited: v18 = fixed 8-tick hold then the F3 check, v21 = hardware event then the F3 check. v21 also adds the class-4 downgrade.

## 9. Open questions (answer with the simulator, ucsim `-t RL6449`, SFR stimulus, stock image)

1. U1 Does v21 ever leave `power_good` without BC.4? Run: arm channel, power_request -> power_on, never raise BC.4; watch state pointer (XRAM 0x8087+) for 1000 ticks. Expect: unchanged (no timer). If it moves, a timer exists that was missed.
2. U2 Which SFR bits precede BC.4 in real hardware: does F3 bit(ch) rise before BC.4 on success, and does BC.4 also fire on failure? Sim: set F3 bit, raise BC.4 -> expect step 6; clear F3, raise BC.4 -> ev_hi |= 1 then fault row; log SFR writes in order.
3. U3 Is the 0x2F8C fault row plus ev_lo.2 row in one pass reachable (both ev_hi & 0xF1 and BC.4)? Sim: raise BC.7 and BC.4 together; record final state pointer and SAT_PWR.
4. U4 Meaning of B(ch).bit2: set in cfg, cleared after limits, bit7 set 2 ticks later. Sim cannot answer; the sim can only show the exact write order and values (list SFR B writes during a power-up with F3 / BC.4 stimulus); hardware needed for meaning.
5. U5 Do the disconnect paths (monitor ev_hi.3 / ev_hi.5) ever write F1 / F2 before `port_cfg`? Sim: raise BC.2 while powered; record F1 / F2 until `port_cfg`.
6. U6 Source of ev_hi 0x20, ev_lo 0x10 / 0x20 and pend_b 0x08: sim: write push-button regs 0x18 / 0x19 / 0x1A, `5E` / `5F`, OP_MOD, DIS_SEN_EN; diff block fields.
7. U7 Role of IRAM 0x24 (channel remap through XRAM 0x8125+ch, `MOV 0x24,#1` at 0x231C): sim: write regs 0x5E / 0x5F with values, check IRAM 0x24 and the `cur_chan` mapping.
8. U8 Unit of A (mA per count) and effect of A written after F1: hardware only; sim records A before and after.
9. U9 pg_miss timing: monitor passes per second while powered (service flag sources) to give the 4-miss window in ms: sim: count monitor entries per tick with and without events.
10. U10 Class-4 route A vs B for the camera: sim with class ADC words for class 4 and HP_EN = 0x0F: which of 0x4A52 / 0x2BBB / 0x2BCF executes first and whether `st_power_on` also runs.
11. U11 Does the power-request timer A (37 ticks) interact with auto mode (cancelled at 0x4013 before power_on)? Sim: mode 3 detect good, check timer A cnt.

## 10. What the rewrite must change (v21 target)

- Replace the 20-tick "power good" hold in `chan.c` by section 5 / 6: F2 then F1; wait for BC.4; read F3; then limits and B.2 := 0.
- A(ch) from calc_limit / tables after power good; host ICUT / ILIM re-applied only while good bit set.
- `-nopower` bench image: keep `hal_power_on` empty; nothing else in this cluster writes F1.
- SFR F3 and BC.4 need simulator support first (U2).
