# BCM59111 PSE firmware: CPU, ISA, structure

Static analysis only, no board access (#26). Images: `hwdb/firmware/` ([README](../hwdb/firmware/README.md)).
Tools (also sfr, re, ghidra, timing: §8-13): `scripts/poe_fw_ident.py` (identification, comparison; log `tmp/logs/poe_fw_ident.log`), `scripts/poe_fw_dis.py` (8051 disassembler; listing `tmp/poe-fw/<image>.asm`, ~9000 lines / 250 KB each, not committed: regenerate).

- **C** = confirmed by the code bytes. **I** = inferred.

## 1. Conclusion

- CPU core: **MCS-51 (8051) compatible**, integrated in the BCM59111. ISA: 8051 machine code, hand/compiler output (C compiler style: `LCALL` to shared tails, `R0..R7` banks via `PSW`). Confidence: very high.
- Language: native 8051 machine code, no bytecode. Compiler: **Keil C51** (C): `?C?CCASE`, `?C?ICALL`, `?C?IMUL`, `?C?LLDXDATA`, `?C?LSTXDATA`, STARTUP.A51 IDATA clear are byte-identical to awto-ddc's Keil-compiled RTD firmware (§10). No compiler string in either image.
- Core: 8051/8052-compatible, 256 B IRAM, Timer 2; **no DW8051-specific SFR is used** (§8): DW8051 not provable, not excluded. The image never calls the ROM (0 call sites below 0x1A00); the ROM calls the image through the hook table.
- Not Cortex-M: word 0/1 are not SP/reset (§2). Not compressed or encrypted: entropy 6.45 bit/B, the string table and opcode statistics decode cleanly.
- Image CRC (C, bench 2026-11-21): CRC-16 (poly 0x8005, init 0, no reflection) big-endian at file offset 0x38FE (CPU 0x52FE) over file 0x0000..0x38FD; both stock images verify (residue 0 over 0x0000..0x38FF). The ROM rejects an image without it: [poe-fw-rewrite.md](poe-fw-rewrite.md).
- Heritage hint: image tag `POETEC_IMAGE` (Broadcom acquired the PowerDsine PoE line; "POETEC" is not a Broadcom name, I). Feature string in both images: `ICUT_ILIM_ACCESS_ENABLED+LEGACY_POWERUP+AUTO_FAULT_CLEAR+FIXED_TLIM+REJECT_AC_NOISE`; v21 adds `+DEFAULT_AF_AUTO_MODE`.

## 2. Evidence

| Test | Result (v21 / Meraki) |
|---|---|
| Linear 8051 sweep, relative-branch (`SJMP/Jcc/DJNZ/CJNE/JB`) targets landing on an instruction boundary | 621/626 = 99.2 % / 618/624 = 99.0 %; same test on random bytes 63 % |
| Mnemonic mix | MOV 4636, MOVX 968, LCALL 688, ANL 474, RET 372, CLR 292, JNB 170, SETB 147 (typical 8051 firmware) |
| `MOV DPTR,#0x8xxx` + `MOVX` | 550 uses, 125 distinct, range 0x8000..0x8140: a memory-mapped register window |
| SFRs in operands | `PSW`, `ACC`, `B`, `DPL/DPH`, `P0..P2`, `TMOD`, `TL1/TH1`, `IE.2`, `IP.n`, `TCON.6` |
| Cortex-M vector check | word0 0xffe33cff, word1 0x48ff103f: fail |
| 8051 `LJMP` at 0x0000 / vectors every 8 B | absent: offset 0 is a hook table (§3), the image is a RAM patch/extension, not the boot image |
| `MOVC` tables | `MOV DPTR,#0x4C23` / `0x4C63` hit file offsets 0x3223 / 0x3263 (`01 02 04 08`): image CPU base = file offset + **0x1A00** (C) |
| FW_REV and CRC flag | code at CPU 0x4071 writes `0x8041 <- 0x15` (v21) and `0x8075 <- 0xAA`: the same values the host reads from regs 0x41 / 0x75 ([poe.md](poe.md) section 3) (C) |

- Address map (C): code addresses < 0x1A00 = on-chip ROM (not in the files, never called by the image); image at 0x1A00..0x59FF; the 16 KiB file is loaded at 0x1A00. Code ends 0x5260, NOP pad to 0x52FF, string `POETEC_IMAGE+...` at 0x5300.
- Host register `r` = XRAM `0x8000 + r` (C): INT=0x8000, OP_MOD=0x8012, FW_REV=0x8041, counters 0x8060.., CRC status 0x8075. Chip-internal registers continue above (0x8078..0x80C4 per-channel blocks, 0x80F1.., 0x8103.., 0x8124.., 0x8133..0x8140 scratch).

## 3. Layout

File offset 0x00..0x2e: 15 records `[0xFF, hi, lo]` = hook table (CPU addresses), 0x2f..0x5f zero, code from 0x60 (CPU 0x1A60). The ROM reads this table to find the image's entry points (I; the ROM is not available). Records are Keil generic pointers (tag 0xFF = code, C); same format in the register dispatch tables (§4). Hooks are plain `using n` functions (`PUSH PSW; MOV PSW,#bank ... POP PSW; RET`, no RETI): the ROM owns the vectors and saves ACC/B/DPTR (I).

| Slot | v21 target | Meraki | First instructions / role |
|---|---|---|---|
| 0 | 0x3CE3 | 0x3D1D | `MOV A,P1; ANL A,#0x0F; MOV 0x29,A`: start-up. Reads the address pins (P1 low nibble) and strap `P0.0`, clears XRAM, then runs the main loop (I: startup entry) |
| 1 | 0x3F10 | 0x3F4A | I2C slave ISR (SFR 0xDD status, 0xF5/0xF6/0xF7 data; §4) |
| 2 | 0x4879 | 0x4921 | ISR: reads 0x8017 (per-channel event flags), services 0x80BF..0x80C3, sets all four channel flags `0x23.1..4`, `MOV 0x93,#0x3F` |
| 3 | 0x4EBA | 0x4EFD | low-priority periodic ISR (masks itself with `CLR IP.1`): calls 0x1B03, 0x37D2 (counters 0x8063..0x806F), 0x34D0 (INT 0x8000.., temperature 0x802C..), 0x29EE (CM/VM 0x8030..0x803F, OP_MOD), 0x316B (per-channel status) |
| 4..7 | 0x4255 0x40DD 0x42CD 0x4345 | 0x422E 0x2F0B 0x42A6 0x431E | per-channel ISRs: `CLR IP.2..5; SETB 0x23.1..4; read SFR 0xBC..0xBF`, then block 0x8078 / 0x808A / 0x809C / 0x80AE (stride 0x12) |
| 8 | 0x4FCD | 0x5010 | timer 1 ISR: `CLR TCON.6; TL1=0xF4; TH1=0xE5; LCALL 0x3C1E` (tick: per-channel timers, temperature) |
| 9..11, 12..14 | 0x1AFD..FF, 0x5220..22 | 0x526E..73 | `RET` stubs (unused slots) |

Slot order and the meaning of slot 0 are inferred from the code; the ROM side of the dispatch is not visible.

## 4. Host I2C interface

I2C slave ISR (slot 1, v21 0x3F10) (C):
- SFR 0xF7 (received byte, copy in `0x34`) is compared with `0xA8` / `0xB8` / `0x80` / `0xA0` / `0x60` (I2C controller status codes, I); register pointer kept in `0x35`.
- Read: `MOV DPH,#0x80; MOV DPL,reg; MOVX A,@DPTR` -> SFR 0xF6 (TX data); the read table (below) can post-process (clear-on-read).
- Write: byte stored in `0x32`, then `LCALL 0x47AB` with `DPTR` = write table; handler index = register (3 B/entry).
- Status error (`0xDD & 0x88 == 0x88`) sets `0x8042 |= 1` and `0x804A = 1` (main loop: reload).

Two dispatch tables, one 3-byte entry `[FF hi lo]` per register 0x00..0x75 (v21: write 0x26EE, read 0x286E; Meraki: 0x26E5 / 0x2865) (C):

- **Writable registers** (handler != no-op `0x5260`; identical list in both images): `01 12 13 14 15 16 17 18 19 1A 1E 1F 42 44 46 47 48 4B 4C 4D 50 51 52 55 56 57 5E 5F`. Typical handler (e.g. 0x50FC for reg 0x13): store byte in XRAM `0x8000+reg`, `SETB` a dirty bit in `0x20..0x23` (`0x21.0` = 0x13, `0x22.1` = 0x14, `0x22.6` = 0x44, `0x21.5` = 0x47); the main loop / ISR 3 applies it.
- Reg 0x12 (OP_MOD) handler 0x2F7B (v21), applied in 0x482F / 0x4B0D. Regs 0x18 / 0x19 / 0x1A (push-button detect/class, power on/off, reset: [poe.md](poe.md) section 3, "not used by stock") have handlers 0x50C2 / 0x50CC / 0x50D6. So the chip implements the LTC4266-style push-button registers.
- **Non-trivial read handlers**: `03` (0x4F57), `05` (0x4F6F), `07` (0x4D0E), `09` (0x4D35), `0B` (0x4F87) = the event / clear-on-read registers (poe.md: 0x03 PWR_EVT_CLR, 0x07 FAULT_EVT_CLR read-clears; 0x05 / 0x09 / 0x0B are their siblings), `60..6F` (0x51B6, stride 6) = the 16 counters (4 per channel). Everything else is a stub that returns the XRAM byte.
- Both tables are 128 records (one data region 0x26EE..0x29EE in v21). Defaults: write 0x5260 (RET), read 0x5223 (RET; per-register RET stubs 0x5224..0x525F). Reg `0x16` write -> 0x5261 (RET). Protocol: §9.
- No entries for `0x43` (DEV_ID), `0x70` (FW_DWN_CTRL): ROM / hardware. Regs 0x41 and 0x75 are plain XRAM bytes written once by the image (0x4071).
- Registers whose XRAM address is touched by code in a mapped way: 0x12 OP_MOD (0x482F, 0x4B0D), 0x13/0x14/0x17/0x44/0x46..0x48 + 5-stride 0x4B..0x58 (config: 0x50F3 table of handlers, apply routines 0x4089 / 0x40A2 / 0x40B0 / 0x45A7 / 0x46FC / 0x4A69 / 0x4B40 / 0x4C90 / 0x4D83), 0x30..0x3F CM/VM (one writer 0x442B), 0x2C temperature (0x34D0, 0x3C1E; the threshold check `0x802C < 0x40` in the main loop), 0x60..0x6F counters (0x51AF).

## 5. Main loop and port state machine

Main loop (v21 0x3D1A, after start-up 0x3CE3..0x3D17) (C):
1. `LCALL 0x230D` (v21): common poll: INT/event/status registers 0x8000..0x8010, flag bits.
2. For each channel `c` (flags `0x23.1..4`, set by the channel ISRs): clear the flag, `R7 = c`, `DPTR = 0x8087 / 0x8099 / 0x80AB / 0x80BD`, `LCALL 0x50AB`. 0x50AB loads a 16-bit code pointer from XRAM at `DPTR` (channel block + 0x0F) and jumps to it: **per-channel state pointer** (state handlers are re-armed with `R4:R5 = next-state address, R3 = delay ticks, LCALL 0x49FF`).
3. Over-temperature: `0x802C < 0x40` and `0x800A.7 == 0` -> `LCALL 0x500A` with `R7 = 0x80` (SUP_EVT 0x800A/B, §14). Timer word `0x5F:0x5E` vs `0x02AA` gates a second check.
4. `0x804A != 0` -> clear and `LCALL 0x3D92` (reload after I2C error), then `LJMP 0x3D1A`.

State handlers (27 `R4:R5` constants, 22 distinct, v21): 0x2EE0 0x2F8C 0x3B3C 0x3FBE 0x41D9 0x448D 0x44ED 0x454A 0x4601 0x46AB 0x48BE 0x4A31 0x4AD7 0x4D5C 0x4DF5 0x4E60 0x4E9D 0x4EEF (idle, 9 users) 0x4F23 0x4F9F 0x4FB7 0x5091 0x509E. Typical (0x4EEF idle): re-arm; (0x4F09): wait for status bit 1 in the channel's flag byte, clear it, set next state 0x4EEF with delay 2; (0x4F23): wait bit 4, next state 0x4DF5. The chain is the detect -> classify -> power-up -> monitor sequence ([poe.md](poe.md) section 3 detect nibble / class nibble / power-good). State names and transitions: §11.

## 6. Two copies

| Item | Stock v21 (switchdrvr, = UDM GPL array) | Meraki (quenode `driver/bcm59111.bin`) |
|---|---|---|
| FW_REV written at 0x8041 | `0x15` (21) | `0x12` (18) |
| Feature string | `...+LEGACY_POWERUO+...+REJECT_AC_NOISE+DEFAULT_AF_AUTO_MODE` (sic `POWERUO`) | `...+LEGACY_POWERUP+...+REJECT_AC_NOISE` |
| Hook table | 15 slots, same roles | 15 slots, same roles, other addresses |
| Differing bytes | 12182 of 16384 (74.4 %) | |
| Same code, shifted | 57.7 % of 8-byte windows of v21 reappear at another offset (most common shifts +58, -9, +94) | |
| Host register tables | write handlers identical list; read handlers identical list | |
| XRAM `0x8000..0x8140` use | 125 distinct addresses, same use counts (+-1) | |
| Last non-pad byte | 0x3974 | 0x3974 |

- Same source tree, different build: v18 (Meraki MX64/65 era) -> v21 (UDM / US-48). Interface (register map, tables, ISR layout) is stable across versions; v21 adds `DEFAULT_AF_AUTO_MODE`.
- Framing is a host property and is not in the image: the GPL array is identical to the stock blob byte for byte ([../hwdb/firmware/README.md](../hwdb/firmware/README.md)).

## 7. Open

- ROM (< 0x1A00): vector table, I2C slave glue, CRC check, `FW_DWN_CTRL` handling. Not available; would settle the slot-to-vector mapping.
- Exact meaning of the PSE peripheral SFRs (§8), the E9..EF math unit and the 0x80C0..0x80C4 / 0x80F1 / 0x8103 / 0x8124.. scratch blocks; CPU clock only estimated (§17).
- Every function is named or described (§14); the measurement chain and math unit are inferred (I), analog register semantics (AB/AC/AD, B5..B7, D1..D6) are still unconfirmed.
- Where class / budget policy lives (docs/poe.md section 7 "inside the chip FW"): class code is kept per channel (IRAM 0x42..0x45) and read by the host; no power-budget logic found (I).

## 8. Core: SFR fingerprint (`scripts/poe_fw_sfr.py`, log `tmp/logs/poe_fw_sfr.log`)

DW8051 databook v3.2a tables 2-6, 4-8 (`/vendors/realtek/analysis/rl6449-dw8051/DW8051-databook.txt`). Counts: direct + bit accesses in the sweep of v21 (Meraki: same set).

| Class | SFRs touched (v21) |
|---|---|
| Standard 8051 | P0 (strap), P1 (address pins), P2, DPL/DPH (66), PSW (30), ACC, B, IE (EA 80x, IE.2, IE.3), IP (IP.1..5), TCON.6 (TR1), TMOD, TL1/TH1 (timer 1 tick). SP never written |
| 8052 | T2CON bits 6, 5 (`SETB 0xCE/0xCD`, once at init) |
| DW8051 extension | **DPL1 DPH1 DPS CKCON SPC_FNC EXIF MPAGE SCON1 SBUF1 EICON: 0 uses.** Only 0xE8 (`MOV 0xE8,#4`, last of the E5..E8 block of 4 per-channel regs: not EIE) and 0xF8 (`ANL/ORL 0xF8,#FE/01`: host INT line: not EIP) collide with DW8051 EIE/EIP |
| Free external-SFR-bus addresses (table 4-8) = PSE peripherals | 0x93 0x95 0x96, A1-A5 A7, AB-AF, B1-B7, BC-BF, C9, D1-D7 D9-DB DD, E5-E7 E9-EF, F1-F7 F9, FF: every one is in the table 4-8 free set |

PSE SFR roles (C = code-evident, I = inferred): BC..BF per-channel event flags, write-1-clear (C); DD / F5 / F6 / F7 I2C slave status / control / TX / RX (C); F8.0 host INT output (I); A1..A4 per-channel limit/config, init 0xF0 (I); B1..B4 per-channel control, init 0 (I); D1..D4 per-channel detect start/result (I); D5 D6 class start masks (I); C9 per-channel output gate, init 0x0F (I); F1 / F2 power enable set / clear (I); AB..AF B5..B7 measurement unit (I); E9..EF math unit with NOP waits, command at EF (I); E5..E8 = 0x04 init (I). Names: `hwdb/firmware/bcm59111-v21.names.tsv`.

Memory spaces:
- IRAM 256 B: `MOV R0,#0xFF; CLR A; MOV @R0,A; DJNZ R0` at 0x50EA clears 0x01..0xFF (C: indirect upper half exists). SP never written by the image (the ROM sets it; stack placement unknown). Register banks: PSW 0x08 (I2C ISR), 0x10 (periodic, 1B03, 316B, ...), 0x18 (timer tick). IRAM 0x20..0x23 bit flags; 0x7C = software argument stack pointer (`DEC 0x7C; MOV R0,0x7C; MOV @R0,AR7`, I).
- XRAM: `MOVX @DPTR` 967, `MOVX @Ri` 0: no MPAGE paging; XRAM 0x8000..0x8140 via `MOV DPTR,#imm` or `MOV DPH,#0x80; MOV DPL,reg`. No writes to program memory (SPC_FNC unused).
- CODE: `MOVC A,@A+DPTR` 51 (tables, switch helper), `MOVC A,@A+PC` 0, `JMP @A+DPTR` 9.

Verdict: **8051/8052-compatible core with 256 B IRAM; DW8051 unproven**. For DW8051: 256 B IRAM, Timer 2, custom SFRs only at addresses the databook lists as free (weak: fits any extensible 8051). Against / missing: no DW8051 extension register used; E8/F8 repurposed. Dallas DS80C320, Silicon Labs, Infineon, Mentor M8051 cannot be separated either (their extension SFRs are unused too). Confidence: 8051/8052-compatible high, DW8051 low. A decisive test needs the ROM (reset code writing CKCON / DPS / MPAGE) or a timed loop on the board.

## 9. Host I2C slave protocol (ISR 0x3F10, hook 1) (C unless noted)

- SFR DD status/control, F7 received byte or status code, F5 ack control, F6 TX data. Bus error `DD & 0x88 == 0x88`: `0x8042.0` and `0x804A = 1`; `DD & 0x22 == 0x22` re-arms (`DD = (DD & F0) | 2`).
- F7 status codes are the Philips/NXP slave codes: `0xA8` / `0xB8` SLA+R ack / data ack (read), `0x60` SLA+W (sets phase flag `0x30 = 1`), `0x80` data received, `0xA0` stop (clears 0x30 / 0x35).
- Write: first byte after SLA+W (0x30 set) = register pointer -> IRAM 0x35; each following byte -> `0x32`, dispatch `host_write_table[0x35]` (`keil_OFFXADD`, `keil_LDGPTR_CODE`, `keil_ICALL`), then **`INC 0x35`: auto-increment**, multi-byte writes hit consecutive registers.
- Read: each byte = `MOVX A,@(0x8000 + 0x35)` -> F6, then `host_read_table[0x35]` post-process (clear-on-read 03/05/07/09/0B; counters 60..6F cleared), `INC 0x35`. Reads start at the pointer of the last write; stop (0xA0) clears it.
- I2C address: hardware (not set by the image; I). FW download (`0x70` blocks `0x80 + off + 32 B`, `0x40` start) is ROM work before this image runs; reg 0x70 has no table entry. The image writes FW_REV 0x15 and CRC_STATUS 0xAA (`init_fw_rev_crc` 0x4071).
- Host INT output (I): `INT & INT_MASK != 0` and `0x8017.7` -> `SETB 0xF8.0` (`int_line_update` 0x5043); `wh_int_mask` and the read-clear handlers `CLR 0xF8.0` when nothing is pending.
- Write handlers store XRAM[0x8000+r] and set a dirty bit in IRAM 0x20..0x23, applied by main loop / periodic ISR. `wh_op_mod` (0x12) sets all four channel service flags (0x23.1..4), restarting the state machines. Push-button regs 0x18 / 0x19 / 0x1A OR the byte into XRAM and set 0x20.0 / 0x22.4 / 0x22.7.

## 10. Compiler fingerprint (Keil C51 vs SDCC vs IAR)

Reference: awto-ddc Keil-compiled RTD firmware (`~/vendors/realtek/analysis/jxc-4k/bank0/disassembly.asm`, `keil_*` names), awto-sdcc `docs/keil-abi.md`. v21 vs reference; Meraki has the same set.

| Signature | In v21 | Verdict |
|---|---|---|
| `?C?CCASE` (POP DPH/DPL, MOVC {hi,lo,val}, XRL, default JMP @A+DPTR) | 0x43FF, instruction-identical | Keil [C] |
| `?C?ICALL` (`MOV DPH,R2;MOV DPL,R1;CLR A;JMP @A+DPTR`) | 0x4425 (host dispatch, start-up) | Keil [C] |
| `?C?IMUL` | 0x43BD identical; inline copies in 0x1B03 | Keil [C] |
| `?C?LLDXDATA` / `?C?LSTXDATA` / `?C?OFFXADD` (= keil_DPTR_add_AxB) | 0x43CF / 0x43DB / 0x43E7, in C51L.LIB link order | Keil [C] |
| STARTUP.A51 IDATALOOP (`MOV R0,#0xFF;CLR A;MOV @R0,A;DJNZ R0`) | 0x50EA; no XDATA clear, no `MOV SP` | Keil [C]; SDCC genRAMCLEAR emits the same loop: not unique |
| Generic pointer tag 0xFF = code (Keil) vs 0x80 (SDCC) | hook and dispatch tables are `[FF hi lo]` | Keil [C] |
| `using n`: `PUSH PSW; MOV PSW,#bank ... POP PSW; RET` | 10 functions | Keil [C] (keil-abi 2.7) |
| Args R7 / R6:R7 / R4..R7, `MOV R7,A; CJNE R7,#imm` | everywhere | Keil [I] |
| Small switch as `DEC A; JZ; ... ADD A,#3; JNZ` | 49FF, 4B71, 4ED5 | Keil [I] |
| Function tails shared by fall-through (`1F23/25/27`, `1F35/3B/3E`, `1F6A/72/74`) | ~60 interior entries | Keil OPTIMIZE(8) common-block merging [I]; SDCC does not |
| Software argument stack `?C_IBP` at 0x7C | all state handlers | Keil reentrant style [I] |
| SDCC runtime (`__mulint`, `__divuint`, `__gptrget` tags 0x00/0x40/0x60/0x80) | none | SDCC excluded [C] |
| IAR (`?XSP` XRAM data stack, `?CALL_IND`) | none; stack in IDATA, helpers match Keil | IAR excluded [I] |
| Absent: `?C?LMUL`, `?C?ULDIV`, `?C?SIDIV`, `?C?UIDIV`, `?C?CLDPTR`, `?C?CSTPTR`, `?C?ICASE` | 8/16-bit math only, 5 inline `DIV AB` | no long/float, small model |

- Compiler library code is inside the image (0x43BD..0x4429); 0 ROM call sites (the only `LJMP 0x0408` is table data at 0x4C24). Keil version not determinable (helpers identical C51 v6..v9). Small model, `bit` flags at 0x20..0x23. Confidence Keil C51: high.

## 11. Per-channel state machine (v21)

Block at `0x8078 + 0x12*c` (c = 0..3): +0 / +1 event flags set by `isr_chan<c>`, +4 / +5 wait flags (timer expiry, events polled by `st_wait_*`), +0x0F:+0x10 state pointer, +0x11 delay ticks. `arm_channel(R7 = c, R4:R5 = next, R3 = delay)` sets service flag `0x23.(c+1)`; the main loop clears it and jumps through the pointer (`run_state` 0x50AB); `tick_timers` (0x3C1E, timer 1 reload TL1/TH1 = 0xF4/0xE5) counts delays down. Tick = 10 ms, f_cpu ~ 8 MHz (§17). Names/comments: names TSV. All [I] unless tagged.

| State | Addr | Role | Next (delay ticks) |
|---|---|---|---|
| `st_port_reset` | 4AD7 | clear per-channel enables / counters (init arms it with 1) | `st_port_shutdown` |
| `st_port_shutdown` | 2EE0 | clear event latches 0x8006..09 and INT bits, re-apply OP_MOD | `st_port_cfg` (2) |
| `st_port_cfg` | 44ED | per-port config (limit, ctrl, C9 gate), zero CM/VM | `st_mode_select` |
| `st_mode_select` | 454A | branch on OP_MOD | `st_port_reset` (1), `st_detect_arm` (3), `st_power_request` (10) |
| `st_detect_arm` | 46AB | DET_CLASS_EN (0x8014) bit? | `st_detect_start` or idle |
| `st_detect_start` | 4601 | D1..D4 = 1 (start detection) | `st_detect_wait` |
| `st_detect_wait` | 1A60 | detection window | `st_detect_result` (4) |
| `st_detect_result` | 3B3C | detect code (D1..D4 >> 4) to SAT_Pn low nibble; **4 = good** (matches host name table) | `st_class_gate` (8); `st_detect_invalid` (15); `st_detect_ok_wait` (5) |
| `st_detect_invalid` | 48BE | increments invalid-signature counter 0x8060+4c (C: counter b0) | `st_retry_wait_a` (16) or idle |
| `st_class_gate` | 41D9 | class enabled? | `st_power_request` (10) or `st_class_done` |
| `st_class_done` | 47E4 | class events, store class nibble (SAT_Pn bits 6:4) | `st_class_eval` |
| `st_class_eval` | 448D | class valid? | `st_power_request` / idle |
| `st_power_request` | 3FBE | OP_MOD: shutdown -> reset; manual -> idle; semi -> `st_power_on` (10); auto -> `st_poweron_hold` (2) | |
| `st_power_on` | 4A31 | power-enable bit (SFR F1) | `st_power_good` |
| `st_poweron_hold` | 4FB7 | power-up wait timer 0x14 | `st_wait_b7` -> `st_port_cfg` |
| `st_power_good` | 2F8C | SAT_PWR (0x8010) enable / good nibbles, INT 0x8002 \|= 0x41 | idle |
| `st_fault_shutdown` | 4D5C | armed by `periodic_port_monitor` (12): fault bits, long cool-down timer | `st_fault_cooldown` -> idle |
| `st_disconnect_cleanup` | 4F9F | armed by monitor (14): clear, timer 0x40/1E | `st_wait_b6` -> `st_port_cfg` |
| `st_idle` | 4EEF | arm timer 0x3C | `st_wait_b7` -> `st_port_cfg` |
| waits / kicks | 4F09 4BA2 4E60 4F3D 4E80 5091 509E 4F23 4DF5 49BD 3A30 4E9D | `st_wait_b1/b3/b6/b6b/b7`, `st_retry_wait_a/b`, `st_detect_ok_wait`, `st_chan_enable`, `st_class_hw_start` (D5/D6 masks), `st_class_wait`, `st_fault_cooldown` | |

802.3 mapping (I): detect = `st_detect_*`, classification = `st_class_*`, power-up = `st_power_*`, disconnect / MPS = DIS_SEN_EN (0x13) + `st_disconnect_cleanup`, overload / thermal = `st_fault_shutdown` and `set_thermal_status` (0x500A; main loop `0x802C < 0x40`). Tick-to-802.3 window mapping: §17.

## 12. Timing (awto-ddc DW8051 model, `scripts/poe_fw_timing.py`)

Assumes DW8051 (4 clk/cycle, MOVX = 2 + CKCON; CKCON never written = reset 1; databook v3.2a tables 2-4, 2-5); a classic 12-clk core makes every figure ~3x larger. I2C ISR (+4 cycles interrupt entry):

| Path | insns | cycles | CPU clk |
|---|---|---|---|
| read byte (SLA+R / data ack) | 53 | 139 | 556 |
| write byte, default no-op handler | 57 | 147 | 588 |
| write byte, reg 0x13 handler | 61 | 157 | 628 |
| register-pointer byte | 30 | 76 | 304 |

Host register read latency = the read path; time = clk / f_cpu (~8 MHz estimate, §17). The image sets no CKCON stretch.

## 13. Ghidra (`scripts/poe_fw_ghidra.py`, Java in `scripts/ghidra/`)

- `python3 scripts/poe_fw_ghidra.py import`: headless Ghidra 12.2 (`~/.local/bin/analyzeHeadless`), BinaryLoader, `8051:BE:16:default`, base 0x1A00 (ROM < 0x1A00 undefined), seeds (hooks, dispatch tables, state pointers, call targets), `ApplyPoeNames.java` (spaces CODE / EXTMEM = XRAM / INTMEM / SFR), `ExportPoe.java`. `regen` re-applies names (~6 s each).
- Project `tmp/ghidra/poefw` in the main checkout (Ghidra rejects dot-prefixed path elements such as `.claude/worktrees`); log `tmp/logs/poe_fw_ghidra.log`.
- Committed: `hwdb/firmware/bcm59111-v21.names.tsv` (~460 rows), `bcm59111-regs.tsv`, `bcm59111-protocol.tsv`, `bcm59111-v21.abi.tsv`, `bcm59111-v21.functions.tsv`, Meraki equivalents. Not committed: `tmp/poe-fw/ghidra/decompiled.c`, `listing.tsv`, `tmp/poe-fw/re/*` (call graph, XRAM / IRAM xref, annotated listing from `scripts/poe_fw_re.py`).
- `EnsureFunctions.java`, `SeedFile.java` copied from awto-ddc `re/ghidra` (same author); `ApplyPoeNames.java` follows awto-ddc `ApplyRegLabels.java` (labels only). Keil register ABI: `scripts/poe_fw_abi.py` infers register signatures (R7 / R6:R7 / R4..R7 returns, R7 / R5 / R3 args; 102 functions, rest use the IBP stack) -> `hwdb/firmware/bcm59111-v21.abi.tsv`, applied as custom-storage signatures by `ApplyKeilAbi.java` (the 8051 module has no Keil calling convention); decompile improves but IBP-stack functions still read poorly. `FixKeilReturns.java` not ported (no non-returning Keil helpers here). Also `ApplyPoeProtocol.java` (§15), `--image` for Meraki (§16).
- Other tools: `poe_fw_sfr.py` (SFR fingerprint), `poe_fw_re.py` (inventory, xref).

## 14. Function inventory, measurement chain, math unit, scratch XRAM

Every function has a name or description: 137 hand rows (`hwdb/firmware/bcm59111-v21.names.tsv`, source `poe-fw-re`) + 190 pattern rows (source `poe-fw-auto`, `scripts/poe_fw_autoname.py`: channel-block / array pointer calculators `ptr_s<stride>_<base>`, `leaf_*`, `xstate_*`, `hostreg_*`, `chcfg_*`, `detcls_*`, `meas_*`, `math_*`; callers / XRAM / SFR in the comment). 3 unnamed Ghidra stubs remain. All [I] except library, host handlers, ISRs.

Periodic ISR (hook 3) order: `meas_cur_offset_trim` (1B03) -> `meas_temp_calib` (37D2) -> `meas_convert_cm_vm_temp` (34D0) -> `periodic_dc_mux_scheduler` (29EE) -> `periodic_detcls_sequencer` (316B).

- Analog front end (I): SFR `AB` = register index (`c|4`, `c|8`, `0x24`, `0x28`, `0x2C`, `0x30` ...), `AC:AD` = 16-bit data (read after setting AB, write to trim); `B5` = ADC input select (2c+1, 2c+9), `B7` / `B6` = 8-bit signed ADC results; `AF` = mux / trigger written by the DC scheduler (1, 2, 3, 5).
- `meas_cur_offset_trim`: per channel, counter IRAM 0x25+c: < 20 settle (accumulator 0x80E5+2c cleared), 20..26 accumulate 7 samples, at 27 reset and `>>3` on the math unit, result written back to AB=c|4 (offset trim loop).
- `meas_temp_calib`: temperature sensor AB=0x2C, offset IRAM 0x4E:4F, slope 0x4A:4B, clamp 0..0x3FF, feeds back AB=0x30. `meas_convert_cm_vm_temp`: scales raw words with `math_mul16` (x11, x3201 or x6403 depending on the HP bit) and `math_shr32` (>>10), TEMP 0x802C = (IRAM 0x5C:5D) >> 1, 0x802E:2F.
- `periodic_dc_mux_scheduler`: sub-state 0x8134 (1..5) steps the analog mux (AF), reads CM / VM words (0x813B, 0x813D), publishes CM/VM host registers via `write_cm_vm_regs`, latches events 0x8000..0x8008.
- `periodic_detcls_sequencer`: per-channel hardware probe sequencer, 7-byte records at 0x8104+7c, `keil_CCASE` on the record state (3, 4, 5, 6, 0x0A, 0x0D), writes D5/D6 (`0x20 | c<<6` ...) to step detection / classification.
- **Math unit E9..EF** (I, medium): 16x16 -> 32 unsigned multiply: load `E9:EA` (a, lsb in E9) and write `ED:EE` (b, lsb in ED): the write starts the multiply, result in `E9..EC` (EC = msb) after ~1 NOP (`math_mul16` 0x4764). 32-bit logical right shift: load `E9..EC`, write `EF = 0x20 | n`, result in `E9..EC` after ~8-18 NOPs (`math_shr32` 0x4777; seen `0x23` >>3, `0x26` >>6, `0x2A` >>10, `0x2E` >>14). Signs are handled in software (abs value, then negate).
- Scratch XRAM (all [I], names in the TSV): 0x80C0 / 0x80C4 per-channel event masks and requests, 0x8103 detect/class-active channel bits, 0x8104+7c detect/class records, 0x80DD..0x80F0 per-channel word arrays (0x80E5+2c offset accumulators), 0x80F1 / 0x80F2 loop indices, 0x8124 `rr_phase`, 0x8129 HP flag, 0x812A / 0x812E math scratch, 0x8132 / 0x8133 channel scratch, 0x8134..0x813E DC scheduler (state, tick, channel, value, flag, CM word, VM word), 0x8140 init index.
- Host register 0x42 is `WDT_TIME` (UDM name): `wh_wdt_time` selects SFR `F9` = 0x0B / 0x0A (watchdog period, I). `set_sup_event` (0x500A) raises SUP_EVT 0x80 when TEMP raw (0x802C) < 0x40 (invalid sensor), not a thermal trip (corrects section 5).

## 15. Protocol documentation in the Ghidra database

- Committed sources: `hwdb/firmware/bcm59111-regs.tsv` (128 host registers: name, access, clear-on-read, write / read handler address, meaning, source, confidence; names from UDM GPL `drivers/net/power/bcm59111.h` fetched with `gh api`, stock use from docs/poe.md, image behaviour from this analysis), `hwdb/firmware/bcm59111-protocol.tsv` (49 opcodes of the 12-byte Broadcom PoE control protocol, source svanheule.net 2026-11-19; Broadcom SDK `src/soc/i2c/bcm59101.c` cited as the I2C-side packet-format reference only).
- `scripts/ghidra/ApplyPoeProtocol.java` (run by `poe_fw_ghidra.py`): enums `poe_host_reg` and `poe_cmd` under `/BCM59111`, labels `HR_xx_<name>` + plate comments on EXTMEM 0x8000+r, handlers renamed `wh_<name>` / `rh_<name>` with plate comments (48 handlers). Regenerates with `poe_fw_ghidra.py import`.
- **The 12-byte protocol is not in the v21 image** (C): no UART SFR use (SCON / SBUF / T2 baud), no 12-byte buffer (`MOVX @Ri` 0), no checksum loop (no `ADD A,@Ri`), no command-opcode switch (the `CCASE` users are the I2C status-code and detect/class sequencer switches), no 0xAF / 0xFD / 0xFE / 0xFF reply constants. The image is only the I2C register slave (§9). The opcode table is reference equates for the SoC-to-MCU UART protocol of the Realtek front-end MCUs, not an interface of this firmware. The register interface is the UDM / Meraki one.

## 16. Meraki v18 vs v21 (`scripts/poe_fw_diff.py`, `hwdb/firmware/bcm59111-meraki.names.tsv`)

324 v21 vs 308 Meraki functions (normalised bytes: absolute code addresses masked): **267 same, 29 changed (>= 0.6 similar), 28 gone, 12 new**; most gone / new / changed are shared-tail relocation (the same code split differently: v21 `periodic_dc_mux_scheduler` 29EE = Meraki 29E5 + 2C9A). Host interface identical: I2C ISR logic, write and read handler lists, register defaults. Meraki names are carried to same / changed functions (`bcm59111-meraki.names.tsv`, `.functions.tsv`, `.abi.tsv`; `poe_fw_ghidra.py import --image hwdb/firmware/bcm59111-meraki.bin`).

Functional differences found (I): FW_REV 0x12 vs 0x15 (`init_fw_rev_crc`); `st_power_good` has an extra per-channel branch in v18 (`JNB ACC.0`, a class-derived value `>>3 & 0x1F`) and different state-pointer constants; `st_port_cfg` v18 also clears a bit (`ANL #F7`) in a per-channel byte; `update_chan_limit` v18 has a `DIV AB` path (limit calculation), v21 does not; `chcfg_4DD0` v18 ORs SFR F2 from a table inline, v21 through a helper; v21 string adds `DEFAULT_AF_AUTO_MODE` (no separate code body found; likely register defaults). No threshold constants differ in the matched functions (temperature `0x40`, counters `0x14/0x1B`, MUL constants equal).

## 17. Tick period and 802.3 windows

- Timer 1: `TMOD |= 0x10` (mode 1, 16-bit), `isr_timer1_tick` reloads TL1/TH1 = 0xF4/0xE5 -> 0xE5F4, so 0x10000 - 0xE5F4 = **6668 timer counts per tick**. Timer count rate = f_cpu / 12 (classic and DW8051 default; DW8051 CKCON T1M is never set). **tick = 6668 * 12 / f_cpu = 80016 / f_cpu**.
- Delays ([I]) as ticks: arm delays (`arm_channel` R3): `st_detect_wait` -> result 4, result -> class gate 8, invalid 15, ok-wait 5, retry wait 16, mode select -> power request 10 / detect arm 3, port shutdown -> cfg 2. Event timers (`set_timer` R5): detect start 50 (0x32), idle 60 (0x3C), class gate 20, power-up hold 20 (0x14), disconnect cleanup 30 (0x1E), fault cool-down 75 (0x4B), class hardware start 100 (0x64), retry wait 200 (0xC8), 250 (0xFA).
- Anchors: detect start 50 ticks = IEEE 802.3 Tdet max 500 ms (tick <= 10 ms); disconnect cleanup 30 ticks = Tmpdo min 300 ms (tick >= 10 ms); power-up hold 20 ticks <= Tpon 400 ms (tick <= 20 ms); class gate 20 ticks within a 200 ms slot (Tclass 6..75 ms measure window is inside it); retry wait 200 ticks = 2.0 s (re-detect spacing); fault cool-down 75 ticks = 750 ms.
- Result: tick = **10 ms** (both anchors tight), hence **f_cpu ~ 8.0 MHz** (80016 / 0.010 = 8.0016 MHz; tick 10.002 ms). Confidence low-medium: the mapping of each timer to an 802.3 window is inferred; one wrong anchor shifts the clock by integer factors (tick 5 / 20 ms = 16 / 4 MHz). Not forced independently by the code.
- Consequence for section 12: at 8 MHz a DW8051 cycle (4 clk) is 0.5 us, so the I2C read-byte path (139 cycles) is ~70 us; a classic 12-clk core would take ~210 us. Host I2C at 400 kHz (22.5 us per byte) therefore outruns the slave ISR unless the ROM / hardware stretches the clock (SFR F5 ack control).
