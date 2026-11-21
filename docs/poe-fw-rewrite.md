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
