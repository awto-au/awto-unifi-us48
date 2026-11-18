# BCM59111 PSE firmware: CPU, ISA, structure

Static analysis only, no board access (#26). Images: `hwdb/firmware/` ([README](../hwdb/firmware/README.md)).
Tools: `scripts/poe_fw_ident.py` (identification, comparison; log `tmp/logs/poe_fw_ident.log`), `scripts/poe_fw_dis.py` (8051 disassembler; listing `tmp/poe-fw/<image>.asm`, ~9000 lines / 250 KB each, not committed: regenerate).

- **C** = confirmed by the code bytes. **I** = inferred.

## 1. Conclusion

- CPU core: **MCS-51 (8051) compatible**, integrated in the BCM59111. ISA: 8051 machine code, hand/compiler output (C compiler style: `LCALL` to shared tails, `R0..R7` banks via `PSW`). Confidence: very high.
- Language: native 8051 machine code; no bytecode. Compiled C (Keil-style common-tail subroutines; `MOV R0,0x7C`, `B`/`MUL AB` index scaling, `MOVC A,@A+DPTR` tables) is I; no source or compiler string in either image.
- Not Cortex-M: word 0/1 are not SP/reset (§2). Not compressed or encrypted: entropy 6.45 bit/B, the string table and opcode statistics decode cleanly.
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

- Address map (C): code addresses < 0x1A00 = on-chip ROM (called from the image, not in the files: 0x4C.. tables and `LCALL 0x0xxx`); image at 0x1A00..0x59FF; the 16 KiB file is loaded at 0x1A00. Image targets reach 0x526D.
- Host register `r` = XRAM `0x8000 + r` (C): INT=0x8000, OP_MOD=0x8012, FW_REV=0x8041, counters 0x8060.., CRC status 0x8075. Chip-internal registers continue above (0x8078..0x80C4 per-channel blocks, 0x80F1.., 0x8103.., 0x8124.., 0x8133..0x8140 scratch).

## 3. Layout

File offset 0x00..0x2e: 15 records `[0xFF, hi, lo]` = hook table (CPU addresses), 0x2f..0x5f zero, code from 0x60 (CPU 0x1A60). The ROM reads this table to find the image's entry points (I; the ROM is not available). Same 3-byte record format is used for the register dispatch tables (§4).

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
- No entries for `0x43` (DEV_ID), `0x70` (FW_DWN_CTRL): ROM / hardware. Regs 0x41 and 0x75 are plain XRAM bytes written once by the image (0x4071).
- Registers whose XRAM address is touched by code in a mapped way: 0x12 OP_MOD (0x482F, 0x4B0D), 0x13/0x14/0x17/0x44/0x46..0x48 + 5-stride 0x4B..0x58 (config: 0x50F3 table of handlers, apply routines 0x4089 / 0x40A2 / 0x40B0 / 0x45A7 / 0x46FC / 0x4A69 / 0x4B40 / 0x4C90 / 0x4D83), 0x30..0x3F CM/VM (one writer 0x442B), 0x2C temperature (0x34D0, 0x3C1E; the threshold check `0x802C < 0x40` in the main loop), 0x60..0x6F counters (0x51AF).

## 5. Main loop and port state machine

Main loop (v21 0x3D1A, after start-up 0x3CE3..0x3D17) (C):
1. `LCALL 0x230D` (v21): common poll: INT/event/status registers 0x8000..0x8010, flag bits.
2. For each channel `c` (flags `0x23.1..4`, set by the channel ISRs): clear the flag, `R7 = c`, `DPTR = 0x8087 / 0x8099 / 0x80AB / 0x80BD`, `LCALL 0x50AB`. 0x50AB loads a 16-bit code pointer from XRAM at `DPTR` (channel block + 0x0F) and jumps to it: **per-channel state pointer** (state handlers are re-armed with `R4:R5 = next-state address, R3 = delay ticks, LCALL 0x49FF`).
3. Over-temperature: `0x802C < 0x40` and `0x800A.7 == 0` -> `LCALL 0x500A` with `R7 = 0x80` (sets a bit in 0x800A/B: thermal status). Timer word `0x5F:0x5E` vs `0x02AA` gates a second check.
4. `0x804A != 0` -> clear and `LCALL 0x3D92` (reload after I2C error), then `LJMP 0x3D1A`.

State handlers (27 `R4:R5` constants, 22 distinct, v21): 0x2EE0 0x2F8C 0x3B3C 0x3FBE 0x41D9 0x448D 0x44ED 0x454A 0x4601 0x46AB 0x48BE 0x4A31 0x4AD7 0x4D5C 0x4DF5 0x4E60 0x4E9D 0x4EEF (idle, 9 users) 0x4F23 0x4F9F 0x4FB7 0x5091 0x509E. Typical (0x4EEF idle): re-arm; (0x4F09): wait for status bit 1 in the channel's flag byte, clear it, set next state 0x4EEF with delay 2; (0x4F23): wait bit 4, next state 0x4DF5. The chain is the detect -> classify -> power-up -> monitor sequence ([poe.md](poe.md) section 3 detect nibble / class nibble / power-good). Mapping individual handlers to named states (detection, classification, power-on, overload, disconnect/MPS) was not done: I.

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
- Meaning of the SFRs 0xBC..0xBF, 0xDD, 0xF5..0xF7, 0x93, 0xA1..0xA4, 0xD1..0xD4 (non-standard, chip-specific) and of the 0x80C0..0x80C4 / 0x80F1 / 0x8103 blocks.
- Named state machine, and where class / budget policy lives (docs/poe.md section 7 "inside the chip FW").
