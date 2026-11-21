# poe-fw status (phase 1, offline, #26)

- Evidence: **C** = confirmed in the stock image / spec, **I** = inferred, **G** = our guess, **U** = hardware meaning unverified.
- DONE = implemented and exercised in the simulator. Nothing here has run on a chip.
- Stock = `hwdb/firmware/bcm59111-v21.bin`. Spec = [docs/poe-fw.md](../docs/poe-fw.md). Overview / bench plan = [docs/poe-fw-rewrite.md](../docs/poe-fw-rewrite.md).

## a. Image layout

| Item | State | Evidence |
|---|---|---|
| 16 KiB file, CPU base 0x1A00 | DONE (build refuses otherwise) | C |
| 15-record hook table `[FF hi lo]` at offset 0, slots 0..14 as stock | DONE | C table, I slot roles |
| Slot 0 start-up, 1 I2C, 2 event ISR, 3 periodic, 4..7 channel ISRs, 8 timer 1, 9..11 RET at 0x1AFD..FF, 12..14 RET at 0x5220..22 | DONE | C / I |
| 0x1AFC user-init RET, 0x1B00 `LJMP` ROM entry (stock IDATA clear, `DD = 0xB0`, `0x804A = 0`) | DONE, copied verbatim | C |
| Hooks are plain functions (`PUSH PSW; MOV PSW,#bank; ...; POP PSW; RET`, no RETI) | DONE (asm wrappers) | C |
| Tag `POETEC_IMAGE+...` at 0x5300 | DONE (stock string, `POWERUO` typo fixed, `+AWTO_OSS` appended) | C |
| `FW_REV` = **0xA1** (new; stock 0x15, Meraki 0x12); read semantics unchanged (Linux driver only logs it) | DONE | C |
| `CRC_STATUS` 0xAA | DONE, written **after** init (stock writes it first) | C |
| Host dispatch tables | DONE, CONST at 0x3000 (stock 0x26EE: our code is larger, no need to match) | C |

## b. Host I2C slave

| Item | State | Evidence |
|---|---|---|
| ISR status sequence (`DD & 0x11`, `F5`/`F6` values, 0xA8/0xB8/0x80/0xA0/0x60, error re-arm) | DONE, SFR value sequence copied | C sequence, U meaning |
| Register pointer, first byte after SLA+W = pointer, auto-increment on read and write | DONE, matches stock in sim | C |
| Write dispatch table: 27 handlers + `0x16` no-op (28 writable per spec) | DONE | C |
| Read dispatch: clear-on-read 03/05/07/09/0B (INT bit masks as stock), counters 60..6F | DONE, matches stock in sim | C |
| INT line (`F8.0`): raised by `evt_raise`, dropped by handlers / `INT_MASK` write | DONE | I |
| Bus error: `0x8042 \|= 1`, `0x804A = 1`; main loop reload | PARTIAL: flag set; the per-channel reload action (stock 0x4CBA) is not ported on purpose (unknown meaning, no port action) | C / TODO |
| `WDT_TIME` (0x42) -> `SFR F9` | DONE | C |
| Push-button regs 0x18 / 0x19 / 0x1A | PARTIAL: bit layout of 0x18 from stock 0x230D (detect / class request); 0x19 / 0x1A layout is **G** (LTC4266 style) | I / G |
| ICUT / ILIM / HP_MOD / HP_EN / timer regs 0x1E 0x1F | PARTIAL: stored, read back, dirty flags set; **not applied to hardware** (stock `calc_limit` 0x3334 + math unit not ported) | C store, TODO apply |
| Regs 0x5E / 0x5F consumer (stock 0x230D: event-to-channel map, 0x80BF..C3) | TODO (defaults only) | I |
| Divergence: table index masked to 0..0x7F (stock reads past the table for pointers >= 0x80) | DONE | n/a |

## c. Channel state machine

| Item | State | Evidence |
|---|---|---|
| States and transitions of docs/poe-fw.md section 11, 10 ms tick, timers (50 / 200 / 100 / 75 / 30 / 60 / 20 ticks) | DONE (`chan.c`) | I |
| Detect: `D1..D4 = 1`, 50-tick window, `= 2`, result `>> 4`, code 4 = good | DONE, SFR sequence from stock 0x4601 / 0x1A60 / 0x3B3C | C sequence, U meaning |
| Detect result scaling (stock 0x3651, SFR D9..DB) | TODO | I |
| Class: `D5 \|= bit`, `D6 \|= bit`, 100-tick window | PARTIAL: result **not read** (stock sequencer 0x316B not ported); reports class 0 (SAT bits 6:4 = 6) | G |
| OP_MOD modes: shut -> reset; manual needs push-button request; semi-auto and auto power up after a good detect | DONE, **auto is G**: stock auto is sequenced by 0x316B / 0x30C3 (limits, B-bit2 trigger), ours drives `F1` like stock semi-auto | I / G |
| Power-up: `C9` gate, `F2 \|=`, `F1 \|=` (HP bit decides mask), 20-tick hold | DONE (not in `-nopower` image) | C sequence |
| Power good | PARTIAL: assumed after the hold, no hardware good indication read | U |
| Fault (`ev_hi & 0x41`) and disconnect (`ev_hi & 0x20` / `ev_lo & 0x30`) from the channel ISR flags, cool-down 75 ticks, auto re-detect (AUTO_FAULT_CLEAR) | PARTIAL: flag-to-meaning mapping is **U** | I / U |
| Counters 0x60..0x6F (invalid signature, short, MPS absent); denied / overload not incremented | PARTIAL | G |
| Per-port enable / disable via OP_MOD, `DET_CLASS_EN` nibbles, `DIS_SEN_EN` | DONE | C |
| One analog probe at a time (`probe_owner`) replacing stock 0x3DA6 / 0x316B arbitration | DONE, **G** | G |
| Temperature check (SUP_EVT when TEMP raw < 0x40) | gated off: TEMP stays 0 without the measurement chain | TODO |

## d. Analog / unknown hardware (`src/hal_analog.c`)

Every access names the stock function. Nothing is invented: DONE items copy the stock SFR write order.

| Item | State |
|---|---|
| SFR init (A1..A5, A7, B1..B4, C9, E5..E8, F1/F2, 96, FF, T2CON.6, IP.2..5) | DONE copy of stock 0x39F3..0x3A2F |
| Timer 1 / EX1 init, `93` from P2 | DONE copy |
| Detect (D1..D4), class (D5/D6), power (F1/F2), gate (C9), ctrl (B1..B4), limit (A1..A4) accessors | DONE copy |
| Measurement chain AB/AC/AD, B5..B7, E9..EF math unit (stock 0x1B03 0x37D2 0x34D0 0x29EE): TEMP, CM, VM | **TODO** (registers read 0) |
| Class result, power-good indication, limit programming (0x316B, 0x30C3, 0x3334) | **TODO** |
| ROM strap call (`0x1900` / `0x1903` pointer in ROM, stock 0x3CF1) | DONE copy (U: ROM content unknown) |

## Deliberate differences from stock

- `OP_MOD` starts 0 (all ports shut); stock leaves whatever reset / ROM left (feature `DEFAULT_AF_AUTO_MODE`).
- `CRC_STATUS` is written last in init.
- Hook entry at slot 0 sets `PSW = 0` (C code needs bank 0).
- Dispatch index mask, no port action on I2C bus error, temperature check gated.
- Internal state layout is ours (struct at XRAM 0x8078, stride 20; IRAM data 0x30..0x76, stock used up to 0x7C).

## Endianness

SDCC is little-endian, stock Keil is big-endian. Internal words are native little-endian (no stock internal words are mirrored). Converted in one place (`HREG_PUT_BE16` / `HREG_PUT_LE16` in `inc/common.h`):

- `0x805E:0x805F` = `0x00E4`: written big-endian as stock (`init_fw_rev_crc`).
- `CM` / `VM` register pairs `0x30..0x3F`: LSB first (host contract, UDM driver); written zero for now.
- Hook table, dispatch tables: `[FF hi lo]` written explicitly in asm (`.db 0xFF, >sym, sym`).
- Download framing (`0x70`, offsets BE16) is ROM work, not in this image.

## Code size, ours (awto-sdcc, small model, `--opt-code-size`) vs stock Keil

Measured by `scripts/poe_fw_build.py` (log `tmp/logs/poe_fw_build.log`, `tmp/poe-fw/sizes.tsv`). Roles, not equal scope: ours omits the measurement chain, ICUT/ILIM apply, class result. Stock sizes from `bcm59111-v21.functions.tsv` (stock merges common tails, so per-symbol stock sizes undercount shared code).

| Role | ours B | stock B |
|---|---|---|
| I2C slave ISR (body + hook wrapper) | 192 | 174 |
| table dispatch | 38 | 17 (plus shared Keil helpers) |
| channel ISRs x4 (asm) | 500 | 487 |
| event ISR 2 | 108 | 69 |
| timer tick (hook + tick) | 253 | 218 |
| periodic ISR | 19 (empty) | 1869 (measurement chain) |
| channel state machine + arm / timers | 2164 | 2639 (states only; stock helpers elsewhere) |
| start-up / init / main loop | 444 | 643 |
| host write handlers | 274 | 78 |
| host read handlers | 270 | 63 |
| poll / events / INT helpers | 495 | 1330 |
| **all code (CSEG)** | **5166** | stock code 0x1A60..0x5260 = 14336 |

Total image bytes occupied (non-zero-fill, incl. tables and tag): see the build log (`image bytes used`). No size win is claimed: for the same scope ours is not smaller (I2C ISR, dispatch, handlers, ISR 2 are larger); the smaller total is mostly missing functionality.

## Layout versus stock (measured on the built images)

| Item | stock v21 | ours (`bcm59111-awto.bin`) |
|---|---|---|
| file size / base | 16384 / 0x1A00 | 16384 / 0x1A00 |
| hook slots 0..8 | 3CE3 3F10 4879 4EBA 4255 40DD 42CD 4345 4FCD | 2EB0 2EB6 2EC1 2ECC 2CAA 2D27 2DA4 2E21 2EDB |
| hook slots 9..14 | 1AFD 1AFE 1AFF 5220 5221 5222 | identical |
| 0x1B00 | `LJMP 0x50EA` | `LJMP 0x2E9E` |
| code starts | 0x1A60 | 0x1B03 (0x1AFC..0x1B02 fixed entry block) |
| host tables | 0x26EE / 0x286E | CONST at 0x3000 |
| tag | 0x5300, `...+DEFAULT_AF_AUTO_MODE` | 0x5300, `...+DEFAULT_AF_AUTO_MODE+AWTO_OSS` (typo `POWERUO` -> `POWERUP`) |
| code extent | 0x1A60..0x5260 (14336 B) | 0x1B03..0x3304 incl. tables (CSEG code 5166 B) |
| IRAM data | 0x24..0x7C | 0x30..0x76 |
