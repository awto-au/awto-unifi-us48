# awto-unifi-us48

Native mainline Linux port + reverse engineering for the **Ubiquiti UniFi US-48-500W** (board `0xeb62`, platform `US48P500`):
Ports: 48 RJ45 + 4 cages labelled SFP+ 1, SFP+ 2, SFP 1, SFP 2; chip/port map in [docs/port-map.md](docs/port-map.md), current cabling in `scripts/cabling.json`.
Broadcom Hurricane 2 BCM53344 (Cortex-A9 + switch, unit 0) with a BCM53346 on PCIe (unit 1, its own A9 = a2), 48× GbE PoE, 2× SFP+, 2× SFP.

## Status

| Area | State |
|---|---|
| Stock firmware | 7.5.15.17146 in `kernel1` (never written); full flash backups in `private/` |
| Boot | uboot-1 (stock) auto-chains awto-uboot (uboot-2) from `kernel0`, which boots mainline Linux; ssh about 14 s after power-on ([docs/autochain.md](docs/autochain.md)) |
| Mainline Linux | v7.4-rc1 + the `mainline/` series, normal and debug profiles ([docs/mainline.md](docs/mainline.md), #15) |
| Unit 0 | `us48_cmic`: ge1 management port, full cold init on every load |
| Unit 1 | `us48_cmic_xe` over PCIe: SFP+ 1 10G optic (#78, link fault fixed #109), DAC parked (#105), 10G-T module (#110) |
| 48 copper ports | bring-up in progress on branch `wip/copper-phy-20261117` (#4, #57, #58) |
| a2 (unit 1 A9) | Linux at 1600 MHz via `us48_a2` remoteproc, virtual NIC to a1 (#85, [docs/a2-linux.md](docs/a2-linux.md)) |
| Fans / thermal | presets 50/70/90/100 %, quiet rule by default, `us48-fan` + `us48-pvtmon` ([docs/fan.md](docs/fan.md)) |
| Hardware facts | `hwdb/` TOML, generated headers/tables ([docs/hwdb.md](docs/hwdb.md)) |
| Serial console | front RJ45 (RS-232), adapter on Selwyn; `scripts/dev.py console` |
| OpenWrt | removed 2026-11-18; last state in tag `openwrt-parked` (#74) |

Test unit (not the house uplink): reboot and flash `kernel0` freely; PoE is off at boot.

## Layout

```
mainline/   patch series + DTS + rootfs for upstream Linux (build_mainline.py, BASE v7.4-rc1)
kernel/     out-of-tree drivers (us48-cmic, us48-a2, us48-pci, us48-fan, us48-pvtmon, ...)
common/     bcm-cmic core shared by Linux and awto-uboot
uboot-port/ awto-uboot (uboot-2) board, drivers, patches (build_uboot.py)
a2fw/       bare-metal a2 probes and boot stub
hwdb/       hardware facts (TOML) -> hwdb/gen
scripts/    dev.py (entry point: `describe`), build_mainline.py, build_uboot.py, backup.py, RE tools
docs/       hardware, mainline, autochain, fan, board, uboot, a2, hwdb, driver-internals, ... (.md)
regdb/      HR2 register/memory tables generated from stock switchdrvr (scripts/hr2_regdb.py, gen_hr2_regs.py)
tmp/        build trees, logs — gitignored, disposable
private/    backups, stock firmware, live captures — gitignored, NOT disposable
```

## Build

```bash
# one-time: gitignored link to the machine-shared Linux store under /mnt/1tnvme/git-mirrors (#45)
ln -s /mnt/1tnvme/git-mirrors/linux tmp/linux-store
python3 scripts/build_mainline.py         # -> tmp/mainline/us48-mainline-normal.bin
python3 scripts/build_uboot.py            # -> tmp/uboot/us48-uboot-chain.bin
```

## Safety

- Test images go in backup slot `kernel0` (`dev.py flash-kernel0`, read-back crc) or RAM (`dev.py u2-boot --from tftp --image ...`).
- Never write `kernel1` (stock), the bootsel record (`0x1ee0000`), `u-boot`, `EEPROM`; never `saveenv`; u-boot-env only the docs/autochain.md image.
- Fans: presets only, no tach, no off. Level 0 (50 %) by default; `us48_fan` (Linux) and awto-uboot go to 100 % at LM75 >= 80 C or die >= 110 C ([docs/fan.md](docs/fan.md) quiet rule) - watch temperature on every boot test.
- Do not connect two forwarding uplinks to the same L2 segment without STP.
