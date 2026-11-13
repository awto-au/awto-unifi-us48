# Stock boot-log facts (not elsewhere in docs)

- Mined by `scripts/bootlog_mine.py` (#50) → `tmp/logs/bootlog_mine.log`. Stock = 3.6.5, U-Boot `usw-v1.1.6.122`, 2026-10-14 cold boot.
- Refs: `C083748:N` = `tmp/logs/console/us48-20261014-083748.log` line N (stock cold boot starts at 451). Lines identical on every stock boot unless noted.
- Marking: C = log line, I = inference.

## U-Boot

- `DEV ID= 0000db56`, `SKU ID = 0x8344` (BCM53344). C083748:453-454
- DDR3, `MEMC 0 DDR speed = 667MHz`, `DRAM: 256 MiB`. C083748:455-462
- Shmoo not retrained on normal boots: `Validate ... OK`, `Press Ctrl-C to run Shmoo ..... skipped`, `Restoring Shmoo parameters from flash ..... done`, simple memory test OK. C083748:457-461 (→ #55)
- Clocks: `arm_clk=400MHz, axi_clk=200MHz, apb_clk=50MHz, arm_periph_clk=200MHz`. C083748:478
- `WARNING: Caches not enabled` (stock and ours). Kernel1 load = `size = 15728640` (whole partition) then LZMA 13659613 B. C083748:463,485,490 (→ #34)
- Bootsel line `BS: a=1 b=0 ff=0 ac=0 bc=0 mr=3 sa=0 sb=0` = active slot 1, no fallback, counts 0, max_retry 3. C083748:483
- PCIe port 0 link after reset (`soc_pcie_hw_init ... its value`):
  - `0x4` cold: LINKSTA `0x1001`, `port 0 is not active!!`
  - `0x1` after stock reboot: `0x9012`, `port 0 is active`
  - `0x0` after our reboot (pre-patch 952 only): `0x1001`, not active
  - Stock kernel retrains the link itself, so cold/warm makes no difference to stock. C083748:465-472 vs :704-714
- Image header: name `Ubiquiti 7.5.15.17146`, load/entry `0x18000`. C083748:488-501
- Stock kernel build: 3.6.5, `gcc 6.3.0 LEDE r3979-2252731af4`, `builder@link-bionic-builder2`, 2018-08-30. C083748:501

## Stock kernel

- CPU `[414fc091] rev 1`; `Calibrating delay loop... 795.44 BogoMIPS (lpj=3977216)` (2 x ~398 MHz). C083748:532
- L2 PL310: 8 ways, `CACHE_ID 0x410000c9`, `AUX_CTRL 0x0a120000`, 128 KiB, enabled by the stock kernel. C083748:538-539
- `GENPLL[5] mdiv=40 rate=2000000000`, `Sel=1 Ovr=1 Div=48`, `UART clock rate 50000000`. C083748:545-547
- Memory: 128 MB lowmem `0xc0000000-0xc8000000` + 128 MB highmem, `244348k` available, vmalloc `0xc8800000-0xf0000000` (632 MB), `NR_IRQS:292`. C083748:514-530
- `BUG: mapping for 0x18000000 at 0xf0000000 out of vmalloc space` (+ `0x19000000 at 0xf1000000`): vendor iotable maps CCA/CCB at VAs above vmalloc end; benign. C083748:506-507
- Kernel timestamps jump +15.74 s at `Switching to clocksource iproc_gtimer` (14/14 boots); clock artifact, not a stall. Use `tmp/logs/us48-watch.log` for wall time. C083748:555
- PCIe window `membase 0x8000000 memlimit 0x10000000`. BCM53346 has 2 functions:
  - `01:00.0`: BAR4 16 MiB `0x08000000`, BAR2 256 KiB `0x0a000000` (CMIC), BAR0 32 KiB `0x0a040000`
  - `01:00.1`: BAR4 16 MiB `0x09000000`, BAR0 32 KiB `0x0a048000`
  - C083748:570-585
- INTx for `00:00.0`, `01:00.0`, `01:00.1` all irq 218 (slot 0, pins 1/2); UART0/1 irq 123 shared. C083748:576-578,598
- `PCIe port 1 in End-Point mode - ignored`. C083748:588
- QSPI `qspi_iproc.1: 1-lane output, 3-byte address`; `m25p80 spi1.0: found mx25l25635e, expected m25p80`. C083748:621-622 (→ #20)

## Stock init and drivers

- `Data abort at addr=0xc8a00000, fsr=0x1406 ignored.` x2 at 17.46 s, right after `ubnt_common` loads (13/14 boots, cold and warm). Vendor hook swallows an external abort (ExT=1) on the first vmalloc page. I: probe of an absent block; same abort class panics mainline (#11). C083748:636-637
- `linux-kernel-bde (368): Payload size exceeds device capability (256)` at 17.79 s after `PCI: enabling device 0000:01:00.0`. I: BDE wants MPS 256 B, falls back (128 B). C083748:639-640
- `ubnt_common: module license 'Proprietary' taints kernel`. C083748:634-635
- `insmod: can't insert '/lib/modules/*/crypto_hash.ko'`: `usr/etc/modules.d/02-crypto-hash` lists a module the rootfs lacks; boot continues. C083748:643
- `rc.platform: applet not found`: `usr/etc/inittab:11` runs `/etc/rc.d/rc.platform`, absent in the image. C083748:648
- Every stock boot rewrites the 1 MiB `cfg` partition (mtd5 `0x1ef0000`) via `cfgmtd` Phase1..4, alternating slot 1/2: write other slot as BACKUP, verify, promote, demote. 29.98 s to 34.69 s (4.7 s). C083748:644-647
  - Consequence: a stock boot is not read-only on flash (wear, and `cfg` changes between captures).
- Telemetry `PUT 'https://trace.svc.ui.com/traces' failed: Couldn't resolve host name` without internet. C083748:4813

## Our 6.18.55 boot

- `bcm-iproc-i2c 48000000.i2c: error -ENXIO: IRQ index 0 not found`, polls instead (see board.md "Open: its IRQ"). C083748:195-196
- `pci_bus 0000:00: 2-byte config write ... may corrupt adjacent RW1C bits` (pcie-iproc quirk, benign). C083748:1339
- `supply ... not found, using dummy regulator` for `lm75`, `pca954x 2-0070`, `spi-nor spi0.0` (DT has no regulators). C083748:846,3467-3486

## Stage timing (seconds since U-Boot banner; `us48-watch.log`, n=7 stock, cold = warm within 2 s)

| Stage | Stock | Ours from `kernel0` |
|---|---|---|
| `Linux version` | 138-139 (LZMA with caches off ~120 s) | ~15 |
| userspace up | cfgmtd Phase1 152 | +11 after kernel |
| shell | 207-221 | ~32 |

- Stock kernel real time: `Linux version` to `Freeing init memory` ~1.5 s; `/sbin/init` to cfgmtd Phase1 ~12 s; `Please press Enter` ~52 s after cfgmtd.
