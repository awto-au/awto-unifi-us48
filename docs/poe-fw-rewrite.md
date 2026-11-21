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
