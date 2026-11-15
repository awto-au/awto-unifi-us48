# Linux on a2 (unit 1 Cortex-A9) (#79)

One kernel image, one initramfs for a1 and a2. Only DTB + bootargs differ. Loader/remoteproc side: [a2-design.md](a2-design.md).

## Memory map (a2 AXI view)
- RAM = hwdb `a2_carveout` (0x08000000, 64 MiB): host DDR low alias via PAXB OARR_0/OMAP_0 (#53). DTS macros come from hwdb (`scripts/build_a2_linux.py`).
- Linux RAM = carveout minus last MiB; last MiB = hwdb `a2_shared` (`hwdb/soc/a2_linux.toml`), `no-map`.
- Kernel at base + 0x8000 (AUTO_ZRELADDR derives the base from where the zImage runs; ARM_PATCH_PHYS_VIRT), reaches ~base + 0x0f30000 with bss.
- zImage at base + 0x1000000, DTB above the kernel end (layout requests in a2-design.md). Built-in initramfs: no external initrd.
- Peripherals (hwdb `a9.toml`, same addresses as a1): GIC dist 0x19021000, GIC cpu 0x19020100, global timer 0x19020200 (PPI 11), TWD 0x19020600 (PPI 13), PERIPHCLK = ARM/2 (conf I, host-timed on unit 1 at 100 and 1000 MHz); DTB value from `build_a2_linux.py --cpu-mhz`.
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
- DTB timer clock must equal the real PERIPHCLK or printk time is scaled (`--cpu-mhz` = us48_a2 `cpu_mhz`).
- ~14 MB uncompressed kernel + 10 MB zImage in 63 MiB leaves ~35 MiB free.
- a1: first boot with HVC_DCC unmeasured (100 ms console probe).

## Bring-up bisect (#79)
- Stub: `a` at entry, `J` at the jump over DCC; boot args `stage`/`status`/`dcc_fail` at carveout +0x200 (a2-design.md).
- `remoteproc0/pcsr`: PC samples; `dcc_stats`: reader alive, words drained.
- PC ranges (ARM state, PCSR = address + 8): stub `0x080000xx`; decompressor `0x09000000` + zImage size (its stack/bss just above); decompressed kernel before the MMU `0x08008000+` (phys = virt - 0xb8000000); after the MMU `0xc0008000+` (System.map in `tmp/mainline/build`).
- Module params (`us48_a2`): `stub_dcc` (default 1), `hdbgen_off` (clear DSCR.HDBGEN after the release; default off = a2 runs with halting debug enabled).

## First boot (#79, 2026-11-15, debug profile, normal ARM 1000)
- Plan A: dcc_log `aJ` (stub CP14 write + a1 reader work). Plan B: PCSR 0x09001xxx (decompressor, spread = inflating), then 0xc0xxxxxx (kernel). Linux reaches `/init` and a shell on hvc0 in ~10 s of a2 time (~50 s wall, L1 only, lockdep).
- Fixes found on the way:
  - `COMPAT_32BIT_TIME=y` in `us48_defconfig`: musl 1.2.6 uses the time32 syscalls for small values; without it `sleep`/`nanosleep` return ENOSYS at once (init and watchdog spin, board resets via SP805).
  - `/init` mounts sysfs before the role check (`/proc/device-tree` is a symlink into `/sys`); a2 had taken the a1 path.
  - `ttyA2` termios is raw: ECHO looped a2's output back into DTRRX as input.
- Stage files from the host: `scp -O` `us48-a2-boot.elf`, `us48-a2-zImage`, `us48-a2.dtb` to `/lib/firmware`; `mount -t debugfs`; `insmod us48_a2.ko payload=1`; `cat /dev/ttyA20 > file &` before `echo start`; keystrokes: `printf 'cmd\n' > /dev/ttyA20`.

## Clock and boot speed (#70, #79, 2026-11-15)
- Unit 1 power-on: `POLICY_DBG` 0x2222 = fid 2 (sys clk, 100 MHz row), PLLARMA 0x01004010 (PLL in reset, unlocked), ch0 mdiv 4, ch1 mdiv 2, ARM_DIV 0x2012. PERIPHCLK 50 MHz (heartbeat 25 beats/s x 2e6 ticks).
- `us48_a2` `cpu_mhz` (default 100 since 2026-11-15, #79; 0 = leave): E2 sequence of `us48_cpu_switch` through the regmap in `start()` before the halt. 400..600 = fid 6 (VCO 4x, ch0 mdiv 4), 700+ = fid 7 (VCO 2x, ch1 mdiv 2). 1000: GO + lock in 149 us, PERIPHCLK 500 MHz host-timed. debugfs `remoteproc0/ccu` = registers + computed MHz.
- Standalone firmware (bustest, selftest) at 800/1000: no mismatch, link clean (`us48_bustest stats`).
- Linux payload (debug and normal profile, with and without L2): early memory corruption (UBSAN in `memblock_free_all`, page-struct oops) at 500/600/800/1000; 400 fails about 3 in 5; 100 MHz boots every time. `cachepolicy=` is ignored on ARMv7 (`Only cachepolicy=writeback supported`): not a test. Poll rate of the DCC reader (5 ms, 1 ms, 20 ms, burst) does not change it. Open in #79.
- `a2_boot_time.py <label> [--param k=v]` = start a2 Linux and time it (a1 uptime in centiseconds); a2 text in `tmp/logs/a2_boot_<label>.txt`.
- Boot-speed table at 100 MHz (a1 wall from `echo start`; the 400 MHz debug zImage rows boot only intermittently):

| Change | banner | shell |
|---|---|---|
| debug zImage, DCC poll 5 ms (baseline; a2 printk 22.0 s) | 29.4 s | 30.2 s |
| poll 1 ms / busy 0, or 200 us / busy 0 | 29.4 / 29.2 s | 29.9 / 29.9 s |
| uncompressed Image at `+0x8000` (`kernel_fw`, `kernel_off`) | 25.4 s | 26.2 s |
| + `quiet`, no earlycon | 24.6 s | 25.1 s |
| normal profile Image, quiet | 1.79 s | 2.50 s |
| + PL310 L2 node (`build_a2_linux.py --l2`) | 1.32 s | 1.58 s |

- Debug profile options (poisoning, lockdep, UBSAN) are the cost, not the console or decompression (4 s).

## Corruption above 100 MHz: offline analysis (#79)
- Leading suspect: Linux UP maps RAM write-back **no-write-allocate** (NMRR `0x40e040e0`, `arch/arm/mm/proc-v7-2level.S:119-140`; `mmu.c:57` default `CPOLICY_WRITEBACK`; write-allocate is forced only for SMP, `mmu.c:452-456`; `CONFIG_SMP` is off). Bustest maps RAM write-back write-allocate (`bustest.c:77`) and never reloads a word it stored a few cycles before.
- A9 L1: 2 line-fill buffers, 1 eviction buffer, 4-entry 64-bit merging store buffer (DDI0388 p7-2/7-3). A store miss on a non-allocating mapping leaves as a merged 64-bit AXI write; the next load of that line issues a line fill with no BRESP wait. AXI does not order AW against AR.
- PAXB has no read-behind-write ordering on at least one path (`pcie-harness.md` "Window flush", 3dd7608). Store-to-load distance shrinks with the CPU clock, so a race that needs the load right behind the store appears above 100 MHz.
- Controls found (regdb): `PAXB_0_PCIE_PAXB_RX_DEBUG_CONTROL_0` 0x1801210c (`RX_IGNORE_BRESP` bit 5 reset 0, `RX_FREE_ARID_COUNT` 1:0 reset 3, `RX_PAXB_FREE_CMPL_BUF_COUNT` 4:2 reset 7); `PAXB_0_PCIE_RC_AXI_CONFIG` 0x18012100 reset 0x3bfdf (AxCACHE 0xe, AxUSER 0x1f); `PAXB_0_PCIE_EP_AXI_CONFIG` 0x18012104 reset 0x3ffff (AxCACHE 0xf = bufferable; stock BDE writes 0).
- Root port DevCtl 0x2c10 on stock = relaxed ordering + no-snoop on (`stock-bootlog-facts.md:38`); mainline PCI core leaves the bit as found.
- A9 always sets AWUSER[8] (early BRESP permitted, DDI0388 p8-7); only an L2C-310 acts on it, so it does not explain the L2-off failure. Linux UP does not touch ACTLR (`proc-v7.S:309-316` is `CONFIG_SMP` only).

## Ordering test (`a2fw/rawtest.c`, `us48-a2-rawtest.elf`)
- Store word, load it back at once, under WBWA / WB-no-WA (Linux) / WT / NC mappings (tests A, B), plus store + line clean-invalidate + reload (C) and store + 9 same-set loads + reload (D). Memory truth re-read through the NC alias after clean+invalidate.
- Output: `trace0` text. `m` = wrong loads, `r` = wrong words in memory; `m>0, r=0` = stale read, `r>0` = lost or late write. Header shows ACTLR and the L1 way size.
- Run at `cpu_mhz=1000`: `insmod us48_a2.ko firmware name`, `echo start`, `cat /sys/kernel/debug/remoteproc/remoteproc0/trace0` (a1 text only, no DCC). Expect A_nwa/B_nwa to fail if the leading suspect is right; repeat at 100 MHz as control.

## Board results (#79, 2026-11-15, unit 1 at 1000 MHz unless stated)
- Tools: `scripts/a2_up.py` (cycle, RAM-boot debug image, debugfs), `scripts/a2_raw.py` (run an a2 ELF, print trace0), `scripts/u2_run.py` (catch the awto-uboot window); `us48_pci` debugfs `paxb` (read both units) and `poke` (write-only, 0x18012100..0x1801210c).
- rawtest: 100 MHz and 400 MHz clean (pass 1-3, all m = r = 0). 600/800/1000 MHz: a2 sits at the undefined-instruction vector (PCSR 0xffff000c = vector 0xffff0004 + 8) right after the header text in most runs; the runs that complete show instruction words (`e59f0110`, `eb009e3c`) in the `r` counters (data line holding code).
  - Corruption is in what a2 reads or writes, before any store-then-load test runs: instruction/data line mix-up, not an ordering race on one word.
- PAXB (reset values on both units): RC_AXI_CONFIG 0x0003bfdf, EP_AXI_CONFIG 0x0003ffff, RX_DEBUG_CONTROL_0 0x0000001f (IGNORE_BRESP 0).
- `init_default_cache_policy()` overrides the `mmu.c:57` default from the CPU section flags (`proc-v7-2level.S` PMD_FLAGS_UP): the one-line `cachepolicy` default has no effect. Test patch `mainline/a2/0001-ARM-v7-UP-section-flags-write-allocate-a2-test.patch` (kernel prints `Memory policy: Data cache writealloc`): 5 boots at 1000 MHz = 1 clean, 4 oops/panic (slab corruption, NULL deref). Write-allocate is not the fix.
- RO + no-snoop off (DevCtl 0x2c10 -> 0x2400 on root port and EP): rawtest 1 of 6 runs complete (control 4 of 6): no gain.
- Pokes (rawtest, 8 runs each, restored after): unit 0 RX_DEBUG 0x1c (serialise reads): 5 of 8 abort/corrupt; unit 1 EP_AXI_CONFIG 0: 4 of 8 abort; unit 0 RC_AXI_CONFIG 0x3e1f: 7 of 8 abort. No change.

## AXI / GENPLL and cache probes (#79, 2026-11-15, unit 1 at 1000 MHz unless stated)
- `us48_pci` debugfs `genpll` (read-only): GENPLL CTRL0..3 + STATUS, CCU ARM_DIV / POLICY_FREQ / POLICY_DBG of both units.
- Power-on: unit 0 (uboot-1 mode 4) CTRL2 `0x00080a00` = AXI 200 / APB 50; unit 1 CTRL2 `0x00080500` = **AXI 400 / APB 100**. Both locked (STATUS `0x205`), NDIV 80, ARM_DIV `0x2012`. Unit 1 is not unlocked or bypassed.
- `us48_a2` `axi_mhz` (default 200; 100/200/250/285/400; 0 = leave): genpll.c sequence (docs/clocks.md E5) before the halt, lock poll 190 us. Persists across a2 resets (GENPLL is not in the a2 reset).
- rawtest at AXI 200 / 400 / 100, 4 runs each: 600 and 1000 MHz fail every run (`a2_raw.py` verdict: undefined-vector PCSR or `r`/`m` != 0); 800 MHz 1 of 2 ok. **AXI clock is not the cause.**
- Linux (normal-profile Image, 1000 MHz, AXI 200, 5 boots each): L2 off 0/5 reach the shell, L2 on 1/5.
- Cache probes (`us48-a2-rawtest-noic.elf` = SCTLR.I off, `-nodc.elf` = SCTLR.C off; `build_a2fw.py`), AXI 200, 1000 MHz, 25 s run each:
  - I-cache off: 5 passes, all m = r = 0, no abort. D-cache off: 16 passes, all m = r = 0, no abort. Cached build: abort within ~1 s, every run.
  - The fault needs I-cache and D-cache both on. Slower fetch/data rate in the probes lowers traffic; a rate effect is not separated from an I+D fill interaction.

## Errata, ID routing and I-fetch/D-fill probes (#79, 2026-11-15, offline)
- Docs in `/vendors/arm` (README manifest): DDI0388I (A9 TRM), UAN0009D (A9 r4 errata), UAN0007D (r2 errata), DDI0397I (NIC-301 TRM).
- A9 r4p1 errata with a Diagnostic bit (CP15 c15 0 c0 1, secure RMW, undocumented in the TRM): 845369 bit 22 (eviction-buffer corruption on streaming transition; needs ACP or 2+ CPUs: UAN0009D p2-28), 794072 bit 4 (DMB loop DoS, MPCore SMP+FW only: p2-23). 761319 (read reorder, SMP shared WB only: p2-15) has no bit. Nothing in r4p1 covers line fills, ID reordering or a UP corruption.
- A9 AXI IDs (DDI0388I p8-3/8-4): M0 reads: NC = 0, linefill 0 = 2, linefill 1 = 3; M1 (I-side) reads: IDs 0-3, 4 outstanding; M0 write interleave capability 1 (Table 8-1 p8-2). The TRM does not say whether read data interleaving/reordering across IDs is tolerated. Linefills are WRAP4 64-bit critical-word-first (p8-3).
- ACTLR (Table 4-36 p4-28/29): no bit limits line fills or forces in-order returns. a2 ACTLR reads 0 (rawtest header, 102 runs): L1 prefetch (bit 2), L2 prefetch hint (bit 1), SMP, FW all off.
- PAXB regdb (`regdb/hr2_regs.tsv`): only `RX_DEBUG_CONTROL_0` (inbound/AXI-master side) has read controls; no outbound read reorder/interleave bit. The NIC-301 interconnect does: `AXIIC_PCIE0_S0_FN_MOD_BM_ISS` 0x1a004008 bit 0/1 = read/write issuing capability of the switch toward the PCIe0 slave forced to 1 (DDI0397I Table 3-2 p3-6); `AXIIC_IHOST_M1_FN_MOD` 0x1a042108 / `AXIIC_PCIE0_M0_MAX_OT` 0x1a043110 exist too.
- Probes (`a2fw/iftest.c`, `iftest_asm.S`, `a2_diag.h`; `build_a2fw.py`, -Werror clean), run with `scripts/a2_raw.py <elf> 1000`; verdict line as for rawtest:
  - `us48-a2-ifa`: loop code in SRAM (0x1b001000), 64 KiB src/dst across PCIe, both L1 on.
  - `us48-a2-ifb`: 64 KiB straight-line code across PCIe (one load/add/store per 32 B line), data in SRAM (4 KiB window), both L1 on.
  - `us48-a2-ifd`: same code and 64 KiB data both across PCIe: every code line fill coincides with a data line fill.
  - `us48-a2-rawtest-diag22`, `-nic`, `-diag22-nic`, `us48-a2-ifd-diag22-nic`: Diagnostic bit 22 and/or NIC PCIe0 slave issuing = 1 (`NIC_ISS=3`), header prints `diag:` and `nic:` read-back.

## I-fetch/D-fill and NIC issuing probes: board results (#79, 2026-11-15, a2 1000 MHz, AXI 200, 4 runs each)
- `us48_a2` rebuilt from 8e6edd4, loaded on a1 (cpu_mhz default 100). ELFs run with `a2_raw.py <name>.elf 1000`.

| ELF | ok / 4 | notes |
|---|---|---|
| `rawtest` (control) | 0 | undefined vector 0xffff000c every run |
| `ifd` (I + D both over PCIe) | 4 | m = r = 0, ~0x4cc passes; does not reproduce |
| `rawtest-nic` (NIC PCIe0 S0 issuing = 1) | 0 | `nic:` reads back 3 (write took), fails as control |
| `ifa` (code SRAM, data PCIe) | 4 | m = r = 0 |
| `ifb` (code PCIe, data SRAM) | 4 | m = r = 0 |
| `rawtest-diag22` | 0 | `diag:` reads back 0x00400000 (bit 22 sticks) |
| `rawtest-diag22-nic` | 0 | both knobs stick, fails |
| `ifd-diag22-nic` | 4 | clean |

- NIC issuing override and Diagnostic bit 22 are not the cause. Linux-side NIC param, 5 boots and bandwidth cost not run (step 2 not clean).
- Streaming I+D fill traffic over PCIe at full rate is clean (ifa/ifb/ifd): the failure is specific to what rawtest does, not to the I/D line-fill interaction. Next: bisect rawtest (mapping changes with caches on, aliasing).

## rawtest bisect and page-table-walk hypothesis (#79, 2026-11-15, a2 1000 MHz, AXI 200)
- rawtest never edits a PTE after the MMU is on: the table (4 aliases of the carveout, built with MMU off) is fixed. The "posted PTE write vs walk" race has nothing to act on in rawtest; Linux PTE updates are not exercised by it.
- Bisect (`a2_raw_batch.py RUNS 1000 <elf>...`, ok/runs; ELFs `us48-a2-rawtest-<x>`, `TESTMASK` in rawtest.c):

| ELF | ok / runs |
|---|---|
| setup only (`TESTMASK=0`) | 4/4 |
| A (A_wb, A_nwa, A_wt, A_nc) | 2/4 |
| A0 / A1 (nwa) / A2 / A3 | 4/4, 2/4 + 7/8, 4/4, 4/4 |
| B, C, D | 4/4 each |
| PT read-back with MMU off (`PT_RB`, all tests; `ptbad` = 0) | 2/4 |
| table in a2 SRAM, walks on-chip (`PT_SRAM`), A1 | 6/8 (A1 same code, table over PCIe: 7/8) |
| WB tests only, aliases mapped / no aliases (`NO_ALIAS`) | 12/12, 12/12 |

- Failures are early (undefined vector right after the header text, before any test body) or whole-pass `r` (every word wrong). Adding the `rfirst` diagnostics to the same source made the unmodified test pass 8/8 (and `PT_SRAM` 8/8): the fault moves with code layout / timing, it does not track a test or a mapping.
- Verdict: PTE write ordering and mismatched-attribute aliasing are not the cause (walks on-chip: same failures; alias-free: no failures but the alias build is also clean, so no separation). No Linux patch written.
- Open: layout-dependent early corruption on cold I/D fills at 1000 MHz.
