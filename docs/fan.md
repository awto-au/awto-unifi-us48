# Fan control (stock firmware 7.5.15)

Reversed 2026-10-12 against `/bin/switchdrvr` sha256 `d76ef8646d7dd1c7109666f41499b46fcf52cd1a7d86e83873567f6bde8593c7` (matched live).

## Result

- Board fan profile is **type 0 = discrete presets**; arbitrary duty is rejected by firmware design.
- Presets (firmware labels, not measured PWM):

  | level | reported duty |
  |---|---|
  | 0 | 50 % |
  | 1 | 70 % (auto default observed) |
  | 2 | 90 % |
  | 3 | 100 % |

- `level 0` (50 %) applied and held: 5 samples over 66 s, 64 °C, all sensors Normal. Not a thermal certification.
- Fan-off / 10 % / 30 %: **not achievable** through the stock path. Lower needs the physical controller + wiring identified.

## Hidden commands

```
swctrl fan set duty  <-1..100>   # -1 = auto; >=0 rejected on this board (profile type 0)
swctrl fan set level <-1..3>
swctrl fan diagnostic            # state-changing, 300 s rate gate — NOT a status call
swctrl env show                  # status
```

## Authorization gate

- Setter rejects (ret 1) unless `/var/run/.swctrl.fan.force` contains the lowercase base MAC (`%02x:…`).
- Procedure used: refuse if marker exists or is a symlink; `umask 077`; noclobber-create; remove only our marker on exit.
- With marker: `duty -1` (auto) succeeds, `level N` succeeds, `duty N` fails.
- Setting is RAM-only; reverts on reboot.

## Call chain (7.5.15 addresses)

| | Address |
|---|---|
| `libubnt` `ubnt_diag_fan_control` | `0x8a128` |
| `libubnt` `rpcclt_usmDbBoxsFanOverrideSet` | `0xc7f50` (dup in `librpcclt.so` `0xa8450`) |
| RPC opcode | `0xd76` (6.3.13 was `0xcfc`) |
| RPC args | `r0=0` unit, `r1=1` flag, `r2` op (0 level, 1 duty, 2 diag), `r3` value |
| `switchdrvr` RPC dispatcher | `0x6af430` |
| USMDB table init / table | `0x6f45e4` / BSS `0x23f3514` (opcode×4; descriptors are **not** linear stride) |
| descriptor / callback | `0x1dc25a0` / `0x6e13c8` |
| backend (MAC gate) | `0x27611c` |
| native setter | `0x26fb90` |
| profile getter / live profile | `0x5fc340` / `*(0x23e8c58) = 0x1d8fe28` (type 0) |
| preset setter | `0x5f6ea8`: 2-bit level → bits 4–5 of board-IO byte, bank via `0x5e9764`, write via `0x5e5d60` |
| board IO transport | `0x125cb08` = `soc_mspi_write8`, `0x125c564` = `soc_mspi_writeread8` (OpenBCM `src/soc/common/mspi.c`) |
| percentage setter (unused here) | `0x5f7274`: hw class 4/9 only, ADT7475 over I2C |
| duty report table | `0x15a381c` |

- Broadcom-derived: SDK/BDE, iProc platform, FastPath core, MSPI.
- Ubiquiti-specific: `ubntbox`/`libubnt`, MAC gate, board bank sequencing, fan profile.

## OpenWrt implication

- Fans are bits 5:4 of byte 0 of a 7-byte write-only MSPI frame; one frame per switch chip (2 fans each, I). Full protocol: [board.md](board.md) MSPI section.
- Write sequence for group g (0/1): set GP0 of unit 0 = 1, GP0 of unit 1 = g (select pins A=1, B=g), MSPI mode 3 (CPOL 1, CPHA 1), send 7 bytes: byte 0 = `level<<4`, bytes 1–6 = port LED fields (must be re-sent from a shadow, not zeroed).
- The frame also carries the front-port link LEDs, so a fan write needs the current LED bytes.
- Stock default after switchdrvr start: level 1 (70 %). Power-on state before that, and whether hardware falls back to full speed without a driver: unknown.
- Needs CMIC register access from Linux (GP pins `CMIC_GP_DATA_OUT`) plus the iProc MSPI; never guess writes on the live board — capture stock traffic first.
