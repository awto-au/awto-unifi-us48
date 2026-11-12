# awto-unifi-us48

OpenWrt port + reverse engineering for the **Ubiquiti UniFi US-48-500W** (board `0xeb62`, platform `US48P500`):
Broadcom Hurricane 2 BCM53344 (Cortex-A9 + switch) with a BCM53346 on PCIe, 48× GbE PoE, 2× SFP+, 2× SFP.

## Status

| Area | State |
|---|---|
| Stock firmware | 7.5.15.17146 installed (official updater), full 32 MiB flash backups verified |
| OpenWrt | `bcm53xx/hr2` subtarget + device; initramfs (Linux 6.18, legacy uImage, debug profile) boots to a shell from kernel0; PCIe enumerates the BCM53346; temperatures via LM75 hwmon; PoE off + fans 50 % at boot (`us48-poe-off`, `us48-fand`) |
| Mainline | 7-patch series on v7.4-rc1, builds, never booted ([docs/mainline.md](docs/mainline.md), #15) |
| Device tree | none from Ubiquiti (stock kernel is machine-ID); ours derives from upstream Switch 8 DTS + reversed stock facts |
| Serial console | front RJ45 (RS-232), adapter on `lizard` (`US48_HOST=lizard`); `scripts/dev.py console` |
| Switch fabric (48 ports) | no Linux driver; MIIM/PHY bring-up started (#4); see [docs/openwrt-port.md](docs/openwrt-port.md) |
| Fan control | stock: discrete presets only (50/70/90/100 %); OpenWrt: same preset frame over MSPI, no tach, no off; see [docs/fan.md](docs/fan.md) |
| PoE / sensors / GPIO | stock reversed statically: 12x BCM59111 behind PCA9548, LM75 x4; see [docs/board.md](docs/board.md) |

Test unit, but in service: powers 5 PoE cameras and uplinks the house LAN on port 50 (10G DAC). PoE off at boot cuts the cameras.

## Layout

```
openwrt/   patch against OpenWrt main (pinned in scripts/build_openwrt.py) + files/ overlay (us48-fand, us48-poe-off)
mainline/  patch series for upstream Linux (build_mainline.py, BASE v7.4-rc1)
scripts/   dev.py (entry point: `describe`), build_openwrt.py, build_mainline.py, backup.py, RE tools
docs/      hardware, openwrt-port, mainline, fan, board, edgeswitch, kernel-pse, uboot, REGISTERS, prior-art, sources (.md)
regdb/     HR2 register/memory tables generated from stock switchdrvr (scripts/hr2_regdb.py, gen_hr2_regs.py)
tmp/       build tree, logs — gitignored, disposable
private/   backups, stock firmware, live captures — gitignored, NOT disposable
```

## Build

```bash
# one-time: gitignored links to the machine-shared stores under /mnt/1tnvme/git-mirrors (#45)
ln -s /mnt/1tnvme/git-mirrors/linux tmp/linux-store
ln -s /mnt/1tnvme/git-mirrors/openwrt-dl tmp/openwrt-dl
ln -s /mnt/1tnvme/git-mirrors/ccache/openwrt tmp/ccache-openwrt
python3 scripts/build_openwrt.py          # -> tmp/tftp/us48-initramfs.bin
python3 scripts/build_mainline.py         # -> tmp/mainline/us48-mainline.bin
```

## Safety

- Test images go in backup slot `kernel0` (`dev.py flash-net` / `flash-kernel0` / `boot-flash`) or RAM (`dev.py boot-ram`).
- Never write `kernel1` (stock), the bootsel record (`0x1ee0000`), `u-boot`, `EEPROM`; never `saveenv`.
- Fans: OpenWrt has presets only (50 % at boot, 100 % above 80 C via `us48-fand`) - watch temperature on every boot test.
- Do not connect two forwarding uplinks to the same L2 segment without STP.
