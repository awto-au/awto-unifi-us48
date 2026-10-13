# Board peripherals (stock firmware 7.5.15, static RE)

How stock firmware drives PoE, sensors, identity, GPIO, LEDs and SFP I2C. Offline disassembly only.

- Inputs: `switchdrvr` sha256 `d76ef864…8593c7` (same build as [fan.md](fan.md)), `ubnthal.ko`, `gpiodev.ko`,
  stock kernel 3.6.5 image (virtual base `0xc0018000`), initramfs file list.
- Addresses: `switchdrvr` = ELF VA (non-PIE, stripped); kernel = VA; `.ko` = section offset.
- **C** = confirmed (code/data cited). **I** = inferred.
- Tools: `scripts/arm_xref.py`, `scripts/arm_xref_index.py` (string/const xrefs, BL callers),
  `scripts/arm_callargs.py` (constant args at call sites), `scripts/poe_msgmap.py`, `scripts/ubnthal_board.py`.

## Software split

| Layer | Role | Evidence |
|---|---|---|
| kernel `iproc-pse` platform driver (`pse_plt`) | requests 3 GPIO IRQs, forwards events to userspace over generic netlink `PSE_FAMILY` | C: probe `0xc0265fc0`, pdata `0xc0f05e00` |
| `gpiodev.ko` | reset button IRQ, system LED, `/proc/gpio/*`; `gpio_set_poe` is a stub (`return 0`) | C |
| `ubnthal.ko` | identity only: EEPROM parse, per-board table, `/proc/ubnthal/*`. **No** I2C/GPIO/PoE code | C: no i2c/gpio imports |
| `poed` (applet of `ubntbox`) | ubus daemon, per-port PoE policy + staggered enable, waits for `/var/run/fastpath/.poe_ready` | C: strings `poed_method_*`, `stagger_ms` |
| `libubnt` | RPC client (`ubnt_set_port_poe_attrib`, `board_get_poe_budget`) | C: exports |
| `switchdrvr` (FastPath + OpenBCM SDK) | all I2C: PoE, temp sensors, SFP; MSPI board IO; GPIO via SDK | C |

- I2C master used by `switchdrvr`: OpenBCM `soc/i2c/smbus.c` on the BCM53344's `CMIC_I2CM_SMBUS_*` registers (soc_reg idx `0x186a` CONFIG, `0x1869` BIT_BANG, `0x186d` MASTER_COMMAND, `0x186f` MASTER_DATA_WRITE). C: `smbus_timeout_recovery` `0x1094050`, `smbus_quick_command` `0x1094d28`.
- Same register layout as iProc SMBus (`i2c-bcm-iproc`: CONFIG `0x00`, MASTER_COMMAND `0x30`). I: physical block = iProc SMBus0 `0x18008000`. Unverified.
- I: kernel has no I2C bus driver (no adapter-name strings found); `/dev/i2c-0`, `/dev/i2c-1` are static nodes in the initramfs cpio (C). Explains "no sysfs clients".

## I2C topology

Device table `0x1e824c4` (OpenBCM `i2c_devices[]`, stride `0x24`: name, 7-bit addr, driver). Superset for all UniFi switch models; rows used on this board marked.

| Name | Addr | Driver (struct) | Part | Used on US48P500 |
|---|---|---|---|---|
| `mux0` | `0x70` | `0x1e82b24` | PCA9548 | C: PoE, TEMP, SFP |
| `mux1` | `0x71` | `0x1e82b24` | PCA9548 | not seen |
| `poe0` | `0x30` | `0x1e7e394` | BCM59111 broadcast | C: init/firmware |
| `poe1`..`poe6` | `0x28`..`0x2d` | `0x1e7e394` | BCM59111 | C: 6 per PoE card |
| `temp0`..`temp3` | `0x48`..`0x4b` | `0x1e829fc` | LM75 | C: TEMP-1..4 |
| `sfp0` / `sfpd0` / `csfp0` | `0x50` / `0x51` / `0x56` | `0x1e82bc4` | SFP EEPROM / DDM / copper-SFP PHY | C |
| `iop0`..`iop3`, `iop4` | `0x20`..`0x23`, `0x25` | `0x1e82b44`, `0x1e82b04` | PCA9555 / 8-bit IO | I: other boards (`0xb3440007` path) |
| `adt0` | `0x2e` | `0x1e82dc0` | ADT7475 | no: fan hw class 4/9 only ([fan.md](fan.md)) |
| `pm0`..`pm3` | `0x40`,`0x41`,`0x44`,`0x45` | `0x1e829dc` / `0x1e829a8` | ISL28022 / INA3221 | not seen |
| `tlc0` | `0x60` | `0x1e82c34` | TLC59116 | no: `tlc59116_init_for_USL2XXPOE` |
| `padc0`, `nvram0/1`, `psumcu0/1` | `0x4b`, `0x53/0x52`, `0x19/0x18` | — | — | not seen |

Mux selection (`0x5f5df0`, struct: mux name, select byte at `+0x20`, optional 2nd-level mux at `+0x24`):

| `mux0` byte | Channel | Behind it |
|---|---|---|
| `0x01`..`0x08` | 0..3 | SFP cage of front port 49..52 (`1 << (port-49)`), C: `0x5f61f8` |
| `0x10` | 4 | PoE card 0: BCM59111 `0x28`..`0x2d`, ports 1–24 |
| `0x20` | 5 | PoE card 1: BCM59111 `0x28`..`0x2d`, ports 25–48 |
| `0x30` | 4+5 | both cards at once — used for broadcast init and firmware download |
| `0x40` | 6 | LM75 `0x48`..`0x4b` (TEMP-1..4), C: `0x5f65e4` |
| `0x00` | — | deselect after every PoE access |

## PoE

- **12× Broadcom BCM59111** (4-port PSE, I2C), 2 "cards" × 6 chips, 4 ports each = 48 ports. C:
  - PoE card table `0x1d90680` (stride `0x82c`, keyed by FastPath board id `0xb3440004`): card count 2, each card = `mux0` select (`0x10` / `0x20`), first port (0 / 24), port count 24, chip list `poe1..poe6` @ `0x28..0x2d`.
  - Port map `0x1d9454c` (48 × {port, card, chip, channel}): port p (0-based) → card `p/24`, chip `(p%24)/4`, channel `p%4 + 1`. Linear.
  - Board selection: system id `0xeb62` → hw class 1 (`0x5fa7b8`) → FastPath board `0xb3440004` (`0x15a385c[1]`) → descriptor `0x1d8be64`.
- Register map is **LTC4266-compatible** (standard PSE register set + `0x4x` high-power block). Message → register table recovered by `scripts/poe_msgmap.py` from `0x612290`:

  | Reg | Per port (ch1..4) | Use (FastPath msg) |
  |---|---|---|
  | `0x00` | chip | interrupt status |
  | `0x01` | chip | interrupt mask (write) |
  | `0x03` | chip | power event, clear-on-read (bits 7:4 = power-good change per port) |
  | `0x07` | chip | fault event, clear-on-read (bits 7:4 = disconnect per port) |
  | `0x0c`..`0x0f` | 1 reg each | port status: detect / class |
  | `0x10` | shared | power status: pwr_good / pwr_enable |
  | `0x12` | shared, 2 bits/port | operating mode |
  | `0x13` | shared | disconnect enable |
  | `0x14` | shared | detect enable (low nibble), class enable (high nibble) |
  | `0x1b` | chip | manufacturer / IC id |
  | `0x2c` | chip | die temperature → `PoE-01..12` sensors |
  | `0x30/0x34/0x38/0x3c` | 16-bit | port current |
  | `0x32/0x36/0x3a/0x3e` | 16-bit | port voltage |
  | `0x41` | chip | firmware revision |
  | `0x43` | chip | device id / silicon revision |
  | `0x44` | shared | high-power enable |
  | `0x46/0x4b/0x50/0x55` | 1 each | high-power mode |
  | `0x47/0x4c/0x51/0x56` | 1 each | current cut (ICUT) |
  | `0x48/0x4d/0x52/0x57` | 1 each | current limit (ILIM) |
  | `0x49/0x4e/0x53/0x58` | 1 each | high-power status |
  | `0x60/0x64/0x68/0x6c` | 32-bit | per-port counters (MPS absent, overload, short, power denied, invalid signature) |
  | `0x70` | chip | firmware download port (see below) |
  | `0x75` | chip | firmware status: `0xaa` = running |

  - Chip-level msg → reg (`0x61281c`): 0→`0x00`, 1→`0x03`, 2→`0x07`, 3→`0x1b`, 4→`0x2c`, 5→`0x41`, 6→`0x75`, 7→`0x43`; write 8→`0x01` (`0x6127b4`).
- Driver `bcm59111` ioctl `0x1087d68` (ops):

  | Op | Function | Action |
  |---|---|---|
  | 0 | `0x1087ca8` | probe: read reg `0x75` of `0x28..0x2d` on each card → presence mask |
  | 1 | `0x1087594` | reset: SoC GPIO (CCA bit 9) high → low → high, `sal_udelay(20)` between edges; then `0x125d24c` |
  | 2 | `0x1087ad0` | firmware download to all chips (`mux0 = 0x30`) |
  | 3 | `0x1087a3c` | init: register writes via broadcast `0x30` (`mux0 = 0x30`) |
  | 4 | `0x1087c1c` | verify reg `0x75 == 0xaa` per chip, both cards |

- Init sequence (table `0x174659c`, broadcast `0x30`), C:
  `01=06` (IRQ mask: power-good + disconnect), `12=ff` (all ports auto), `13=0f`, `14=ff` (detect+class on),
  `44=0f` (HP enable), per port `HPMD=01`, `CUT=11`, `LIM=c0`.
- Firmware download, C (`0x1087ad0`):
  - Blob: 16 KiB in `switchdrvr` `.data` at VA `0x1e7a394`, length word at `0x1e7a390` (`0x4000`); sha256 `31496a90bead4c7feb87994ad30d626b70d07e112d79d60b587bf6b35ebdd889`. Not copied.
  - Target: `0x7f` if chip `0x28` reg `0x43 == 0x60` (plus 1 ms between chunks), else broadcast `0x30`.
  - 512 block writes to reg `0x70`, 35 bytes each: `0x80`, offset (BE16), 32 data bytes. Then write `0x70 = 0x40` (start).
  - Status strings: `PoE deviceID(0x%x) found, start firmware load process`, `PoE firmware check failed, reset the module`.
  - OpenWrt needs this blob at runtime; redistribution status unknown.
- Interrupts (kernel, C):

  | Linux GPIO | CCA bit | IRQ | Name | Trigger | switchdrvr handler |
  |---|---|---|---|---|---|
  | 4 | 0 | 256 | `pse_alarm_int` | falling | `0x1087534`: log only |
  | 5 | 1 | 257 | `pse_slave_int` | falling | `0x10879e4`: card 1 (ports 25–48) |
  | 12 | 8 | 264 | `pse_master_int` | falling | `0x10879a0`: card 0 (ports 1–24) |

  - Kernel: IRQ → `disable_irq_nosync` → work → `pse_nl_port_evt_unicast(type)` (alarm 0, master 1, slave 2) → `enable_irq`. Dispatch table in switchdrvr `0x1e7e3b4`.
  - Card handler `__bcm59111_board_evt_handle` `0x1087680`, per chip:
    - read `0x00`; bit 2 (disconnect) → read `0x03`, clear 2-bit LED state of ports in bits 7:4, push to MSPI, read `0x07` (clear).
    - bit 1 only (power good) → read `0x03`, set LED state of ports in bits 7:4, push to MSPI.
  - switchdrvr also exports GPIO 4/5/12 via sysfs (`edge both`, `active_low 1`) for hw class 0–4, C: `0x5e52f4`.
- Budget: ubnthal max 500 W, reserved 64 W → 436 W = observed budget. I: switchdrvr/libubnt `board_get_poe_budget` use these; not traced.
- PoE LEDs: 2 bits/port in MSPI board IO, 6 bytes per card (`0x125cf2c`, bank 0 = card 0, bank 2 = card 1). C.

## Temperature sensors

| Sensor | Device | Path | Evidence |
|---|---|---|---|
| TEMP-1..4 | LM75 `temp0..3` @ `0x48..0x4b` | `mux0 = 0x40` | C: `0x5f65e4` (`TEMP-%d` → `temp%d` N-1) |
| PoE-01..12 | BCM59111 reg `0x2c` | via PoE monitor task cache, msg `0xe` | C: `PoE-%d` → `0x2c2b08(N-1)`; I: index = card·6 + chip |

- Raw → °C conversion for reg `0x2c` not decoded.

## GPIO map (SoC ChipcommonA GPIO, `0x18000060` DIN / `0x64` DOUT / `0x68` OUTEN)

Stock kernel `GPIOA` chip: base 4, 12 lines, IRQ base 256 (`0xc0f06380`). Linux GPIO n = CCA bit n−4. C unless marked.

| Linux GPIO | CCA bit | Function | Evidence |
|---|---|---|---|
| 4 | 0 | PSE alarm IRQ | kernel pdata |
| 5 | 1 | PSE card 1 IRQ | kernel pdata |
| 6, 7 | 2, 3 | SFP presence (active low) for SDK port ids `0x1a`/`0x1b` | `0x5f5d14` → `0x5e56cc`; I: which cages |
| 10, 11 | 6, 7 | system LED: `led` bit 1 → CCA 6, bit 0 → CCA 7 | `gpiodev` `gpio_set_led`; I: colour per bit |
| 12 | 8 | PSE card 0 IRQ | kernel pdata |
| 13 | 9 | BCM59111 reset (pulse low) | ioctl op 1 `0x1087594` |
| 15 | 11 | reset button, IRQ 267, level-low | `gpiodev` `gpio_reset_irq_setup` (`0xeb62` → 267) |

- switchdrvr "board pin" API `0x5e56cc`/`0x5e57c8`: pins 0–3 = BCM53344 `CMIC_GP_*` (soc_reg `0x1856` DATA_IN, `0x1857` DATA_OUT), pins 4–15 = CCA bits 0–11.

## MSPI board IO (relation to [fan.md](fan.md))

- One MSPI transport (`soc_mspi_write8` `0x125cb08`) serves fans and PoE LEDs. C.
- Bank / chip select driven by CMIC GP pins 0–3: `0x125cb24` (4 banks) writes `CMIC_GP_DATA_OUT` bits 1:0; fan path `0x5e9764` (6 banks) sets pins via `0x5e57c8`. C; exact truth tables not decoded.
- Banks 0/2: PoE LED bytes (6 per card). Banks 1/3: 7-byte frames (`0x125cfc4`), purpose unknown.
- I: shift-register chain or CPLD for LEDs + fan presets; no PoE power enable path through it (PoE enable is BCM59111 I2C).

## Board table (`ubnthal.ko`)

`scripts/ubnthal_board.py` decodes it. Table `.rodata+0x4e8`, 28 entries, stride `0x66d8`; US-48-500W at `.rodata+0x53de0`. C:

| Offset | Field | US48P500 |
|---|---|---|
| `+0x0000` | name | `US-48-500W` |
| `+0x0040` | platform | `US48P500` |
| `+0x0076` | unit count | 1 (logical; FastPath has 2 chips) |
| `+0x0078` | unit[0]: port count | 52 |
| `+0x007c + 24·i` | port: phy, log, caps (`+8`, u32) | phy = log = 1..52 (identity) |
| | caps 1–48 / 49–50 / 51–52 | `0x1a02` / `0x10` / `0x08`; PoE = caps & `0x17a00` → 48 ports |
| `+0x05bf` | CPU port | 0 |
| `+0x3000` | OOB port block | absent |
| `+0x6454` | system id | `0xeb62` |
| `+0x6464` | fan count | 4 |
| `+0x64b8` / `+0x64ba` | max / reserved PoE W | 500 / 64 |
| `+0x64d1` / `+0x64d2` / `+0x64d6` | LCM count / LED count / LED prefix | 0 / 0 / empty |

- Unknown non-zero bytes: `+0x5c = 7`, `+0x6e = 0x5a`, `+0x70 = 0xe6`, `+0x64bc = 01 03 03`, `+0x64cc = 3a 48 0b`.
- Hw class (switchdrvr `0x5fa7b8`, index id − `0xeb10`): `eb63`→0, **`eb62`→1**, `eb65`→2, `eb64`→3, `eb66`→4; all five → FastPath board `0xb3440004`.

## EEPROM (mtd6) as parsed by `ubnthal`

`bcm5334x_scan_eeprom` `0x49e0`: `iproc_qspi_flash_read(0x1dff0000, 4096)` = flash `0x1ff0000` (BSPI window `0x1c000000`). C:

| EEPROM offset | Field |
|---|---|
| `0x00` | eth0 MAC (6) |
| `0x06` | eth1 MAC (6) |
| `0x0c` | system / board id, BE16 (`0xeb62`) |
| `0x0e` | vendor id, BE16; must be `0x0777` else id/rev set to `0xffff` |
| `0x10` | BOM rev, BE32; board revision = byte `0x13` (`ffffffff` → invalid) |

- Also reads 8 bytes at flash `0x1ffe040` (mapped `0x1dffe040`) into sysinfo `+0x10a8`; consumer not traced.
- `manufid`, `mfgweek`, `qrid`, hash/anon ids: printed by `systeminfo_dump`; source offsets not decoded.

## Two-chip layout

- FastPath descriptor `0x1d8be64`: 2 units, device ids `0x8344`, `0x8346`; 4 link entries {unit, port, peer unit, peer port, `0xd`}: unit0 p28 ↔ unit1 p28, unit0 p29 ↔ unit1 p29. C.
- Snake test scripts (`/usr/etc/switch/snake.es48.{0,1}.soc`): unit 0 = `ge0-ge23` + `ge24`,`ge25`; unit 1 = `ge0-ge23` + `xe0`,`xe1`; inter-chip = 2-port HiGig trunk. C (file content).
- I: unit 0 carries 24 copper + SFP 51–52 (1G), unit 1 carries 24 copper + SFP+ 49–50 (10G). Which copper block is ports 1–24 not established.
- Copper PHY MDIO addressing: not decoded.

## Open questions

- Physical controller behind `CMIC_I2CM_SMBUS` on HR2 (iProc SMBus0 `0x18008000`?) — decides whether upstream `i2c-bcm-iproc` reaches the PoE/sensor bus.
- BCM59111 firmware: licence for redistribution; whether chips keep firmware across warm reboot (stock always reloads?).
- BCM59111 reg `0x2c` temperature formula.
- MSPI bank truth table; banks 1/3 content; default board-IO state at power-up.
- SFP presence GPIO → cage mapping; SFP+ (49/50) presence source.
- Front port → (unit, port) map and PHY addresses.
- EEPROM offsets of `manufid` / `mfgweek` / `qrid`; use of the `0x1ffe040` bytes.
