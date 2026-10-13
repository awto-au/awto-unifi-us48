# EdgeSwitch ESWH 1.12.2 cross-check of the UniFi board findings

Second, independent build of the same board logic (ES-48-500W / US-48-500W share the main PCB).
Offline static RE only. **C** = confirmed in code/data, **I** = inferred.

- Input: `switchdrvr` from ESWH 1.12.2 (build 5930595, 2026-05-19), ELF VA, stripped, non-PIE.
- UniFi addresses = 7.5.15 `switchdrvr` as in [board.md](board.md) / [fan.md](fan.md).
- ESWH kernel image (Linux 3.6.5) has no `pse_*` / `iproc-pse` strings; PoE interrupts come via sysfs GPIO edge files from `switchdrvr`.
- Tools: `scripts/eswh_poe_tables.py` (PoE card tables), `scripts/arm_dis.py`, `scripts/elf_peek.py`, `scripts/arm_xref_index.py`.
- UniFi <-> ESWH function join: `__func__` names from both `*.names.tsv` exports.

## Function correspondence (UniFi VA -> ESWH VA)

| Function | UniFi | ESWH |
|---|---|---|
| `bcm59111_init` / `_read` / `_write` | `0x1087238` / `0x10872c8` / `0x1087408` | `0x101a9bc` / `0x101a8ac` / `0x101a79c` |
| `bcm59111_board_alert` / `__bcm59111_board_evt_handle` | `0x1087534` / `0x1087680` | `0x101aa44` / `0x101ab74` |
| `bcm59111_board_mux` | `0x10878e0` | `0x101add0` |
| ioctl op reset / fw load / init / probe / verify | `0x1087594` / `0x1087ad0` / `0x1087a3c` / `0x1087ca8` / `0x1087c1c` | `0x101aa98` / `0x101af80` / `0x101af3c` / `0x101b0bc` / `0x101b14c` |
| chip-level msg -> reg | `0x61281c` | `0x594e74` |
| port-level msg -> reg | `0x612290` | `0x594af4` |
| PoE init reg table | `0x174659c` | `0x16b13b1` |
| PoE card table (board `0xb3440004`) | `0x1d90680` | `0x1c9d5c0` |
| PoE port map (48 x {port, card, chip, ch}) | `0x1d9454c` | `0x1c9d278` |
| board descriptor (`0xb3440004`) | `0x1d8be64` | `0x1c99448` |
| per-sensor fan thresholds | `0x1d8ffec` | `0x1c9cdc8` |
| SFP mux select (per board id) | `0x5f5fd8` | `0x58f46c` |
| sensor read (`TEMP-N`/`PoE-N`) | `0x5f65e4` | `0x58f174` |
| MSPI bank select / write8 / 6-byte LED push | `0x125cb24` / `0x125cb08` / `0x125cf2c` | `0x11b96a0` / `0x11b9684` / `0x11b9a68` |
| `soc_mspi_config`, `mspi_cmd` | `0x125c0dc`, `0xdf6d50` | `0x11b8d5c`, `0xdf8a50` |
| `hpcUbntPoeMonitorTask` | `0x610a50` | `0x599fdc` |

## board.md claims

| Claim | UniFi evidence | ESWH evidence | Agree |
|---|---|---|---|
| PoE = 2 cards, `mux0` sel `0x10` / `0x20`, ports 0-23 / 24-47 | card table `0x1d90680` | `0x1c9d5c0`: card0 sel `0x10` first 0 count 24, card1 sel `0x20` first 24 count 24; 1 mux each (C) | yes |
| 6 x BCM59111 per card at `0x28..0x2d` (`poe1..poe6`) | table | chip list packed `name[0x20]+addr` per card (C) | yes |
| Port -> card `p/24`, chip `(p%24)/4`, channel `p%4+1` | `0x1d9454c` | `0x1c9d278` byte-identical rule over 48 entries (C) | yes |
| Board id `0xb3440004` = 48-port PoE | `0x15a385c` | same id; `0xb3440003` = 24-port PoE (1 card, sel `0x10`); `0xb3440006` = 48 non-PoE, `0xb3440005` = 24 non-PoE (C) | yes |
| Chip-level msg -> reg `0,3,7,0x1b,0x2c,0x41,0x75,0x43` | `0x61281c` | `0x594e74` same (C) | yes |
| Port-level msg -> reg (`0x0c-0x0f`, `0x30..0x3e`, `0x60..0x6c`, `0x12`, `0x44`, `0x46/4b/50/55`, `0x13`, `0x14`, `0x47..0x57`, `0x49..0x58`, `0x10`) | `poe_msgmap.py` | `0x594af4` same values; set variants (msg `0x17-0x1e`) share the regs of their get (C) | yes |
| PoE init table: `01=06 12=ff 13=0f 14=ff 44=0f`, per port `HPMD=01 CUT=11 LIM=c0` via broadcast `0x30`, mux `0x30` | `0x174659c` | `0x16b13b1`: 17 pairs identical, written to `0x30` (C) | yes |
| FW load: reg `0x70`, 35-byte blocks `0x80, off BE16, 32 B`, start `0x70=0x40`, target `0x7f` if reg `0x43==0x60` else `0x30` | `0x1087ad0` | `0x101af80` same framing; target `0x7f` + 1 ms/block when reg `0x43` is `0x60` **or `0xa1`** (C) | yes + new |
| Probe = read reg `0x75` of `0x28..0x2d`, `0xaa` = running | `0x1087ca8` | `0x101b0bc` / `0x101b14c` (`0xaa`), mux `0x10`/`0x20` then `0` (C) | yes |
| Reset = CCA GPIO bit 9 high -> low -> high, 20 us | `0x1087594` | `0x101aa98`: OUTEN `0x18000068` and DOUT `0x18000064`, bit `0x200`, `sal_udelay(0x14)` between edges (C) | yes |
| PoE IRQ pins: Linux GPIO 4 alarm, 12 card0 (master), 5 card1 (slave), falling | kernel pdata + `0x5e52f4` | `0x598b1c` registers pins 4, 12 (all PoE models) and 5 (only models 0,1 = 48-port); `echo falling > .../edge` in `ubnt_hal_intrsrv.c` (C) | yes |
| LM75 `0x48..0x4b` via `mux0=0x40` | `0x5f65e4` | `0x58f174`: `local_c0 = 0x40` on mux struct, device name `temp%d` (N-1), reading `/10` (C) | yes |
| SFP mux `1 << (port-49)` for 49..52 | `0x5f61f8` | `0x58f46c`: SDK idx `0x30..0x33` -> `1 << (idx-0x30)` on `mux0`; 24-port boards `4 << (idx-0x18)` (C) | yes |
| SFP presence = Linux GPIO 6,7 (CCA 2,3) for SDK ports `0x1a`/`0x1b` | `0x5f5d14` | `0x58eedc` + `0x591348`: ports `0x1a/0x1b` -> pin `port-0x14` = 6,7, value inverted (active low) (C) | yes |
| Budget 500 W - 64 W | ubnthal table | not found as a table in ESWH; no `500000`/`436` budget constant tied to PoE code (open) | not confirmed |
| Fan: discrete presets, 2-bit level in bits 5:4 of a board-IO byte | `0x5f6ea8` | `0x590244`: `byte = (level&3)<<4 \| (byte&0x0f)` (C) | yes |
| Banks 0-3 via CMIC GP pins | `0x125cb24` "bits 1:0" | see Corrections | refined |

## Corrections / refinements to board.md

- **Bank select (C, both builds):** bank = {unit1.GP pin 0, unit0.GP pin 0}. `CMIC_GP_DATA_OUT` (soc_reg `0x1857`) bit 0 on unit 0 = bank bit 0, bit 0 on unit 1 = bank bit 1.
  - bank 0 = (0,0), 1 = (0,1), 2 = (1,0), 3 = (1,1) (bank = u1<<1 | u0).
  - board.md says "writes `CMIC_GP_DATA_OUT` bits 1:0"; the code touches only bit 0, once on each switch chip.
  - Pin directions set at init (`0x5910dc`, soc_reg `0x1860` OUT_EN): GP pins 0,2,3 outputs on both units for the 48-port models (`0xd`); unit 1 of the non-"500W" 48-port variant `0xc` (I: model index <2 gets pin 0, 4 does not).
  - GP pins 2,3 (SDK ports `0x1a`/`0x1b`) are SFP outputs, value inverted: `0x58ed5c`/`0x58ed9c`/`0x58ede0` (I: TX-disable).
- **CCA GPIO init per model (C, `0x5910dc`):** 48/24-port 500W/750W/250W models set OUTEN `|= 0x2c0` (bits 6,7,9) and DOUT `|= 0x200` (bit 9 high = PoE reset idle). Confirms bit 9 = reset and bits 6,7 = system LED outputs.
- **MSPI frames (C/I):** 4 blocks of an 8-byte stride buffer, frame length per block from a 4-byte table (`0x2303fe0`, filled in `0x5903ec`): ES48 = `6, 6, 7, 7` bytes.
  - Blocks 0,1 (bank 0/2): 6 bytes, PoE LED state, 2 bits/port, 4 ports/byte, MSB-first (`(3-(p&3))*2` shift), written by the PoE event handler (`0x101ab74` -> `0x11b9a68`).
  - Blocks 2,3 (bank 1/3): 7 bytes = control byte + the same 6 LED bytes (`0x58fe0c` set, `0x58fd80` get: byte index `1 + (p%24)/4`).
  - Control byte bits 5:4 = fan preset level (`0x590244`: fan 0 -> block 2, fan 1 -> block 3); bits 3:0 and 7:6 not touched by the fan setter (unknown).
  - => answers board.md "banks 1/3: 7-byte frames, purpose unknown": fan level + LED state per card.
  - Max MSPI transfer in `soc_mspi_cmd` helper `0x11b915c`: 16 bytes (`len + rdlen < 0x11`).
  - A 16-bit companion frame (block 4, 2 bytes, MSB-first bit per index, active low, `0x5902b8`) drives 12 LED bits for SDK ids `0x12..0x1d` (`0x590314`). I: front-panel link/SFP LEDs.
- **Fan thermal policy (C):** per-sensor 4-threshold rows identical to UniFi (16 rows, 4 LM75 + 12 BCM59111; first row 45/55/60/65 degC). Levels 0..3 at t0..t2, t3 = overheat alert. Level is written to **both** fan groups (set(0,lvl), then set(1,lvl) if the second unit exists, `0x592688`).
  - `fan_temp_mapping_interval` is a log string in `ubnt_hal_sys.c`; the interval value is not in the table (open).
- **Device-id `0xa1` (C):** ES FW loader treats reg `0x43` in `{0x60, 0xa1}` as the "new" PoE silicon (target `0x7f`, 1 ms between blocks). UniFi code (`0x1087ad0`) only tests `0x60`; a chip reporting `0xa1` would take the broadcast path with no delay there.

## BCM59111 temperature (reg 0x2c) - answers board.md open question

- Formula (C, both builds): `T = 367 - 2 * raw`, accepted only when `raw > 0x53` (T below 201); otherwise sensor marked invalid and value set to sentinel `0xffffd8f1` (-9999).
  - UniFi: `0x610a50` region (`* -2 + 0x16f`, two sites). ESWH: `0x598df0` (init) and `0x599fdc` (monitor task).
  - Update filter: new value is taken only when within +49 / -49 of the previous one, or the previous is the sentinel.
  - ESWH `ChipStatus` prints `temperature = %u` from this converted field.
- I: unit is degC (consumed as a PoE-NN sensor and compared with the same threshold rows as the LM75s). Not checked against a live reading.
- Sensor path: `PoE-N` -> RPC msg `0xe` (`0x28f960`) returns the stored int directly; no further scaling (C).
- LM75 path: i2c word read via `bcm_i2c_ioctl`, then `/10` (value is in 0.1 degC units after the SDK helper); threshold compare in whole degC (C for the divide, I for the 0.1 degC unit).
- Other chip fields in the same init (C): reg `0x1b` -> `raw>>5` and `raw&0x1f` (manufacturer / IC id, nibble-ish split), reg `0x43` -> device id / silicon rev, reg `0x41` firmware rev.

## Port map / PHY / SerDes config (BCM53344, ubnt_hal_sys.c `0x592cd0`)

Applied for boards `0xb3440004` and `0xb3440006` (48-port); per-unit SDK properties. C.

| Property | Value |
|---|---|
| `phy_an_c73` / `phy_an_c37` | 0 / 3 |
| `serdes_if_type_xe.1` | 9 |
| `serdes_preemphasis_xe.1` | `0x5a54` |
| `serdes_driver_current_xe.1` | `0x2020` |
| `phy_port_primary_and_offset_N`, N = 2..9 | `0x207 .. 0x200` (primary PHY addr 2, offset 7..0, reversed) |
| N = 10..17 | `0xa00 .. 0xa07` (primary 0xa, offsets 0..7) |
| N = 18..25 | `0x1200 .. 0x1207` (primary 0x12, offsets 0..7) |
| `port_phy_addr_N`, N = 18..25 | `0x14 .. 0x1b` |
| `trunk_extend` | set (value string not decoded) |

- SDK port numbering: ge0..ge23 = SDK ports 2..25 (identity with N above); SDK 26/27 = ge24/ge25 on unit 0 and xe0/xe1 on unit 1; SDK 28/29 = inter-chip HiGig links (descriptor `0x1c99448` has the same 4 link entries as UniFi `0x1d8be64`: unit0 p28 <-> unit1 p28, p29 <-> p29, type `0xd`). C.
- Descriptor lists device ids `0x8344` (unit 0, 48-port) — second unit's `0x8346` not re-checked here. I.
- Snake scripts `snake.es48.{0,1}.soc` are referenced by the burn-in test (`rcload /mnt/sdk/...`), same names as UniFi. C.
- I: the 24 copper ports per unit use the same PHY map (octal PHY blocks at 2, 0xa, 0x12); which unit holds front ports 1-24 is still not in these tables. Front-port -> (unit, port) map lives in FastPath `hpc`/`sysapi` tables not decoded.

## Model / board identity (ESWH)

- Model index from a string compare (`0x5924b4`): `es48`=0 (ES-48-750W), `es48l`=1 (**ES-48-500W**), `es24`=2, `es24l`=3, `es48n`=4 (48 Lite), `es24n`=5, `es16`=6, `es12f`=7, `ep16`=8, `es8`=9. C (string table `0x14ee548..`).
- Name tables: long names `0x570d30`, short names `0x570e38`, same index.
- FastPath board ids: ES24 PoE `0xb3440003`, ES48 PoE `0xb3440004` (750W and 500W share it), ES24 non-PoE `0xb3440005`, ES48 non-PoE `0xb3440006`, `0xb3440007` / `0xb3460001` = smaller 12/16-port boards. C.
- 500W vs 750W differ by model index only in the code paths reviewed (same card table, same thresholds: `0x1c9cf4c` rows for model 0 and 1 point at the same threshold table). The PoE power budget itself is not in these tables (open).

## Open

- PoE power budget source (500 W / 64 W reserve) in ESWH; not in `hpc_ubnt_poe.c` tables.
- Control byte bits 3:0 / 7:6 of the 7-byte MSPI frame.
- Whether `0xa1` silicon exists on the US-48-500W (affects FW loader branch).
- Front port -> (unit, port) mapping and which unit has copper ports 1-24.
- Interval and level values of the fan mapping task (`fm_*` log fields).
