# BCM59111 PSE firmware rewrite (phase 1, offline)

Issue #26. Decision (Dan 2026-11-21): core is a DW8051, reversal spec ([poe-fw.md](poe-fw.md)) is clear, write the firmware ourselves. Vendor binaries may live in the repo. Status per item: [../poe-fw/STATUS.md](../poe-fw/STATUS.md).

## Scope

- Open-source replacement for the stock v21 image (`hwdb/firmware/bcm59111-v21.bin`), same load contract: 16 KiB, CPU base 0x1A00, hook table at offset 0, loaded into chip RAM by the host on every boot (no flash).
- Source `poe-fw/` (C, awto-sdcc mcs51 small model + asm skeleton). Hardware access isolated in `poe-fw/src/hal_analog.c`, each access names the stock function it copies.
- Register header and dispatch tables are generated from `hwdb/firmware/bcm59111-regs.tsv` (`scripts/poe_fw_genhdr.py`); the build fails if they are stale.
- Phase 1 = no board. Nothing has run on a chip.

## Done / not done (details: STATUS.md)

- DONE: image layout, hook table, host I2C slave (all registers, auto-increment, clear-on-read, INT line), channel state machine skeleton (detect -> class -> power-up -> monitor, fault / disconnect, auto re-detect), build + layout checks, simulator tests.
- PARTIAL: class result, power-good indication, ICUT / ILIM application, push-button regs 0x19 / 0x1A, auto mode (ours behaves like semi-auto).
- TODO: measurement chain (TEMP, CM, VM: registers read 0), detect scaling, class sequencer, limit programming (stock 0x3334 + math unit E9..EF), regs 0x5E / 0x5F consumer.
- Consequence: temperature, voltage and current registers are zero; the Linux driver sees no usable temperature / power numbers from this image.

## Build and test

- `python3 scripts/poe_fw_build.py`: writes `hwdb/firmware/bcm59111-awto.bin` and `bcm59111-awto-nopower.bin` (no power-up path), checks layout, log `tmp/logs/poe_fw_build.log`, per-function sizes `tmp/poe-fw/sizes.tsv`. Exit 1 with the reason on any layout failure.
- `python3 scripts/poe_fw_sim.py [--stock-diff]`: ucsim_51 `-t RL6449` (DW8051 model), log `tmp/logs/poe_fw_sim.log`. `--stock-diff` runs the same I2C scenarios on the stock image: the only observed difference is `FW_REV`.
- Harness notes: every ucsim `step` is sent alone (queued console input aborts a running step), a free TCP port per run, IRAM zeroed like the stock `rom_init`, ROM 0x03..0xFF filled with RETI, simulated interrupts off (the harness calls the hooks).
- Sim limits: ROM (< 0x1A00) absent (vectors RETI, strap pointer -> RET), analog SFRs are plain registers (detect result forced), no timing model beyond ticks injected by the harness.
- Regenerate header: `python3 scripts/poe_fw_genhdr.py` after editing the regs TSV.

## Loading with the existing driver

- `kernel/bcm59111` (`fw_load=1`) calls `request_firmware()` with the DT property `firmware-name` of the chip node, default `bcm59111-v21.bin` (default unchanged).
- Test image: copy `hwdb/firmware/bcm59111-awto-nopower.bin` (first) to `/lib/firmware/` on the board and set `firmware-name = "bcm59111-awto-nopower.bin";` on ONE chip node in the DT.
- The driver only downloads when reg 0x75 != 0xAA: the chip must have no firmware (cold chip / reset). The driver does not pulse the reset line (GPIO 9, M3): a chip running stock firmware is not reloaded.
- FW_REV reads 0xA1 (driver logs it only). The driver's post-load config (`0x13=0f`, `0x14=ff`, `0x44=0f`, ICUT / ILIM) works on the register interface; ICUT / ILIM values are stored but not applied to the analog side yet.

## Risk

- The firmware is loaded into RAM every boot: a crashing or hung image is cured by a reload / chip reset.
- A wrong power-up sequence can drive 54 V onto a port. Our power-up path (`F2 |=`, `F1 |=`, `C9` gate) copies the stock SFR writes but replaces the stock auto-mode sequencing (limits, B-bit trigger) with a guess, and takes "power good" from a timer. Limits A1..A4 stay at the init value 0xF0.
- Therefore: bench steps start with no PD attached and all ports shut (OP_MOD = 0, which this image also sets at start), and the first loads use the `-nopower` image, which never writes `F1` and never reports power good.
- MSPI port fields (docs/poe.md section 6) stay `0` for the whole bench.

## Bench plan (first board run)

One chip only, one port at a time, Dan present. Steps 1..3 are read-only apart from the firmware download.

1. Cold chip, no PD on any port of that chip, all MSPI fields 0. Load `-nopower` image on that chip (DT `firmware-name`, `fw_load=1`).
2. Read-only compare against stock (a second chip with stock firmware, or a saved stock register dump): regs 0x41 (0xA1 expected), 0x43, 0x75 (0xAA), 0x00..0x11, 0x12 (0 expected), 0x13 / 0x14 / 0x44 / 0x46..0x58 after the driver's config, 0x2C and 0x30..0x3F (zero expected: not ported). Record differences in #26.
3. Write / read round trip on 0x13, 0x14, 0x1E, 0x1F (restore after); read-clear behaviour of 0x03 / 0x05 / 0x07 / 0x09 / 0x0B and 0x60..0x6F; confirm the INT pin (CCA IRQ line) behaves as with stock if wired.
4. Detect / classify on a known PD (dummy load with known signature): port still shut -> set OP_MOD for that port to auto (`0x12`), `-nopower` image: expect SAT_Pn detect nibble 4 (good), no power on the pair (meter on the port before connecting anything powered). Compare the detect nibble with stock on the same PD.
5. Only after step 4 matches stock and the analog values are understood: load the full `bcm59111-awto.bin`, no PD, then the known PD with a meter. Check `F1`-driven power-up, hold time, fault / disconnect handling against 802.3at windows. Not before the measurement chain is ported (current limits and CM / VM are not available).
6. Stop and file in #26 at the first difference from stock that is not listed in STATUS.md.

## Bench results (2026-11-21, chip PoE-12 = 8-002d, `-nopower` image, all ports shut, no PD)
- Cold chip load needs a valid image CRC: stock v21 and Meraki carry CRC-16 (poly 0x8005, init 0, no reflection) big-endian at file offset 0x38FE over 0x0000..0x38FD (residue over 0x0000..0x38FF = 0). Without it the ROM never starts the image (reg 0x75 = 0xFF, reg 0x41 = 0xFF, reg 0x43 = 0xC0 ROM id). `poe_fw_build.py` now writes and checks it.
- Step 1 PASS: driver logs `firmware rev 0xa1, 4 ports shut down, firmware loaded`.
- Step 2 PASS: regs 0x0C..0x5F vs stock chip 8-002c differ only at 0x41 (0xA1 vs 0x15, expected), 0x11 SAT_PIN (address pins: 0x28..0x2D read 08 18 28 38 0C 1C on stock and test alike), and 0x2C / 0x2E / 0x2F (stock reads measurement values, ours 0: measurement chain is TODO). 0x12, 0x13, 0x14, 0x44, 0x46..0x58, 0x5F identical.
- Step 3 PARTIAL: write/read round trip on 0x13, 0x1E, 0x1F verified and restored; 0x12 stays 0, 0x75 stays 0xAA. Not run: clear-on-read behaviour, INT pin, 0x14 / 0x44 writes.
- Steps 4..6 not run (need a PD and a meter; measurement chain, class sequencer and limits are TODO).
- Bench loading: `tmp/poe-fw-override` (untracked, `PoE-NN=<file>`) is staged into the image by `build_mainline.py`; `S06us48-poe` then passes `fw_override=` to the driver and keeps all ports shut. `scripts/poe_fw_bench.py` does the register compare (it needs ssh to the board; over serial use `i2cdump -f -y -r 0x0c-0x5f 8 0x2d b`).

## Host watchdog (awto image only)

Dan 2026-11-21: the chip must be poked every 200 ms, else drop all PoE and tell the driver; the driver can request a restart. Present in both images (`-nopower` too).

- Registers (added to `hwdb/firmware/bcm59111-regs.tsv`, source `awto rewrite`; stock ignores both):
  - `0x20` HOST_WD_TIME, W (read returns the last written value): timeout in 10 ms ticks (timer-1 tick, docs/poe-fw.md section 17). 0 = disarmed. Non-zero write arms and reloads the countdown = the poke; writing the same value again pokes. 1 is treated as 2 (range 2..255). 200 ms = 20.
  - `0x21` HOST_WD_STAT: read b0 EXPIRED (latched, not cleared by reading), b1 ARMED, b7 = 1 always (feature marker, no FW_REV test needed). Write b7 = 1 = restart request; writes without b7 do nothing.
- Why not `0x42` WDT_TIME: stock semantics are kept; it programs the 8051's own watchdog period (SFR F9, `wh_wdt_time`).
- Boot default: disarmed, OP_MOD 0 (all ports off) until the host arms it.
- Countdown: one decrement per timer-1 tick while armed (`wd_tick` in the tick hook). Expiry = the 20th tick after the last poke for a value of 20.
- Expiry:
  - tick hook: latch EXPIRED, clear ARMED (cannot re-fire), SUP_EVT |= 0x40 (both copies), INT bit 7 (SUP) set, INT output (`F8.0`) asserted if INT_MASK b7 and MISC_CONF b7 allow (same gating as every other event).
  - main loop (next pass, well under a tick): OP_MOD := 0 and the PORT_RESET path for every channel (power off, `F1` / `F2`, SAT_PWR cleared, PWR_EVT raised). No new analog sequence.
  - INT choice: all 8 stock INT bits are taken; SUP (bit 7) with SUP_EVT bit 0x40 is used (stock uses 0x80 for invalid temperature). The host reads 0x0B to clear the INT cause; EXPIRED stays latched until a restart.
- While EXPIRED: OP_MOD writes are stored but the state machines treat the mode as 0, nothing powers. Arming while EXPIRED is accepted (the watchdog runs, EXPIRED stays).
- Restart (STAT write b7 = 1): clears EXPIRED and the SUP_EVT 0x40 / INT bit 7 cause, line off, disarms; main loop shuts all channels (OP_MOD 0) and re-initialises the state machines. The host then writes OP_MOD and re-arms with HOST_WD_TIME.
- No watchdog action on I2C bus errors.
- Tests: `scripts/poe_fw_sim.py` section "host watchdog" (disarmed default, arm, pokes at 10 / 19 / 28 ticks, expiry on exactly the 20th tick, outputs cleared, INT, no re-fire, OP_MOD while expired, restart, clamp, re-arm, disarm), both images.

## Bench results: host watchdog (2026-11-21, chip PoE-12 = 8-002d, `-nopower` image, ports shut, kernel driver `host_wd_ms` = 200)
- Load: `firmware rev 0xa1 ... firmware loaded`, then `host watchdog armed: 200 ms, poke every 100 ms` (driver detects the feature from HOST_WD_STAT b7).
- Steady state: 6 reads over several seconds, HOST_WD_STAT 0x82 (feature + armed), HOST_WD_TIME 0x14, no expiry, no poke failure.
- Forced expiry (`i2cset` HOST_WD_TIME = 2, i.e. 20 ms, between two driver pokes): STAT went 0x82 -> 0x81 (expired, disarmed) within ~40 ms; the next driver poke cycle logged `firmware host watchdog expired: all PoE was dropped; restarting and re-enabling 0 port(s)`, wrote the restart; STAT back to 0x82, TIME 0x14, OP_MOD 0x00.
- Not tested: expiry with powered ports (the `-nopower` image never powers one; needs the full image and a PD), real stall of the kernel/workqueue, interaction with the measured-power watcher, INT line.

## Phase 2: measurement, class (offline, #26)

Ported from the stock disassembly (addresses = stock v21 CPU addresses). Evidence: C = code bytes, I = inferred meaning, G = our guess, U = hardware unverified. The sim stubs the analog side with one constant word, so its checks are arithmetic regressions of the port, not hardware validation.

### Analog SFRs as read from the stock code

| SFR | Fact | Ev |
|---|---|---|
| `AB` / `AC` / `AD` | Indirect analog register file: write index to AB, read AC (low) and AD (high); word = `AD << 8 \| AC` (0x34ED..0x3513, 0x37D8..0x37E0). Indices seen: `0x2C` temperature sensor, `0x30` temperature feedback value, `ch\|4` voltage word, `ch\|8` current / class-probe word, `ch` third word (stored, unused). | C access, I meaning |
| `B5` / `B6` / `B7` | Trim register file: B5 = index, B6 / B7 = signed 8-bit data. At start-up (0x415C): `0x14` -> temperature offset (B6), `0x10..0x13` -> per-channel offsets (B6 + base from `D7`), `1` -> slope (B7), `0` -> B7 constant and B6, `0x15` -> B6 added to the `0` value. | C access, I meaning |
| `D7` | Trim word: bits 5:0 (sign bit 5) base for the per-channel offsets, bit 7 flag (0x416C..0x4181). | C / I |
| `AF` | Analog mux for the class sample: `ch*4 + {1, 2, 3, 5}` (0x2A3E, 0x2ACB, 0x2AFC, 0x2B49, 0x2ADC). | C values, I meaning |
| `D1..D4` | Detect: write 1 to start, 2 for the second phase, result `>> 4` = detect code (4 = good) (0x4601, 0x1A60, 0x3B3C). | C / I |
| `D5` / `D6` | Class start: both OR-ed with the channel bit, both cleared by 0x227C. | C / I |
| `A1..A4` | Per-channel limit byte, init 0xF0; auto mode loads it from table 0x4C03 indexed by class code (`F0 48 84 F0 CC F0 F0 F0`), F2 enable from table 0x4C0B (only class code 4) (0x30DA..0x30EA). | C tables, I meaning |
| `F1` / `F2` | Power enable set / enable; stock semi-auto path ORs F1 (0x4A31). | C |
| `C9` | Equals `~DIS_SEN_EN` after a host write (0x23EF..0x23F8): disconnect-sense gate. | C |
| `E9..EF` | Math unit: write `E9:EA` and `ED:EE` = 16x16 -> 32 multiply, result `E9..EC` after one NOP (0x4764); `EF = 0x20 \| n` = 32-bit logical shift right by n on `E9..EC` after ~8 NOPs (0x4777; n = 3, 6, 10, 14). Replaced by C arithmetic here. | C |
| Class decode | ADC word thresholds `0x6F 0xF7 0x189 0x233 0x322` give code 6 (class 0), 1, 2, 3, 4, 7 (above class 4) (0x4900). The code is `SAT_Pn` bits 6:4, exactly as the kernel driver reads it. | C |

### Ported

- Calibration (0x415C), TEMP raw (reg 0x2C = low byte of `(offset + word(AB=0x2C)) >> 1`), regs 0x2E / 0x2F (stock bench read 0x89 / 0x23 on a live chip), per-channel VM (`word(c|4) * 11`) and CM (`word(c|8) * 3201 >> 10`, 6403 for class 4) for powered channels, class code from the ADC word, class-table limit `A1..A4` and F2 enable at power-up, temperature check (SUP_EVT 0x80 when TEMP raw < 0x40) now live.
- Runs from the main loop once per 10 ms tick; stock runs it from the periodic ISR at an unknown rate (G). Temporaries in XDATA (IRAM is full: data ends 0x7F; stock used up to 0x7C).

### Not ported (TODO)

- 0x1B03 current offset trim (feedback writes to `AB = c | 4`): CM / VM carry the uncorrected offset.
- The 0x29EE class mux schedule (AF 1 / 2 / 3 / 5 with four sample points) and the 0x316B / 0x30EF sequencer: ours does one sample 14 ticks into the 100-tick class window (tick unit G).
- Power-good from hardware (the event bit that 0x2F8C waits for is not identified): still a 20-tick hold.
- ICUT / ILIM apply (0x3334 `calc_limit`, math unit, clamps 0x0A / 0x15 / 0x2A): host values are stored, the limit byte comes from the class table only. HP_EN is read for the F1 mask only.
- Stock auto-mode trigger (clear `B(ch).bit2` after programming limits, 0x30EF..0x3126): ours still uses F1.
- Detect result scaling (0x3651, SFR D9..DB).

### Which image for which bench step

| Image | Never powers | Detect | Class | TEMP / CM / VM | Use |
|---|---|---|---|---|---|
| `bcm59111-awto-nopower.bin` (this is the detect-only image: no separate third variant) | yes | yes | yes | yes (CM / VM stay 0: no channel is powered) | bench steps 1..5: register compare, detect nibble, class nibble, TEMP |
| `bcm59111-awto.bin` | no | yes | yes | yes | step 6 only, after 4 / 5 match stock on the second port, PD = the camera, meter on the pair |

### Bench steps 4..6 with the camera (phase 2)

Chip PoE-12 runs ours, a second port of a stock chip (same camera, moved) is the reference. Host watchdog stays armed by the driver.
4. `-nopower` image: TEMP (reg 0x2C) vs stock (stock live read 0xA7 at a similar temperature; the unit differs by ~1 C per count), reg 0x2E / 0x2F vs stock (0x89 0x23 seen); camera on one port of ours: `SAT_Pn` detect nibble 4 and class nibble (camera class from the datasheet / stock reading) via the driver's port status; compare with stock on the same camera. A different class nibble means the AF mux / sample time (G) or the thresholds need work: record the `0x813x`-equivalent word by reading nothing else (we have no raw-word register: add a debug reg if needed).
5. Same with the camera moved between ports; power must stay off (check with a meter / the driver's mW = 0).
6. Full image, camera, ports shut first: driver enables one port. Expect power-good bit, `SAT_PWR` 0x11, driver mW and V plausible against the stock chip with the same camera (CM / VM use the uncorrected offsets: expect a small difference), disconnect: unplug the camera, expect MPS-absent counter (0x63) and SAT_PWR 0 within ~300 ms. Stop at the first difference; limits (ICUT / ILIM) are not applied from the host values yet (class table only).

### Board result of the phase-2 `-nopower` image (camera on chip PoE-07 ch1, MSPI field 2) and what is missing

- Observed: SAT_P0..3 stay 0x00 (stock: 0x06 empty, 0x4x with a class-x PD), SAT_PWR 0, reg 0x64 = 5 (invalid-signature counter of ch1), reg 0x04 = 0x02. Reading: our detect ran on ch1 five times and read detect code 0 (`D(n) >> 4` = 0, counted as invalid), so the detect result is not available from `D1..D4` with our start (D=1) / second phase (D=2) / read sequence. Likely cause (I): in stock the hardware probe is stepped by the periodic sequencer 0x316B (record states 3, 4, 5, 6, 0x0A, 0x0D, `D6 = 0x20 \| c << 6`, `D5` / `D6` writes, ADC sample thresholds 0x2A / 0x1E / 0x48 / 0x35 / 0x41 / 0x0C) and the `D` result is only valid after that sequence. That sequencer is **not ported** (TODO, ~300 B of stock code, tick unit of the periodic ISR unknown). Until then detect and class cannot work on real hardware; only the register / host-interface / watchdog / measurement paths are testable.
- New read-only diagnostics in this image (not in stock): regs 0x71..0x74 = live raw `D1..D4`, regs 0x24..0x27 / 0x28..0x2B = class-probe ADC word per channel (`AB = c | 8`) low / high bytes (0x76..0x7D would overlap our channel block at 0x8078). Dump these with a PD attached, with and without our state machine running, to see what the hardware returns.
- Stock live dump (camera powered on chip PoE-10 ch3, class 4): reg 0x2E / 0x2F = 0x1B / 0x23 = same word as VM ch3 (0x3E / 0x3F = 0x1B / 0x23): 0x231B = 8987 x 5.835 mV = 52.4 V, so 0x2E / 0x2F is the supply voltage word `F * 11` and VM of a powered channel equals it (code-consistent: both are `x * 11`, 0x34D0). CM ch3 (0x3C / 0x3D) = 0xB5 / 0x00. TEMP 0x2C = 0xA2. ICUT(ch3) 0x56 = 0x11 and 0x58 = 0x01 after power-up (stock writes both; ours leaves 0xE2 / 0x00: the limit sequencer 0x3334 is not ported). 0x12 = 0xC0, 0x11 = 0x38.
- To match stock on a powered class-4 camera port the new image would have to read: SAT_Pn = 0x4x with detect nibble 4 (class 4; the stored dump itself shows 0x0C..0x10 = 0x00 for the powered chip, so that capture does not show them: re-read), SAT_PWR (0x10) with the ch3 enable and good bits (stock naming: enable low nibble, good high nibble), TEMP in the same range (0xA2..0xA7), 0x2E / 0x2F = supply word (~0x231B..0x2389), VM / CM of the port = 0x231B / ~0x00B5, ICUT 0x11 and 0x4E / 0x58 = 1. Ours reaches none of these on hardware yet (no detect result).

## Phase 3 status: detect / class sequencer (NOT ported yet) and steady-state target

Stock steady-state reference (v21, two cameras delivering, coordinator capture 2026-11-21, `tmp/logs/poe_regs_stock_steady_c10_cam_p4.txt` / `_c7_cam_p2.txt`): empty port SAT_Pn = 0x06; camera port 0x44 (detect good, class 4); SAT_PWR = 0x88 (c10 ch3) / 0x22 (c7 ch1); OP_MOD 0xFF; TEMP 0xA0 / 0xA1; supply word 0x2E/0x2F = 0xFA 0x22 / 0x10 0x23; powered channel VM = the supply word (raw 0x22FA / 0x2305), CM raw 0x0333 (ch3) / 0x0139 (ch1); ICUT of the powered channel 0x11 and its +2 reg 0x01; unused channels 0. These are the expected values for bench steps A / B below; they are targets, not something the current image reaches.

What the disassembly shows about the sequencer (decoded in this phase, evidence C = code bytes; port not written):

- Entry `periodic_detcls_sequencer` 0x316B (called every periodic ISR pass after 0x29EE): runs only if XRAM 0x80C0 bits 7:4 (per-channel "sequencer active") are non-zero; works on one channel per call, `c = XRAM[0x8076]` (round robin). C
- Per-channel record at `0x8104 + 7c`: +0 state, +1 counter A, +2 counter B, +3 sample (`word(AB=c|4) >> 2`, low byte, computed at 0x317B..0x3195), +4 baseline, +5 / +6 further samples. Helper map: 0x1FB0 / 0x1FB5 state, 0x20B0 / 0x20B5 counter A, 0x20E3 / 0x20E5 counter B, 0x2017 / 0x2018 sample, 0x20D3..0x20D5 baseline, 0x2039 / 0x203E field +5, 0x2134 / 0x2135 field +6. C
- State dispatch (`keil_CCASE` table at 0x31C5, entries `{hi lo value}`): state 2 -> set state 3; 3 -> 0x31EB (state := 4, wait while `[+3] < 0x2A`, then 0x32C4: state := 0x0D); 4 -> 0x3205 (counter A++, at counter A >= 0x1E: state := 5, `D6 = ((c << 6) \| 0x20)`; else `[+4]` update and compare against 0x28); 5 -> 0x321F; 6 and 7 -> 0x3245; 8 and 9 -> 0x32DB; default -> end. After the dispatch (0x3311): if state >= 0x0A: `[0x8077 + 18c] = 7` (channel sequencer code), `[0x8081 + 18c .. ] = 0x14` (timer), then 0x227C clears `D5`, `D6` and `0x80C0 &= 0x0F` (releases the probe). C
- Comparisons used by the states: `(sample + 6) < baseline` (signed 16-bit) -> state 0x0D (probe lost); thresholds 0x2A, 0x1E, 0x28, 0x48, 0x35, 0x41, 0x0C, 0x14, 0x0A, 0x02 seen in 0x31EB..0x32F5. C values, I meaning (class / short / open decisions are not yet traced to a result byte).
- 0x29EE (class mux schedule, read in phase 2): sub-states 1..5 on the tick counter `0x8135` (compare 5, 9, 14, 17), `AF = ch*4 + {1, 2, 3, 5}`, samples `word(AB = c | 8)` at tick 5 (`0x813B`) and 14 (`0x813D`), class code `0x4900(0x813D)`, class stored in `[0x80DD + 2c]` bits 5:3, the CM byte (0x8139) compared with IRAM 0x17 (value not traced), then `0x8103 &= ~bit`. The counter unit is one periodic-ISR call; the period of that ISR is not known (timer or timer 2: unresolved). C / U
- Not yet read: the result / publish code that turns these states into SAT_Pn 0x06 / 0x4x (0x2C79..0x2EDD monitor, 0x2F8C power good, 0x30EF..0x3126 limit programming and B(ch).bit2 trigger, 0x3334 `calc_limit`, 0x1B03 offset trim, 0x3651). Each is 100..480 bytes with 16/32-bit math and 20+ record fields; a faithful port needs all of them and a way to model the analog side per state in the sim.

Planned bench plan once the sequencer exists (unchanged): A = `-nopower` image on chip PoE-07 with the camera on ch1: expect SAT_P1 0x44, other ports 0x06, TEMP ~0xA1, supply word ~0x2310, SAT_PWR 0, CM / VM 0. B = full image, same setup: SAT_PWR 0x22, SAT_P1 0x44, VM ~0x2305, ICUT (reg 0x4C) 0x11 and 0x4E = 0x01. Everything in A / B is a target; only the register layout and the arithmetic of TEMP / 0x2E / 0x2F / CM / VM have been modelled (phase 2), nothing about detect, class timing, power-good or limits has been confirmed against hardware.

## Phase 4: event-driven detect, hook 3 wiring, oracle diff (offline, #26)

Evidence: C code/oracle run, I inferred, G our guess, U unverified.

### What changed
- Detect flow as stock (C): D(c) = 1, 50-tick timer, BC bit 6 -> `ev_lo` bit 0 -> result read 4 ticks later (0x3B3C) -> D>>4 classifier with HP_Pn_MOD overrides, 9 -> 6 -> SAT_Pn; timeout path D = 2, nibble cleared, idle after 2 ticks.
- Class: code 4 clears the HP scale flag, class gate arms the 0x29EE mux (AF = ch*4 + {1,2}, 0x4900 thresholds); SAT_P1 0x04 then 0x44, open port 0x06.
- Hook 3 arming (C, bench): end of `hal_meas_calib` sets IP.1 and T2CON.5 (stock 0x41D4 / 0x41D6). Without it the periodic ISR never fires.
- Hook 3 body counts calls only; the chain (class_step every call, meas_step every 4th, lag cap 8) runs in the main loop (G: keeps the I2C ISR and the watchdog poke path free).
- Power-up as stock (C, oracle): F2/F1 at power-on, then wait for the hardware power-good event (BC bit 4, F3 bit set), no timeout; at the event C9 release, F2, A limit, B pulse, OV_CUT 0x11 (class 4 only, G for other classes), SAT_PWR good nibble; B bit 7 two ticks later.
- `arm()` sets the service flag at once (stock 0x49FF); the delay only re-arms the flag. Shutdown mode parks in `st_port_shutdown` (stock 0x2F5C). Both found by the oracle diff.
- SUP_EVT bit 7 set at the first measurement, no INT (C, oracle; meaning I).
- CONST moved 0x4000 -> 0x4400 (code grew).

### Oracle diff (`scripts/poe_fw_oracle_diff.py`, engine `scripts/poe_fw_oracle.py` + `scripts/mcs51.py` from wip/poe-seq-6)
- Same analog model and host setup for stock v21, awto and awto-nopower; rewrite host writes go through its I2C ISR.
- Result, camera / open / timeout / no-power-good scenarios, channel 1 and 0, pt 1 and 4: SFR write log (C9 F1 F2 A1..A4 B2 D1..D4 D9 S95 BD) identical in value, order and tick for the awto image; final published registers identical.
- nopower: identical up to SAT_P1 0x44 and SAT_PWR enable (0x01 / 0x02); F2/F1 power-on writes and everything after the power-good event absent by design; F1 never non-zero.
- Remaining explained differences: measurement pacing (rewrite meas_step every 4th hook-3 call, stock every call; same rate at the bench 4.45 calls/tick); stock's offset trim (0x1B03) not ported.
- CPU (oracle cycle count, 1 tick = 20000 cycles at 7.9 MHz / 4): stock hook 3 4155 cycles/call = 94 % of a tick at 4.45 calls/tick; rewrite hook 3 42 cycles, hook 8 about 1900 cycles per tick, chain pass about 725 cycles per call: about 26 % in total.

### New diagnostic registers (awto image, read-only)
| Reg | Meaning |
|---|---|
| 0x16 | status 0x43 |
| 0x1C / 0x1D | detect path (1 event, 2 timeout) / raw D at read time |
| 0x22/0x23, 0x2D/0x40, 0x45/0x4F | hook 3, hook 8, hook 2 entry counters (lo/hi) |
| 0x24..0x27 / 0x28..0x2B | class-probe word low / high per channel |
| 0x71..0x74 | live raw D1..D4 |
| 0x76/77, 0x59/5A, 0x5B/5C, 0x5D/54 | detect words A, B, C, D (D9-indexed) |

### Bench steps
1. Load `bcm59111-awto-nopower.bin`, ports shut, no PD: read 0x22/0x23 and 0x2D/0x40 twice 1 s apart: hook 3 about 440/s, hook 8 about 98/s; 0x20 watchdog poke still works.
2. OP_MOD auto for the camera port, camera attached: 0x1C = 1 (event), 0x1D = 0x40, SAT_Pn 0x04 then 0x44, SAT_PWR enable bit only (0x02 for port 2), F1 stays 0.
3. Empty port: SAT_Pn 0x06, 0x1C = 1, 0x1D = 0x60.
4. Full image `bcm59111-awto.bin`, camera: SAT_Pn 0x44, SAT_PWR 0x02 then 0x22 once F3/BC bit 4 arrive, OV_CUT (0x4C for port 2) 0x11, HP_SAT 0x4E = 1, CM/VM live. If SAT_PWR stays 0x02 the hardware power-good event or F3 meaning differs (U): file the 0x1C/0x1D and counters.

## Board result 1 (nopower fcf699a, chip PoE-07, camera on channel 1) and Probe v2

### Decode of the board registers (code reading, C)
- 0x1C = 0x11: high nibble = channel 1, low nibble = path 1 (result read). Written only in `ST_DETECT_RESULT`: D(1) = 1 was written, channel event bit 6 arrived (ISR set `ev_lo` b0), the 4-tick delay ran, and the D(1) read happened.
- 0x1D = 0x00: raw D(1) at that read. Stock does `swap a; anl #0x0F` on D (stock 0x3B73), so the code is the HIGH nibble. High nibble 0 = code 0 = no valid signature: SAT nibble 0, invalid counter 0x64 +1, DET_CLASS_EVT 0x02, no class, no power. The timeout path was not taken (it would read 0x1C = 0x12).
- 0x71..0x74 = 00 04 00 00 (read later): D(1) low nibble 4, high nibble 0. Stock does not read the low nibble; it is a status field (meaning U).
- OP_MOD 0x00 transient: only the host watchdog trip/restart path or init writes it (`chan_shutdown_all`; 0x21 stayed 0x82), so it was the driver's enable sequence. A failed detect does not reset the channel.
- 0x7E = 0x01 is internal state: host regs 0x78..0x7F overlap the channel struct (channel 0 state = shutdown, parked).
- Stage that stopped: D = 1 written, event bit 6 seen, result read, result = code 0. The hardware answered with an empty result.

### Oracle D encoding
- Oracle feeds D(c) = code << 4 (high nibble): matches the stock read path (C, 0x3B3C..0x3B7B). Not changed: a low-nibble layout would make stock itself read 0, and stock detects the camera on this board.
- Strong lead (C from the oracle, not ported): stock rewrites the indirect words AB = 4..11 (AC/AD) on EVERY periodic call (0x1B03: per-channel offset/gain calibration from the AB 0..3 references and ADC B5/B6/B7). Oracle: stock 576 AC/AD writes in 35 ticks, rewrite 0. The class-probe words 0x24..0x2B read 0x0000 on the board for all channels. Detect and class samples are most likely uncalibrated without it. `scripts/poe_fw_oracle_diff.py` prints the count.
- Fixed from the diff: any result other than 4 re-detects after 61 ticks like stock (was 200); scenario `bad` (D code 0) added, 0 write-log differences.
- Fixed: XSEG moved 0x80C0 -> 0x80C8; the channel struct (0x8078..0x80C7) overlapped the first XDATA variables (per_cnt, calibration values) through channel 3.

### Probe v2 (`hwdb/firmware/bcm59111-awto-probe2.bin`)
Nopower base (no F1/F2/C9 at power-up), host watchdog and CRC/layout checks kept. Read-only evidence except 0x40.
| Reg | Meaning |
|---|---|
| 0x2D | free-running tick (hook 8 count low byte, 10.13 ms) |
| 0x1E | tick at the last D = 1 write |
| 0x1F | ticks from D = 1 to the first BC bit 6 seen (0xFF = none) |
| 0x74 | ticks from D = 1 to result read or timeout (0xFF = none) |
| 0x45 | low nibble: D = 1 written; high nibble: event bit 6 seen (bit per channel) |
| 0x4F | low nibble: timeout path taken; high nibble: result read (bit per channel) |
| 0x1C / 0x1D | last path (ch<<4 / 1 result, 2 timeout) / raw D at result read |
| 0x24..0x2B | state history of channel 1, newest first (state at each change as seen by the state machine) |
| 0x30..0x3F | raw D(ch) ring, 16 samples at ticks after D = 1: 1 2 3 4 5 6 8 10 15 20 25 30 40 50 60 70; 0xEE = not sampled yet; refilled at each D = 1 |
| 0x71..0x73 | live raw D1..D3 (D4 not written) |
| 0x76/77, 0x59/5A, 0x5B/5C, 0x5D/54 | detect words A, B, C, D as read by the classifier (lo, hi) |
| 0x40 | write 1: clear flags and restart detect on every channel whose OP_MOD is not 0; reads 0 when done |
- Hook counters: hook 3 0x22/0x23; hook 8 0x2D only (0x40 and the hook-2 regs are probe regs here).
- State numbers (chan.c enum): 0 reset, 1 shutdown (parked if OP_MOD = 0), 2 cfg, 3 mode select, 4 detect arm, 5 detect start, 6 detect wait, 7 detect result, 8 detect invalid, 9 class gate, 10 class wait, 11 power request, 12 power on, 13 power wait, 14 power trigger, 15 monitor, 16 fault, 17 disconnect, 18 idle, 19 generic wait.
- Read-out: load, ports shut, wait 2 s; set OP_MOD 0x0c; read in one pass 0x1E..0x1F, 0x24..0x2B, 0x30..0x3F, 0x45, 0x4F, 0x74, 0x2D at about 1 s and 5 s; write 0x40 = 1 and repeat to capture again.
