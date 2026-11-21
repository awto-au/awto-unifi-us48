# Cluster 4: offset trim, detect classification, measurement (BCM59111 stock v21, v18 compared)

- Offline, no board. Addresses = CPU code addresses (v21). Tags: C = code bytes (`scripts/poe_fw_dis.py`), I = inferred, U = unknown.
- Port: `poe-fw/src/seq_trim.c` + `poe-fw/inc/seq_trim.h`. Compile check: `python3 scripts/poe_fw_build.py --seq-compile`. `POE_FW_SEQ` is default off; the normal images are unchanged (source built with and without these changes gives identical bytes; the committed `hwdb/firmware/bcm59111-awto*.bin` were already stale vs the source before this work).
- Corrections to the names TSV / earlier docs (C):
  - 0x37D2 does not write analog regs (reads AB=0x2C and 0x30 only). It is temperature only, not counters.
  - 0x3651 is not scaling: it is the detect-signature classifier (4 samples from D9..DB).
  - The 0x80E5+2c accumulators belong to 0x1B03, not 0x34D0.
  - Counters 0x8060..0x806F are not touched by this cluster's functions (sites in section 8).

## 1. Detect flow: the SFR accesses that produce the detect code (top finding)

Order, all C unless noted (channel block `0x8078 + 0x12c`, +1 = event byte, +5 = timer-expiry byte):

1. 0x4601 `st_detect_start`: only if bit 6 of block+1 is set (cleared here). `D(c) = 1` (SFR D1..D4). `set_timer(c, timer 1, 0x32 = 50 ticks)`. State pointer := 0x1A60. Block+3 bit 0 cleared.
2. Hardware runs the detection. On completion the channel event SFR (BC / BD / BE / BF for c = 0..3) bit 6 is raised; the channel ISR (0x4255.. for ch0: `JNB ACC.6` at 0x4269) acks it (`BC = 0x40`) and sets block+1 bit 0 (0x426F..0x4275). Same for the other channels at stride 0x12.
3. 0x1A60 `st_detect_wait` (runs on every service of the channel):
   - If block+5 bit 0 (timer 1 expired: no event within 50 ticks) is set: clear it, `D(c) = 2` (0x1A83..0x1A92), clear the detect nibble of IRAM 0x42+p, copy to SAT_Pp, raise the detect event (0x4E1A), arm 0x4EEF (idle) with delay 2. This is the timeout path, no result is read.
   - Else if block+1 bit 0 (hardware event) is set: clear it, arm 0x3B3C `st_detect_result` with delay 4 (0x1AF0..0x1AF6).
4. 0x3B3C `st_detect_result`: `raw = D(c)` (switch on logical c: D1, D2, D3, D4), `nib = raw >> 4`, `code = 0x3651(c, nib)` (section 2), then publish (section 3).

- Why this matters for the rewrite: `chan.c` `ST_DETECT_PHASE2` takes the timeout path as the normal path (writes D = 2 after the 50 ticks, then reads D 4 ticks later). A read of D after `D = 2` giving nibble 0 (counter 0x64 = 5, SAT 0) is what that sequence produces; stock reads D only after the hardware event bit (BC.6 -> block+1.0). I (the ISR event is set by hardware; whether our `chan_isr.s` sets `ev_lo` bit 0 for BC.6 must be checked in cluster 1/5).
- Raw D values for a real PD / open port: not recoverable from the stock dumps (they hold only SAT_Pn: open 0x06, camera 0x44). From the code: nibble 1..5 / 7 passes through 0x3651 unchanged, 9 -> 6, 0xF -> software classification (section 2), the camera result 4 can only come from the hardware nibble 4 directly (the 0xF branch never returns 4; only the HP_Pn_MOD overrides can). Default HP_Pn_MOD = 0x01 (bit 0 only), so camera: raw high nibble 4; open port: raw high nibble 6, or 9, or 0xF with `C - D >= 92` or `C >= 853`. See U-items.

## 2. 0x3651 `detect_classify(R7 = c, R5 = nib)` (C arithmetic, I code names)

- Samples: `A,B,C,D = word(D9 = 4c + 0, +1, +2, +3)`, each word: `D9 = idx; hi = DA; lo = DB` (DA read first). Order of reads in the code: A, B, C, D (0x365E, 0x366C, 0x3684, 0x3693). Nothing in the image writes the sample file; it is filled by hardware (U).
- Differences (s16): `AC = A-C, BD = B-D, AD = A-D, AB = A-B, CD = C-D`.
- Only if `nib == 0x0F`:
  - `CD >= 0x5C` or `C >= 0x0355` (unsigned) -> 6
  - else `BD >= 0x96` or `AC >= 0x96` -> 5
  - else `AC > 0x55 && BD > 0x55 && AD > 0x55` -> 2
  - else `AD > 3*AB + 10` (16-bit keil_IMUL, 0x373E) -> 2, otherwise -> 3
- Then `m = HP_Pn_MOD(p)` (0x8046 + 5p, via 0x4D83 masks 8 / 4 / 2):
  - `m & 8`: `code` not in {9, 1} -> 4. Else `m & 4`: `A > B > C > D` (unsigned) and `AD > 0x6E` -> 4.
  - `code == 9` -> 6. Store `IRAM[0x42+p] = (old & 0xF0) | (code & 0xF)`.
- Channels: samples and the D(c) switch use logical c; `p = remap(c)` (0x50E0: IRAM 0x24 != 0 ? `[0x8125+c]` : c; set from host regs 0x5E.7 / 0x5F at 0x2313..0x2391) goes to 0x8133, HP_MOD and IRAM 0x42+p.
- Host code names (docs/poe.md): 1 short, 2 CPD too high, 3 R-sig too low, 4 good, 5 R-sig too high, 6 open. Comparisons giving each code: 6 / 5 / 2 / 3 above; 4 = hardware nibble 4 or HP_MOD bit 3 / bit 2 override; 1 = hardware nibble 1 only (never produced by the classifier); 9 and 6 map to 6.

## 3. 0x3B3C after the classifier (C)

- `p = [0x8133]`. IRAM[0x42+p] already holds the new code (low nibble).
- MISC_CONF (0x8017) bit 6 set: compare the new nibble with `SAT_Pp & 0x0F`: equal -> `SAT_Pp = IRAM[0x42+p]` without event; different -> store and raise the detect event 0x4E1A. Bit 6 clear: always store and raise.
- 0x4E1A: `DET_CLASS_EVT (0x8004)` |= mask (table 0x4C23) with bit 0x08 per channel pair, INT handling (0x8001.3 / 0x8017.7 gates).
- If low nibble == 4 (good): `byte [0x80DE+2c] = ((byte | 1) & 0x3F) | 0x40` (this clears bit 5, the CM scale flag), block+6 = 0x25, block+7:+8 = 0x00 0x80, arm 0x41D9 `st_class_gate` with delay 8.
- Else if `HP_Pn_MOD(p) & 2` (legacy power-up): set_timer(c, 0x10, 0xFA), arm 0x4F23 delay 5 (legacy class-probe path, cluster 1: 0x316B / 0x3A30).
- Else: arm 0x48BE `st_detect_invalid` delay 15.
- 0x3A30 (`st_class_wait`) belongs to the legacy path (cluster 1); not decoded here.

## 4. Periodic order and state (C)

- ISR order (0x4EC1..): 0x1B03, 0x37D2, 0x34D0, then 0x29EE, 0x316B (others).
- Analog regs via AB: reads `AB; AC; AD`, writes `AB; AC; AD`. Trim regs: B5 select, B6 / B7 8-bit.
- IRAM: 0x49 c49, 0x4A:4B slope, 0x4C:4D off4c, 0x4E:4F temp_off, 0x5C:5D tsum, 0x5E:5F f, 0x62..65 cal_ch, 0x6A (.7 trim valid = D7.7, .0 mode), 0x25+c counters, 0x23.5 accumulate flag.
- XRAM: 0x80C5+6c {word(c|4), word(c|8), word(c)} (BE); 0x80F3+4c vm_scaled (BE16); 0x80F5+4c cm_scaled (BE16); 0x80E5+2c accumulators; 0x80F1 loop index.

## 5. 0x415C calibration (called once from 0x3CE3) (C)

- `F4 = 0`; B5=0x14: `temp_off = s8(B6)`; `FF = 0x60`; `d7 = D7`; `0x6A = d7 & 0x80`; `base = d7 & 0x3F`, bit 5 -> `base += 0xC0`; `base += 0x51` (u8); for c: B5 = 0x10+c, `cal_ch[c] = u8(base + B6)`; B5=1: `slope = s8(B7)`; B5=0: `c49 = B7`, `off4c = s8(B6)`; B5=0x15: `off4c += s8(B6)`; then IP.1 = 1, T2CON.5 = 1.
- Existing `hal_meas_calib` matches except: no 0x6A.7 flag, no IP.1 / T2CON.5 (0x41D4 / 0x41D6).

## 6. 0x1B03 offset trim, 0x37D2 temperature, 0x34D0 convert (C arithmetic; U meaning)

### 0x1B03 (every pass, c = 0..3)

- Reads: AE (once per channel), regs AB = c, c|0x28, c|0x24, trim B5 = 2c, 2c+1, 2c+8, 2c+9. Writes: AB = c|8, then AB = c|4. Side effects: IRAM 0x25+c, 0x6A.0, 0x23.5, XRAM 0x80E5+2c.
- `flag0 = 1` (0x6A.0). AE bit 2c: `acc_on = 1` (0x23.5 is cleared once per call, stays set for later channels). Else: AE bit 2c+1 -> `flag0 = 0`; and `cnt[c] = 0` in both else-cases.
- `d = word(AB=c) - (valid ? 0x100 + cal_ch[c] : 0x151)` (u16).
- If d != 0: B5 = 2c+9; `x = s8(B7) + (valid ? (flag0 ? -18 : -50) : 0)`; x != 0: `p = low16(x*d)`, S = bit 7 of (hi(d) ^ hi(x)), `A = sext16((p >> 2) | S fill)`; `y = s8(B6)` (same B5); y != 0: `B = ((S2 ? 0xFFFF : 0) << 16) | low16(y*d)` (S2 by XOR). d == 0 skips all (A = B = 0).
- `B += 0x80`. B5 = 2c+8: `u = B7 + (valid ? (flag0 ? 0x0D : 0xE0) : 0)` (u8 wrap); `A += u << 7`.
- `absv = A.31 ? (hi = ~top + CY, lo = ~byte0 + 1) : low16(A)` (top = byte 3; CY = carry out of the preceding 32-bit add; stock quirk: INC gives no carry into hi, and hi uses the top byte).
- `T = (word(AB=c|0x28) * absv) >> 6; A.31: T = -T; T += B`. `s = s8(B6) - (flag0 ? 0 : 5)`. `v = low16((T >> 8) + s)`; `v.15 -> 0`; `(v >> 8) & 0x7C -> 0x3FF`. Write AB = c|8 = v.
- Term 2: B5 = 2c+1: `x = s8(B7)`, `A = sext16(low16(x*d) >>arith 2)`; B5 = 2c: `A += B7 << 7`; `absv` uses byte 1 bit 7 (same CY quirk); `T = (word(AB=c|0x24) * absv) >> 14; A.31: T = -T; T += s32(s8(B6))`; `v2 = low16(T)`, `T.31 -> 0`, `(v2 >> 8) & 0x7C -> 0x3FF`.
- If acc_on: `n = ++cnt[c]`; n < 0x15: `acc = 0`; else `acc += v2`, n >= 0x1C: `cnt = 0; v2 = acc >> 3` (8 samples). Write AB = c|4 = v2 every pass.
- Outputs c|8 (read as CM raw) and c|4 (VM raw) by 0x34D0 in the same pass. Whether a read returns the written value: U2.

### 0x37D2 (C)

- `tsum = temp_off + word(0x2C)`; `t = tsum - 0x151`; `prod = low16(slope*t)`; `A = sext16(prod >>arith 2) + c49*128 + 0x280`; `absv` as above (byte 1 bit 7); `q = (word(0x30) * absv) >> 14; A.31: q = -q; q += s16(off4c)`; `f = q.31 ? 0 : low16(q)`; `f.10 -> 0x3FF` (only bit 10 tested).
- Published by 0x34D0 tail (0x3620): `TEMP(0x802C) = low8(tsum >> 1)`; `0x802E:2F = low16(f*11)`.
- `meas.c meas_temp` matches except the abs-idiom quirk (edge cases only).

### 0x34D0 (C)

- Per c: read `word(c|4)`, `word(c|8)`, `word(c)` into 0x80C5+6c.
- If `[0x8077+0x12c] == 0x0B` (set by st_power_good 0x30A0): `vm = low16(raw_c4 * 11)` (0x4764, b = 0x000B); `k = (byte 0x80DE+2c).5 ? 6403 : 3201` (immediates at 0x359A / 0x358C: 0x1903 / 0x0C81); `cm = low16((raw_c8 * k) >> 10)` (0x4777, EF = 0x2A). Else both 0.
- Hand-off (0x35E8): `[0x8104+7c] == 1` -> := 2, `[0x8077+0x12c] := 6`, `[0x8105+7c] = [0x8108+7c] = [0x8109+7c] = [0x810A+7c] = 0`.
- Tail: 2E/2F and TEMP as above. CM / VM host regs are not written here: `write_cm_vm_regs` 0x442B (R7 = c, R5 CM lo, R4 CM hi, R3 VM lo, R2 VM hi -> 0x8030+4c) is called from 0x2ED3 (monitor 0x2C79, path 0x2E8C: CM from 0x80F5+4c, VM from 0x80F3+4c; gated by block byte `0x807C+0x12c` bit 1, cleared on use) and 0x453F (st_port_cfg: zero).
- HP flag byte 0x80DE+2c bit 5: set at 0x2BB8 / 0x2BC4 (class-4 completion in 0x29EE, both HP_EN branches, with F2 |= bit, F1 |= bit / bit*0x11); cleared at 0x24EC, 0x2DBE, 0x2F3B, 0x3BD4 (detect good), 0x4DF1, 0x4EAB.

## 7. Math unit helpers

- 0x4764: `a (R7 lo, R6 hi) * b (R5 lo, R4 hi)` -> E9..EC (R7 = lsb); writes E9, ED, EA, EE, one NOP. 0x4777: `E9..EC = R7..R4; EF = 0x20 | R3`, 7+ NOPs, result E9..EC. Shift amounts in this cluster: 3 (0x1EE7), 6 (0x1CD3), 10 (0x35C4), 14 (0x1E29, 0x38A1). I (unit semantics), C (register use).
- 0x43BD keil_IMUL, 0x43CF / 0x43DB LLDXDATA / LSTXDATA (BE), 0x521B: register shuffling.

## 8. Counter sites (outside the four functions; owners clusters 1-3)

- +0 invalid-sig `++` at 0x48BE (u8 wrap). +1 denied `++` via 0x215F (callers 0x44D2, 0x2E5B, 0x404A). +2: high nibble `++` at 0x2FEE..0x3002 (power good) and 0x2CD0.. . +3 MPS `++` at 0x2D7C..0x2D87. Host reads clear them (0x51B6..0x5210).

## 9. v21 vs v18 (Meraki): byte-identical bodies unless noted

Method: `poe_fw_dis.py` on both images, instruction text compared with absolute jump / call targets masked (C-both = identical).

| Function / helper | v21 | v18 | Result |
|---|---|---|---|
| offset trim | 0x1B03 | 0x1B03 | C-both (650 insns) |
| convert | 0x34D0 | 0x350A | C-both (215); immediates 3201 / 6403 / 11 equal |
| detect classifier | 0x3651 | 0x368B | C-both (218); thresholds equal |
| temperature | 0x37D2 | 0x380C | C-both (220) |
| calibration | 0x415C | 0x41B1 | C-both (64) |
| math mul / shr | 0x4764 / 0x4777 | 0x47C2 / 0x47D5 | C-both |
| helpers (29: 1F5D 1F74 1FB3 1FB5 203E 2084 2086 20B5 20C4 20C5 20D3 2125 2126 2134 21A3 21A4 21B9 21D3 21D6 2213 2231 224A 37C8 43BD 43CF 43DB 4D83 51AF 521B) | | shifted (2213 -> 2221, 4D83 -> 4DEB, 51AF -> 5201) | C-both |
| write_cm_vm_regs | 0x442B | 0x44E8 | C-both |
| monitor publish block | 0x2E8C..0x2EDD | 0x2EB7..0x2F08 | C-both (47 insns) |
| channel remap | 0x50E0 | 0x5132 | C-both |
| st_detect_invalid (counter +0) | 0x48BE | 0x4966 | same; table 0x4C23 -> 0x4C63 (same 64 bytes), state constants shifted |
| state 0x0B store (power good) | 0x30A0 | 0x30D4 | same store; v18 power good has an extra branch (cluster 3) |
| HP bit 5 set | 0x2BB8 via 0x2258 | 0x2BB8 inline | same effect |

Not compared byte-wise (cluster 1): 0x3B3C / 0x4601 / 0x1A60 flow; only the 0x3651 call is covered.

## 10. Evidence check (board, stock v21, cameras delivering)

- Supply word: 0x22FA = 8954 = 814*11, 0x2310 = 8976 = 816*11 (exact multiples of 11).
- VM: ch3 0x22FA = 814*11, ch1 0x2305 = 8965 = 815*11: `vm = raw*11` consistent (8954 * 5.835 mV = 52.25 V).
- TEMP 0xA0 / 0xA1: `tsum >> 1` -> tsum 0x140..0x143 (inverse only).
- CM ch3 0x0333 = 819: `(C*3201) >> 10` with C = 262 (838662 -> 819.0), or `C*6403` with C = 131. Consistent.
- CM ch1 0x0139 = 313: not reachable by truncating `(C*k) >> 10` for k = 3201 (C=100 -> 312, 101 -> 315) or 6403 (50 -> 312, 51 -> 318). With +512 before the shift: k = 3201, C = 100 -> 313, C = 262 -> 819 still. So the shifter rounds (U1) or the CM word is not this product. `seq_trim.c` truncates; `-DSEQ_SHR_ROUND` selects rounding.
- CM ch1 / ch3 differ 2.6x with similar reported power: CM raw unit and per-channel k unknown (U3).

## 11. Deviations of poe-fw/src/meas.c / chan.c from stock

| # | Item | Stock | Ours |
|---|---|---|---|
| 1 | 0x1B03 | every pass, rewrites c|8 and c|4 words | not ported; raw words used |
| 2 | order | 1B03, 37D2, 34D0 | temp, then per channel |
| 3 | cadence | periodic ISR (hook 3), period not derived (cluster 5); 1B03 average needs 28 passes | every 10 ms tick from main loop |
| 4 | VM / CM gating | `[0x8077+0x12c] == 0x0B` | `SAT_PWR` bit |
| 5 | k selection | byte 0x80DE+2c bit 5 (set at class-4 completion, cleared at the sites in section 6) | `chans[c].flags & 2` |
| 6 | CM / VM publish | only from monitor 0x2ED3 (timer flag) and zeroed by 0x453F | every step, 0 when not powered |
| 7 | storage | 0x80F3 / 0x80F5 scaled words; raw triple 0x80C5 incl. third word | direct to host regs |
| 8 | hand-off | rec state 1 -> 2, code := 6 inside 0x34D0 | missing |
| 9 | temp abs idiom | CY quirk | exact negate (edge cases only) |
| 10 | TEMP validity | no gate; 0x3D51 tests TEMP < 0x40 from the first pass | `meas_ok` gate |
| 11 | calibration | sets 0x6A.7, IP.1, T2CON.5 | none of the three |
| 12 | detect result | 0x3651 classifier + HP_MOD overrides + 9 -> 6 | `D >> 4` raw |
| 13 | detect timing | read D after hardware event BC.6 (block+1.0) + 4 ticks; 50-tick expiry = abort (D = 2, idle) | D = 2 after 50 ticks, read 4 ticks later (the timeout path) |
| 14 | detect publish | MISC_CONF.6 change-only event; good: byte 0x80DE+2c := ((b | 1) & 0x3F) | 0x40 | unconditional |
| 15 | shifts | math unit (U1) | C `>>` truncating |
| 16 | diagnostics | none | regs 0x24..0x2B, 0x71..0x74 (awto only) |
| 17 | temp, 2E/2F, scale immediates, calibration reads | sections 5, 6 | match |

## 12. Open questions (simulator-answerable)

- U1: does `EF = 0x20 | n` round? E9..EC = 320100, EF = 0x2A: read E9:EA, 312 (truncate) or 313 (round).
- U2: does a read of AB = c|4 / c|8 after a write return the written value or the live ADC (feedback vs overwrite)? Write 0x155, read back.
- U3: raw CM unit and k per channel: run 0x34D0 with word(c|8) = 100 / 262, both k, compare with board CM / VM pairs.
- U4: AE bits 2c / 2c+1: which hardware condition? Model AE = 0x01 / 0x02 and count passes until the c|4 write equals the 8-sample average (expect 28).
- U5: 0x6A.0 vs the valid-trim constants (0x0D / 0xE0, -18 / -50, -5): same hardware mode?
- U6: monitor publish rate (bit 1 of `0x807C+0x12c`): needs the timer model (cluster 5).
- U7: does the negate quirk (R6 = ~top byte, CY not from INC) occur? Sweep word / B6 / B7 and count A.31 = 1 or byte 1 bit 7 = 1.
- U8: HP_Pn_MOD bits 3 / 2 (override to code 4): sim samples A > B > C > D with AD > 110, m = 4 must give 4.
- U9: detect event: with D(c) = 1 and the sim raising BC.6 after N ticks, does stock reach 0x3B3C 4 ticks later and read nibble 4 (camera) / 6 (open)? With no event for 50 ticks: D(c) = 2, SAT nibble cleared, idle.
- U10: what raw `D(c)` and sample words (A, B, C, D) does the hardware deliver for a camera and an open port (needed to test the 0xF branch): read `DA:DB` at D9 = 4c..4c+3 on stock-equivalent bench, or from a trace.
