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
- PoE LEDs: 2 bits/port in MSPI board IO, 6 bytes per card (`0x125cf2c`; select pins (0,0) card 0, (0,1) card 1; see MSPI section). C.

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

Write-only 8-bit frames to four chains selected by two CMIC GPIO pins. Fans, PoE LEDs and port LEDs share it. C unless marked.

**Controller**
- SDK `soc_mspi_*` (`mspi.c`) on CMICm register space: SPCR0_LSB `0x1500`, STATUS `0x1520`, TXRAM `0x1540` (stride 8), RXRAM `0x15c0`, CDRAM `0x1640`. Same layout as the iProc QSPI MSPI block (`0x18027200`: TXRAM `+0x40`).
- I: same physical MSPI/pads as the boot-flash QSPI. Basis: SDK claims the bus with `CMIC_OVERRIDE_STRAP` + `CMICM_BSPI_MAST_N_BOOT`; only one CS exists, so extra devices need external select pins. Unproven: no register-level alias check done.
- Always unit 0's MSPI (`0x125cb08(0, ...)`); unit 1 contributes only select line B.
- Setup on first use (`0x5e5d60`): `soc_mspi_config(unit 0, device 0, CPOL 1, CPHA 1)` = SPI mode 3, 8 bit, SPBR 8. Device 0 leaves `CMIC_GP_DATA_OUT` untouched. Read path (`0x5e5df4`) reconfigures CPOL 0 / CPHA 1.
- Frame = 1..16 bytes, CS held (CDRAM `0x80`) until the last byte. Completion poll: SPIF in STATUS, SDK limit 10 ms.
- Callers hold a semaphore (`0x5e5d60`); no retries.

**Select lines** (pin A = unit 0 `CMIC_GP_DATA_OUT` bit 0, pin B = unit 1 bit 0; unit 1 skipped on single-chip classes)
- Pins set before every transfer, never restored. Output-enable of the GP pins not found in switchdrvr (HR2 `soc_mspi_init` does not set it): I: strap/default or bootloader.

| A | B | Frame | Len | Content | Fan-path bank (`0x5e9764`) | SDK-path bank (`0x125cb24`) |
|---|---|---|---|---|---|---|
| 0 | 0 | PoE LEDs card 0 (front 1–24) | 6 | 2 bits/port | 0 | 0 |
| 0 | 1 | PoE LEDs card 1 (front 25–48) | 6 | 2 bits/port | 1 | 2 |
| 1 | 0 | unit 0: fan + port LEDs front 1–24 | 7 | byte 0 fan, bytes 1–6 LEDs | 2 | 1 |
| 1 | 1 | unit 1: fan + port LEDs front 25–48 | 7 | same | 3 | 3 |

- The two bank numberings are swapped for (0,1)/(1,0): fan path `A = bank>>1, B = bank&1`; SDK path `A = bank&1, B = bank>>1`. Table is by pin state.
- Fan-path banks 4, 5 (unit 0 pins GP2/GP3 = 00 / 01) exist only for board `0xb3440007` (12-bit SFP tx-disable out + 12×2-bit in, the only MSPI read: `0x5f77b4`). Not used on US-48-500W. I: other board class.

**Frame layout** (US-48-500W, hw class 1)
- 2-bit field per port, MSB first: port index `i` → byte `base + (i>>2)`, bits `7-2*(i&3) : 6-2*(i&3)` (masks `fc f3 cf 3f`, `0x15a3818`).
- PoE frames: `base = 0`. SDK path: byte = BCM59111 chip (0..5), field = channel 1..4 = PSE bits 4–7. Set `01` on power-good, clear on disconnect. HAL path `0x5f6a40` writes `01`/`10` by port state (`0x1d901c4`: 2 for states 4/7, 1 for 1/5/8). Colour per value not known.
- Port LED frames: `base = 1` (class 1; classes `0xc,0xd,0xf,0x10..0x12` use 0 -> offset test `0x5f6c04`). Writer `0x60620c` maps 9 port states to value 0 (off) / 1 (states 4,7) / 2 (1,2,3,5,6,8); blink = off, 100 ms, on. Front ports 49–52 have no field here: I: separate path (not found).
- Fan byte 0: bits 5:4 = preset level (0..3, `0x5f6ea8`; read `0x5f6e84`); bits 3:0 cleared by `0x5f6d78` at init (mask 1/2/4/8/0xf; meaning unknown, I: per-fan enable/tach select); bits 7:6 unused.
- Fan group = chip: unit 0 -> frame (1,0), unit 1 -> frame (1,1). `swctrl` native setter `0x26fb90` writes both when dual-chip. 4 fans -> I: 2 per group.

**Shadows (BSS)**
- HAL: `0x23e8c08` + 8·bank (fan-path bank 0..5, 6×8 bytes, zeroed by `0x5f782c`); frame lengths `0x23e8c38 + bank` = {6,6,7,7} for classes 0–9; bank 4/5 lengths `0x23e8c3c` = {2,3} only class `0xe`; bank 4 data `0x23e8c28`, bank 5 read buffer `0x23e8c30`. Semaphore `0x23e544c`; "mode set" flag `0x23e5450`.
- SDK: PoE LED 6 B/card at `0x254aae4`, 7 B/card frames at `0x254aaf4` (never written by any code found; zero), semaphore `0x254aaf0`. Flush all four: `0x1087a84` (bcm59111 driver; call site not found).

**Startup sequence** (switchdrvr per unit, `0x5f7bfc`)
1. `0x5f782c`: zero shadows; optional `/var/run/fastpath/.poe_defconfig` lines `bank-2 index, byte, hex value` preset bytes of the 7-byte frames (producer not found in rootfs).
2. `0x5f7ad8`: fan profile type 0 -> byte 0 low nibble cleared, level 1 (70 %) -> byte 0 = `0x10`, frame sent (first MSPI transfer, mode setup).
3. `0x5f7a54` after PoE firmware reset (`0x61095c`, `0x611834`): rewrite frames (1,0) and (1,1).
4. PoE events / link changes rewrite single frames afterwards. No MSPI read on this board; no CPLD id/version read.
- Power-on state before step 2 unknown (shift-register content undefined unless hardware default). Whether fans run full speed until step 2 not known.
- I: four 74HC595-style daisy chains (write-only, odd frame lengths 6/7 B = 48/56 bit, no readback); a CPLD would normally expose an id read. Board photos would settle it.

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

- FastPath descriptor `0x1d8be64`: 2 units, device ids `0x8344` (unit 0, BCM53344), `0x8346` (unit 1, BCM53346); 4 link entries {unit, port, peer unit, peer port, speed code `0xd`}: unit 0 p28 <-> unit 1 p28, unit 0 p29 <-> unit 1 p29. C.
- Inter-chip links: 2 x 13G HiGig (logical ports 28/29 are the HR2 "13G" ports). `0x5fb3cc` sets both ports on both units to speed 13000 Mb/s. C.
- Snake scripts (`/usr/etc/switch/snake.es48.{0,1}.soc`): unit 0 = `ge0-ge23` + `ge24`,`ge25`; unit 1 = `ge0-ge23` + `xe0`,`xe1`; inter-chip = 2-port HiGig trunk. C.

## Port map

Config for board `0xb3440004` is injected into the SDK by `0x5fb5e0` (called from `0x5e4834`); same code on both units. C.

- `bcm5615x_config = 10`: OpenBCM Option 2 = 24 x 1G (octal PHY) + 2 uplinks (53344: 1G; 53346: 1G/10G) + 2 x 13G HiGig; `p2l_mapping_24_2_2`. C.
- Physical -> logical (`p2l`): phys 2..9 -> logical 9..2 (reversed), 10..25 identity, 26/28/30/32 -> 26/27/28/29. I: table taken from OpenBCM source, selected by config id (not read from the binary).
- `phy_port_primary_and_offset_<logical>` (table `0x15a3eac`): logical 2..9 -> `0x0207..0x0200`, 10..17 -> `0x0a00..0x0a07`, 18..25 -> `0x1200..0x1207`. Three octal PHYs per chip; primary/offset follow physical lane. C.
- `port_phy_addr_<logical>` set only for 18..25 -> `0x14..0x1b`; equals the SDK default for phys 18..25. All other PHY addresses are SDK defaults (`_soc_phy_addr_bcm56150`, by physical port, external MIIM bus 0): phys 2..9 -> `0x02..0x09`, 10..17 -> `0x0b..0x12`, 18..25 -> `0x14..0x1b`. I: not confirmed against hardware.
- Other props: `phy_an_c73=0`, `phy_an_c37=3`, `trunk_extend=0x1`; unit 1 xe ports: `serdes_if_type_xe.1=9` (SFI), `serdes_preemphasis_xe.1=0x5a54`, `serdes_driver_current_xe.1=0x2020`. C.
- SerDes: internal QSGMII block `(phys-2)/8` (3 blocks), lane `(phys-2)%8`; uplinks on TSC4 lanes phys 26, 28 (HiGig phys 30, 32). I: SDK table `_hurricane2_phy_addr_default`.
- Front port (1..52) -> (unit, logical) from the FastPath table at `0x1d8ecb4` (52 x {slot 0, port-1, unit, logical}; lookup `0x5e35c4`). C.

| Front | Unit (chip) | Logical | Octal PHY, MDIO addr (bus 0) |
|---|---|---|---|
| 1..8 | 0 (53344) | 2..9 | A, `0x09..0x02` (front 1 = `0x09`, ch7) |
| 9..16 | 0 | 10..17 | B, `0x0b..0x12` |
| 17..24 | 0 | 18..25 | C, `0x14..0x1b` |
| 25..32 | 1 (53346) | 2..9 | A, `0x09..0x02` |
| 33..40 | 1 | 10..17 | B, `0x0b..0x12` |
| 41..48 | 1 | 18..25 | C, `0x14..0x1b` |
| 49, 50 | 1 | 26, 27 (`xe0`, `xe1`) | SFP+, no PHY seen |
| 51, 52 | 0 | 26, 27 (`ge24`, `ge25`) | SFP, no PHY seen |

- Per-port generator: `scripts/us48_portmap.py`.
- Port LEDs: no LED-processor microcode or `led*` SDK property found in switchdrvr (only register names); copper port LEDs go through the MSPI frames above. I. Front 49–52 LEDs not located.

## Open questions

- Physical controller behind `CMIC_I2CM_SMBUS` on HR2 (iProc SMBus0 `0x18008000`?) — decides whether upstream `i2c-bcm-iproc` reaches the PoE/sensor bus.
- BCM59111 firmware: licence for redistribution; whether chips keep firmware across warm reboot (stock always reloads?).
- BCM59111 reg `0x2c` temperature formula.
- MSPI: power-on default of the board-IO chains; meaning of fan byte 0 bits 3:0; LED value colours; output-enable of CMIC GP0 (who sets it); same physical block as boot-flash QSPI?; source of `.poe_defconfig`.
- SFP presence GPIO → cage mapping; SFP+ (49/50) presence source.
- Port map: PHY MDIO addresses are SDK defaults, unverified on hardware; front 49–52 LEDs; link LED value semantics.
- EEPROM offsets of `manufid` / `mfgweek` / `qrid`; use of the `0x1ffe040` bytes.
