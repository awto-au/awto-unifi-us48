# OpenWrt port

## Approach

- New **`bcm53xx/hr2` subtarget** in OpenWrt main (patch: `openwrt/0001-*.patch`).
  - bcm53xx = Northstar Cortex-A9 iProc; its kernel already has `SPI_BCM_QSPI`, `I2C_BCM_IPROC`,
    `GPIO_BCM_XGS_IPROC` (HR2 GPIO), SP804/SP805, `HW_RANDOM_BCM2835` (NSP RNG).
  - `hr2/config-6.18` adds `ARCH_BCM_HR2`, `CLK_BCM_HR2`, `PCIE_IPROC_PLATFORM` + `_MSI`, `DEBUG_BCM_HR2`
    (UART `0x18000400`); drops 5301X / 53573 / NSP.
  - Generic subtarget devices unchanged: `image/Makefile` swaps `TARGET_DEVICES` for `hr2.mk` only when `SUBTARGET=hr2`.
- DTS `target/linux/bcm53xx/dts/bcm53344-ubnt-unifi-us-48-500w.dts`, from upstream `bcm53340-ubnt-unifi-switch8.dts`:
  - `serial0 = &uart1` → `ttyS0` like stock.
  - flash partitions all `read-only` until restore is proven.
  - `pcie0` enabled (BCM53344 CMIC + BCM53346). `pcie1` left disabled: no evidence of use.
- Image: `zImage + appended DTB` in a legacy uImage (`uImage none`), load `0x00008000`.
  - `ARM_ATAG_DTB_COMPAT` off → DT `bootargs` win over U-Boot's `mem=`/`mtdparts=`.
  - initramfs only; no sysupgrade image until a flash layout is chosen.
- Kernel 6.18 (OpenWrt bcm53xx default).

## Boot test (milestone 1) — not yet run

1. Serial console attached and stock boot captured end-to-end.
2. Break into U-Boot: `version`, `printenv`, `bdinfo`, `help` — read only, no `saveenv`.
3. `setenv ipaddr …; setenv serverip …; tftpboot 0x01000000 us48-initramfs.bin; bootm 0x01000000`.
4. Expected unknowns: RSA-signature check in `bootm`, U-Boot-armed watchdog, PCIe link-up.
   - Strings place the RSA check in the embedded Ubiquiti app (`go ${ubntaddr}` → `uwrite`), not U-Boot `bootm`;
     disassembly pending (awto-terminal#74).
   - Board facts come from UniFi U-Boot/kernel; the D-Link HR2 GPL tree is a hint only.
5. Fans: stock U-Boot/CPLD default speed is unknown — watch temperature, power off if fans stop.

## Milestones

| # | Goal | Blocker |
|---|---|---|
| 1 | initramfs to shell on serial | serial header + cable |
| 2 | QSPI flash read (mtd), I2C scan read-only, PCIe enumerates `14e4:8344`/`8346` | — |
| 3 | management Ethernet | HR2 AMAC (`brcm,nsp-amac`) wiring to switch CPU port unknown; may need CMIC packet DMA |
| 4 | switch fabric: 48 ports, VLAN, STP | no upstream Linux driver for XGS CMIC; OpenMDK/OpenBCM are Broadcom-licensed, not GPL |
| 5 | fans / PoE / sensors | MSPI board IO protocol ([fan.md](fan.md)); PoE controller unidentified |
| 6 | flash install + sysupgrade | proven backup restore path; layout using kernel0/kernel1 |

## Prior art

- Upstream Linux has `ARCH_BCM_HR2` (BCM53342/3/4/6) and the Switch 8 DTS; no switch driver.
- OpenWrt forum: [UniFi BCM53343](https://forum.openwrt.org/t/add-support-to-unifi-switch-bcm53343/92097) —
  ES-8-150 experimental image (post 44), US-16-150 boots without Ethernet (post 73);
  [US-8-60W](https://forum.openwrt.org/t/openwrt-support-for-unifi-switch-8-us-8-60w/112888/7) boots without networking.
- None of these has working switch ports.
