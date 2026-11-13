# PCI layer for the second chip (us48_pci)

Module `kernel/us48-pci/us48_pci.c` (+ `us48_pci.h`). Owns PCI `0000:01:00.0` (BCM53346, unit 1). No netdev code. Issue #54 (a2 driver), #46 (two-controller split).
C = confirmed (evidence in repo/issues), I = inferred.

## What it owns
- PCI ID `14e4:8346`, function 0 only (01:00.1 shares the ID, rejected). C ([driver-internals.md](driver-internals.md) 14, us48_cmic.c)
- `pcim_enable_device`, `pci_set_master`, BAR0 (32 KB) + BAR2 (256 KB CMIC) mapped. C (#53 probe 2)
- DMA: coherent mask 32-bit (as us48_cmic); chip inbound window is 128 MiB, so client bus addresses must be below `US48_PCI_DMA_LIMIT` (0x08000000). C (stock DCBs at ~0x075fxxxx), I (window size >= 128 MiB, not decoded)
- Shutdown: IMAP0_7 restored, bus master cleared (stock U-Boot PCIe init hangs on live DMA). C (us48_cmic shutdown comment)
- BAR0 map: chipcommon at +0x0, endpoint PAXB at +0x2000+off. C (driver-internals 14.2); 4 KiB AXI window at +0x7000 selected by IMAP0_7 (+0x2c1c, default 0x18032001). C (docs/a2.md)

## API (`us48_pci.h`, all EXPORT_SYMBOL_GPL)
| Function | Does | Source of the sequence |
|---|---|---|
| `us48_pci_get/put` | refcounted handle, `ERR_PTR(-ENODEV)` if unbound | - |
| `us48_pci_cmic_read/write(off)` | raw BAR2 word | us48_cmic.c |
| `us48_pci_bar0_read/write(off)`, `us48_pci_paxb_read/write(off)` | raw BAR0 / BAR0+0x2000 word | driver-internals 14.2 |
| `us48_pci_schan(op, blk, addr, data, words, ack)` | S-channel op, serialised; core = `common/bcm-cmic/bcm_cmic_schan.c` (included) | bcm_cmic_schan.c |
| `us48_pci_axi_read32/write32/write` | unit-1 AXI word/buffer via IMAP0_7 + window; window left on last page, restored at remove/shutdown | a2_loader.py `page/rd/wr` |
| `us48_pci_a2_host_window(en)` | OARR_0 = 0x08000001 (0 = off), OMAP_0 = 0: a2 AXI 0x08000000.. = host phys 0x0.. | docs/a2.md, #53 |
| `us48_pci_a2_reset` | clear then set CRU_CONTROL.ARM_RST_N (0x1803e000 b4), read-modify-write | a2_loader.py `reset` (writes 0x20, 0x30) |
| `us48_pci_a2_halt` | unlock LAR 0xc5acce55, DSCR.HDBGEN, DRCR halt, wait DSCR.halted | a2_loader.py `start` |
| `us48_pci_a2_set_pc(pc)` | halted only: DSCR.ITREN, DTRRX = pc, ITR `mrc p14` (0xee100e15) + `mov pc,r0` (0xe1a0f000), ITREN off | a2_force_pc.py |
| `us48_pci_a2_release` | DRCR = restart + clear sticky, wait DSCR.restarted | a2_loader.py `start` |
| `us48_pci_a2_load(addr, img, len)` | host words into a2 AXI (SRAM 0x1b000000, 16 KiB) | a2_loader.py `load` |
| `us48_pci_a2_run(entry)` | halt + set_pc + release | a2_loader.py `start` |
| `us48_pci_a2_pc_sample` | read PCSR (debug +0x84) | a2_force_pc.py (read only) |
| `us48_pci_a2_boot_from_pcie` | `-EOPNOTSUPP` | docs/a2.md: PCIe boot path not taken |

- Locking: `win_lock` covers IMAP0_7 + window + OARR_0 + debug sequences + raw BAR0/BAR2 word access; `schan_lock` covers S-channel. One owner of the window, as #54 requires.
- Mailbox protocol (stub cmd 1/2/3, heartbeat) is NOT in this layer: it is stub-specific (`tools/a2_stub.S`); belongs to the future remoteproc-style client.
- Debug waits: 1 ms backstop (`DBG_WAIT_US`), expiry logs DSCR and returns -ETIMEDOUT. Not measured on the bus. I

## Consumers
- `us48_cmic` (netdev, unit 1): today owns the same PCI ID and BAR2. C
- a2 client (#54): remoteproc-style driver on `us48_pci_a2_*` + stub mailbox.
- `rust/a2ctl`: later a client via char device/debugfs instead of raw BAR access.

## Migration of us48_cmic (not done)
- Both drivers match `14e4:8346`; only one can bind. Until migrated, load only one of them.
- Step 1: drop `pci_driver` from us48_cmic; `us48_cmic_init` calls `us48_pci_get()`, uses `us48_pci_cmic_*`/`us48_pci_schan` or takes a BAR2 pointer accessor (add `us48_pci_cmic_base()` then).
- Step 2: DMA device = the PCI device (`pdev->dev`); expose it via an accessor (not yet exported).
- Step 3: move the shutdown (clear master, PERST# note) to us48_pci; netdev keeps `ndo_stop` DMA abort.
- Step 4: us48_cmic `request_module`/`MODULE_SOFTDEP("pre: us48_pci")`; stock ordering (unit 1 first at boot, #46) preserved.
- Step 5: us48_cmic's platform (unit 0) front-end stays independent; schan core to be shared, not duplicated: if it also links `bcm_cmic_schan.c`, export it from one module instead of including twice.

## Verification state
- Builds against `tmp/linux-mainline` (v7.4-rc1) out of tree, no compiler warnings; modpost unresolved-symbol warnings only because `Module.symvers` is absent (same as build_mainline). Log `tmp/logs/us48_pci_build.log`. C
- Never loaded or run on the board. I

## Open unknowns
| # | Unknown | C/I |
|---|---|---|
| 1 | Debug-access timing on the PCIe path (1 ms backstop is a guess) | I |
| 2 | Word-wide window writes only proven via U-Boot `mw.l`; ordering/posting of back-to-back writeL via Linux readl/writel untested | I |
| 3 | DSCR bit meanings (b0 halted, b1 restarted, b13 ITRen, b14 HDBGen, b24 InstrCompl) are ARMv7 debug arch, matched by scripts; not re-read from a datasheet | I |
| 4 | PCSR at debug +0x84 shares the ITR offset; script `a2_force_pc.py` reads it, value meaning unverified | I |
| 5 | PCI inbound window size and whether coherent mask 32 is safe for high-bank RAM (bus >= 0x08000000 would miss it) | I |
| 6 | Whether Linux PERST# at RC probe disturbs a2/IMAP0_7 state | I |
| 7 | `reset` with RMW vs script's absolute 0x20/0x30 (other CRU_CONTROL bits on unit 1 read 0x30) | I |
| 8 | Boot via PCIe strap override (`STRAPS_SW_OVERRIDE`, BOOT_DEV 7): not taken by a2 | C |
| 9 | a2 stalled by an AXI read to an unused range cannot be halted; recovery = reset | C (#53) |
| 10 | IRQ/MSI from a2 to host, a2 power-down, L2-as-RAM on a2: no known registers | I |
