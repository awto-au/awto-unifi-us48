# awto-unifi-us48

OpenWrt port + reverse engineering for the **Ubiquiti UniFi US-48-500W** (board `0xeb62`, platform `US48P500`):
Broadcom Hurricane 2 BCM53344 (Cortex-A9 + switch) with a BCM53346 on PCIe, 48× GbE PoE, 2× SFP+, 2× SFP.

## Status

| Area | State |
|---|---|
| Stock firmware | 7.5.15.17146 installed (official updater), full 32 MiB flash backups verified |
| OpenWrt | `bcm53xx/hr2` subtarget + device; initramfs builds (Linux 6.18, legacy uImage), **not yet booted** |
| Device tree | none from Ubiquiti (stock kernel is machine-ID); ours derives from upstream Switch 8 DTS + reversed stock facts |
| Serial console | not yet attached — required before any boot test |
| Switch fabric (48 ports) | no Linux driver; see [docs/openwrt-port.md](docs/openwrt-port.md) |
| Fan control | stock: discrete presets only (50/70/90/100 %); see [docs/fan.md](docs/fan.md) |
| PoE / sensors / GPIO | stock reversed statically: 12× BCM59111 behind PCA9548, LM75 ×4; see [docs/board.md](docs/board.md) |

## Layout

```
openwrt/   patches against OpenWrt main (pinned in scripts/build_openwrt.py)
scripts/   build_openwrt.py, backup.py, verify_firmware.py
docs/      hardware.md, openwrt-port.md, fan.md, board.md, sources.md, REGISTERS.md
regdb/     HR2 register/memory tables generated from stock switchdrvr (scripts/hr2_regdb.py, gen_hr2_regs.py)
tmp/       build tree, logs — gitignored, disposable
private/   backups, stock firmware, live captures — gitignored, NOT disposable
```

## Build

```bash
python3 scripts/build_openwrt.py          # -> tmp/tftp/us48-initramfs.bin
```

## Safety

- RAM boot only (`tftpboot` + `bootm`). No `saveenv`, no flash writes, no `sf erase` until a backup restore is proven.
- Stock fans are firmware-controlled; OpenWrt has **no fan or thermal control yet** — watch temperature on every boot test.
- Do not connect two forwarding uplinks to the same L2 segment without STP.
