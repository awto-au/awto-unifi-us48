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
| PoE 1-48 | MSPI board-IO frames (0,0)/(0,1), 2 bits/port (C: board.md, poe.md section 6) | not implemented (PoE driver #26; colours unknown) | out of scope |

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
| `STATUS` | `0x20004` | `RUNNING` bit 8, PC 7:0: read and logged |
| `CLK_PARAMS` | `0x20050` | not written. Reset `0x505b8d80`: half period 20, refresh 6 000 000 clocks |
| `DATA_RAM` | `0x20400` | 256 words, bits 7:0 |
| `PROGRAM_RAM` | `0x20800` | 256 words, one byte each |

- Load order (stock `0x120ed64`, board.md; OpenBCM `ledproc_load`): clear EN, write 256 program words (zero-padded), zero data RAM `0x80-0xff`, write our chain bytes, set EN. Only LEDUP0 (stock loads `uc 0` only, C). LEDUP1 untouched (BT: does it drive anything?).
- Clocks: LED_CLK 5 MHz is the datasheet rate (clocks.md `[DS]`), the reset half period 20 gives 5 MHz at a 200 MHz LEDUP clock (I), so the rated maximum already runs. The refresh period (30 ms at 200 MHz, I) only sets blink and activity resolution; not changed. `chain` reports the measured rate.
- Data RAM map (ours, matches OpenBCM convention): `0x00-0x7f` hardware scan-in, `0x80-0x9f` `pack` area (`SCANCHAIN_ASSEMBLY_ST_ADDR` reset `0x80`), `0xa0-0xbf` CTL per slot, `0xc0-0xdf` scan port per slot, `0xe0-0xe7` program variables.

### Program (`kernel/us48-cmic/us48_ledup.asm`, 125 B)

- Ours; stock's program (switchdrvr `0x15a38ac`, 164 B, Ubiquiti's own, not a published Broadcom program) was analysed and its chain format adopted: [ledup-stock.md](ledup-stock.md) (bytes, annotated listing, comparison). ISA (opcodes, operand forms) from OpenBCM sdk-6.5.16 `tools/led/tools/ledasm.h`; structure after the Greyhound/Hurricane3 examples. Licence: Broadcom `Legal/LICENSE`; no Broadcom text copied.
- `scripts/ledasm.py`: our assembler -> `us48_ledup_prog.h` (generated; `--check` in review). `--verify` assembles `gh_sdk53402.asm` byte-identical to its hex in OpenBCM `rc/gh_ledup.soc` (2026-11-19). `--sim`: reference model, 25/25 cases (semantics I).
- Per slot: `port SPORT[s]`; value = CTL bits 1:0; bits in the blink mask (3:2) blank on the speed's TICK bit (slow 480 / mid 240 / fast 120 ms half period at 30 ms refresh, I); ACT (bit 6) blanks the slot on odd TICKs while `pushst RX` or `TX` is set; xor POL; shift NBITS bits **LSB first** (stock order); `send (TOTAL)`.
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

- Patterns (cells = slot x bit, st1008f rows -> bits): booting = bit 0 slow blink; error = all fast blink (120 ms vs st1008f 128 ms); fallback = bit 0 on, bit 1 mid blink (240 vs 256 ms); panic = per slot one bit on, the other fast blink, swapped per `panic_blink` call; on/off = all cells.
- Digits: 0 off, 7 on, 1-3 fast, 4 mid, 5-6 slow (st1008f has six blink rates, LEDUP three). One blink speed per slot: two cells of one port with different speeds = `-EINVAL`.
- Differences from st1008f (HR2 reasons):
  - Booting comes from Linux (driver probe), not U-Boot: awto-uboot does not load LEDUP. No U-Boot latch to read at init.
  - `state` is the driver's mode, not a register readback (the program has no readable per-cell mode).
  - `hw` = offload with default flags; `auto` = per-LED triggers (st1008f `auto` = its per-cage policy).
- Boot: `S95us48-leds` writes `clear` only while `state` = booting (st1008f stage-0 rule).

## Board test plan

`scripts/led_test.py` (dry-run default; `--apply`, `--interactive` for Dan's eyes). Unknowns it answers:

1. Pinmux: CCA lines 6/7/11 act as GPIO under awto-uboot (stock kernel never touches mux, C). `button`, `sysled`.
2. System LED colour per pin, polarity. `sysled`.
3. Button idle high, press gives `KEY_RESTART` press/release. `button --interactive`.
4. `us48_cca_gpio` with outputs: SFP presence (lines 2/3) still reads as before (#21 `sensors_test.py`).
5. LEDUP runs on both units (STATUS bit 8, TICK advances, refresh rate). `ledup`.
6. Chain format: confirm the stock-derived default (2 bits, LSB first, front order, 28 slots, pol 0). `chain --interactive` asks once; only on "no" does it scan nbits/order/pol. First `patterns` with the defaults.
7. Colour per bit; bicolour LEDs that need `01`/`10` (anti-parallel) vs two LEDs. `ports`.
8. Scan-chain RX/TX bits valid for `pushst` on HR2 (activity flicker on 8<->17, 32<->41). `links`.
9. Front 49-52 LEDs: on the LEDUP0 chain (slots of xe49/xe50 and xe51/xe52), on LEDUP1, or elsewhere.
10. Panic pattern only through `pattern panic`, never a real panic.
