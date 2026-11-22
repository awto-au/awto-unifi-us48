# PoE signalling: detection, classification, power-up, limits

- Values marked IEEE are the 802.3af/at (clause 33) figures for a Type 2 PSE, from the standard, not read from the BCM59111 and not checked against it.
- Values marked BCM are this chip's registers; marked bench are measured on this board (front 40 camera, PoE-10 ch3, 2026-11-22).
- Register map: `docs/poe.md` section 3; policy and budget: `docs/poe-pse.md`.

## Sequence (one port)

| Step | What the chip does | IEEE value | Chip / bench |
|---|---|---|---|
| 1 Detect | Two probe voltages on the pair, measures the slope (V/I) | probe 2.7..10.1 V at the PD, step >= 1 V, source <= 5 mA | BCM: enable bit `0x14` low nibble; result in `SAT_Pn` `0x0c+c` bits 3:0 |
| | Valid signature | 23.75..26.25 kOhm (25 kOhm nominal), parallel C 0.05..0.12 uF | |
| | Invalid / reject | R < 15 kOhm or > 33 kOhm; 15..23.75 and 26.25..33 kOhm = PSE may choose | |
| | Detect time | <= 500 ms | bench: open port keeps cycling detect, `SAT` 0x06 |
| 2 Class | Class probe voltage, reads the PD's class current | 15.5..20.5 V, I limit 50 mA | BCM: enable bit `0x14` high nibble; result `SAT_Pn` bits 6:4; bench: camera `SAT` 0x44 = detect 4 (good), class 4 |
| | Class current ranges | class 0: 0..4 mA, 1: 9..12, 2: 17..20, 3: 26..30, 4: 36..44 mA | |
| | Class time | 10..75 ms per event | |
| | Type 2 (class 4) | two class events separated by a mark event of 6.9..10 V | |
| 3 Power-up | Switches the full port voltage on with inrush limiting | 50..57 V at the PSE output; inrush <= 450 mA for 50..75 ms | bench: 52.2..52.4 V at the port; enable to delivering 1.2 s |
| | Power-good | port voltage reached, PD out of inrush | BCM: `STAT_PWR` `0x10` bits 7:4 (ch0 = bit 4), power-enable bits 3:0 |
| 4 Run | Overload cutoff Icut | Type 2: 684..~750 mA minimum cutoff; held for tOVLD 50..75 ms before shutdown | BCM: ICUT `0x47+5c` |
| | Current limit | short-circuit / inrush clamp, above Icut | BCM: ILIM `0x48+5c`, stock default `0xc0` |
| | Maintain power signature (MPS) | PD must draw >= 10 mA (Type 2: 10 mA) at least 60 ms in every 360 ms | BCM: counter `0x63+4c` (MPS absent) |
| | Removal | port voltage below 2.8 V within 500 ms of MPS loss | |

## BCM registers

- ICUT cutoff mA = 18.75 x (range + 1) x count (Meraki GPL `bcm_poe.c`); count in low bits, range in bits 7:6, bit 7 "always 1" there. Raw 1..17 with range 0 = 18.75..319 mA.
- Stock class-4 power-up writes ICUT `0x11` (17); in auto mode the firmware rewrites ICUT / ILIM from class tables at power-good.
- Measurement: CM `0x30+4c` 16-bit, 122.07 uA per count; VM `0x32+4c` 16-bit, 5.835 mV per count; TEMP `0x2c` = 367 - 2 x raw (deg C).
- Detect nibble (`SAT_Pn` bits 3:0): 1 short, 2 CPD too high, 3 R-sig too low, 4 good, 5 R-sig too high, 6 open circuit.
- Counters `0x60+4c`: b0 invalid signature, b1 power denied, b2 short / overload, b3 MPS absent.
- Operating mode `0x12`, 2 bits per channel: 0 off, 1 manual, 2 semi-auto, 3 auto (stock default). Limits written by the host are honoured outside auto.

## Board-specific

- MSPI mode-select field must be 2 (`us48_boardio.poe_field`, default 2); with 0 every port reads open (`docs/poe.md` M1).
- Bench camera (class 4): 52.3 V, steady 43..44 mA (about 2.3 W) idle; driver showed up to 7.7 W under activity.
- Sweep of ICUT raw 17..1 on this camera: powered down to raw 2 (37.5 mA), tripped at raw 1 (18.75 mA); why a 44 mA load holds at 37.5 mA is open (#26).
