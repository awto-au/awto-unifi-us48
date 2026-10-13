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
  - `pcie0` enabled: BCM53346 at `01:00.0`/`01:00.1`. Local BCM53344 CMIC is AXI `0x48000000`, not PCIe (stock `/proc/iomem`). `pcie1` left disabled: no evidence of use.
  - `i2c@48000000` = CMIC SMBus (`brcm,iproc-i2c`, polled, 100 kHz) → PCA9548 `0x70`. DT `i2c0`/`i2c1` stay disabled (unused by stock). [board.md](board.md#i2c-master-issue-7).
- Busybox `i2cdetect`/`i2cget`/`i2cset`/`i2cdump`/`devmem` + `kmod-i2c-core` (i2c-dev) for milestone 2; no packages feed needed.
- Image: `zImage + appended DTB` in a legacy uImage (`uImage none`), load/entry `0x00018000` (as stock;
  `0x8000` collides with U-Boot's ATAG list — [uboot.md](uboot.md)).
  - `ARM_ATAG_DTB_COMPAT` off → DT `bootargs` win over U-Boot's `mem=`/`mtdparts=`.
  - initramfs only; no sysupgrade image until a flash layout is chosen.
- Kernel 6.18 (OpenWrt bcm53xx default).

## First boot results (2026-10-13, image in kernel0, `scripts/dev.py flash-kernel0`)

- Kernel 6.18.55 boots: earlycon on UART1 @ 0x18000400, DT model, 2×128 MiB memory, L2C-310, arm_global_timer 200 MHz. C.
- `timer@34000` / `timer@35000` (SP804): "clock not found" → not registered (harmless; global timer used). C.
- **PCIe panic**: `iproc_pcie_config_read32` reads APB_ERR_EN (`0xf40`) while scanning behind root port `14e4:8344` → external abort. HR2 PAXB lacks that register; `brcm,iproc-pcie` maps to the PAXB layout that has it. Workaround: `pcie0` disabled. Fix: kernel patch (HR2 compatible without APB_ERR_EN). C.
- **No reset handler**: panic → "Reboot failed -- System halted"; needs power-cycle. Stock uses `northstar_restart` (kernel-pse.md). I.
- Stock U-Boot → our uImage: load 0x18000, `bootm` CRC OK, ATAGs ignored (DT bootargs win). C.

### 2026-10-14: OpenWrt shell (milestone 1 done)

Image: `pcie0` disabled + SP805/SP804 50 MHz clocks; kernel0 crc `4659639a`. Console capture: `tmp/logs/console/us48-20261014-083748.log`.

- Userspace (`root@OpenWrt:~#`, BusyBox 1.38.0) 59 s after `bootm`. C.
- MTD: all 7 stock partitions, read-only. C.
- I2C: `i2c-2` = CMIC SMBus `48000000`, poll mode ("no irq resource"); `i2cdetect -y -r 2` → **only `0x70`** (PCA9548). `i2c-0`/`-1` = ChipcommonB. C.
- `sp805-wdt 18039000.watchdog: registration successful`; `reboot` → U-Boot banner 5.0 s later. Panic now auto-reboots. C.
  - After an SP805 reset U-Boot reports PCIe `LINKSTA 0x1001`, "port 0 is not active" (cold boot: `0x9012`, active). Stock kernel then retrains and enumerates `01:00.0/.1` normally → harmless. C.
- UART: 8250 reports `base_baud = 1562500` (25 MHz from dtsi `osc`); real UART clock 50 MHz → line ran at ~223 kbaud after the driver took over. Fix: `&uart1 { clocks = <&apb_clk>; }`. C (read the shell at 223214 baud).

## Boot test (milestone 1) — procedure

Full procedure + evidence: [uboot.md](uboot.md). Summary:

1. Serial console attached and stock boot captured end-to-end. Don't hold the reset button at power-on
   (bootcmd's `ureset_button` erases `cfg` + `u-boot-env` after ~3 s).
2. Any key within 3 s stops autoboot. Read-only: `version`, `printenv`, `coninfo`. No `saveenv`, no `reset` (NULL handler), no `sf` writes.
3. **No Ethernet in U-Boot** (`tftpboot` fails) → `loady 0x01000000`, ymodem-send `us48-initramfs.bin` (≈ 8–10 min at 115200).
4. `bootm 0x01000000`: header + data CRC only, no RSA (RSA lives in the uncalled ubnt-app path). U-Boot arms the A9
   watchdog (30 s) and stops it inside `bootm`. Handoff: ATAGs, machine ID 0xBB8, caches/MMU off.
5. Fans: U-Boot doesn't touch the board-IO chains — power-on default speed unknown; watch temperature.

## Milestones

| # | Goal | Blocker |
|---|---|---|
| 1 | initramfs to shell on serial | **done 2026-10-14** (kernel0 boot) |
| 2 | QSPI flash read (mtd), `i2cdetect -r` on the CMIC bus shows `0x70`, PCIe enumerates `14e4:8346` | mtd + I2C done 2026-10-14; PCIe needs pcie-iproc patch (APB_ERR_EN) |
| 3 | management Ethernet | stock has **no** Ethernet MAC in use: `eth0` is a `tun` device fed by switchdrvr via CMIC packet DMA ([hardware.md](hardware.md#stock-control-path)). Needs switch init (ports, SerDes, PHYs, CPU port) + CMIC DMA driver = milestone 4 work |
| 4 | switch fabric: 48 ports, VLAN, STP | no upstream Linux driver for XGS CMIC; OpenMDK/OpenBCM are Broadcom-licensed, not GPL |
| 5 | fans / PoE / sensors | MSPI board IO protocol ([fan.md](fan.md)); PoE = BCM59111 + firmware blob, I2C controller mapping ([board.md](board.md)) |
| 6 | flash install + sysupgrade | proven backup restore path; layout using kernel0/kernel1 |

## Prior art

- Upstream Linux has `ARCH_BCM_HR2` (BCM53342/3/4/6) and the Switch 8 DTS; no switch driver.
- OpenWrt forum: [UniFi BCM53343](https://forum.openwrt.org/t/add-support-to-unifi-switch-bcm53343/92097) —
  ES-8-150 experimental image (post 44), US-16-150 boots without Ethernet (post 73);
  [US-8-60W](https://forum.openwrt.org/t/openwrt-support-for-unifi-switch-8-us-8-60w/112888/7) boots without networking.
- None of these has working switch ports.
