# Stock LEDUP program (switchdrvr) and the chain format it implies

Issue #14. Legend: **C** = confirmed from the binary / Ghidra export, **I** = inferred, **BT** = board-test item (see [leds.md](leds.md)).
Extraction: `python3 scripts/ledup_extract.py` (log `tmp/logs/ledup_extract.log`); disassembly: `python3 scripts/ledasm.py --disasm FILE`.

## Extraction (C)

- Binary: stock rootfs `bin/switchdrvr` (ELF32 ARM, PT_LOAD VA `0x10000` = file offset 0, so offset = VA - `0x10000`).
- Program: VA `0x15a38ac` = file offset `0x15938ac`, 256-byte slot, 164 bytes used, rest zero. Slots `0x15a39ac/3aac/3bac/3cac/3dac` are the other board variants (168/186/167/152/152 B); not used by the US-48.
- Loader `0x120ed64(unit, uc 0, prog, 0x100)` (via `0x120f064`), called from `0x5fb3cc` (`ubnt_hal_sys.c`, board cases `0x33`/`0x35`):
  - Clear LEDUP `CTRL` bit 0, write 256 program words (zero-padded past the length), zero data-RAM words `0x80-0xff`, set `CTRL` bit 0.
  - Same program (`DAT_005fb5cc` = `0x15a38ac`) on **both units** (loop unit 0, 1). No per-unit program.
  - No data-RAM values written by the loader beyond the zeroing; no `CLK_PARAMS` write, no scan-delay write.
- Link state comes from the SDK linkscan callback registered right after the load (`bcm_linkscan_register(unit, 0x6fb38)`): `0x6fb38(unit, port, info)` RMWs data-RAM byte `0xa0 + port`: bit 0 = (`info->linkstatus == 1`), bit 7 cleared, other bits kept. `port` = logical (SDK) port. C.

## Program bytes (164 B)

```
02 02 60 e0 06 e0 d2 0a 70 16 67 75 28 67 60 06
e0 80 60 e0 77 04 06 e0 d2 1a 70 26 28 67 60 06
e0 80 60 e0 77 16 06 e0 28 67 40 06 e0 80 60 e0
80 28 67 40 12 e1 85 05 d2 05 71 3e 52 00 3a 38
67 86 75 8f 32 00 32 01 b7 97 75 55 16 e1 da 02
71 8f 67 55 57 32 03 32 04 c7 97 75 96 77 9d 57
67 86 75 8f 32 00 32 01 b7 97 75 7e 16 e1 da 02
71 8f 67 7e 57 90 90 a2 07 c2 07 80 80 57 32 04
97 75 96 77 9d 57 12 a0 06 e0 f8 15 1a 00 57 32
0e 87 32 0e 87 57 32 0e 87 32 0f 87 57 32 0f 87
32 0e 87 57
```

## Annotated listing

Names (OpenBCM `tools/led/example/*` convention): scan-chain status bits `RX 0`, `TX 1`, `SPEED_C 3` (100M), `SPEED_M 4` (1G), `ZERO 0xe`, `ONE 0xf`. `L` = data RAM `0xe0` (logical port), `PH` = data RAM `0xe1` (activity phase, 0..4).

| PC | Code | Meaning |
|---|---|---|
| 00 | `ld a,2; ld (L),a` | L = 2 |
| 04 | `ld a,(L); cmp a,10; jz 16` | loop L = 2..9 |
| 0a | `call 75; port a; call 60` | `port` = `11 - L` (physical 9..2); emit one port via 60 |
| 0f | `L++; jmp 04` | |
| 16 | loop L = 10..25 | `port a` with `a = L` (physical = logical), `call 60` |
| 26 | `port L; call 40` | L = 26: SFP+/SFP cage 0 (physical 26) |
| 2b | `L++; a = L+1; port a; call 40` | L = 27: scan port 28 (physical 28 = cage 1); the link byte is read at L = 27 |
| 34 | `PH++; if PH >= 5: PH = 0` | blink phase, once per refresh |
| 3e | `send 56` | 26 ports x 2 bits = 52, plus 4 trailing zero bits (pack area zeroed by the loader, I) = 28 slots |
| 60 | GE port, 2 bits | `call 86` (C = link, bit 0 of `0xa0 + L`); no link: `jmp 8f` "00". Activity = `pushst RX, pushst TX, tor, pop`. Activity and `PH < 2`: "00". Else `7e`: `pushst SPEED_M`: 0 "01", 1 "10" |
| 40 | cage port, 2 bits | link as 60; activity phase as 60; colour at 55: `pushst SPEED_C, pushst SPEED_M, tand`: both (10G) "10", else "01" |
| 75 | `a = ((a-2) ^ 7 & 7) + 2` | logical 2..9 -> physical 9..2 (docs/port-map.md) |
| 86 | `b = (0xa0 + L); tst b,0` | C = link bit |
| 8f / 96 / 9d | `pushst ZERO/ONE; pack` x2 | emit "00" / "01" / "10" (first pushed = first on the wire) |

## What it does (C code, I wire meaning)

- Per chain (both units alike): 26 ports in ascending **logical** order = front 1-24, then cages. Unit 0: front 1-24, SFP 1/2 (front 51/52). Unit 1: front 25-48, SFP+ 1/2 (front 49/50). Logical 2..25 = front order, so "front ascending then the two cages".
- 2 bits per port, then 4 zero bits (56 bits, 28 slots).
- Per port symbol, first bit sent first:
  - "00" off: no link (data RAM bit 0 clear) or activity-off phase.
  - "10": GE 1G (`SPEED_M`); cage 10G (both speed bits).
  - "01": GE 10/100M; cage anything but 10G (so a 1G SFP in the SFP cages is "01").
  - "11" is never sent. Polarity: no inversion (active-high symbols, no padding bits other than the 4 trailing zeros).
- Activity: RX or TX status bit sampled each refresh; while set the symbol is "00" for 2 of 5 refreshes (`PH` 0, 1) and normal for 3. Idle port: steady.
- Link comes from the CPU (linkscan callback). Speed and activity come from the hardware scan-chain bits: stock relies on them being valid on HR2 (C code, I that they are valid: BT).
- Not set by the loader: `CLK_PARAMS`, scan delays, pad mux (LED_CLK/LED_DATA are CMIC pin config elsewhere, see board.md).

## Published programs

- Compared (2026-11-19) with every `tools/led/example/**/*.asm` (77 assembled with `scripts/ledasm.py --find`; ~30 pre-HR legacy files use macros our assembler does not take) and the hex programs in `rc/{hr3_led,gh_ledup}.soc` of OpenBCM sdk-6.5.16 (`Broadcom-Network-Switching-Software/OpenBCM`).
- None identical; byte similarity at most 0.25 (hr3_led.soc, HR3 `bcm956160k`). So Ubiquiti's own program (or a variant of an unpublished reference). Family resemblance only: data RAM `0xa0` link bytes, `0xe0` port counter, `call`/`ret` per-port emitters, the same `pushst` constants (`bcm956160r_opt6` uses the same style).
- Hence the bytes are committed here.

## Our program vs stock

- Kept ours (`kernel/us48-cmic/us48_ledup.asm`): the CPU owns policy (blink, per-bit offload), stock has fixed policy. Stock is not strictly better.
- Matched to stock: bit order on the wire (LSB first, so our bit 0 = stock first bit = 1G/10G colour), 2 bits, no polarity inversion, slot order (ascending front, cages last), 28 slots (56 bits) per chain, same program on both units.
- Activity: same as stock since #14 fix (RX|TX -> blank while PH < 2 of 5). Needs the SDK scan setup (PORT_ORDER_REMAP etc., [leds.md](leds.md)), which stock gets from `soc_hurricane2_misc_init`, not from this program.
- Differences: stock colours cages by its speed scan bits, ours by the offload flags (defaults = stock symbols); stock never blinks for other conditions.
