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

## Live MSPI capture (stock 7.5.15, 2026-10-14, `scripts/mspi_capture.py`, read-only `/sys/bcm/mem`)

CMIC unit 0 MSPI @ AXI `0x48001500`. Captured after `swctrl fan set level N`; diff level 0 → level 1:

| Reg | level 0 (last xfer = 6-byte chain) | level 1 (last xfer = fan chain) |
|---|---|---|
| `+0x14` / `+0x24` (ENDQP, = len−1) | 5 | 6 |
| TXRAM `0x1540` + 8·i (stride 8) | `00 00 00 00 00 00` (+ stale `aa` @ i=6) | `10 aa aa aa aa aa aa` |
| CDRAM `0x1640` + 4·i | `80`×5, `00` | `80`×6, … |

- Fan frame = 7 bytes: byte 0 = `level << 4` (C), bytes 1–6 = `0xaa` = every 2-bit port field = 2 (I: PoE mode select 802.3, see PoE report — not LEDs).
- 6-byte all-zero frame = PoE card chain with PoE off (I).
- `0x48002000–0x200c` (CMIC GP block: DATA_OUT `+4`, OUT_EN `+8` as written by `us48-fand`) unchanged between levels: select pins don't depend on the level.
- SPCR0 `0x1500` = `0x8d`, `0x1504` = `0x83` (mode/baud, decode vs OpenBCM `mspi.c`).

## OpenWrt: `us48-fand` (openwrt/files, #12, #19)

- Fans are bits 5:4 of byte 0 of a 7-byte write-only MSPI frame; one frame per switch chip (2 fans each, I). Full protocol: [board.md](board.md) MSPI section.
- Write sequence for group g (0/1): GP0 of unit 0 = 1 (A), GP0 of unit 1 = g (B, via PCIe BAR `0x0a002000` of `01:00.0` after enabling it), both set as outputs; MSPI mode 3 (CPOL 1, CPHA 1); send 7 bytes: byte 0 = `level<<4`, bytes 1-6 = 0.
- Policy (Dan 2026-11-15), same in awto-uboot (`fan.c`, one-shot at boot, `fan <0-3>` command) and mainline Linux (`us48-fand`, staged into the mainline initramfs from `openwrt/files` by `build_mainline.py`):
  - 50 % (byte 0 `0x00`) while all 4 LM75 (`0x48..0x4b`, mux `0x70` ch 6) < 80 C; 100 % (`0x30`) at >= 80 C until all <= 50 C.
  - Die PVTMON (both units, S-channel TOP `0x02050800`, T = 418 - 0.5556 x code): >= 100 C forces 100 %.
  - Any sensor unreadable: 100 %.
  - awto-uboot reads the LM75 over the CMIC SMBus (`0x48000000`, mux select + pointer write only) and has no loop: hot or unreadable at boot stays 100 % until Linux fand or `fan 0`.
  - Replacement = the Linux drivers below (#19); fand stays in the image, disabled while the driver is present.
- Verified by ear on all 4 fans (#12): `0x30` louder than `0x00`; byte-0 bits 7:6 and 3:0 change nothing (sweep `scripts/fan_sweep.py`).
- No tach (stock reports 0 RPM). No fan IC on any PCA9548 channel (no `0x2e` / ADT7475), so fan control is not I2C. Fan-off needs the drive circuit identified (board photos) or a power-side switch not yet found; candidate GPIOs probed with `scripts/fan_gpio_probe.py` (#12).
- Stock default after switchdrvr start: level 1 (70 %). Power-on state before that, and whether hardware falls back to full speed without a driver: unknown.

## Linux drivers (#19, mainline only)

Policy is in the DT thermal zones, no daemon. Modules are loaded by `us48-thermal` (init S14), after `us48_pci` (S04).

| Piece | Where | Notes |
|---|---|---|
| LM75 x4 | upstream `lm75` + `#thermal-sensor-cells` (`mainline/0012`) | zones `lm75-48/49/4a/4b` |
| Die temperature | `kernel/us48-pvtmon` | unit 0: DT platform device, CMC1 S-channel window (CMC0 is `us48_cmic`); unit 1: aux `us48_pci.pvtmon` through the locked `us48_pci_schan`. Formula + regs: hwdb `top_pvtmon`, `PVTMON_CALC_*`. Any S-channel error, code 0 or a result outside -40..150 C = -EIO |
| MSPI | `kernel/us48-cmic-mspi` | write-only SPI controller, polled |
| GP pins | `kernel/us48-cmic-gpio` | 4-pin gpio-chip per unit (unit 0 platform MMIO, unit 1 aux over BAR2, link-checked) |
| Fan | `kernel/us48-fan` | SPI client: hwmon `pwm1` (127/179/230/255 = 50/70/90/100 %) + `pwm1_enable` (1 manual, 2 thermal), cooling device `us48-fan` states 0-3 |
| DT | `mainline/0013` | `thermal-zones`: LM75 trip 80 C active, hysteresis 30 C (back at 50 C); die trip 100 C, hysteresis 10 C; maps `<&fan 3 3>`; step_wise |

MSPI verdict (spi-bcm-qspi vs CMIC MSPI at `0x48001500`):
- Registers match: SPCR0/1, NEWQP/ENDQP, SPCR2, STATUS, CPTQP, TXRAM `+0x40` stride 8, RXRAM `+0xc0`, CDRAM `+0x140` (hwdb `mspi` = `cmic_mspi`; `drivers/spi/spi-bcm-qspi.c` offsets and bit fields identical, CDRAM PCS/CONT/BITSE/DT).
- `+0x180` is iProc `MSPI_WRITE_LOCK`, CMIC `CMICM_BSPI_MAST_N_BOOT` (bus claim). spi-bcm-qspi writes it only with a BSPI resource; stock SDK and fand set it to 1.
- Not usable upstream as is: `bcm_qspi_probe` fails with "no IRQs registered" (no interrupt is known for the CMIC MSPI; the CMIC IRQ is open, board.md) and has no polled mode; it also programs SPCR3/MSPI_REV (not in the CMIC regdb) and SPCR2 `0xe0` (stock capture: `0x40`).
- Result: minimal driver `us48-cmic-mspi` (write-only, polled, 1-16 byte chains, SPBR from the DT clock, PCS 0, CONT on all but the last slot, DTL = RDSCLK = 1 as fand).

Fail-safe (the thermal core alone does not: an unreadable zone gives `THERMAL_TEMP_INVALID` and the governor is skipped):
- `us48-fan` re-reads every zone in `us48,watch-zones` each 2 s; a missing or unreadable zone sets level 3.
- Level 3 on probe until all zones read OK, on unbind, and after any frame error (retried every 2 s).
- Rmmod order: `us48_fan` before `us48_pvtmon` (the watcher holds no zone reference).

Switching:
- Drivers (default): `us48-thermal` loads the modules; `us48-fand` init exits when `/sys/class/thermal/cooling_device*/type` has `us48-fan`.
- fand: `touch /etc/us48-fand.force`, reboot (or `/etc/init.d/us48-thermal stop; /etc/init.d/us48-fand restart`).
- Die sensors also appear as hwmon devices (`temp1_input`): fand's LM75 loop would count them, so never run both.
