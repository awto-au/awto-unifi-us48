# Stock boot-log facts (not elsewhere in docs)

- Mined by `scripts/bootlog_mine.py` (#50) → `tmp/logs/bootlog_mine.log`. Stock = 3.6.5, U-Boot `usw-v1.1.6.122`, 2026-10-14 cold boot.
- Refs: `C083748:N` = `tmp/logs/console/us48-20261014-083748.log` line N (stock cold boot starts at 451). Lines identical on every stock boot unless noted.
- Marking: C = log line, I = inference.

## U-Boot

- `DEV ID= 0000db56`, `SKU ID = 0x8344` (BCM53344). C083748:453-454
- DDR3, `MEMC 0 DDR speed = 667MHz`, `DRAM: 256 MiB`. C083748:455-462
- Shmoo never re-run: 542 `Validate Shmoo parameters ... OK` and 542 `Press Ctrl-C to run Shmoo ..... skipped` across all `tmp/logs/console/*.log` (2026-11-14 count), so mtd2 is not rewritten per boot. Not retrained on normal boots: `Validate ... OK`, `Press Ctrl-C to run Shmoo ..... skipped`, `Restoring Shmoo parameters from flash ..... done`, simple memory test OK. C083748:457-461 (→ #55)
- Clocks: `arm_clk=400MHz, axi_clk=200MHz, apb_clk=50MHz, arm_periph_clk=200MHz`. C083748:478
- `WARNING: Caches not enabled` (stock and ours). Kernel1 load = `size = 15728640` = `0xF00000` (kernel1 is 15424k = `0xF10000`: the last 64 KiB, the bootsel record, is not read), then LZMA 13659613 B. C083748:463,485,490,972 (→ #34)
- Bootsel line `BS: a=1 b=0 ff=0 ac=0 bc=0 mr=3 sa=0 sb=0` = active slot 1, no fallback, counts 0, max_retry 3. C083748:483
- PCIe port 0 link after reset (`soc_pcie_hw_init ... its value`):
  - `0x4` cold: LINKSTA `0x1001`, `port 0 is not active!!`
  - `0x1` after stock reboot: `0x9012`, `port 0 is active`
  - `0x0` after our reboot: `0x1001`, not active. Patch 952 (633f650) fixes it for the U-Boot reboot path only; after a mainline 7.4.0-rc1 reboot it is still `0x0` / `0x1001` / `port 0 is not active!!` (no PERST at shutdown in mainline). us48-20261114-201141.log:55,59,62 and :947,951,954
  - Stock kernel retrains the link itself, so cold/warm makes no difference to stock. C083748:465-472 vs :704-714
- Image header: name `Ubiquiti 7.5.15.17146`, load/entry `0x18000`. C083748:488-501
- Stock kernel build: 3.6.5, `gcc 6.3.0 LEDE r3979-2252731af4`, `builder@link-bionic-builder2`, 2018-08-30. C083748:501

## Stock kernel

- CPU `[414fc091] rev 1`; `Calibrating delay loop... 795.44 BogoMIPS (lpj=3977216)` (2 x ~398 MHz). C083748:532
- L2 PL310: 8 ways, `CACHE_ID 0x410000c9`, `AUX_CTRL 0x0a120000`, 128 KiB, enabled by the stock kernel. C083748:538-539
- `GENPLL[5] mdiv=40 rate=2000000000`, `Sel=1 Ovr=1 Div=48`, `UART clock rate 50000000`. C083748:545-547
  - `GENPLL[5]` is queried again by QSPI (:620) and UART: I: `[5]` is a clock ID that resolves to APB 50 MHz, not a physical 2 GHz channel.
- Caches: stock `cr=10c53c7d` (SWP on), `Memory policy: Data cache writealloc`, L2 `AUX_CTRL 0x0a120000` (no early BRESP / FLZ / prefetch / clock gating). Ours 6.18.55: `cr=10c5387d`, writeback, L2 `AUX_CTRL 0x4e120001`. C083748:502,505,539 vs :72,107 (→ #17, #56)
- Stock idle load average ~3.7 on the 400 MHz core (PoE off, no controller; `up 14 min`: 3.79, 3.70, 2.29). Ours idles at 0.1-0.8. C083748:4820 (also :4615, :5065, :5868)
- Memory: 128 MB lowmem `0xc0000000-0xc8000000` + 128 MB highmem, `244348k` available, vmalloc `0xc8800000-0xf0000000` (632 MB), `NR_IRQS:292`. C083748:514-530
- `BUG: mapping for 0x18000000 at 0xf0000000 out of vmalloc space` (+ `0x19000000 at 0xf1000000`): vendor iotable maps CCA/CCB at VAs above vmalloc end; benign. C083748:506-507
- Kernel timestamps jump +15.74 s at `Switching to clocksource iproc_gtimer` (14/14 boots); clock artifact, not a stall. Use `tmp/logs/us48-watch.log` for wall time. C083748:555
- PCIe window `membase 0x8000000 memlimit 0x10000000`. BCM53346 has 2 functions:
  - `01:00.0`: BAR4 16 MiB `0x08000000`, BAR2 256 KiB `0x0a000000` (CMIC), BAR0 32 KiB `0x0a040000`
  - `01:00.1`: BAR4 16 MiB `0x09000000`, BAR0 32 KiB `0x0a048000`
  - C083748:570-585
- Root-port PCIe cap dump (config regs, `0xb0` DevCap `0x8000`): MPS max **128 B**; DevCtl `0xb4` = `0x2c10` (MPS 128, MRRS 512, relaxed ordering + no-snoop); LnkCap `0xb8` = `0x5c12` (5 GT/s x1); LnkSta `0xbe` = `0x9012`. BDE's 256 B request exceeds the RC limit (→ clocks.md, #56). C083748:569
- Enumeration: `00:00.0` `14e4:8344` class `0x060400` bridge; `01:00.0` and `01:00.1` `14e4:8346` class `0x020000` (both Ethernet; `.1` has its own CMIC-sized BAR0). Bridge prefetch window `0x08000000-0x0a7fffff`, all BARs `64bit pref`. Class lines from our 6.18.55 boot C083748:1340-1352; window/BARs C083748:4595-4604 (stock logs print BAR assignments only).
- PCI command register: cold boot `0140 -> 0143` (RC, :587) and `0140 -> 0142` (EP, MEM only, :639); after a stock reboot `0146 -> 0147` (us48-20261013-181009.log:194, us48-20261014-120052.log:258, us48-20261113-132714.log:1358). BusMaster on the EP is set by BDE, not the kernel.
- INTx for `00:00.0`, `01:00.0`, `01:00.1` all irq 218 (slot 0, pins 1/2); UART0/1 irq 123 shared. C083748:576-578,598
- `PCIe port 1 in End-Point mode - ignored`. C083748:588
- QSPI `qspi_iproc.1: 1-lane output, 3-byte address`; `m25p80 spi1.0: found mx25l25635e, expected m25p80`. C083748:621-622 (→ #20)

## Stock init and drivers

- `Data abort at addr=0xc8a00000, fsr=0x1406 ignored.` x2 at 17.46 s, right after `ubnt_common` loads (13/14 boots, cold and warm). `0x1406` = asynchronous/imprecise external abort (ExT=1, FS=0b10110); the address is not reliable (it is vmalloc + 2 MiB). Vendor hook swallows it. I: probe of an absent block. Mainline's `0x1008` (synchronous, non-linefetch, `0xc88a01fc` PCIe config read, #11) is a different class. C083748:636-637
- `linux-kernel-bde (368): Payload size exceeds device capability (256)` at 17.79 s after `PCI: enabling device 0000:01:00.0`. I: BDE wants MPS 256 B, falls back (128 B). C083748:639-640
- `ubnt_common: module license 'Proprietary' taints kernel`. C083748:634-635
- `insmod: can't insert '/lib/modules/*/crypto_hash.ko'`: `usr/etc/modules.d/02-crypto-hash` lists a module the rootfs lacks; boot continues. C083748:643
- `rc.platform: applet not found`: `usr/etc/inittab:11` runs `/etc/rc.d/rc.platform`, absent in the image. C083748:648
- Every stock boot rewrites the 1 MiB `cfg` partition (mtd5 `0x1ef0000`) via `cfgmtd` Phase1..4, alternating slot 1/2: write other slot as BACKUP, verify, promote, demote. 29.98 s to 34.69 s (4.7 s). C083748:644-647
  - Consequence: a stock boot is not read-only on flash (wear, and `cfg` changes between captures).
- `/var/run/fastpath/` holds readiness gates `switchdrvr_ready` and `.poe_ready`, `startup-config` (34192 B), `proc-watchdog`, and the socket list. us48-20261112-155058.log:44135-44180
- Loopback listeners `127.0.0.1:2222` and `127.0.0.1:23` (`:23` = FastPath CLI, hardware.md). us48-20261112-155058.log:44185-44188
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
