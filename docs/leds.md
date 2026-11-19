# LEDs and the reset button (mainline)

Issues: #60 (button, system LED, GPIOs), #14 (port LEDs), #139 (same LED interface as awto-tl-st1008f 300-16). Board facts: [board.md](board.md) GPIO map, MSPI, Port LEDs; stock gpiodev: [kernel-pse.md](kernel-pse.md) section 3.
Legend: **C** = confirmed (cited), **I** = inferred, **BT** = board-test item ([test plan](#board-test-plan)).

## What drives which LED

| LED | Driver | Linux | Status |
|---|---|---|---|
| System LED white / blue | ChipcommonA GPIO 6 / 7 (stock Linux GPIO 10 / 11), high = on (C: gpiodev `gpio_set_led` writes the value bits unchanged) | DT `gpio-leds` `white:status`, `blue:status` | colour per pin I (BT) |
| Reset button | ChipcommonA GPIO 11 (stock GPIO 15, IRQ 267), low = pressed (C: gpiodev `IRQF_TRIGGER_LOW`) | DT `gpio-keys-polled`, `KEY_RESTART` | BT |
| Copper link/activity 1-48 | CMIC LEDUP0 of the owning unit (C: stock loads an LEDUP program into unit 0 and unit 1, board.md) | `us48_cmic` LEDUP program + LED class devices | chain format = stock ([ledup-stock.md](ledup-stock.md)), BT confirms |
| SFP+ 1/2, SFP 1/2 (49-52) | not located (board.md: no MSPI field) | stock: last two slots of each chain, unit 1 = 49/50, unit 0 = 51/52 (C, [ledup-stock.md](ledup-stock.md)); ours: slots exist for the ports the driver registers (unit 0: 24 copper + xe51/xe52 since #143, as stock's 26), rest padding (`slots=28`) | BT |
| PoE 1-48 | MSPI board-IO frames (0,0)/(0,1), 2 bits/port (C: board.md, poe.md section 6) | `us48_boardio`: LED class `ge<N>::poe` + `poe_pattern` ([PoE LEDs](#poe-leds-kernelus48-boardio-26)) | colours BT; PSE trigger TODO (#26) |

## Reset button and system LED (DT mainline 0027, kernel/us48-cca-gpio)

- GPIO controller: `cca0` = out-of-tree `us48_cca_gpio` (from #21, mainline 0026), loaded by `S05us48-sfp`. The built-in `gpio-keys-polled` / `gpio-leds` probe (deferred) when it loads; the white blink starts there, not at kernel start.
  - Outputs only on unit 0 lines 6/7 (`CCA_U0_OUTPUTS`): DOUT then OUTEN (hwdb `cca.GPIO_DOUT` / `GPIO_OUTEN` = regdb `CHIPCOMMONA_GPIOOUT` / `GPIOOUTEN`). C: stock gpiodev `gpio_init_led` sets OUTEN 6/7, `gpio_set_led` writes DOUT 6/7; uboot-1 drives only 6/7 (hwdb periph.toml). Every other line, and all of unit 1, stays input-only (`-EPERM` on set/output).
  - Alternative dropped: mainline `brcm,nsp-gpio-a` has the same register map (regdb offsets match its macros, base `0x18000020`) but requires an IO-control region HR2 lacks; a second driver on `0x18000060` would collide with `us48_cca_gpio`.
  - 0027 board DTS: `gpio-line-names` on `cca0`, keys, LEDs. No hogs.
- Button: `gpio-keys-polled`, `poll-interval` 20 ms (>= 4 samples in an ~80 ms tap, I; one MMIO read per poll). `us48_cca_gpio` has no IRQ chip; the ChipcommonA IRQ (SPI 91) is shared with the UARTs.
  - No action bound in userspace yet (factory reset / failsafe: #60). Stock U-Boot erases `cfg` + `u-boot-env` if held ~3 s at power-on: never hold it while powering on.
- System LED: white `linux,default-trigger = "timer"` (500/500 ms = stock boot `echo 20 > /proc/gpio/led_pattern` at 120 bpm, C kernel-pse.md 2.2/3.3); `S95us48-leds` sets steady white at the end of boot. Blue: `panic-indicator` (ledtrig-panic blinks it in the panic loop).
  - Colour per pin: I, from UniFi "flashing white = initialising" and the stock boot pattern lighting pin 6.
- Untouched lines (never driven): 0/1/8 PSE IRQs, 9 BCM59111 reset (#24, #26), 2/3 SFP presence, 4/5/10 unknown.

## Port LEDs: LEDUP

### Hardware (regdb `CMIC_LEDUP0_*`, hwdb `cmic0.LEDUP_*`)

| Register | Offset | Use |
|---|---|---|
| `CTRL` | `0x20000` | bit 0 `LEDUP_EN` (RMW); scan delays 3:1, 9:4 left as found (`0x6a` on unit 0, `0x42` on unit 1, hwdb/validation) |
| `STATUS` | `0x20004` | `RUNNING` bit 8, PC 7:0: read and logged. Board 2026-11-19 (both units): STATUS `0x7d` = PC at the program end, RUNNING 0 between refreshes; TICK advances, so running = TICK step (#14) |
| `CLK_PARAMS` | `0x20050` | not written. Reset `0x505b8d80`: half period 20, refresh 6 000 000 clocks |
| `DATA_RAM` | `0x20400` | 256 words, bits 7:0 |
| `PROGRAM_RAM` | `0x20800` | 256 words, one byte each |

- Load order (stock `0x120ed64`, board.md; OpenBCM `ledproc_load`): clear EN, write 256 program words (zero-padded), zero data RAM `0x80-0xff`, write our chain bytes, set EN. Only LEDUP0 (stock loads `uc 0` only, C). LEDUP1 untouched (BT: does it drive anything?).
- Clocks: LED_CLK 5 MHz is the datasheet rate (clocks.md `[DS]`), the reset half period 20 gives 5 MHz at a 200 MHz LEDUP clock (I), so the rated maximum already runs. The refresh period only sets blink and activity resolution; not changed. `chain` reports the measured rate: 22.4 Hz (44.6 ms) on both units, board 2026-11-19, debug image 6434910 (the 30 ms / 200 MHz guess was wrong; blink half periods scale by 1.5x).
- Data RAM map (ours, matches OpenBCM convention): `0x00-0x7f` hardware scan-in, `0x80-0x9f` `pack` area (`SCANCHAIN_ASSEMBLY_ST_ADDR` reset `0x80`), `0xa0-0xbf` CTL per slot, `0xc0-0xdf` scan port per slot, `0xe0-0xe7` program variables.

### Program (`kernel/us48-cmic/us48_ledup.asm`, 125 B)

- Ours; stock's program (switchdrvr `0x15a38ac`, 164 B, Ubiquiti's own, not a published Broadcom program) was analysed and its chain format adopted: [ledup-stock.md](ledup-stock.md) (bytes, annotated listing, comparison). ISA (opcodes, operand forms) from OpenBCM sdk-6.5.16 `tools/led/tools/ledasm.h`; structure after the Greyhound/Hurricane3 examples. Licence: Broadcom `Legal/LICENSE`; no Broadcom text copied.
- `scripts/ledasm.py`: our assembler -> `us48_ledup_prog.h` (generated; `--check` in review). `--verify` assembles `gh_sdk53402.asm` byte-identical to its hex in OpenBCM `rc/gh_ledup.soc` (2026-11-19). `--sim`: reference model, 25/25 cases (semantics I).
- Per slot: `port SPORT[s]`; value = CTL bits 1:0; bits in the blink mask (3:2) blank on the speed's TICK bit (16 / 8 / 4 refreshes: slow 714 / mid 357 / fast 178 ms half period at the measured 44.6 ms refresh); ACT (bit 6) blanks the slot on odd TICKs while `pushst RX` or `TX` is set; xor POL; shift NBITS bits **LSB first** (stock order); `send (TOTAL)`.
- All policy is in the CPU; the chain format is a runtime setting (`chain`), default = stock's:
  - 2 bits per port, LSB first: bit 0 = first bit on the wire = stock's 1G/10G symbol "10", bit 1 = 10/100M symbol "01". Bit 0 = `green`, bit 1 = `amber` (colour names I).
  - Polarity 0 (no inversion), order `front` (ascending; unit 0 front 1-24 then 51/52, unit 1 front 25-48 then 49/50), `slots=28` (56 bits, 4 trailing zero bits), same program on both units.
  - Scan port = physical port (stock: logical 2-9 reversed to 9-2, 26/27 -> 26/28).
  - Stock reads RX, TX, SPEED_C, SPEED_M scan bits, so they are valid on HR2 (C code, BT).

### Driver (`kernel/us48-cmic/us48_cmic_led.c`)

- Per unit at probe (after `register_netdev`): program load, slot map, two LED class devices per port: `<port>:green:lan` (bit 0), `<port>:amber:lan` (bit 1). Colours I (UniFi: green 1G, amber 10/100; BT).
  - `brightness_set`: software on/off. `blink_set`: offloaded to the nearest LEDUP speed (timer trigger).
  - netdev trigger hw offload (`hw_control_*`, default trigger `netdev`): `link`, `link_10/100/1000/10000`, `rx`+`tx` (both or neither; the program blanks on either). Value from the CPU link state (netdevice notifier, like the SDK linkscan writing link into data RAM); activity from the scan chain (I: RX/TX bits as the OpenBCM examples, BT).
  - Defaults: green = `link_1000 link_10000 rx tx`, amber = `link_10 link_100 rx tx`.
- Panic: `panic_blink` writes the panic pattern lock-free (single-word MMIO writes; unit 1 = PCIe BAR2), alternating per call. ledtrig-panic already owns `panic_blink` for the blue system LED, so ours chains to it instead of st1008f's "only if unset".

### sysfs: `/sys/devices/platform/us48-leds/` (st1008f 300-16 names, #139)

| Attribute | Same as st1008f | Meaning here |
|---|---|---|
| `state` (ro) | yes | `hw|booting|error|panic|fallback|custom` + ` (manual)` + ` (error latched)` |
| `pattern` | yes | `auto` (LED class policy), `hw` (all ports link/activity offload, defaults), `off`, `on`, `booting`, `error`, `panic`, `fallback`, or 2 rows x N digits 0-7 (row = bit 0, then bit 1; columns = ports in front order, see `ports`); `-EBUSY` while latched |
| `error` | yes | any write latches the error pattern, text = reason; read `1 <reason>` |
| `clear` (wo) | yes | release latch and manual pattern -> `auto` |
| `ports` (ro) | = `cages` | column, front, port, unit, slot, scan port, link Mb/s, CTL |
| `chain` | extra | chain format `nbits= pol= order=front|rfront|phys|rphys extra= slots= act=` (default = stock: `2 0 front 0 28 1`); per unit STATUS, CTRL, CLK_PARAMS, TICK and refreshes since the last read |

- Patterns (cells = slot x bit, st1008f rows -> bits): booting = bit 0 slow blink; error = all fast blink (178 ms vs st1008f 128 ms); fallback = bit 0 on, bit 1 mid blink (357 vs 256 ms); panic = per slot one bit on, the other fast blink, swapped per `panic_blink` call; on/off = all cells.
- Digits: 0 off, 7 on, 1-3 fast, 4 mid, 5-6 slow (st1008f has six blink rates, LEDUP three). One blink speed per slot: two cells of one port with different speeds = `-EINVAL`.
- Differences from st1008f (HR2 reasons):
  - Booting comes from Linux (driver probe), not U-Boot: awto-uboot does not load LEDUP. No U-Boot latch to read at init.
  - `state` is the driver's mode, not a register readback (the program has no readable per-cell mode).
  - `hw` = offload with default flags; `auto` = per-LED triggers (st1008f `auto` = its per-cage policy).
- Boot: `S95us48-leds` writes `clear` only while `state` = booting (st1008f stage-0 rule).

## PoE LEDs (kernel/us48-boardio, #26)

### Hardware
- Chains (A,B) = (0,0) front 1-24, (0,1) front 25-48; 6 B each, 2-bit field per port, MSB first: port `i` of the card -> byte `i>>2`, bits `7-2(i&3) : 6-2(i&3)` ([board.md](board.md#mspi-board-io-relation-to-fanmd), C).
- What sits at the chain ends: not identified (I: write-only shift registers; no readback).
- Field values, stock writers `0x5f6a40` / `0x60615c` (C code): `0` off, `1` 802.3 port with power-good, `2` port in passive mode (any state). `3` is never written. Colours: unknown (BT).

### Ownership and chain safety
- `us48_boardio` owns the MSPI chains: one mutex, both select pins (A = unit 0 GP0, B = unit 1 GP0), `us48_mspi_frame(chain, buf, len)`. Clients: `us48_fan` (`US48_MSPI_FAN0/1`) and the PoE LEDs (`US48_MSPI_POE_LED0/1`).
  - The select-gpios sit on the fan DT node (mainline 0013), so `us48_fan` probe calls `us48_boardio_attach()` and, after its first fan frame (stock order), `us48_poe_leds_attach()`. A PoE LED failure does not fail the fan probe.
  - Fan frames are byte-identical to the pre-boardio driver (9480d5c): `scripts/mspi_frame_check.py` compiles both against host stubs and compares every transfer (pins, length, bytes) and return code over probe, states 0-3, manual pwm, quiet / loud fail level, sensor loss, floor, write errors, unbind.
- A PoE LED write cannot reach a (1,x) PoE mode-select frame:
  - Chain = enum with a fixed (A, B, length) table; the length must match (6/6/7/7), so a 6-byte frame is refused for a 7-byte chain.
  - Before every frame, under the lock: A, then B, each read back (us48_cmic_gpio returns the DATA_OUT bit of an output); a mismatch = `-EIO`, nothing sent. Stock also sets both pins before every transfer, never restores them.
  - The PoE LED code names only `US48_MSPI_POE_LED0/1`; it never builds a 7-byte frame.
  - Field values 0-2 only: LED class `max_brightness` 1 = field 1; `poe_pattern` digits 0-2, `3` = `-EINVAL`.
- GP pin output enable: unchanged from `us48_fan` since 0013: `devm_gpiod_get_index(... GPIOD_OUT_HIGH/LOW)` -> us48_cmic_gpio `direction_output` writes DATA_OUT, then OUT_EN (regdb `CMIC_GP_DATA_OUT` / `CMIC_GP_OUT_EN`).
- Writes: per card, only when the frame differs from the last acked one. Probe sends zeros on both cards (poe.md section 11 step 1). Unload: LEDs off (field 0).
- Nothing here touches a BCM59111 register or enables a port.

### Linux interface
- LED class `ge<N>::poe`, N = front 1-48, parent `/sys/devices/platform/us48-board-io`. Brightness 1 = field 1 (stock "802.3 power-good"). No colour in the name until BT. No upstream PoE function name (`include/dt-bindings/leds/common.h`, v7.4-rc1).
- `default_trigger` = `ge<N>-poe-good`. TODO (#26): `bcm59111` registers one simple trigger per PI and fires 1 on power-good, 0 on disconnect; needs its IRQ path (`0x03` bits 7:4 / `0x07`, docs/poe-pse.md). Not registered today: LEDs keep their value. PI -> front map is I (`4(NN-1)+p+1`, poe-pse.md).
- `/sys/devices/platform/us48-board-io/poe_pattern` (rw): 48 digits 0-2 in front order, or `off`. `poe_frame_errors` (ro): failed PoE LED frames since probe.
- Field 2 (passive colour) only through `poe_pattern` (tests); the passive power path itself is not implemented (poe-pse.md section 3).
- `scripts/led_cycle.py`: chase lights the PoE LED of the chased port (field 1 green pass, 2 yellow pass); solid phase all PoE 0/1/2/1; skipped with a note when `poe_pattern` is absent.

### Board check (Dan at the panel; read-only for PoE)
Preconditions: image with `us48_boardio` + `us48_fan` from this branch. PoE stays off: no `portN_enable` / `poe_test.py enable`, `bcm59111` (main = hwmon only) not changed. Cables: only what the session needs (ge1 management).
1. Baseline, read only: `wire_level`, `frame_errors` (fan spi device), `pwm1`, `pwm1_enable`; `poe_pattern` = 48 zeros, `poe_frame_errors` = 0; dmesg has no `select pins not set` / `PoE LED`. Per bound BCM59111: `i2cget -y -f BUS ADDR 0x12` and `0x10` (read only): `0x12` = `0x00`, `0x10` = `0x00`.
2. After boot all 48 PoE LEDs dark. Note anything lit before Linux (power-on content unknown).
3. `echo 1 > /sys/class/leds/ge1::poe/brightness`, then ge24, ge25, ge48 (one at a time, back to 0): the LED that lights, its colour. Confirms card split and bit order.
4. `poe_pattern` with front 1 = `2`: colour of field 2. Then all `1`, all `2`: every LED follows.
5. `scripts/led_cycle.py --only chase --dwell 0.05 --rounds 1`: each PoE LED walks with its port LED.
6. Fans unaffected: fan sound unchanged through steps 3-5; `wire_level`, `frame_errors` = baseline. Then `pwm1_enable` 1, `pwm1` 255, back to 2: fans follow, PoE LEDs keep their state (chains independent).
7. Repeat the step 1 reads: `0x12` / `0x10` unchanged, `poe_frame_errors` 0, no PD powered.
8. `echo off > poe_pattern`; `rmmod us48_fan us48_boardio`, insmod both: LEDs off, one fan frame, no errors.
- Record colours per field and any mismatch in #26 / #14; then name the LEDs with colours.

## Board test plan

`scripts/led_test.py` (dry-run default; `--apply`, `--interactive` for Dan's eyes). Unknowns it answers:

1. Pinmux: CCA lines 6/7/11 act as GPIO under awto-uboot (stock kernel never touches mux, C). `button`, `sysled`.
2. System LED colour per pin, polarity. `sysled`.
3. Button idle high, press gives `KEY_RESTART` press/release. `button --interactive`.
4. `us48_cca_gpio` with outputs: SFP presence (lines 2/3) still reads as before (#21 `sensors_test.py`).
5. LEDUP runs on both units (TICK advances, refresh rate; STATUS bit 8 is 0 between refreshes). `ledup`; program RAM = header: `prog`.
6. Chain format: confirm the stock-derived default (2 bits, LSB first, front order, 28 slots, pol 0). `chain --interactive` asks once; only on "no" does it scan nbits/order/pol. First `patterns` with the defaults.
7. Colour per bit; bicolour LEDs that need `01`/`10` (anti-parallel) vs two LEDs. `ports`.
8. Scan-chain RX/TX bits valid for `pushst` on HR2 (activity flicker on 8<->17, 32<->41). `links`.
9. Front 49-52 LEDs: on the LEDUP0 chain (slots of xe49/xe50 and xe51/xe52), on LEDUP1, or elsewhere.
10. Panic pattern only through `pattern panic`, never a real panic.
