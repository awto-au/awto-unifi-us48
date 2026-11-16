# Linux on a2 (unit 1 Cortex-A9) (#79)

One kernel image, one initramfs for a1 and a2. Only DTB + bootargs differ. Loader/remoteproc side: [a2-design.md](a2-design.md).

## Memory map (a2 AXI view)
- RAM = hwdb `a2_carveout` (0x08000000, 64 MiB): host DDR low alias via PAXB OARR_0/OMAP_0 (#53). DTS macros come from hwdb (`scripts/build_a2_linux.py`).
- Linux RAM = carveout minus last MiB; last MiB = hwdb `a2_shared` (`hwdb/soc/a2_linux.toml`), `no-map`.
- MiB below it = hwdb `a2_vnet` (`0x0be00000`): inside the `memory` node, `reserved-memory` without `no-map` (mapped write-back, not allocated), rings of `us48-vnet` ([a2-design.md](a2-design.md)). `/init` on a2 loads `us48_vnet` and sets `vnet0` 10.48.0.2/30.
- Kernel at base + 0x8000 (AUTO_ZRELADDR derives the base from where the zImage runs; ARM_PATCH_PHYS_VIRT), reaches ~base + 0x0f30000 with bss.
- zImage at base + 0x1000000, DTB above the kernel end (layout requests in a2-design.md). Built-in initramfs: no external initrd.
- Peripherals (hwdb `a9.toml`, same addresses as a1): GIC dist 0x19021000, GIC cpu 0x19020100, global timer 0x19020200 (PPI 11), TWD 0x19020600 (PPI 13), PERIPHCLK = ARM/2 (conf I, host-timed on unit 1 at 100 and 1000 MHz); DTB value from `build_a2_linux.py --cpu-mhz`.
- Not in the DTS: DDR controller 0x60000000 (stalls a2), QSPI/flash, PCIe, PL310 only with `--l2` (default since 2026-11-16, see "a2 L2"), CCB/APB peripherals.

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
- Page tables, stack and all data cross PCIe on cache misses; L2 absorbs them since 2026-11-16 ("a2 L2" below).
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
- `us48_a2` `cpu_mhz` (default 1600 since 2026-11-16, Dan's choice, #88; 100 was the default 2026-11-15; 0 = leave): E2 sequence of `us48_cpu_switch` through the regmap in `start()` before the halt. 400..600 = fid 6 (VCO 4x, ch0 mdiv 4), 700+ = fid 7 (VCO 2x, ch1 mdiv 2). 1000: GO + lock in 149 us, PERIPHCLK 500 MHz host-timed. debugfs `remoteproc0/ccu` = registers + computed MHz.
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

## No-DCC / no-poll test (#79, 2026-11-15, a2 1000 MHz, AXI 200, fans 100 %)
- rawtest never uses DCC (results in the a2_shared trace); only a1's DCC poller touches a2 debug regs while a2 runs. `us48_a2` param `dcc_poll=0` = no poller after the release; `hdbgen_off=1` = HDBGEN cleared after the release. `a2_raw.py` / `a2_raw_batch.py` take `k=v` params. `a2_raw` reads `pcsr` once after the run (diagnostic only).
- Reproducer = `us48-a2-rawtest-orig` (rawtest.c from fc0b9c3, before the bisect edits; the current rawtest passes 4/4 and 8/8 with diagnostics code).

| Params | ok / runs |
|---|---|
| poller on, HDBGEN on (control) | 0/4, 1/8 |
| `dcc_poll=0` | 1/4, 0/8 |
| `hdbgen_off=1` | 2/8 |
| `dcc_poll=0 hdbgen_off=1` | 0/8 |

- Failures identical with the poller off (undefined vector 0xffff000c, or `pass=` missing). a1 debug access and HDBGEN are not the cause; Linux no-DCC boots not run (step 1 not clean).

## H1 stale predictor / H2 CPU stability (#79, 2026-11-15, a2 1000 MHz, AXI 200, `dcc_poll=0`)
- Entry state (`us48-a2-entryprobe`, `start.S` `ENTRY_PROBE`): SCTLR `0x00c52078` at entry = Z (bit 11) 0, I/C/M 0, V (bit 13) 1; ACTLR 0; Diagnostic 0; MIDR `0x414fc091` (r4p1). Branch prediction is off in every a2fw (nothing sets Z) and the entry code already does ICIALLU + BPIALL before I-cache on; `mmu_on` does D set/way invalidate + TLBIALL before M/C. `boot.S` does the same plus full invalidate with I off first.
- Variants of `rawtest_orig.c` (= rawtest.c from fc0b9c3; `start.S` flags `ENTRY_Z0/Z1/FULL/NOPS`), ok / runs:

| ELF | ok / runs |
|---|---|
| `rawtest-orig` (control) | 0/12 (+0/4 batch, +0/4 in the power-cycle test) |
| `-z0` (SCTLR.Z cleared; same as entry state) | 9/12 |
| `-z1` (Z = 1, branch prediction on) | 10/12 |
| `-inv` (full ICIALLU/BPIALL/TLBIALL/DCISW with caches off before any enable) | 0/4 |
| `-inv-z0` | 3/4 |
| first a2 start after a power-cycle (`a2_up.py`), orig | 0/4 |
| `-n1..-n8` (1..8 nops at entry) | 1/4, 1/4, 2/4, 1/4, 3/4, 1/4, 4/4, 0/4 |

- H1 not supported: Z is 0 at entry (no BTB use), entry invalidation changes nothing (`-inv` 0/4), power-cycled first start fails the same. Pass rate moves with a 1-word shift (0/16 -> 9/12) but not monotonically with the shift (n1..n8): per-build bias plus run noise, not a stale-state effect.
- H2 (`us48-a2-stress400/600/800/1000`, `stress.c` + `stress_asm.S`): loop copied to a2 SRAM, data and counters in SRAM, no PCIe traffic while it runs; ALU chain (mul/eor/ror) + 8 KiB store/load checksum, known answers from `build_a2fw.py`, duration from PMCCNTR = 10 s at the named clock:

| a2 MHz | runs | mismatches (m + r) | passes per run |
|---|---|---|---|
| 600 / 800 / 1000 | 4 each | 0 | 0x1b2e / 0x243e / 0x2d4d (identical every run) |

- H2 not supported: the CPU + L1 are stable at 1000 MHz (40 s total at 1000, 40 s at 800, 40 s at 600). Die max 88 C, LM75 max 66 C. Core voltage: regdb has `TOP_AVS_SEL_REG` (0x02074c00) and `TOP_PVTMON_CTRL_1.PVTMON_SELECT` (CMIC space); not read, H2 is clean.
- Open: the corruption needs PCIe-resident code + data traffic with caches on (rawtest-class); streaming I+D (ifa/ifb/ifd) and SRAM-only CPU load are clean. Next: capture LR_und / SPSR_und at the first undefined (vector via VBAR in SRAM, or a1 halt + ITR read) to see which fetch returned what.

### SRAM-only clock sweep (#79, 2026-11-15, `scripts/a2_sram_sweep.py`, `us48-a2-stress<MHz>`, AXI 200, `dcc_poll=0`, fans 100 %)
- 2 runs per clock, 10 s each (PMCCNTR); mismatches (m + r) 0 in every ok run; log `tmp/logs/a2_sram_sweep.log`.

| a2 MHz | result | passes per run | passes / MHz | die0 / die1 / LM75 max (C) |
|---|---|---|---|---|
| 1000 | ok x4 | 0x2d4d (11597) | 11.597 | 88 / 66-67 / 60 |
| 1200 | ok x2 | 0x365c (13916) | 11.597 | 88 / 66-67 / 60 |
| 1400 | ok x2 | 0x3f6b (16235) | 11.596 | 88 / 66-67 / 60 |
| 1550 | ok x2 | 0x4637 (17975) | 11.597 | 88 / 66-67 / 60 |
| 1600 | ok x2 | 0x487b (18555) | 11.597 | 88 / 67 / 60 |
| 1650 | ok x2 | 0x4abf (19135) | 11.597 | 88 / 67-68 / 60 |
| 1700 | ok x2 | 0x4d02 (19714) | 11.596 | 88 / 67-68 / 60 |
| 1750 | FAIL run 1 | no trace, `pcsr 0x1c000010`, a2 stopped | - | 88 / 67 / 60 |

- Pass count is linear in MHz (11.60 +- 0.01 per MHz), identical across runs: the clock is real up to 1700 MHz.
- 1750: PLL locks (ARM 1750000011 Hz read back), a2 never prints the banner = fails at start, before the SRAM loop.
- Highest clean SRAM-only clock 1700 MHz; first fail 1750 (sweep stopped there; 1800..1950 not tried).
- Die temperature does not move with clock (PVTMON granularity ~0.5 C); die max 88 C, LM75 max 60 C.
- Capacity: a2 SRAM 16 KiB; L2-as-RAM lockdown (128 KiB) not tested. Only small bare-metal code fits; Linux cannot run from SRAM.

### debugfs `capture`: first vector fetch (#79, driver only, no a2 code)
- `remoteproc0/capture`: write `arm [vc] [skip] [ms]` / `disarm`; read = state + dump. Needs `dcc_poll=0`; default limit 30000 ms (expiry logs limit + elapsed). Armed while a2 is offline: DBGVCR (secure + non-secure copies, 0x1a00001a) is written before the release, the thread starts after it. Debug a1 image only (debugfs). Runner: `scripts/a2_cap.py LINUX|<elf> <cpu_mhz> [--skip] [--plain] [--runs N]`.
- `arm`: back-to-back PCSR reads, hit when PCSR is in 0xffff0000..0xffff002f (vector page, PCSR = address + 8). PCIe latency means a2 may be past the vector stub at halt.
- `arm vc`: DBGVCR (offset 0x1c) catches undef / prefetch abort / data abort: a2 halts at the vector fetch itself. Linux takes normal page faults: `skip` resumes past user-mode origin aborts and kernel accesses to user addresses 0x1000..0xbfffffff (DBGVCR cleared for the restart, set again right after), counts them, and captures the first other abort.
- rawtest: its vector page is unmapped, so a vector catch never fires (the vector fetch itself aborts); plain `arm` sees a prefetch-abort loop at 0xffff000c. Use Linux.
- Dump: r0-r14, CPSR, DFSR/DFAR/IFSR/IFAR/SCTLR/TTBR0, banked sp/lr/spsr of und/abt/svc/sys; 8 words at LR-8 as a2 sees them (ldr through its caches) vs the carveout in a1's WC mapping, VA -> PA by ATS1CPR/PAR.
- Ends with ITRen cleared, DBGVCR cleared and a2 reset (not resumed).

## Fault capture and the AXI -> PCIe write-before-read fix (#79, 2026-11-16)
- Setup: debug a1 image (RAM boot), a2 Linux = normal-profile zImage, DTB `quiet` (no earlycon), `dcc_poll=0`, AXI 200, `a2_cap.py LINUX <MHz> --skip`, limit 6 s. Fault = first non-normal abort. Temps: die0 88-89 C, die1 80-81 C.
- Baseline (no fix), runs with a fault / runs: 400 MHz 0/4, 600 0/4, 700 0/4, 800 3/4, 1000 5/6 (+4/4, 3/4 in earlier set-ups). First failing clock about 800 MHz.
- Same site every time: kernel-mode `str` in `mas_wr_node_store` (maple tree), DFAR 0x4, DFSR 0x805 (translation fault, write), code at the PC identical on a2 and in DDR. The parent word of a node reads 0.
- Capture compares 256 B around every kernel-pointer register: a2 view (ldr through its caches) vs a1's WC mapping of the pool; differing words get a DCCMVAC, and after that memory is re-read. Result in every failing run: one clean 32 B line (first line of a maple node) holds zeros on a2 while DDR holds the correct node data (`c06a0ea5 0 1 2 3 4 5 6`). Memory is unchanged 50 us, 1 ms and after a second clean; a2 re-fetch after DCIMVAC returns the DDR value. Dirty lines elsewhere write back normally.
- Verdict: stale read (a line fill returned older data than the earlier write-back of that line), not a lost write. a1 side is not involved: the pool is WC (`dma_alloc_wc`), a1 never writes it while a2 runs, DDR reads are stable.
- Cause: `PAXB_0_PAXB_TX_DEBUG_CFG` (0x18012f20, regdb) reset 0x8: `EP_TX_WR_RD_ORDER` (bit 2) = 0, so unit 1 may issue a read before an earlier write (eviction write-back, then refill of the same line). `RC_TX_WR_RD_ORDER` (bit 3) is already 1.
- Inbound side of unit 0 (a2 reads of DDR): with a1 DDR write load (`dd` to tmpfs) the order bit alone is not enough; unit 0 `RX_DEBUG_CONTROL_0.RX_FREE_ARID_COUNT` = 0 (one inbound read at a time) is needed as well.

| a2 1000 MHz, AXI 200, 6 s | a1 idle | a1 DDR write load |
|---|---|---|
| none | 5/6 fault | 5/6 bad (3 fault, 2 hang) |
| EP_TX_WR_RD_ORDER only | 0/6 fault (1 of 3 hangs in a boot test) | 2/6 fault |
| RX_FREE_ARID_COUNT 0 only | - | 2/6 bad (1 fault, 1 hang) |
| both | 0/6, 3/3 boots | 0/13 (7 + 6), faults skipped 215-238 = userspace reached like the 100 MHz control |

- Both set, no capture: Linux shell 3.3-3.6 s after start, 3/3 at 1000 MHz and 6/6 with the module params; 1600 MHz 2/2 (capture: 0/4 faults at 800, 1200, 1400, 1600). 1650 and 1700 MHz: kernel pointers go wild (garbage DFAR / PC 0x1c000010), a2 hangs; SRAM-only code is clean to 1700 (above).
- Unfixed a1 load at 600 MHz (order off): 1/6 fault (+ 0/4 idle), same stale-line signature.
- Fix in `us48_a2`: `paxb_order` (default 1) sets EP_TX_WR_RD_ORDER before the release; `rx_arid` (default 0 since the sweep below; -1 = leave 3) sets unit 0 RX_FREE_ARID_COUNT while a2 runs and restores 3 in stop. debugfs `remoteproc0/paxb_tx` reads/writes the unit 1 register. `cpu_mhz` default is 1600 (Dan 2026-11-16, #88).
- Open: the 1650 MHz failure is not stale lines (not captured with the dump).

### RX_FREE_ARID_COUNT sweep (#79, #88, 2026-11-16, base 8224728)
- Reliability: debug a1 image (RAM boot), `a2_cap.py LINUX <MHz> --skip --limit-ms 6000 --runs 6 rx_arid=N`, `paxb_order=1`, a1 DDR write load = `dd` 48 MiB to tmpfs in a loop (two loops were running), a2 Linux normal zImage, AXI 200. Fault = captured abort (`state done`, DFAR 0x4); clean = limit expiry with userspace reached.

| rx_arid | 1000 MHz faults / runs | 1400 MHz faults / runs |
|---|---|---|
| 3 (reset, order only) | 2/6 (earlier) | - |
| 2 | 1/6 | 0/6 |
| 1 | 4/12 (0/6, then 4/6) | 0/6 |
| 0 | 0/6 (+0/13 earlier) | 0/6 |

- Only 0 is clean: default `rx_arid=0`.
- a2 Linux start (normal image, `dcc_poll=1`, a1 idle, 15 s limit for the shell): rx_arid 2 hung in 7 of 13 starts (stub `aJ`, no Linux output), rx_arid 3 0/2, 1 0/1, 0 0/1 (+0/1 with the new default); small samples.
- Throughput cost: none measurable (table in core-bringup.md, "RX_FREE_ARID_COUNT cost").

## Reboot with a2 running / unit 1 wedged (#90, #91, 2026-11-16)
- Trigger: a2 read of CMIC COSLC `0x48023000` (hwdb_gaps.py adhoc) wedges the unit 1 CMIC AXI slave; a2 stays `running`.
- Before the fix, `reboot` with a2 running hung in `device_shutdown` (no `Restarting system`): pcie-iproc shutdown asserts PERST# while the DCC poller has a read in flight. a1 stalls, SP805 bites, uboot-1 loops at `DRAM: 256 MiB` (stalled PCIe transaction survives the reset; hangs before `soc_pcie_hw_init` prints). Manually asserting PERST# on the wedged board (CLK_CONTROL = 0 with a2 polling) reproduces it.
- Fix: `us48_a2` reboot notifier calls `rproc_shutdown()` (DCC stop, halt best effort, a2 reset) before device shutdown. a1 then restarts, uboot-1 passes, awto-uboot cycles PERST# and links up.
- Not fixed: the CMIC stays wedged across PERST# and a2 reset. awto-uboot `fan: probing unit 1 BAR at 0x08040000` reads BAR2 and stalls, SP805 loop until a relay power cycle.
- A wedge with a1 CMIC users loaded (pvtmon, xe, fan, gpio) stalls a1 itself on the first CMIC read; the test (`warm_reboot_test.py w`) unloads them first.
- Test: `scripts/warm_reboot_test.py w` (3 of 3: banner 3.0 s, awto-uboot reached, then the fan-probe loop).

## a2 L2 (PL310) and the stop/start halt (#85, #88, #102, 2026-11-16)
- DT: `build_a2_linux.py` adds the PL310 node by default (`--no-l2` omits it). Unit 1 PL310 resets with aux way size [19:17] = 0 (reserved): the kernel sized it 8 ways x 8 KB (64 kB), aux `0x46000001`. The unit 0 PL310 reads 8 x 16 KB, aux `0x4e120001`, because uboot-1 `lowlevel_init` (`0x1c0005b8..0x1c0005c8`, literal `0x0a120000` at `0x1c001300`) writes `(aux & ~0xf0000) | 0x0a120000`.
- a1 cannot reach the PL310 of unit 1 (reads through the regmap return 0, id included); the boot stub does the write (`a2fw/boot.S`, flag `US48_A2_BOOT_F_L2AUX`, `us48_a2 l2_aux=1` default): CTRL = 0, aux as stock, then the kernel invalidates and enables. With it a2 prints `L2C-310 cache controller enabled, 8 ways, 128 kB`, `AUX_CTRL 0x4e120001` (same as a1), id `0x410000c9` (r3p3; errata lines as on a1: 769419 is unconditional in the kernel for any revision, the revision-gated ones do not apply). Kernel adds early BRESP, full line of zeros (ACTLR bits set by `l2c310_starting_cpu`), NS lockdown; nothing else is written.
- Maintenance reaches the PoC: `us48_vnet` uses the DMA API (`dma_sync_single_*`, BIDIRECTIONAL = clean+invalidate incl. `outer_flush_range`), so the publish read-back misses L2 and queues behind the write at PAXB (`paxb_order`). vnet ping and 10 MiB TCP both ways ran clean with L2 on.
- Stop: no L2 flush needed: a2 is reset, memory it owned is rewritten at the next start; the stub switches a leftover enabled L2 off before the kernel.
- Results (a2 1000 MHz, AXI 200, debug a1 RAM boot, same session): vnet unchanged within noise (ping a1 -> a2 4.7-6.8 ms vs 5.0, TCP 1.86-1.88 / 1.82-1.90 vs 1.85 / 1.86-2.00 MB/s: a1 debug image bound); a2 boot to vnet up 2.1 s vs 3.1 s; a2 memory bandwidth in core-bringup.md. Stale-read check: `a2_cap.py LINUX 1000 --skip --limit-ms 6000` with two a1 `dd` 32 MiB tmpfs write loops: 5 of 5 runs clean (userspace reached, no abort), 3 more with one loop, 0 faults (run 6 hit ENOMEM on a1 from the load, not an a2 fault).
- Bare-metal `bustest` has no outer-cache maintenance (its NC-alias truth check would flag L2), so there is no L2-on bustest row; the a2 Linux `us48bench` rows replace it.
- Halt time (`us48_a2`, debugfs `halt_n/halt_fail/halt_last_us/halt_max_us`): 30 stop/start cycles each at 100 MHz, 1000 MHz L2 off, 1000 MHz L2 on, 12 more under TCP traffic: typical 15-20 us, outliers 65-196 us, one 2931 us. `HALT_WAIT_US` = 3700 (1.25 x 2931).
- Wedge seen once (normal image, after a2 Linux had run vnet traffic, stop then start): DSCR `0x3004002` (running, not halted) persisted through every later start; a2 then sat at PC `0x08000000` for any firmware; only a power cycle cleared it. Not a slow halt. `us48_a2` stop restored unit 0 `RX_FREE_ARID_COUNT` to 3 before halting a2 (reads in flight); it now halts and resets first. Unproven cause: 70+ stop/start cycles with the new order (12 under traffic) did not wedge; the old order wedged within about 60.
- Normal image check, 3c820ff (2026-11-16, a1 `7.4.0-rc1-norm+` from kernel0 crc 82cb3d47, a2 normal zImage, 1000 MHz, AXI 200, `vnet_test.py`, 10 pings, 10 MiB TCP):

| a2 | start to vnet up | ping a1->a2 / a2->a1 avg ms | TCP a1->a2 / a2->a1 MB/s | us48bench 8M rd/wr/cp MB/s |
|---|---|---|---|---|
| L2 on | 1.5 s | 0.76 / 1.08 (cold), 0.77 / 1.00 (RAM boot) | 5.04 / 4.35 (cold), 5.12 / 4.33 (RAM boot) | 67 / 254 / 66 |
| L2 off | 2.3 s | 1.02 / 1.95 | 2.03 / 2.07 | 23 / 230 / 23 |

- a2 1600 MHz, L2 on (f4ce402, 2026-11-16, normal image from kernel0 crc d8f3e289, awto-uboot slot crc 79637aa2, two cold boots, `vnet_test.py` + `a2_bw.py`, 10 pings, 10 MiB TCP; cpu_mhz default 1600, DTB `us48-a2-1600-l2.dtb`):

| a2 | start to vnet up | ping a1->a2 / a2->a1 avg ms | TCP a1->a2 / a2->a1 MB/s | us48bench 8M rd/wr/cp MB/s |
|---|---|---|---|---|
| cold boot 1 | 1.49 s | 0.70 / 1.25 | 5.43 / 4.48 | 67 / 256 / 66 |
| cold boot 2 | 1.48 s | 0.73 / 1.10 | 5.55 / 4.52 | not run |

- Normal image has no debugfs: halt stats come from the debug image, same commit, a2 1000 MHz L2 on, `a2_halt_cycles.py` 30 cycles: `halt_n` 60, `halt_fail` 0, `halt_max_us` 204 (typical 15-25 us, one 93). 10 more stop/start cycles with a 10 MiB TCP transfer running: n 83, fail 0, max 204.
- Seen once, normal image, a2 L2 on: `a2 halt: DSCR 0x3004002, limit 3700 us, elapsed 8980 us, ret -110` on a start after a stop; the next start worked, no power cycle. The a2 shell had been stuck on an `nc` (console only; vnet still pinging) before that stop. Elapsed exceeds the 3700 us limit by 2.4x, so it is a slow halt, not the persistent wedge (#102).
- Seen once, debug a1 (a2 normal zImage, L2 on, 1000 MHz): a2 oops in `irq` setup at boot (`NR_IRQS`, NULL write at 0x4, pc `0xc0423d5c`), next boot clean. About 14 L2-on a2 boots this session.

## a2 boot oops at 1000 MHz and the DCC poll (#103, 2026-11-16)
- The 6 % NULL write (pc `0xc0423d5c` = `mas_wr_node_store`, after `NR_IRQS`) ends in the panic delay loop: all 8 PCSR samples = `0xc04136d8` (`__loop_delay`, System.map of build-normal), identical for oops boots with DCC text and for `dcc_poll=0` no-marker boots. A no-marker boot without DCC is an oops (debug a1 RAM boot, 60 boots per config, blocks of 15: ctl 5 oops, `dcc_poll=0` 2 no-marker, all at that PC; no hang or other failure).
- Pooled with the earlier normal-image counts: poll on 12/180, `dcc_poll=0` 5/180 (Fisher p about 0.14): no demonstrated DCC effect.
- Normal image, 1000 MHz, ping marker, blocks of 15: ctl 2/30, `dcc_late_us=20000` 1/30, DSCR-only poll (`dscr_only=1`, DTB `cap1000-l2.dtb` = no earlycon) 0/30; poll 100 ms makes a2 `/init` die (no hvc0), not an oops. Mechanism is not the DTRTX reads, the rate, or earlycon blocking.
- Clock (debug a1, L2 on, ping marker, 15-boot blocks): 1000 MHz 1/30, 1200 0/30, 1400 0/30, 1600 1/120 (the 1600 oops: same NULL write at 0x4, pc `0xc0423d5c`, but at 0.66 s after the timers, not at `NR_IRQS`). `a2_cap.py LINUX 1600 --skip` under two a1 `dd` tmpfs loops: 6 clean (userspace, limit expiry), 2 module-load prefetch aborts at 0xbf00004c (known false positive), 2 runs ENOMEM on a1 (load too large; rerun with 16 MiB loops).
- Tools: `a2_ram` (sysfs bin file of the `us48_pci.a2.0` device, raw a2 pool read while a2 is started) and `scripts/a2_dmesg.py` (finds the printk ring in a 16 MiB dump, prints the text). a2's dirty D-cache lines are not in DDR: after an oops the ring is incomplete from a1's side, so `a2_ram` shows the log of a boot that ran, not a post-mortem. `a2_boot_cycles.py --diag` prints PCSR (debug image) and RAM milestones for each non-ok boot.
