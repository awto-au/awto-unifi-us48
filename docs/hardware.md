# US-48-500W hardware

Observed on stock firmware 2026-10-12 (6.3.13 → 7.5.15). Sources: `/proc/ubnthal/system.info`, `/proc/cpuinfo`,
boot log, `/proc/bus/pci/devices`, `/proc/interrupts`, `swctrl port|sfp show`.

## Identity

| | |
|---|---|
| Model / platform | US-48-500W / `US48P500`, system ID `0xeb62`, board revision 6 |
| Base MAC | `80:2a:a8:9c:da:d8` |
| SoC | BCM53344 (Hurricane 2, iProc), ARM Cortex-A9, single core |
| Second switch chip | BCM53346, PCIe `0000:01:00.0` + `.1` (`14e4:8346`) |
| CPU-side PCI | `0000:00:00.0` `14e4:8344`: iProc PCIe root port (no BAR in `/proc/iomem`; I: RC reports the SoC ID) |
| BCM53344 CMIC | AXI `0x48000000-0x4803ffff` (`bcmiproc-cmicd` in `/proc/iomem`), not PCIe |
| RAM | 256 MiB: 128 MiB @ `0x00000000` + 128 MiB @ `0x68000000` |
| Flash | Macronix MX25L25635E, 32 MiB, QSPI (`spi1.0`) |
| I2C | `/dev/i2c-0`, `/dev/i2c-1` static nodes, no kernel adapter; switchdrvr drives the CMIC SMBus ([board.md](board.md#i2c-master-issue-7)) |
| PoE | GPIO IRQs `pse_alarm_int`, `pse_slave_int`, `pse_master_int`; budget reported 436 W |
| Cooling | firmware reports 4 fans, 16 temperature sensors |
| Ports | 48× GbE copper, 4 cages labelled SFP+ 1, SFP+ 2, SFP 1, SFP 2 (stock 49–52); chip, logical port and PHY per port: [port-map.md](port-map.md); current cabling: `scripts/cabling.json` |

- CPU: one Cortex-A9 (ARMv7, MIDR `0x414fc091`) on unit 0, no second core. SCU config `0x19020004` = `0x00000100` (CPU count field 0 = one core, fixed by the MPCore RTL), one processor in `/proc/cpuinfo`; the kernel's `NR_CPUS=2 -> nr_cpu_ids=1` RCU line is benign. CoreSight IDs answer at `0x19011000/2000/3000` only (probe 2026-11-13, #53). The other Cortex-A9 is a2 on unit 1 ([a2.md](a2.md), driver #54).
## Interface rates: rated | board | run | gap (rule: run at rated max)
| Interface | Rated max | Board allows | We run | Gap reason |
|---|---|---|---|---|
| QSPI flash MX25L25635E (read) | 1 I/O 80 MHz (fast read, 8 dummy), 2 I/O 70 MHz, 4 I/O 70 MHz (datasheet v1.3, `/vendors/macronix/mx25l25635e/`) | not checked (BSPI quad lines wired? #20) | stock: 50 MHz, 1 lane, 3-byte address (`qspi_iproc: 1-lane output, 3-byte address`); uboot-2 same | inherited default, no recorded reason (#20); 4 I/O at 70 MHz = 35 MB/s vs 6.25 MB/s now |
| CPU | see #17 | | 400 MHz (`arm_clk=400MHz`) | #17 |
| DDR3 | see #55 | | 667 MHz | #55 |

- Two-chip split: front 1-24 + 51-52 on the BCM53344, 25-50 on the BCM53346; 2x13G HiGig between them ([board.md](board.md#two-chip-layout)). Unit 0 (live scan): 16 integrated copper PHYs + one external octal PHY; unit 1 not scanned (board.md Port map).
- Do not infer PHY or PoE-controller part numbers from the model name.

## Console and boot

- UART 115200 8N1, stock cmdline `console=ttyS0,115200`.
- Stock `ttyS0` = UART1 (`0x18000400`) = upstream DT `uart1`. Verified in the UniFi 7.5.15 kernel:
  `plat_serial8250_port` table at image offset `0xeed820` lists UART1 then UART0 (`scripts/stock_uart_order.py`).
  Same order as D-Link HR2 GPL `mach-iproc/common.c` — D-Link used as a hint only.
- Front-panel **RJ45 "Console" port**, RS-232 levels (MAX3321 transceiver), Cisco-style: rollover RJ45→DB9 cable
  (or USB↔RJ45 console cable), 115200 8N1, no flow control ([US-48-500W QSG](https://dl.ubnt.com/qsg/US-48-500W_US-48-750W/US-48-500W_US-48-750W_EN.html)).
  No case opening or TTL adapter needed. Current adapter: PL2303 USB-RS232 on `lizard` (`US48_HOST=lizard`).
- U-Boot `usw-v1.1.6.122-gd2310f3e` (was `usw-v1.1.4.115-g14af1ee6`); `loadaddr=0x01000000`,
  `ubntboot=bootm 0x01000000`; `tftpboot`, `bootm` present (no NIC, so `tftpboot` fails).
- U-Boot contains `FATAL:Image is not RSA signed..Aborting` in the ubnt app only; plain `bootm` does not enforce it (our unsigned uImage boots, [uboot.md](uboot.md) §1).
- Stock cmdline also passes `mem=128M@0x0 mem=128M@0x68000000` and `mtdparts=spi1.0:...` (same map as below), `ubnt_active_slot=1`.

## Flash map

| mtd | Offset | Size | Name |
|---|---|---:|---|
| 0 | `0x0000000` | 768 KiB | u-boot |
| 1 | `0x00c0000` | 64 KiB | u-boot-env |
| 2 | `0x00d0000` | 64 KiB | shmoo (DDR training) |
| 3 | `0x00e0000` | 15360 KiB | kernel0 |
| 4 | `0x0fe0000` | 15424 KiB | kernel1 (active after upgrade) |
| 5 | `0x1ef0000` | 1024 KiB | cfg |
| 6 | `0x1ff0000` | 64 KiB | EEPROM (board identity) |

Identical to upstream `bcm53340-ubnt-unifi-switch8.dts`.

## Stock firmware

- Image `US.bcm5334x.v7.5.15+17146.260825.0129`, 14107455 bytes,
  sha256 `c50774062dd01e54806433f070618dc3843b4d23d003813a301e0d7427702603`
  ([vendor API](https://fw-update.ui.com/api/firmware-latest?filter=eq~~platform~~US48P500&filter=eq~~channel~~release)).
- Container: UBNT header (268 B) → PART `u-boot` (447118 B) → PART `kernel0` (13659677 B) → `ENDS` + 256 B RSA signature + 4 B pad.
- Kernel: legacy uImage, LZMA, load/entry `0x18000`, Linux **3.6.5** (GCC 6.3.0); LZMA initramfs → 52 MB newc cpio.
- No `IKCFG_ST` config or DTB in the kernel (board is machine-ID, not DT).
- Proprietary modules: `linux-kernel-bde`, `linux-user-bde`, `ubnthal`, `gpiodev`, `ubnt_common` (vermagic `3.6.5 SMP mod_unload ARMv7`).
- `scripts/verify_firmware.py` checks vendor sha256/size/version, UBNT + PART CRCs, uImage CRCs; does **not** verify the RSA signature.
- Upgrade path used: `fwupdate.real -c -d` (check), then `/usr/etc/syswrapper.sh upgrade2` → wrote mtd0 + mtd4; mtd3 left as-is.

## Stock control path

```
swctrl / UniFi agent → libubnt → FastPath RPC (Unix datagram, /var/run/fastpath/) → switchdrvr
  → linux-user-bde / linux-kernel-bde (CMIC: AXI unit 0, PCIe unit 1) + ubnthal / gpiodev → PoE, sensors, fans (MSPI board CPLD/IO)
```

- Management `eth0` = `tun` device (`/sys/class/net/eth0 → devices/virtual/net`), no MAC driver, no AMAC IRQ in `/proc/interrupts`. switchdrvr moves CPU frames between the fabric (CMIC packet DMA, IRQs 194/218 `linux-kernel-bde`) and the tun. Live, stock 7.5.15, 2026-10-13. C.
  - → No management network without the switch SDK; a Linux mgmt port needs switch init + CMIC DMA driver.
- Internal CLI: `telnet 127.0.0.1 2222` (paced input needed; `enable`, `show environment`).
- `swctrl stp show` unsupported on 7.5.15.

## Backups

- `scripts/backup.py` (read-only, verified). Snapshots taken 2026-10-12 in `private/backups/`: `20261012T033725Z` (6.3.13), `20261012T055324Z` (7.5.15). Not committed.

## Unit 1 (BCM53346) MDIO scan (2026-11-13, `scripts/mdio_scan_u1.py`, #31)
- Clause-22 ID1/ID2, PHY 0-31, internal buses 0-3 and external buses 0-2 via unit 1's own CMIC MIIM (uboot-2). C
- Internal bus 0: 16 PHYs at 1-4, 6-9, 11-14, 16-19, ID `0x600d:0x85b0` (integrated GPHYs). C
- External bus 0: 8 PHYs at 20-27, ID `0x600d:0x845b` (external octal PHY). 16 + 8 = 24 copper ports = front 25-48. C
- Internal bus 1: `0x600d:0x8770` at 2, 4, 6, 8; `0x0143:0xbff0` at 17 and 31 (SerDes/other internal devices; not identified). C
- Internal buses 2-3, external buses 1-2: no response. C
- Same shape as unit 0 (16 integrated + one external octal PHY).

## Unit 1 (BCM53346) I2C / SPI / GPIO probe (2026-11-13, `a2ctl i2c-scan`, #31)
- CCB I2C controllers at AXI 0x18038000 and 0x1803b000: registers respond (CFG accepts enable, TIM_CFG 0x19003200), but a quick-write probe never completes for any of 117 addresses (start/busy stays set past 5 ms). Inconclusive, not "no devices": pins may be unmuxed or the bus held low. Board I2C is driven from unit 0's CMIC SMBus (docs/board.md), so unit 1's own I2C is probably unused. C
- QSPI window 0x1c000000 reads all zero and BSPI register 0x18027000 = 0x401: no flash answering on unit 1. C
- ChipcommonA GPIO input (0x18000060) = 0x00000fc3, output and enable registers (0x64, 0x68) = 0. C
- Not done: unit 1 CMIC SMBus scan, MSPI JEDEC read, UART, pinmux decode.

## Second chip (BCM53346) copper PHY bring-up (2026-11-13, `scripts/phy.py`, #31)
- After the reset into uboot-2 every one of the 24 copper PHYs reads BMSR 0x79c9 (link down); the integrated GPHYs have BMCR 0x1940 = POWER_DOWN set. C
- Minimal bring-up: BMCR = 0x1340 (clear power-down, AN enable, restart AN, FD) on internal bus 0 PHY 19 -> link up in ~5 s, partner ability 0x4de1 (100BASE-TX FD) = the camera on front 40. So front 40 = internal bus 0 address 19; the 16 integrated GPHYs (1-4, 6-9, 11-14, 16-19) are most likely fronts 25-40 in order, the octal PHY (external bus 0, 20-27) fronts 41-48 (I, only 19 verified). C (19), I (rest)
- Stock (swctrl, same day): port 1 up 1G, port 40 up 100M, port 49 up 10G (DAC to sw1 lan3), 50-52 down.
