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
- Debug profile (`DEBUG_APPLETS` / `DEBUG_PACKAGES` in `scripts/build_openwrt.py`): busybox i2c/devmem/etc. + strace, gdbserver, perf, tcpdump, ethtool, i2c-tools, `kmod-i2c-core`; kernel debug set in `hr2/config-6.18`. Initramfs must stay < kernel0 (15 MiB).
- Overlay `openwrt/files/`: `us48-poe-off` (S13, BCM59111 op-mode reg `0x12` = 0 on all 12 chips, readback logged, #24) and `us48-fand` (S15, fans 50 % until LM75 max >= 80 C, full until <= 50 C; bench-only, #12 #19).
- Image: `zImage + appended DTB` in a legacy uImage (`uImage none`), load/entry `0x00018000` (as stock;
  `0x8000` collides with U-Boot's ATAG list — [uboot.md](uboot.md)).
  - `ARM_ATAG_DTB_COMPAT` off → DT `bootargs` win over U-Boot's `mem=`/`mtdparts=`.
  - initramfs only; no sysupgrade image until a flash layout is chosen.
- Kernel 6.18 (OpenWrt bcm53xx default).

## Build tree

- `tmp/openwrt-build`: shallow clone of OpenWrt main at `OWRT_COMMIT` (`scripts/build_openwrt.py`).
- Downloads + ccache are machine-shared (#45): `.config` gets `CONFIG_DEVEL=y`, `CONFIG_DOWNLOAD_FOLDER`, `CONFIG_CCACHE=y`, `CONFIG_CCACHE_DIR`.
  - Paths: gitignored symlinks `tmp/openwrt-dl`, `tmp/ccache-openwrt` (or `$US48_OPENWRT_DL` / `$US48_CCACHE_DIR`, `--dl-dir` / `--ccache-dir`); build aborts if missing.
  - `DEVEL=y` is required: without it `make defconfig` drops both options.
  - Other buildroots share the dirs: only add files, never delete.

## Bring-up results (kernel0 boots, 2026-10-13/14; evidence in #2, #11, #16)

- Kernel 6.18.55 boots: earlycon on UART1 @ 0x18000400, DT model, 2x128 MiB memory, L2C-310, arm_global_timer 200 MHz. C.
- Userspace (BusyBox 1.38.0) 59 s after `bootm`. Console capture: `tmp/logs/console/us48-20261014-083748.log`. C.
- Stock U-Boot -> our uImage: load 0x18000, `bootm` CRC OK, ATAGs ignored (DT bootargs win). C.
- MTD: all 7 stock partitions, read-only. C.
- I2C: `i2c-2` = CMIC SMBus `48000000`, poll mode ("no irq resource"); `i2cdetect -y -r 2` -> **only `0x70`** (PCA9548). `i2c-0`/`-1` = ChipcommonB. C.
- UART: 8250 took `base_baud = 1562500` from the dtsi 25 MHz `osc`; real UART clock 50 MHz. Fixed by `&uart1 { clocks = <&apb_clk>; }`. C.
- SP805/SP804 get the 50 MHz APB clock: `sp805-wdt` registers (probe 1.15 s), `reboot` -> U-Boot banner 5.0 s later. C.
  - SP805 armed from U-Boot before `bootm` (`scripts/dev.py`): early panic/hang -> reset ~20 s; procd feeds it. C.
  - Restart = stock DMU reset via `syscon-reboot` (DMU `0x1803f200` bit 1, as `northstar_restart`), outranks the SP805 handler (#11).
- PCIe: patch 950 (skip APB_ERR_EN on `brcm,iproc-hr2-pcie`) + 951 (config-read abort -> all-ones, as pci-imx6, HR2 window only) + 952 (keep PERST# released at shutdown, so U-Boot sees an active link after our reboot): root port `00:00.0`, BCM53346 `01:00.0` + `01:00.1` (`14e4:8346`), BARs assigned, link 5.0 GT/s x1. Without 951 an absent `01:00.2` read aborts -> panic. C.
- Images go in over the LAN: `scripts/dev.py flash-net` (scp, dd to kernel0, sha256; 75 s total), then `boot-flash`.

## Boot test (milestone 1) — procedure

Full procedure + evidence: [uboot.md](uboot.md). Summary:

1. Serial console attached (`scripts/dev.py console`, `US48_HOST=lizard` for the remote adapter) and stock boot captured end-to-end. Don't hold the reset button at power-on
   (bootcmd's `ureset_button` erases `cfg` + `u-boot-env` after ~3 s).
2. Any key within 3 s stops autoboot. Read-only: `version`, `printenv`, `coninfo`. No `saveenv`, no `reset` (NULL handler), no `sf` writes.
3. **No Ethernet in U-Boot** (`tftpboot` fails) → `scripts/dev.py boot-ram` / `flash-kernel0`: `loady 0x01000000` ymodem at 460800 ([uboot.md](uboot.md) §8). Over the LAN instead: `flash-net`.
4. `bootm 0x01000000`: header + data CRC only, no RSA (RSA lives in the uncalled ubnt-app path). U-Boot arms the A9
   watchdog (30 s) and stops it inside `bootm`. Handoff: ATAGs, machine ID 0xBB8, caches/MMU off.
5. Fans: U-Boot doesn't touch the board-IO chains — power-on default speed unknown; `us48-fand` takes over in userspace; watch temperature.

## Milestones

| # | Goal | Blocker |
|---|---|---|
| 1 | initramfs to shell on serial | **done 2026-10-14** (kernel0 boot) |
| 2 | QSPI flash read (mtd), `i2cdetect -r` on the CMIC bus shows `0x70`, PCIe enumerates `14e4:8346` | **done 2026-10-14**: PCIe via patches 950+951 |
| 3 | management Ethernet (not started) | stock has **no** Ethernet MAC in use: `eth0` is a `tun` device fed by switchdrvr via CMIC packet DMA ([hardware.md](hardware.md#stock-control-path)). Needs switch init (ports, SerDes, PHYs, CPU port) + CMIC DMA driver = milestone 4 work |
| 4 | switch fabric: 48 ports, VLAN, STP | in progress (#4): MIIM scan + first PHY link from our code done; S-channel + MAC + CPU DMA next. No upstream Linux driver for XGS CMIC; SDK options: [prior-art.md](prior-art.md) |
| 5 | fans / PoE / sensors | LM75 hwmon, fan presets (`us48-fand`) and PoE-off at boot **done**. Open: temperature-driven fan policy (#19), BCM59111 PSE driver + per-port control (#26, #24), fan-off (drive circuit unknown, #12) |
| 6 | flash install + sysupgrade | proven backup restore path; layout using kernel0/kernel1 |

## Prior art

- Upstream Linux has `ARCH_BCM_HR2` (BCM53342/3/4/6) and the Switch 8 DTS; no switch driver.
- OpenWrt forum: [UniFi BCM53343](https://forum.openwrt.org/t/add-support-to-unifi-switch-bcm53343/92097) —
  ES-8-150 experimental image (post 44), US-16-150 boots without Ethernet (post 73);
  [US-8-60W](https://forum.openwrt.org/t/openwrt-support-for-unifi-switch-8-us-8-60w/112888/7) boots without networking.
- None of these has working switch ports.
- Full survey + reuse ranking: [prior-art.md](prior-art.md).
