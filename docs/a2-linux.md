# Linux on a2 (unit 1 Cortex-A9) (#79)

One kernel image, one initramfs for a1 and a2. Only DTB + bootargs differ. Loader/remoteproc side: [a2-design.md](a2-design.md).

## Memory map (a2 AXI view)
- RAM = hwdb `a2_carveout` (0x08000000, 64 MiB): host DDR low alias via PAXB OARR_0/OMAP_0 (#53). DTS macros come from hwdb (`scripts/build_a2_linux.py`).
- Linux RAM = carveout minus last MiB; last MiB = hwdb `a2_shared` (`hwdb/soc/a2_linux.toml`), `no-map`.
- Kernel at base + 0x8000 (AUTO_ZRELADDR derives the base from where the zImage runs; ARM_PATCH_PHYS_VIRT), reaches ~base + 0x0f30000 with bss.
- zImage at base + 0x1000000, DTB above the kernel end (layout requests in a2-design.md). Built-in initramfs: no external initrd.
- Peripherals (hwdb `a9.toml`, same addresses as a1): GIC dist 0x19021000, GIC cpu 0x19020100, global timer 0x19020200 (PPI 11), TWD 0x19020600 (PPI 13), PERIPHCLK fixed 200 MHz (conf I).
- Not in the DTS: DDR controller 0x60000000 (stalls a2), QSPI/flash, PCIe, PL310 (stays off as left by the ROM; the shared kernel only inits L2 from a DT node), CCB/APB peripherals.

## Console: DCC
- `CONFIG_HVC_DCC` + `earlycon=dcc`, bootargs `console=hvc0 earlycon=dcc` (a2 DTB `chosen`). DT bootargs win over `CONFIG_CMDLINE` (`CMDLINE_FROM_BOOTLOADER`, checked in drivers/of/fdt.c); a1 DTB has none and keeps its `ttyS0` cmdline.
- a2 writes CP14 DBGDTRTX; a1 reads it out-of-band through a2's CoreSight (`cs_debug0.DSCR` bit 29 TXfull, `DTRTX` +0x8c). Input: a1 writes `DTRRX` +0x80.
- Reader: product = `us48-a2` remoteproc tty (a2-design.md); bring-up = `scripts/a2_dcc_console.py` (uboot-2 console, ~40 chars per 0.3 s).
- On a1 the same option is inert: `hvc_dcc_check` writes DTRTX, nobody drains it, gives up after 100 ms (jiffies run: console_init follows local_irq_enable), hvc0 not registered. Costs 100 ms at a1 boot.
- Bootargs of a1 are untouched; `earlycon=dcc` is not in the a1 cmdline.

## Boot flow
1. a1: `us48_a2` remoteproc, `payload=1`, firmware = boot stub ELF; copies zImage + DTB into the carveout, writes stub boot args.
2. a1: start the DCC reader, then release a2.
3. Stub: r0 = 0, r1 = ~0, r2 = DTB, `bx` zImage. No appended DTB in the plain zImage: head.S keeps r2 (ARM_APPENDED_DTB only acts on the DTB magic at the zImage end).
4. Kernel -> `/init` (script, `openwrt/files/init`): `ubnt,us48-a2` in `/proc/device-tree/compatible` -> banner, `/sys`, devtmpfs, shell on `/dev/console` (= hvc0). Otherwise umount `/proc`, `exec /sbin/init` (a1, procd).
- Modules: same `/lib/modules/<release>` in the one initramfs; a2 loads none by default.
- Module updates on a2 for now = remoteproc reload of the image (`stop`, replace files, `start`). Later: virtio-net/9p over rpmsg.

## Build
- `python3 scripts/build_mainline.py` builds the kernel (single build). `python3 scripts/build_a2_linux.py [--build]` packages `tmp/a2/zImage` (copy of the plain zImage), `tmp/a2/us48-a2.dtb`; checks `.config` (AUTO_ZRELADDR, PHYS_VIRT, HVC_DCC, CMDLINE mode) and warns on hwdb regions inside the decompressed kernel.
- Log: `tmp/logs/build_a2_linux.log`.
- Same image on a1: `us48-mainline.bin` appends the a1 DTB; a2 uses the plain zImage.

## Risks
- a1 reader late or stopped: a2 blocks in DCC (earlycon per character); hvc0 never registered if the first drain is >100 ms late.
- The decompressed kernel (RAM base + 0x8000 .. +0xe77044 with bss) overwrites the stub MiB: fine, the stub has jumped; the layout check is in `build_a2_linux.py`.
- The decompressor prints nothing (`DEBUG_UNCOMPRESS` needs `!ARCH_MULTIPLATFORM`): the first output is `earlycon` at `parse_early_param`, so a silent a2 is not yet "kernel dead" (#79 bisect).
- Atomics: ldrex/strex on host DDR over PCIe, local monitor only (UP, non-shared mapping); untested on a2.
- Page tables, stack and all data cross PCIe on cache misses; L2 off. Expect slow boot; L1 only.
- PERIPHCLK 200 MHz assumed (global timer rate); wrong rate = wrong time only. Clock follows #70.
- ~14 MB uncompressed kernel + 10 MB zImage in 63 MiB leaves ~35 MiB free.
- a1: first boot with HVC_DCC unmeasured (100 ms console probe).

## Bring-up bisect (#79)
- Stub: `a` at entry, `J` at the jump over DCC; boot args `stage`/`status`/`dcc_fail` at carveout +0x200 (a2-design.md).
- `remoteproc0/pcsr`: PC samples; `dcc_stats`: reader alive, words drained.
- PC ranges (ARM state, PCSR = address + 8): stub `0x080000xx`; decompressor `0x09000000` + zImage size (its stack/bss just above); decompressed kernel before the MMU `0x08008000+` (phys = virt - 0xb8000000); after the MMU `0xc0008000+` (System.map in `tmp/mainline/build`).
- Module params (`us48_a2`): `stub_dcc` (default 1), `hdbgen_off` (clear DSCR.HDBGEN after the release; default off = a2 runs with halting debug enabled).
