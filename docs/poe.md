# PoE (BCM59111): how stock 7.5.15 drives it, and a safe Linux sequence

Offline static RE of stock UniFi 7.5.15 for #23; feeds #24 (PoE off/on) and #26 (`pse-pd` driver). No hardware touched for this document.

- **C** = confirmed in code/data (address cited). **I** = inferred. **M** = needs a live measurement.
- `switchdrvr` addresses = ELF VA. `sw.c:N` = line in the Ghidra export `tmp/ghidra/out/switchdrvr/switchdrvr.c` (gitignored; regenerate with `scripts/ghidra_export.py`). `ubntbox` / `libubnt` / ESWH = same convention.
- Tools: `scripts/ghidra_func.py ADDR` (decompiled function + callers/callees), `scripts/poe_ubntbox_xref.py` (string refs in the section-less `ubntbox`), `scripts/poe_msgmap.py`, `scripts/elf_peek.py`, `scripts/arm_dis.py`.
- Reference driver: Ubiquiti GPL `bcm59111.{c,h}` (UDM kernel) at `/vendors/ubiquiti/udm-kernel-bcm59111/` ("UDM").
- Cross-check build: EdgeSwitch ESWH 1.12.2 `switchdrvr` (same board logic).

## 1. Summary

- Opmode `0x12` field + MSPI port field are written together for every mode change (`0x60620c` → `0x612fd8` + `0x5f6c04`). C.
- Only opmodes 0..4 are accepted on this board (per-port mask `0x1f`, table `0x1d94b04`); 5..8 return rc 4 before any write. C.
- MSPI port field (frames (1,0)/(1,1) bytes 1..6): `0` none, `1` passive 24 V (opmode 4), `2` 802.3 (opmodes 1,2,3). C for code, **I for the physical meaning** (§6).
- Stock default for every port = opmode 3 (auto), written at start-up by FastPath/poed. C (`0x6152cc` → 3 except hw class 0xf).
- The init table `01=06 12=ff 13=0f 14=ff 44=0f …` at `0x174659e` is **never executed** on this board (ioctl op 2 has no caller). Runtime config goes through per-attribute setters. C (§4).
- Budget 436 W = ubnthal 500 − 64, computed in libubnt only. No host-side budget or priority enforcement found. I (§7).
- Reset = CCA bit 9, pulse high → low 10 ms → high. Host path waits 1 s, then loads FW, then waits 2 s. C (§8). Polarity active-low: I.
- Warm reboot leaves FW and opmode running (live, #24). C.

## 2. Software chain

| Layer | Function | Evidence |
|---|---|---|
| `poed` (applet in `ubntbox`) | waits `/var/run/fastpath/.poe_ready`, then `brcm_stage_enable_port` per port, `stagger_ms` apart (default 200 ms, max 2000 ms) | `ubntbox` `0x454d8`; stagger consts `0x459b0..0x459d8`. C/I |
| `poed` config generator | writes `/var/run/fastpath/.poe_defconfig`, lines `"%d %d %#x\n"` = card, byte 0..5, value (4 ports × 2 bit, port 0 in bits 7:6) | `ubntbox` `0x5b018`, fmt `0x16ac2c`. C |
| `libubnt` | `ubnt_set_port_poe_attrib`: attr byte 0 → opmode 0, 1 → opmode 4 (pasv24), 2 → opmode 3 (auto), else EINVAL | `libubnt.so.1.0.0.c:23932`. C |
| `libubnt` | `ubnt_get_port_poe_attrib`: opmode 3,6 → auto; 4,7 → passive, 24.0 V, 2400 | `libubnt.so.1.0.0.c:23728`. C |
| `libubnt` | `board_get_poe_budget` = max(ubnthal `+0x64b8` − `+0x64ba`, 0) = 500 − 64 | `libubnt.so.1.0.0.c:796`. C |
| FastPath `ubnt_poe` (usmdb/cfg) | default cfg per port `0x2c03a0`; startup `0x2c0e60` (force-shutdown runtime state, writes `.poe_ready`) | sw.c:375345, 375725. C |
| `hpc_ubnt_poe` / `ubnt_poe_common` | msg dispatcher `0x6118a0`, opmode `0x60d688` → `0x60620c`, monitor `0x610a50` | sw.c:696431, 694507, 690418, 695998. C |
| SDK `bcm59111.c` | raw I2C helpers, FW download `0x1087ad0`, IRQ handler `0x1087680` | sw.c:2029372, 2029182. C |

- `swctrl poe set` names (`ubntbox` table `0x1a24d8`): auto=2, poe=3, poeplus=4, poeplusplus=5, off=6, pasv24=7, passthrough=8. These are ubntbox ids, not FastPath opmodes. C.
- Name → FastPath opmode: off → 0, pasv24 → 4, auto → 3 (via libubnt attr 0/1/2). poe / poeplus / poeplusplus: only af/at/bt limits differ (poed per-cap table `0x4571c`); opmode stays 3. I.
- `passthrough` = `usmDbUbntPoeIntfOpModePassthroughSet` (value 9/0), cap bit `0x4000`; ubnthal caps for ports 1..48 are `0x1a02` (no `0x4000`). `gpio_set_poe` is a stub. Not applicable here. C/I.
- Port caps `0x1a02` = bits `0x200`, `0x800`, `0x1000`. In poed: `0x200` gates pasv24 (mode byte 1), `0x800/0x1000/0x2000/0x10000` gate 802.3 types (limits 32 W / 15.4 W / 60 W / 90 W, hp 4/1/6/7), `0x4000` passthrough. I.

## 3. BCM59111 register map as stock uses it

Access: every transfer is one SMBus byte transaction (`soc_i2c_read_byte_data` `0x1095edc`, `soc_i2c_write_byte_data` `0x10963f8`). Multi-byte values = consecutive registers, one transaction each, LSB first (`bcm59111_read` `0x10872c8`, `_write` `0x1087408`). Only FW download uses a block write (`0x1097ed8`, 35 B, §4). C.
Bus: PCA9548 `0x70` on the CMIC SMBus; card 0 = mux `0x10`, card 1 = `0x20`, both = `0x30`; chips `0x28..0x2d`, broadcast `0x30` (59121: `0x7f`). Port p (0-based): card `p/24`, chip `(p%24)/4`, channel c = `p%4 + 1`. See [board.md](board.md#poe).
Per-channel fields: bit/field index = c−1. Msg ids from `scripts/poe_msgmap.py` (`0x612290`, `0x61281c`).

| Reg | Width | Name (UDM) | Stock use | Bits | Ev |
|---|---|---|---|---|---|
| `0x00` | 8 R | INT | card IRQ handler `0x1087680`; chip msg 0 | UDM: 0 PWR_EN, 1 PWR_GOOD, 2 DIS, 3 DET, 4 CLASS, 5 TCUT, 6 TSTA, 7 SUP. Stock tests `&4` (disconnect), `&0x62 == 2` (good only), `&0x60` (fault) in `0x60e924` (register decompiled via a lost argument) | I |
| `0x01` | 8 W | INT_MASK | `0x66` written once per chip at card init (`0x6118a0` → `0x60df2c` → chip msg 8). UDM post-init writes `0x06` | `0x66` = PWR_GOOD, DIS, TCUT, TSTA | C (value), I (names) |
| `0x03` | 8 R | PWR_EVT_CLR | IRQ handler reads it after INT bit 1/2 | bits 7:4 = power-good change per channel (ch1 = bit 4), bits 3:0 = power-enable change. Read clears | C (use), I (clear) |
| `0x07` | 8 R | FAULT_EVT_CLR | read after disconnect (INT bit 2) to clear | bits 7:4 disconnect per channel | C (use), I (bits) |
| `0x02,04,05,06,08..0b` | | event / clear pairs | not used by stock | UDM names only | I |
| `0x0c+(c−1)` | 8 R | SAT_Pn | `0x612cd8` | bits 3:0 detect, bits 6:4 class (stock masks 3 bits; UDM uses `>>4`) | C |
| `0x10` | 8 R | SAT_PWR | `0x612ef0` | bits 3:0 = power-enable (ch1 = bit 0), bits 7:4 = power-good (ch1 = bit 4). UDM macros have the two swapped | C (stock naming), I (hw; matches LTC4266) |
| `0x12` | 8 RW | OP_MOD | `0x612fd8` read-modify-write | 2 bits/channel, ch1 = bits 1:0: `00` shutdown, `01` manual, `10` semi-auto, `11` auto | C (RMW), I (names; UDM + LTC4266) |
| `0x13` | 8 RW | DIS_SEN_EN | disconnect enable, `0x6131a4` | bit c−1; default `0x0f` | C |
| `0x14` | 8 RW | DET_CLASS_EN | detect `0x613340` (low nibble, bit c−1), class `0x6134dc` (high nibble) | default `0xff` | C |
| `0x1b` | 8 R | ID | chip msg 3 `0x60e168` | stock: `>>4` manufacturer, `&0xf` IC id | C |
| `0x2c` | 8 R | (temp) | `0x60e1c8`; monitor `0x610a50` | °C = 367 − 2·raw when raw > `0x53`, else invalid | C |
| `0x30+4(c−1)` | 16 R, LSB first | CM | current `0x612d14` | mA = raw × 122.07 / 1000 | C |
| `0x32+4(c−1)` | 16 R, LSB first | VM | voltage `0x612da0` | V = raw × 5.835 / 1000 | C |
| `0x41` | 8 R | FW_REV | chip msg 5; log only | `0x15` (v21) on all 12 chips (live, #24) | C |
| `0x43` | 8 R | DEV_ID | chip msg 7; FW loader `0x61052c` | `>>5` device id, `&0x1f` silicon rev. Stock accepts `(v & 0xe0) == 0xc0` (59111) or `0x60` (59121) | C |
| `0x44` | 8 RW | HP_EN | HP enable, `0x613678`; read msg 0xe | bit c−1; default `0x0f` | C |
| `0x46+5(c−1)` | 8 RW | HP_Pn_MOD | HP mode, `0x61377c` | default `0x01` | C |
| `0x47+5(c−1)` | 8 RW | ICUT | power cut, `0x6137c8`; host converter `0x6094e8` | see below | C |
| `0x48+5(c−1)` | 8 RW | ILIM | limit, `0x613814`; setter `0x600250` takes a raw byte | default `0xc0` | C |
| `0x49+5(c−1)` | 8 R | HP status | msg 0x15; read only | | C |
| `0x60+4(c−1)` | 4 R | counters | `0x612c74`, accumulated per port by the monitor | b0 invalid signature, b1 power denied, b2 low nibble short, b2 high nibble overload, b3 MPS absent | C |
| `0x70` | block W | FW_DWN_CTRL | FW download (§4) | `0x80` + BE16 offset + 32 B data; `0x40` = start/force CRC | C |
| `0x75` | 8 R | CRC_STATUS | FW check `0x61085c`, `0x1087c1c` | `0xaa` = FW running | C |
| `0x11,15,17,18,19,1a,1e,1f,42` | | UDM names only | not used by stock | `0x18/0x19/0x1a` = push-button regs in LTC4266 (detect/class, power on/off, reset) | I |

- Host ICUT converter (`0x6094e8`, mA input, valid < 2363): mA ≤ 1181.25 (= 63 × 18.75) → `(mA / 18.75) & 0x3f | 0xc0`; else `((mA >> 1) / 18.75) & 0x3f | 0x80`. C. Default cfg = 638 mA → `0xe2`. Meaning of bits 7:6 is I.
- Three different ICUT values exist for the same chip: init table `0x11`, stock runtime `0xe2` (default cfg), UDM `0x50`. Which one stock leaves in the chip: M (read `0x47` on a stock-configured unit). ILIM: table `0xc0`, UDM `0x00`.
- Detect nibble (host name table `0x1d7097c..` / status printer `0x537f5c`): 1 short, 2 CPD too high, 3 R-sig too low, 4 good, 5 R-sig too high, 6 open circuit, 7 = host's "FW load error" marker (`+0x30d` set to 7 when card init fails, `0x6118a0`). Class nibble: 1..4 = class 1..4, 6 = class 0. C (host), I (raw = same codes; LTC4266 layout).
- Temperature poll: monitor loop every 2 s (`osapiSleep(2)`) plus 50 ms per port. C.

## 4. Stock sequence, in order

Card init (`0x6118a0` phase 2 → `0x611834`), C:

1. `0x61095c` → `0x61085c`: read reg `0x75` of all 12 chips. All `0xaa` → FW running: skip 2..7, go to 8.
2. `0x5f7abc`: zero the MSPI shadow frames (memset `0x30`). C.
3. `0x60e420`: CCA bit 9 high, 10 ms, low, 10 ms, high (§8).
4. `osapiSleep(1)`: 1 s. C.
5. `0x61052c`: mux select (`0x30` for hw class 0..4), read reg `0x43` of card 0 chip 0 (`0x60e0c8`). `(v & 0xe0) == 0xc0` → 59111 blob (16 KiB, VA `0x1e7a394`, length at `0x1e7a390`); `0x60` → 59121 blob (`0x1e7e3c4`); else "check failed, skip firmware init".
6. `0x1087ad0` FW download: re-reads reg `0x43` of chip `0x28`; `0x60` → target `0x7f` and 1 ms `usleep` per block, else target `0x30` broadcast. 512 × block write to reg `0x70`: `[0x80, off_hi, off_lo, 32 data]` = 35 B, byte offsets `0, 0x20, …`. Then byte write `0x70 ← 0x40`. C.
7. `osapiSleep(2)`: fixed 2 s. Then `0x61085c` again: every chip reg `0x75 == 0xaa`. Fail → retry from 2 (up to 3 more times, "PSE init failed 0x%04x, retry %d"). C.
8. `0x5f7a54`: push MSPI frames (1,0) and (1,1) (§6). C.
9. Per chip: read `0x43`, `0x41`, `0x1b`, `0x2c`; write INT mask `0x66`. C.
10. Monitor task `ubnt_poe_monitor` created (`0x610a50`). Every 10th loop (~20 s) it runs steps 1..8 again and, if FW had been lost, "Re-apply PoE config" (`0x2c02c8`). C.
11. FastPath cfg apply per port (`0x2c0a30` → msg 2 → `0x6097e4`), order: HP enable, HP mode, detect enable, disconnect enable, class enable, ICUT, ILIM. Opmode is applied separately (`0x60d688`). Defaults (`0x2c03a0`): opmode 3, hp enable 1, hp mode 1, enables 1/1/1, cut 638 mA, limit `0xc0`. C.
12. `0x2c0e60`: log "force-shutdown … runtime only", create `/var/run/fastpath/.poe_ready`. poed then stages each port (`stagger_ms` apart). C.

- The table at `0x174659e` (17 pairs: `01=06 12=ff 13=0f 14=ff 44=0f`, per port `46=01 47=11 48=c0`) is written by ioctl op 2 (`0x1087a3c`) only. `bcm_i2c_ioctl` callers in the UniFi image are LED / fan code and debug shell. Reading board.md as "stock init" is wrong. C (no caller), I (dead code).
- ioctl op numbers are 0 reset, 1 FW download, 2 init regs, 3 verify, 4 `0x1088458` (unread), 5 probe (`0x1087d68`; matches UDM `RST/INIT/CFG/CRC/LED_INIT/PROBE`). board.md's 0..4 numbering is shifted. C.
- Block-write wire format: stock SDK has two paths. CMIC hardware path (live master `SMB_EN = 1`, board.md) sends addr, reg `0x70`, then the 35 bytes. Bit-bang path additionally sends a count byte. Hardware path used: I.
- FW load duration: not measured (M, #26).

### 4.1 Stock vs UDM driver

| Item | Stock | UDM `bcm59111.c` |
|---|---|---|
| FW chunk | 32 B data, 35 B write, 512 writes | 8 B data, 11 B write, 2048 writes |
| Start | write `0x70 ← 0x40`, sleep 2 s, read `0x75` once | write `0x70 ← 0x40`, poll `0x75` every 50 ms up to 1 s |
| Dev id check | `v & 0xe0` (any silicon rev) | exact `0xc0` / `0x60` |
| Skip-load test | `0x75 == 0xaa` on all chips | `0x41 == 21` |
| Post-FW config | per-attribute setters (§4 step 11), INT mask `0x66` | table: `01=06`, `12=00` (ubnt-poe build) or `ff`, `13=0f`, `14=ff`, `44=00`, HPMD `01`, ICUT `0x50`, ILIM `0x00` |
| Opmode change | RMW `0x12` only | also rewrites `0x14 = 0xff` |
| Class decode | bits 6:4 | `>>4` (4 bits) |
| `0x10` | enable = low nibble, good = high nibble | macros opposite |
| Reset | CCA GPIO 9 pulse | `en-gpio` property, no pulse |
| Passive 24 V | MSPI board IO (§6) | not supported (`POE_CAP_ACTV_3AT \| POE_CAP_OFF`) |

## 5. Opmodes 0..8: exact writes

Enum (ESWH name table `0x14f32e4`, UniFi CLI/show strings): 0 SHUTDOWN, 1 MANUAL, 2 SEMI AUTO, 3 AUTO 2P, 4 PASSIVE 24V 2P, 5 PASSIVE 54V 2P, 6 AUTO 4P, 7 PASSIVE 24V 4P, 8 PASSIVE 54V 4P. C.
Setter `0x60620c(port, mode)` order (sw.c:690418), C:

1. `0x614a00(port, mode)` allowed? else return 4. Table `0x1d94b04`: 48 × `{port, mask 0x1f}`.
2. `0x6152f8` maps port → PoE-in index via card-entry `+0x828`; that pointer is 0 on this board (`0x1d90ea8`), so the "port is a PoE input → rc 8" check never fires. C.
3. If chip type attr `0x17` == 1 (BCM59111): `0x612fd8(port, mode)`.
4. MSPI field: read shadow (`0x5f6b70`), then the sequence below.

`0x612fd8` (sw.c:697320): modes 0..3 → field = mode; 4, 5 → field 0; 6..8 → return 1, no write. Reads reg `0x12`, replaces the field, writes `0x12` back.

| Opmode | Name | Allowed here | Reg `0x12` field | MSPI field |
|---|---|---|---|---|
| 0 | shutdown | yes | `00` | `0` |
| 1 | manual | yes | `01` | `2` |
| 2 | semi-auto | yes | `10` | `2` |
| 3 | auto | yes (stock default) | `11` | `2` |
| 4 | passive 24 V | yes | `00` | `1` |
| 5 | passive 54 V | no (rc 4) | would be `00` | would be `2` |
| 6 | auto 4-pair | no | no write (rc 1) | would be `2` |
| 7 | passive 24 V 4-pair | no | no write (rc 1) | would be `1` |
| 8 | passive 54 V 4-pair | no | no write (rc 1) | would be `2` |

- MSPI sequence, new field `v` (1 or 2), current `cur`: `cur == v` → nothing. `cur != 0` → write 0, sleep 100 ms, write `v`. `cur == 0` → write `v`, sleep 50 ms. Mode 0: if `cur != 0` write 0, sleep 100 ms. Each write sends the whole 7-byte frame. C.
- Order is BCM first, MSPI second. Switching passive → auto therefore enables the chip while the passive field is still `1`, for up to the transfer time. Linux must reverse it (§11). C (order), I (hazard).
- Semi-auto / manual: stock never writes the push-button regs (`0x18..0x1a`), so these modes do not power a port by themselves (I). poed only uses modes 0, 3, 4.
- ESWH `0x59976c` has the same masks (`0x16e` → 2, `0x90` → 1, mode 0 → 0) and the same 100 ms / 50 ms sleeps; its CLI asks "Setting Passive power mode might damage unsupported device, are you sure…". C.

## 6. 24 V passive path (MSPI board IO)

Frame layout: [board.md](board.md#mspi-board-io-relation-to-fanmd). 7-byte frames (1,0) = ports 1..24, (1,1) = ports 25..48; byte 0 fan, bytes 1..6 = port fields; port `i` (0..23 in frame) → byte `1 + i/4`, bits `7−2(i&3) : 6−2(i&3)` (mask bytes `3f cf f3 fc` at `0x15a3818`). C.

| Field | Meaning | Ev |
|---|---|---|
| `00` | no power path | C (`0x60620c` mode 0; shadow zeroed at boot) |
| `01` | passive 24 V, spare pairs 4,5 / 7,8 | C (written only for opmodes 4, 7); I (physical) |
| `10` | 802.3 path, BCM59111 on pairs 1,2 / 3,6 | C (written for 1,2,3,5,6,8); I (whether it gates power: M) |
| `11` | never written | C |

Evidence that bytes 1..6 are PoE mode select, not LEDs:
- Only writers: `0x60620c` via `0x5f6c04` (sw.c:680312; sole caller `0x60620c`) and the `.poe_defconfig` loader `0x5f782c` (sw.c:680706). C.
- poed generates `.poe_defconfig` from the per-port PoE config: value 2 for config mode 3, 1 for config mode 2 (`ubntbox` `0x5b098..0x5b0e8`), so the file name and content are PoE config. C (generator), I (config enum ↔ opmode; the port-record enum differs from poed's stage enum and was not fully resolved).
- 100 ms / 50 ms settle sleeps between field changes, and the "may cause damage" warning for passive mode. C.
- Stock fan frame capture (fan.md) has bytes 1..6 = `0xaa` = every port field 2 with every port on default auto. C.
- Real PoE LEDs are frames (0,0)/(0,1) (6 B), written by `0x5f6a40`: value `1` when an 802.3 port has power-good, `2` when the port is in passive mode (any state), else 0 (`0x60615c`, sw.c:690390; values `0x1d901c4/5` = 2/1 on hw class 1). C (code), I (colours).

Rules for Linux (safe default):

- Field `1` is the only value that can put 24 V on spare pairs. Never write it unless the user enables passive mode on that port explicitly. Default for all 48 ports = `0`.
- Enabling sequence for passive: `0x12` field `00` (chip off) → field `0` → 100 ms → field `1` → 50 ms. Do not enable while a PD is detected or link is up (stock warns: "check the link status before running PASSIVE mode"). I.
- Frames must be written with bytes 1..6 = `0x00` at the first transfer after boot (fan-only writes included), and the fan driver must keep the shadow when it updates byte 0. C (rationale).
- Power-on content of the shift registers: unknown (M). First stock write is the fan frame `0x5f7ad8`, preseeded from `.poe_defconfig`.

## 7. Budget and priority

- Number: 500 W − 64 W = 436 W from ubnthal table `+0x64b8` / `+0x64ba`; consumed only by `board_get_poe_budget` (libubnt) and `board_has_poe` (budget ≠ 0). C.
- Host-side enforcement: none found. I. Evidence:
  - monitor sums per-card consumption into `DAT_023ecfd0` (`0x610a50`, sw.c:696224); the only reference is that write.
  - no budget compare in the opmode setter or in cfg apply (§5, §4 step 11); no priority attribute among the setters in the `0x6118a0` dispatch table (`0x23ed508..0x23ed538`).
  - "Configure PoE port priorities / power limit mode" exist only as CLI help strings.
  - `Off (power budget exceeded)` is entry 20 of a generic status table `0x16e78d0` (PD63000-style codes) used by the SDK debug shell, not by UBNT code.
- Per-port limits are chip registers only: ICUT `0x47+5p` and ILIM `0x48+5p` (§3). Class / budget policy inside the chip FW: unknown. The chip counts "power denied" (`0x61+4p`) and stock only displays it. M: provoke with PDs above 436 W.
- Stock default is "everything auto". Overload of the 500 W PSU is therefore handled by the PSU / chip, not by host policy. I.
- Linux: static budget and priority in `pse_core` (as #26) is the only enforcement available; it must be added by us.

## 8. Reset, GPIO, warm reboot

| Item | Fact | Ev |
|---|---|---|
| Reset line | CCA GPIO bit 9 (Linux GPIO 13), shared by all 12 chips | C |
| Live host path | `0x60e420`: `0x5e57c8(0, 13, v)` → RMW CCA `0x18000064`: 1, wait, 0, wait, 1. Wait = `0x5c04dc(0x14)`: argument in µs, clamped to ≥ 10000 → **10 ms** each | C (sw.c:695026, 648127) |
| Unused SDK path | ioctl op 0 `0x1087594`: OUTEN `0x18000068 \|= 0x200`, OUT bit 9 high, `sal_udelay(20)`, low, 20 µs, high. Only place that sets OUTEN bit 9 in the UniFi image | C |
| Polarity | active-low, idle high (final state high; ESWH init sets DOUT bit 9 = 1) | I |
| OUTEN | observed `0x2c0` on OpenWrt after stock (bits 6, 7, 9); source in UniFi image = unused op 0, or boot ROM / U-Boot | C (obs, #12), I |
| After reset | 1 s sleep, then FW load (§4) | C |
| IRQ lines | CCA 0 = alarm (log only), CCA 1 = card 1, CCA 8 = card 0; falling edge; kernel forwards over genl `PSE_FAMILY`. Handler `0x1087680`: read `0x00`; bit 2 → read `0x03`, clear LED bits, read `0x07`; bit 1 only → read `0x03`, set LED bits | C ([kernel-pse.md](kernel-pse.md)) |
| SMBus alert | `i2cdetect` on card-0 bus shows `0x0c` (ARA) | C (#12 comment) |
| Warm reboot | all 12 chips answered after stock → OpenWrt: FW `0x15`, `0x12 = 0x00` (stock had run `swctrl poe set off`); no reset in between | C (live, #24) |
| Cold boot straight into OpenWrt | chip state with no stock before it | M |
| U-Boot | no I2C, no GPIO 9, no MSPI | C ([uboot.md](uboot.md)) |

## 9. Temperature

- Reg `0x2c` per chip: `T = 367 − 2·raw` °C, valid only when raw > `0x53` (else invalid, stored −9999). New value accepted only within ±49 of the previous. C (`0x610a50`, `0x6118a0`, ESWH `0x598df0`).
- Unit °C and raw→physical mapping checked against 65–85 °C live readings only. I.
- Stock thresholds: 16-row table, column 3 = alert/max: PoE-01..12 = 75, 75, 75, 80, 85, 85, 80, 75, 75, 75, 70, 65 °C ([board.md](board.md#temperature-sensors), `0x1d8ffec`). Use of columns 0..2: fan level steps (I, ESWH `fm_*`).
- No PoE action on over-temperature in host code: the monitor stores the value only. Thermal protection is chip-side: INT bits TCUT/TSTA (mask `0x66`). I.

## 10. Open items

- M1: MSPI field `10` required for 802.3 power? Test with BCM auto and field `0` on a dummy-load PD, then field `2`. Needs owner (#24 checklist).
- M2: Read `0x44`, `0x46..0x48`, `0x01`, `0x13`, `0x14` on a stock-configured chip (read-only) to settle ICUT/ILIM/mask values.
- M3: Cold-boot chip state; reset polarity and OUTEN source (scope or read CCA `0x18000064/68` right after POR).
- M4: FW load time on the real bus (sets the timeout; #26).
- M5: Chip-side behaviour on over-budget (power-denied counter).
- M6: Physical meaning of field `01` (which pins carry 24 V, enable mapping): measure on a dummy load before ever enabling.
- R1: poed port-record mode enum vs stage enum, and how poe / poeplus / poeplusplus set HPMD / ICUT / ILIM (usmdb `FUN_002c…` setters not fully traced).
- R2: ioctl op 4 `0x1088458`.

## 11. Safe init sequence for Linux

Confidence per step: C = stock does exactly this, I = inferred, M = unverified.

1. **MSPI first.** Write frames (1,0) and (1,1): byte 0 = fan level (fan.md), bytes 1..6 = `0x00`. Write frames (0,0) and (0,1) = zeros. Keep a shadow; every later write sends the shadow. (C: stock zeroes shadows before resetting the chips; I: that this prevents 24 V.)
2. **Probe.** Per card: select mux (`0x10` / `0x20`), per chip `0x28..0x2d` read `0x43`; require `(v & 0xe0) == 0xc0`. Treat NAK as "no FW" (#24 does this). (C)
3. **FW running?** Read `0x75`: `0xaa` and `0x41 == 0x15` on all 12 → skip to 5. (C for `0x75`, I for adding `0x41`.)
4. **Else reset + load.** Never reset a chip whose FW is fine (warm-reboot persistence is relied on).
   - CCA bit 9: set OUTEN, drive high, low ≥ 10 ms, high. (C timing, I polarity)
   - wait 1 s. (C)
   - load per card (not broadcast `0x30`): 512 × `[0x80, off_hi, off_lo, 32 B]` to reg `0x70`, then `0x70 ← 0x40`. (C format; per-card instead of broadcast: I)
   - poll `0x75 == 0xaa` per chip; timeout = measured load time × 1.25, logged on expiry (M value). Stock fixed-sleeps 2 s. Retry whole sequence ≤ 3 times. (C)
5. **PoE off immediately.** Every chip: `0x12 ← 0x00`, read back; also `0x14 ← 0x00` is not needed while `0x12 = 0`. Do this before any other config. (C: verified on hardware, #24.) FW default auto mode may power PDs between FW start and this write (I): keep this step in the same function as step 4.
6. **Config, ports still off.** Per chip: `0x01 ← 0x66`, `0x13 ← 0x0f`, `0x14 ← 0xff`, `0x44 ← 0x0f`, per channel `0x46+5c ← 0x01`, ICUT, ILIM. (C values for all but ICUT/ILIM.) ICUT/ILIM: use the stock runtime formula (`0xe2` for 638 mA, ILIM `0xc0`) until M2 settles it. (I)
7. **Enable one port (802.3), staggered ≥ 200 ms apart (poed default).** In this order: confirm MSPI field is `0` → write field `2` → sleep 50 ms → `0x12` channel field `11`. Stock writes the chip first and the MSPI field second; the reverse order here avoids a chip-on / path-off window. (C for sleeps and values; I for the reversed order; M1 decides whether the field write is needed at all.)
8. **Disable one port.** `0x12` channel field `00` → MSPI field `0` → sleep 100 ms. (C: the chip-then-MSPI order matches stock.)
9. **Passive 24 V, opt-in only.** Precondition: `0x12` field `00` and MSPI field `0`. Then field `1` after 100 ms, wait 50 ms. Per-port, explicit enable (DT property + sysfs / ethtool vendor knob), default off, refuse if `0x0c+` detect shows a valid signature or link is up. (C values and timing, I meaning, M6.)
10. **Monitor.** Every 2 s read `0x10`, `0x0c+`, `0x30+4c`, `0x32+4c` (LSB first), `0x2c`. Re-check `0x75` every ~20 s; on loss, repeat step 4 and re-apply config. (C: stock does the same.) IRQ: read `0x00`, then `0x03` / `0x07` to clear.
11. **Remove / shutdown.** Leave chips and MSPI fields as they are; do not reset. (I: matches the warm-reboot behaviour verified in #24.)

## 12. Corrections to board.md

- "Init sequence (table `0x174659c`)" is not executed (§4). Table starts at `0x174659e`.
- ioctl op numbers: 0 reset, 1 FW download, 2 init regs, 3 verify, 4 `0x1088458`, 5 probe (§4).
- Reset pulse in use is 10 ms (`0x5c04dc` clamps µs to ≥ 10000), not 20 µs (§8). OUTEN bit 9 is set only by the unused op 0.
- "Port LED frames, base = 1" (bytes 1..6 of the 7-byte frames) are PoE mode select fields (§6). Real PoE LEDs are frames (0,0)/(0,1).
- `0x10`: bits 3:0 power-enable, bits 7:4 power-good (§3).
- Port-field mask bytes in memory order: `3f cf f3 fc` (`0x15a3818`).
- PoE init: FW check is `0x75 == 0xaa` on all chips, not a rev compare; waits 1 s after reset and 2 s after FW start (§4).
- Budget: no host enforcement found (§7).
