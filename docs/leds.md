# LEDs and the reset button (mainline)

Issues: #60 (button, system LED, GPIOs), #14 (port LEDs), #139 (same LED interface as awto-tl-st1008f 300-16). Board facts: [board.md](board.md) GPIO map, MSPI, Port LEDs; stock gpiodev: [kernel-pse.md](kernel-pse.md) section 3.
Legend: **C** = confirmed (cited), **I** = inferred, **BT** = board-test item ([test plan](#board-test-plan)).

## What drives which LED

| LED | Driver | Linux | Status |
|---|---|---|---|
| System LED white / blue | ChipcommonA GPIO 6 / 7 = stock Linux GPIO 10 / 11 (same pins, base 4), high = on | DT `gpio-leds` `white:status`, `blue:status` | white C (board); blue: stock-equivalent write never shows blue (board 2026-11-19), see [Blue](#blue-system-led) |
| Reset button | ChipcommonA GPIO 11 (stock GPIO 15, IRQ 267), low = pressed (C: gpiodev `IRQF_TRIGGER_LOW`) | DT `gpio-keys-polled`, `KEY_RESTART` | C (board 2026-11-19) |
| Copper link/activity 1-48 | CMIC LEDUP0 of the owning unit (C: stock loads an LEDUP program into unit 0 and unit 1, board.md) | `us48_cmic` LEDUP program + LED class devices | chain format = stock, C (board 2026-11-19) |
| SFP+ 1/2, SFP 1/2 (49-52) | LEDUP0 chain slots 24/25: unit 1 = SFP+ 1/2, unit 0 = SFP 1/2 (C, board 2026-11-19) | own bit map: [cage LEDs](#copper-and-cage-leds-board-2026-11-19-image-8c0391a-dan) | SFP+ second colour BT |
| PoE 1-48 | MSPI board-IO frames (0,0)/(0,1), 2 bits/port (C: board.md, poe.md section 6) | `us48_boardio`: LED class `ge<N>::poe` + `poe_pattern` ([PoE LEDs](#poe-leds-kernelus48-boardio-26)) | colours BT; PSE trigger TODO (#26) |

## Reset button and system LED (DT mainline 0027, kernel/us48-cca-gpio)

- GPIO controller: `cca0` = out-of-tree `us48_cca_gpio` (from #21, mainline 0026), loaded by `S05us48-sfp`. The built-in `gpio-keys-polled` / `gpio-leds` probe (deferred) when it loads; the white blink starts there, not at kernel start.
  - Outputs only on unit 0 lines 6/7 (`CCA_U0_OUTPUTS`): DOUT then OUTEN (hwdb `cca.GPIO_DOUT` / `GPIO_OUTEN` = regdb `CHIPCOMMONA_GPIOOUT` / `GPIOOUTEN`). C: stock gpiodev `gpio_init_led` sets OUTEN 6/7, `gpio_set_led` writes DOUT 6/7; uboot-1 drives only 6/7 (hwdb periph.toml). Every other line, and all of unit 1, stays input-only (`-EPERM` on set/output).
  - Alternative dropped: mainline `brcm,nsp-gpio-a` has the same register map (regdb offsets match its macros, base `0x18000020`) but requires an IO-control region HR2 lacks; a second driver on `0x18000060` would collide with `us48_cca_gpio`.
  - 0027 board DTS: `gpio-line-names` on `cca0`, keys, LEDs. No hogs.
- Button: `gpio-keys-polled`, `poll-interval` 20 ms (>= 4 samples in an ~80 ms tap, I; one MMIO read per poll). `us48_cca_gpio` has no IRQ chip; the ChipcommonA IRQ (SPI 91) is shared with the UARTs.
  - No action bound in userspace yet (factory reset / failsafe: #60). Stock U-Boot erases `cfg` + `u-boot-env` if held ~3 s at power-on: never hold it while powering on.
- System LED: white `linux,default-trigger = "timer"` (500/500 ms = stock boot `echo 20 > /proc/gpio/led_pattern` at 120 bpm, C kernel-pse.md 2.2/3.3); `S95us48-leds` sets steady white at the end of boot. Blue: `panic-indicator` (ledtrig-panic blinks it in the panic loop).
- Untouched lines (never driven): 0/1/8 PSE IRQs, 9 BCM59111 reset (#24, #26), 2/3 SFP presence, 4/5/10 unknown.

### Blue system LED

- No pin conflict: "GPIO 10/11" (vendor-stack.md, board.md) = stock Linux numbers, chip `GPIOA` base 4 = CCA 6/7 (kernel-pse.md 2.1). C
- Stock writes, all three paths identical to ours (C):
  - gpiodev `gpio_init_led` `0x08`: OUTEN `0x18000068` |= `0xc0`. `gpio_set_led` `0x60`: value bit 0 -> DOUT bit 7, bit 1 -> DOUT bit 6, no inversion, no timer/PWM (no `0x18000088/8c` use).
  - uboot-1 app `usetled` (`0x67032d40`): pattern 1 = CCA 7 high + CCA 6 low, 2 = 6 high, 3 = both; setter `0x67031f20` sets OUTEN then DOUT.
  - syswrapper.sh: `set_ready_led` = `set_led 1` "Solid blue"; unadopted default = `set_led 2` (white); `"12" will alternate blue and white`; upgrade `12`, reset `3`.
- So blue = CCA 7 high, CCA 6 low (C, stock code). Board 2026-11-19 (Dan, image 5ec20f4, many 0.5 s rounds): no blue in any of the 4 combinations, at most faint. Not explained by software; stock shows blue only once adopted (`set_ready_led`), so this unit may never have shown blue.
- Board check (read/observe only, no new pin): `led_test.py sysled` reads CCA DIN/DOUT/OUTEN with blue on (DIN bit 7 = pad level); at the uboot-1 prompt `usetled 1` (stock's own write, no Linux); stock firmware `echo 1 > /proc/gpio/led_pattern`. Blue under stock too -> our boot chain differs (pad mux); dark under stock -> hardware (LED).

## Port LEDs: LEDUP

### Hardware (regdb `CMIC_LEDUP0_*`, hwdb `cmic0.LEDUP_*`)

| Register | Offset | Use |
|---|---|---|
| `CTRL` | `0x20000` | bit 0 `LEDUP_EN`; scan delays intra 3:1 = 5, start 9:4 = 6 (SDK; before: unit 0 `0x6a`, unit 1 `0x42`) |
| `PORT_ORDER_REMAP_0_3..28_31` | `0x20010-0x2002c` | 4 x 6-bit per word; SDK values (below). Reset 0 |
| `XLPORT_LED_CHAIN_CONFIG` | S-channel `0x02022b00`, blocks 5, 6 | `INTRA_DELAY` 3:0 = 6 (SDK, stock trace both units). Reset 0 |
| `STATUS` | `0x20004` | `RUNNING` bit 8, PC 7:0: read and logged. Board 2026-11-19 (both units): STATUS `0x7d` = PC at the program end, RUNNING 0 between refreshes; TICK advances, so running = TICK step (#14) |
| `CLK_PARAMS` | `0x20050` | not written. Reset `0x505b8d80`: half period 20, refresh 6 000 000 clocks |
| `DATA_RAM` | `0x20400` | 256 words, bits 7:0 |
| `PROGRAM_RAM` | `0x20800` | 256 words, one byte each |

- Scan-chain setup = OpenBCM `_hurricane2_ledup_init` (`private/reference/openbcm-hurricane2.c`, from `soc_hurricane2_misc_init`, every unit), confirmed by stock traces (`tmp/mmio_stock_init.log` u0 `0x20010` = `0x001440c2` .. `0x2002c` = `0x0079f821`; `tmp/schan_stock_writes*.txt` blk 5/6 `0x02022b00` = 6 on u0 and u1). C
  - REMAP word i = ports 4i+2 .. 4i+5; words starting 26 and 30 reversed ("Transpose MXQ Block ports"). Data RAM 0-255 zeroed.
  - Without it (our driver before this fix; hwdb/validation 2026-11-16: REMAP = 0 on both units) `pushst` reads no live port status: RX/TX read as set on every linked port -> constant fast flicker in hw mode (#14).
  - Order: `XLPORT_LED_CHAIN_CONFIG` at probe before pwrsave (SDK: before `_soc_hu2_power_Optimize`) and again after a unit 0 XLPORT0 power-up (I: the power-down may lose it); CTRL + REMAP at LED init.
- Load order (stock `0x120ed64`, board.md; OpenBCM `ledproc_load`): clear EN, scan setup, write 256 program words (zero-padded), zero data RAM, write our chain bytes, set EN. Only LEDUP0 (stock loads `uc 0` only, C). LEDUP1 untouched.
- Clocks: LED_CLK 5 MHz is the datasheet rate (clocks.md `[DS]`), the reset half period 20 gives 5 MHz at a 200 MHz LEDUP clock (I), so the rated maximum already runs. The refresh period only sets blink and activity resolution; not changed. `chain` reports the measured rate: 22.4 Hz (44.6 ms) on both units, board 2026-11-19, debug image 6434910 (the 30 ms / 200 MHz guess was wrong; blink half periods scale by 1.5x).
- Data RAM map (ours, matches OpenBCM convention): `0x00-0x7f` hardware scan-in, `0x80-0x9f` `pack` area (`SCANCHAIN_ASSEMBLY_ST_ADDR` reset `0x80`), `0xa0-0xbf` CTL per slot, `0xc0-0xdf` scan port per slot, `0xe0-0xe7` program variables.

### Program (`kernel/us48-cmic/us48_ledup.asm`, 134 B)

- Ours; stock's program (switchdrvr `0x15a38ac`, 164 B, Ubiquiti's own, not a published Broadcom program) was analysed and its chain format adopted: [ledup-stock.md](ledup-stock.md) (bytes, annotated listing, comparison). ISA (opcodes, operand forms) from OpenBCM sdk-6.5.16 `tools/led/tools/ledasm.h`; structure after the Greyhound/Hurricane3 examples. Licence: Broadcom `Legal/LICENSE`; no Broadcom text copied.
- `scripts/ledasm.py`: our assembler -> `us48_ledup_prog.h` (generated; `--check` in review). `--verify` assembles `gh_sdk53402.asm` byte-identical to its hex in OpenBCM `rc/gh_ledup.soc` (2026-11-19). `--sim`: reference model, 48/48 cases incl. idle / busy / RX-only / TX-only ports (semantics I).
- Per refresh: TICK++, PH = (PH + 1) mod 5 (stock activity phase).
- Per slot: `port SPORT[s]`; value = CTL bits 1:0; bits in the blink mask (3:2) blank on the speed's TICK bit (16 / 8 / 4 refreshes: slow 714 / mid 357 / fast 178 ms half period at the measured 44.6 ms refresh); ACT (bit 6): `pushst RX`, `pushst TX`, `tor` set -> slot blank while PH < 2 (= stock: off 2 of 5 refreshes, ~4.5 Hz; idle = steady); xor POL; shift NBITS bits **LSB first** (stock order); `send (TOTAL)`.
- All policy is in the CPU; the chain format is a runtime setting (`chain`), default = stock's (C, board 2026-11-19):
  - 2 bits per port, LSB first: bit 0 = first bit on the wire = stock symbol "10", bit 1 = "01". Polarity 0, order `front` (unit 0 front 1-24 then 51/52, unit 1 front 25-48 then 49/50), `slots=28` (56 bits, 4 trailing zero bits), same program on both units.
  - Scan port = physical port (stock: logical 2-9 reversed to 9-2, 26/27 -> 26/28); valid only with the SDK REMAP above.

### Copper and cage LEDs (board 2026-11-19, image 8c0391a, Dan)

| Port | bit 0 only ("10") | bit 1 only ("01") | both ("11") | Evidence |
|---|---|---|---|---|
| Copper 1-48 | green | amber | **dark** (anti-parallel bicolour, I) | custom pattern; error (11 <-> 00): no copper blink; panic (11 <-> one bit): amber / green blink |
| SFP+ 1/2 (unit 1 slots 24/25) | green | not seen alone (BT) | purple (SFP+ 2 panic 11 <-> 10: green / purple) | custom; panic. SFP+ 1 panic (11 <-> 01) reported solid green: conflict, BT |
| SFP 1/2 (unit 0 slots 24/25) | dark | green | green | custom bit 0 dark; panic SFP 1 (11 <-> 01) solid green, SFP 2 (11 <-> 10) flashes green; error blinks |

- SFP 1/2 were never skipped: their green is bit 1, bit 0 lights nothing. Fits stock: BCM53344 cages are 1G (board.md config 10), stock only sends them "01".
- Driver `col_bit()`: copper and SFP+ green = bit 0, amber = bit 1; SFP 1/2 green = bit 1, no amber device. Patterns and hw mode light one colour per port, never "11".
- `pattern` digits stay raw (row = chain bit): "11" on copper is dark, bit 0 on SFP 1/2 is dark.

### Driver (`kernel/us48-cmic/us48_cmic_led.c`)

- Per unit at probe (after `register_netdev`): scan setup, program load, slot map, LED class devices `<port>:green:lan`, `<port>:amber:lan` (none for SFP 1/2 amber).
  - `brightness_set`: software on/off. `blink_set`: offloaded to the nearest LEDUP speed (timer trigger).
  - netdev trigger hw offload (`hw_control_*`, default trigger `netdev`): `link`, `link_10/100/1000/10000`, `rx`+`tx` (both or neither; the program blanks on either). Value from the CPU link state (netdevice notifier, like the SDK linkscan); activity from the scan chain; ACT only while that colour is lit.
  - Defaults = stock symbols: copper green `link_1000 link_10000 rx tx`, amber `link_10 link_100 rx tx`; SFP+ green `link_10000 rx tx`, amber `link_10 link_100 link_1000 rx tx`; SFP 1/2 green `link rx tx`. `link` reads 0 on copper/SFP+ green: the speed flags carry the link.
- Load after boot: module param `booting` -1 (default) = booting pattern unless LEDUP is enabled and its program RAM already holds ours (module reload: `rmmod` leaves the program running) -> `auto` (hw); 1 = booting; 0 = hw. No uptime rule. A warm reboot that keeps LEDUP running would also start in hw (I, untested).
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

- Patterns (one colour per port; SFP 1/2 use green for amber): on = green; booting = green slow blink; error = amber fast blink (178 ms vs st1008f 128 ms); fallback = amber mid blink (357 vs 256 ms); panic = green / amber fast blink alternating between neighbouring slots, swapped per `panic_blink` call; off.
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
- LED class `ge<N>::poe`, N = front 1-48, parent platform device `us48-board-io` (child of the fan spi device: `/sys/devices/platform/48001500.spi/spi_master/spi1/spi1.0/us48-board-io`; stable path `/sys/bus/platform/devices/us48-board-io`). Brightness 1 = field 1 (stock "802.3 power-good"). No colour in the name until BT. No upstream PoE function name (`include/dt-bindings/leds/common.h`, v7.4-rc1).
- `default_trigger` = `ge<N>-poe-good`. TODO (#26): `bcm59111` registers one simple trigger per PI and fires 1 on power-good, 0 on disconnect; needs its IRQ path (`0x03` bits 7:4 / `0x07`, docs/poe-pse.md). Not registered today: LEDs keep their value. PI -> front map is I (`4(NN-1)+p+1`, poe-pse.md).
- `/sys/bus/platform/devices/us48-board-io/poe_pattern` (rw): 48 digits 0-2 in front order, or `off`. `poe_frame_errors` (ro): failed PoE LED frames since probe.
- Field 2 (passive colour) only through `poe_pattern` (tests); the passive power path itself is not implemented (poe-pse.md section 3).
- Board check 2026-11-20 (Dan at the panel, PoE off, flashed 5ec20f4 + module reload, C):
  - Position: the PoE LED is at the top left of each port, in the top row of ports.
  - Card 1 (front 1-24; chain (0,0)): field 1 = green (front 1, 24), field 2 = yellow (front 1).
  - Card 2 (front 25-48; chain (0,1)): field 1 = yellow (front 25, 48), field 2 = green (front 25). The two cards have the colours swapped.
  - Stock's meaning (1 = 802.3 power-good, 2 = passive) therefore shows green/yellow on card 1 and yellow/green on card 2 (colour per field I, from the stock writer).
  - Fans stayed quiet (`wire_level` 0, `frame_errors` 0) and PoE stayed off through the test.
- `scripts/led_cycle.py`: chase lights the PoE LED of the chased port in the colour of the port LED (field chosen per card, `poe_field`); solid phase all PoE green; skipped with a note when `poe_pattern` is absent.

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

`scripts/led_test.py` (dry-run default; `--apply`, `--interactive` for Dan's eyes). Done 2026-11-19 (8c0391a): LEDUP runs on both units, chain format, copper colours, cages on slots 24/25, button, white LED. Open:

1. `regs`: CTRL delays 6/5 and REMAP = SDK on both units (read only).
2. `links`: idle linked port steady green; ~4.5 Hz blink only with traffic (8<->17, 32<->41, SFP+ 1).
3. `cages --interactive`: 10 / 01 / 11 per cage; SFP+ "01" colour and the SFP+ 1 panic conflict.
4. `patterns`, `latch`: colours as listed above.
5. `reload --interactive`: state `hw` after `mod_reload.py us48_cmic`.
6. `sysled`: blue + CCA DIN/DOUT/OUTEN readback; then the [Blue](#blue-system-led) stock comparisons.
7. Panic pattern only through `pattern panic`, never a real panic.
