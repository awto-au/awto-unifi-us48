# Sources

Third-party source is referenced, not vendored. Local copies (with sha256 manifests) stay in the private workspace.

| Source | Version | Use | License |
|---|---|---|---|
| [Linux](https://github.com/torvalds/linux) `arch/arm/boot/dts/broadcom/bcm-hr2.dtsi`, `bcm53340-ubnt-unifi-switch8.dts` | master | DTS base | BSD / GPL |
| [OpenWrt](https://github.com/openwrt/openwrt) | `0ad1348fb2a7f49c9c5941d4d8c98f88b6974e8d` | build tree | GPL-2.0 |
| [Broadcom OpenMDK](https://github.com/Broadcom/OpenMDK) | 2.10.9 @ `db9c678696800d1ebb8d331cb462acde85c31ffb` | `cdk/PKG/chip/bcm53344`, `bcm53346`, `bcm56150_a0` (shared HR2 regs), BMD init/VLAN/STP/CPU RX-TX, PHY lib | Broadcom Switch APIs license |
| [Broadcom OpenBCM](https://github.com/Broadcom-Network-Switching-Software/OpenBCM) | SDK 6.5.16 @ `6d2330ce1b4fc49d681cffcf09af408661be4c62` | `devids.h` (`0x8344`, `0x8346`), `src/soc/esw/hurricane2.c`, `linux-kernel-bde.c`, `src/soc/common/mspi.c` | Broadcom; `gpl-module`, `dcb-gpl-modules` are GPLv2 |
| [D-Link DGS-1210/ME B1 GPL](https://dlink-gpl.s3.amazonaws.com/GPL1900051/DGS-1210-ME-B1-Series_GPLSourceCode.tar.gz) | v7.0.3.0 (2019-05-14) | Linux 3.6.5 iProc/HR2 platform (`CONFIG_MACH_HR2`, iProc GPIO/I2C/PCIe/QSPI/GMAC) | GPL |

- D-Link archive: 226355704 bytes, sha256 `fc4b0316311f4b0b639e980912bb40b49f1bb592047cc24463faca25650dc65e` (local; no publisher hash), 62663 entries.
- Ubiquiti GPL source for US48P500 6.3.13 / 7.5.15: not found; request from Ubiquiti.
- GitHub code search finds none of `usmDbBoxsFanOverrideSet`, `"swctrl fan"`, `ubnt_hal_sys`, `BCM53344_DEVICE_ID` — index gaps, not absence (OpenBCM has the last one).

## Related projects

- [heXeo/ubnt-fan-speed](https://github.com/heXeo/ubnt-fan-speed) @ `2f225abdde847924ef0875e92fb35a3a229ee44d` — UDM Pro hwmon fan daemon; excludes board `0xeb62`, needs hwmon paths absent here. Not applicable.
- [TidalPaladin/unifi-us48-repair](https://github.com/TidalPaladin/unifi-us48-repair) — US-48 (non-PoE, 2 fans, 2× BCM53344) board photos. Different board.
