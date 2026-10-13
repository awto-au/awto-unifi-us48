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
| CPU-side PCI | `0000:00:00.0` `14e4:8344` (BCM53344 CMIC) |
| RAM | 256 MiB: 128 MiB @ `0x00000000` + 128 MiB @ `0x68000000` |
| Flash | Macronix MX25L25635E, 32 MiB, QSPI (`spi1.0`) |
| I2C | `/dev/i2c-0`, `/dev/i2c-1`; no sysfs clients enumerated |
| PoE | GPIO IRQs `pse_alarm_int`, `pse_slave_int`, `pse_master_int`; budget reported 436 W |
| Cooling | firmware reports 4 fans, 16 temperature sensors |
| Ports | 48× GbE copper, 2× SFP+ (49–50), 2× SFP (51–52) |

- Two-chip split (24/24?) and inter-chip link are **not** established — OpenMDK descriptions say each chip has 24 GbE + 16 integrated copper PHYs.
- Do not infer PHY or PoE-controller part numbers from the model name.

## Console and boot

- UART 115200 8N1, stock cmdline `console=ttyS0,115200`.
- Stock `ttyS0` = UART1 (`0x18000400`) = upstream DT `uart1`. Verified in the UniFi 7.5.15 kernel:
  `plat_serial8250_port` table at image offset `0xeed820` lists UART1 then UART0 (`scripts/stock_uart_order.py`).
  Same order as D-Link HR2 GPL `mach-iproc/common.c` — D-Link used as a hint only.
- Header location on the PCB: not yet identified.
- U-Boot `usw-v1.1.6.122-gd2310f3e` (was `usw-v1.1.4.115-g14af1ee6`); `loadaddr=0x01000000`,
  `ubntboot=bootm 0x01000000`; `tftpboot`, `bootm` present.
- U-Boot contains `FATAL:Image is not RSA signed..Aborting` — unknown whether plain `bootm` enforces it (first boot test answers this).
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
  → linux-user-bde / linux-kernel-bde (PCIe CMIC) + ubnthal / gpiodev → PoE, sensors, fans (MSPI board CPLD/IO)
```

- Internal CLI: `telnet 127.0.0.1 2222` (paced input needed; `enable`, `show environment`).
- `swctrl stp show` unsupported on 7.5.15.

## Backups

- `scripts/backup.py` (read-only, verified). Snapshots taken 2026-10-12 in `private/backups/`: `20261012T033725Z` (6.3.13), `20261012T055324Z` (7.5.15). Not committed.
