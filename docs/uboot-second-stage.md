# Second-stage mainline U-Boot with network (design, issue #46)

Goal: a mainline U-Boot, chainloaded from stock `usw-v1.1.6.122`, with a polled CMIC-DMA Ethernet driver, so test kernels are TFTP'd to RAM (no 9.9 KiB/s `loady`, no kernel0 flash wear).

- Design only. No code, no hardware touched. Nothing below was run on the board.
- **C** = confirmed (cited doc/source), **I** = inferred / untested.
- Models: st1008f `docs/UBOOT-MAINLINE.md` + `DEFAULT-BOOT.md` (`/mnt/2tb/git/awto-tl-st1008f/docs/`), UNVR `docs/uboot-port-plan.md` (`/mnt/2tb/git/awto-unvr/docs/`).
- Sources of facts: [uboot.md](uboot.md) (stock U-Boot), [driver-internals.md](driver-internals.md) (`DI §n` below), [prior-art.md](prior-art.md) "Chip family", [board.md](board.md).

## Decisions

| Item | Choice | Why |
|---|---|---|
| Network port | **unit 0 (BCM53344, AXI CMIC `0x48000000`) copper front 1** (logical 2, phys 9, GPHY bus 0 addr 9) | AXI only: no PCIe RC driver, no BAR/remap (DI §8); no firmware blob; GPHY init = plain MIIM (DI §4, §7) |
| Rejected: unit 0 SFP 51/52 | phys 26/28 = TSC cores | TSC init needs the 32 728 B ucode download (DI §9). Vendor firmware blob in a public repo + 1G optics not fitted today (51 has a 10G-SR in a 1G cage) |
| Rejected (for now): unit 1 front 50 (10G DAC) | verified live (DI §13.5) but only as a warm-boot fix-up | Cold needs: mainline `pcie_iproc` has no HR2 (PAXB v1) compat (C, only `paxb-v2`/`paxc-v2`), HOSTMEM remap (DI §8), full TSC stage 0-2 + ucode (DI §9). Kept as milestone M7 (10G TFTP) |
| Switch state at entry | **cold** | stock U-Boot never touches the switch (uboot.md §5 "Switch fabric: not initialised", C). The driver runs the full chip init and must not assume stock Linux ran |
| Entry mechanism | stock `bootm` of a legacy uImage (type kernel, arch ARM) holding `u-boot.bin` | stock does the clean handoff for free (below) |
| MAC | EEPROM eth0, flash `0x1ff0000` + 0x00 (6 B) | DI/board.md: `ubnthal` reads it from there (C). Fallback: locally administered random + warning |
| Test IP | `192.168.25.207` (`us48-boot`, registry `planned`), serverip = host | `docs/network/ip-registry.toml`. MAC column still blank: fill with the EEPROM MAC |

First-port caveat: DI §12 is **open** for exactly this port (front 1: PHY link up, GPORT0 MIB stayed 0 with UMAC RX enabled, #47). Steps DI §12.5 are the candidate fix. If it still fails at M3 the fallback is unit 1 front 50 (M7 work pulled forward).

## 1. Chain / boot flow

### Handoff state (C, uboot.md §4)
- `bootm` on a Linux-type uImage ends with: IRQ+FIQ masked, I-cache off + invalidated, D-cache flushed + off, MMU off, PL310 L2 disabled, SVC mode, `wdt_stop` already done (private watchdog disabled), r0 = 0, r1 = 3000 (machid), r2 = `0x8000`.
- Our U-Boot therefore starts as if it were Linux: no cache/MMU/watchdog clean-up needed. Unlike st1008f (`go`, cache hazards) this needs no custom handoff code.
- Image header: ih_os Linux (5), arch ARM (2), type kernel (2), comp none, **load = entry = `TEXT_BASE`**. Stock checks magic + hcrc + dcrc only (uboot.md §1, C), no signature.
- `bootm` ATAGs are written at `0x8000` after the copy (uboot.md §4, C): harmless, our entry ignores r2. Keep `TEXT_BASE` away from `0x8000..0x8200`.

### Addresses

| Item | Value | Basis |
|---|---|---|
| Image in RAM before `bootm` | `0x01000000` (`loadaddr`) | stock default; `loady`/`sf read` target (uboot.md §6, C) |
| `TEXT_BASE` (link = load = entry) | `0x04000000` | inside the free low-alias area `0x01000000..0x07030000` (C, uboot.md §6), clear of ATAGs `0x8000`, kernel `0x18000`, image copy (`0x01000000` + <1 MiB) and stock's ubnt-app copy `0x07030000` |
| Init stack | `0x03f00000` top (`CFG_SYS_INIT_SP_ADDR`) | below `TEXT_BASE`, above a ~12 MiB test kernel at `0x01000000` |
| DRAM bank | `0x60000000`, 256 MiB fixed (`dram_init`) | stock hard-codes the same (C, uboot.md §5). Low 128 MiB alias at `0x0` (I, matches stock `mem=128M@0x0`; `boot-ram` already boots kernels at `0x18000` through it) |
| Relocation | to `ram_top` = `0x70000000` (top of the bank) | stock's own copy at `0x6ffa0000` is dead after the handoff (C); after relocation `0x04000000` is free again for the kernel |
| TFTP / kernel buffer | `0x01000000`, `bootm` copies to `0x18000` | same as `boot-ram`; uImage load/entry `0x18000` rule unchanged (uboot.md §4) |
| DMA window | 1 MiB inside the bank, mapped non-cacheable (`mmu_set_region_dcache_behaviour`) | no L2/CMIC-coherence evidence (I); avoids flush/invalidate bugs. Descriptors + packet buffers live here |

- Low alias outside the DRAM bank: bootm to `0x18000` runs through U-Boot's memmove, no bank check on legacy images (I). If mainline complains, add a second `bi_dram` entry `0x0/128 MiB` and set `bootm_low=0`.
- Low alias is device-mapped (uncached) in our own page table unless added as a bank (I). Cost: 6 MiB copy at uncached speed, well under a second.

### Low-level init
- `CONFIG_SKIP_LOWLEVEL_INIT` (no PLL, DDR, clock, cache reset): stock already did DDR + CPU clock (uboot.md §5, C).
- **Skip**: DDR shmoo, QSPI re-init of stock's `mtd2` writes, the 30 s watchdog arming (stock `wdt_arm` is in stock's `board_init_r`, not ours).
- **Do**: own page table + D-cache on (mainline `dcache_enable`), `ARM global timer` or SP804-class tick for `udelay` (see section 3), UART1 `0x18000400` NS16550 at 115200 (C, uboot.md §3). Console clock rate to be read from the stock divisor, not guessed (uboot.md Open items: UART input clock not measured).
- `reset`: stock's table handler is NULL (uboot.md §3); our `reset` command writes the iProc chip reset (to be found, I), else documented as power-cycle only.

### Watchdog (C, uboot.md §2)
- Stock arms the Cortex-A9 private watchdog (`0x19020620`, 30 s, reset mode) and `bootm` stops it (disable keys `0x12345678`, `0x87654321` at `+0x634`, control 0). So our stage starts with **no watchdog running**.
- Policy: stay disarmed while at the prompt / during TFTP. Optional `wdt arm <s>` command, petted in the poll loops, for unattended `ramload`; always disarmed before our own `bootm` (Linux arms its own).
- The task's "SP805": the stock U-Boot code uses the A9 private watchdog; SP805 is not referenced in uboot.md (I: it is the Linux-side iProc watchdog). Confirm which one OpenWrt uses (`openwrt-port.md`) before the DT.
- Hang recovery today = power cycle. A stuck switch init therefore needs the poll timeouts in section 2.

### Where our image lives

| Mode | Command chain | Flash writes |
|---|---|---|
| **RAM (default, M1-M5)** | reboot → stock prompt → `loady 0x01000000` (460800, ~1 MiB image ≈ 25 s) → `bootm 0x01000000` | none |
| Flash slot (later) | stock prompt → `sf probe 0; sf read 0x01000000 <slot> <len>; bootm 0x01000000` | one-time write of the slot |

- Slot candidate: **head of `kernel0`** (`0xE0000`, 15 MiB, test slot, project rules allow writing it). Once the network works, test kernels no longer go through `kernel0`, so it can hold the chain image permanently (hdr + ~0.6-1 MiB, well under `KERNEL0_LEN` `0xF00000`).
- Stock `ubntboot` (bootsel) needs no change: it boots the active slot (kernel1, stock) by default; our chain runs only from `dev.py` (prompt, key pressed in the 3 s window). A power-up auto-chain (st1008f `DEFAULT-BOOT`) would need `bootsel force-fallback` / `force_bootsel` and `saveenv` = **forbidden** by CLAUDE.md. Out of scope.
- Never touch `kernel1`, the bootsel record (`0x1ee0000`), `u-boot`, EEPROM, env (CLAUDE.md).
- Image size budget: st1008f `u-boot.bin` is 456 593 B with the net stack (C, their doc); expect 450-550 KiB here (I).

## 2. Network driver design (U-Boot DM)

Shape: `drivers/net/bcm-hr2-cmic.c` (DM_ETH, polled, legacy `NET_LEGACY` stack like st1008f), one switch-core part + thin U-Boot glue. Optional later: shared register/descriptor header with the Linux driver (#47), st1008f `shared/` pattern.

### Bring-up sequence for unit 0 front 1 (all steps cold, idempotent)

| # | Step | Detail | DI § | Tag |
|---|---|---|---|---|
| 1 | CMIC endianness + `CMIC_CPS_RESET` pulse, 10 ms, redo endian init | OM `_bmd_reset`. Resets only if unit 0 state is unknown (cold). Risk: reaches the HiGig/unit-1 side? Not traced | §2 reset 1 | C (OM), I (side effects) |
| 2 | SBUS ring map (4 words), `CMIC_SBUS_TIMEOUT=0x7d0` | `0x11122200`, `0x00430000`, `0x00005004`, 0 | §1, §2 reset 3,6 | C |
| 3 | `TOP_SOFT_RESET_REG` release: XLP, QSGMII2X, GP0-2, `QGPHY_RST_L=0xf`; then EP/IP/MMU | no PTP/BS PLL work (not needed) | §2 reset 5,7 | C |
| 4 | No IDDQ writes for 0x8344 | `TOP_QGPHY_CTRL_0` untouched; verify `EXT_PWRDOWN` bit for lane 0 read-only | §2 step 10, §10, §12.3 | C |
| 5 | Pipe reset: ING/EGR HW_RESET, poll DONE | OM limit 5 ms | §2 init 1 | C |
| 6 | Port maps (phys↔logical, MMU), core PLL load, TDM table (`hu2_tdm_cascade`, 75 entries; same table as stock config 10), `IARB_TDM_CONTROL` | | §2 init 2-4, §10 | C |
| 7 | MIIM clock: `CMIC_RATE_ADJUST*` DIVISOR 40 | 5 MHz MDC | §2 init 5 | C |
| 8 | MMU: `CFAPCONFIG`, `IP_TO_CMICM_CREDIT_TRANSFER`, HOL limits for CPU, `MMUPORTENABLE` (CPU + port 2) | | §2 init 7, §5 | C |
| 9 | Default VLAN profile, `GPORT_RSV_MASK`, `GPORT_CONFIG` (CLR_CNT, GPORT_EN) | | §2 init 8-9 | C |
| 10 | Port 2: `PORT_TAB[2]`, `EGR_PORT`, `EGR_ENABLE[phys 9]`, MAC reset pulse (`0x01802058` → `..5B` → `0x0180005B`) | | §2 init 10, §12.5 step 7 | C |
| 11 | GPHY (bus 0, addr 9): top reset, DSP clock, `DSP_TAP10=0x011b`, SUPER_ISOLATE=0, `MODE_SEL=0`, `POWER_DOWN=0`, 1000FD + AN (`MII_CTRL=0x1340`), EEE off. RDB enter/exit around RDB regs | MIIM `0x02000000 \| A<<16 \| data` (bus 0, C22; I for bus-0 form) | §4, §7, §12.4 | C (OM + stock), I (HW) |
| 12 | VLAN 1 = {CPU port 0, logical 2} **only**; STG forwarding for port 2 (and `EGR_VLAN_STG`); `EGR_VLAN[1]`; `EPC_LINK_BMAP_64` bits 0 and 2 | other 47 ports never enabled: no loop onto the house LAN | §6, §5 "What gets a frame" | C (recipe), I (HW) |
| 13 | CMIC DMA init: abort stale channels, `CMIC_RXBUF_EP_RLS_CRED`, `COS_CTRL_RX_*[ch1]`, ch0 TX / ch1 RX control | `DMA_CTRL` bits 5:4 = 0 (LE host, I) | §3, §5 | C (OM+SW), I (LE) |
| 14 | Link poll: BMSR link + `MAC_MODE` bit 5 LINK_STATUS | §12.3 checks 1, 12 as debug dump | §12.3 | C |

- Steps 5-10 are chip-wide reset-defaults + one port; stock's 24-port bring-up (PoE, other GPHYs, XLPORT, HiGig) stays out. PoE is not touched (off at boot, CLAUDE.md).
- Open DI §12 question (GPHY→MAC path) is resolved at M3 by the §12.5 sequence; log the §12.3 checks on failure.
- Not in scope: fans (U-Boot never touches them, uboot.md §5; init of the switch adds heat, keep sessions short, watch temperature per CLAUDE.md), LEDs, EEE, MIB stats beyond a debug counter dump.

### Packet path (polled)

| Part | Design | DI § |
|---|---|---|
| RX | 4-8 RX DCBs chained, buffers 2 KiB (frame limit 1600), ch1, `DMA_EN`; poll DCB `w15` DONE (bit 31); length `w15[15:0]`; source port `w12[7:0]` must be 2; re-arm by rewriting the DCB. Initial design: single-DCB re-arm per packet (OM recipe) then chain if too slow | §5 RX, §3 |
| TX | OM two-DCB form (16 B head with HG header + rest), dest logical port 2, ch0, poll `DMA_STAT` bit 0; or normal switched form (HG=0) via the ingress pipe (I). Start with the HG form (C, `_bmd_tx`) | §5 TX |
| Cache | DMA window uncached; no flush/invalidate calls. If the window is cached later: flush before TX, invalidate before RX poll | |
| Admit filter | accept RX only with `SRC_PORT == 2`; drop reason words ignored | §5 |
| Stop / `bootm` | on `eth_ops.remove` or before `bootm`: clear `EPC_LINK_BMAP_64` bit 0, abort ch0/ch1, `DMA_EN` off (OM `rx_stop`). No DMA into memory Linux reuses | §5 abort |
| Status | `ethstat` command: DMA_STAT/CTRL, DCB state, `MAC_MODE`, MIB (`GRPKT`), ring indexes | §12.3 |

### Waits (every bound with basis; first run logs the elapsed time, then tightened)

| Wait | Limit | Basis | On expiry |
|---|---|---|---|
| S-channel op | 20 µs polled, hard cap 1 ms | expected ≈ µs (one SBUS round trip, I); SW uses `schan_timeout_usec` (not measured); SBUS timeout reg 2000 cycles | abort bit (`SCHAN_CTRL` b2), log op + header + `SCHAN_ERR`, fail init |
| MIIM op | 16 µs | 64-bit frame at 5 MHz MDC = 12.8 µs, ×1.25 | log PHY addr + reg, fail |
| Pipe reset DONE | 5 ms | OM `_bmd_init` (C) | log both reset regs, fail |
| PLL / reset settle | 10 ms | OM + SW reset delay (C) | n/a (fixed delay) |
| DMA abort | 1 ms | one in-flight descriptor, tens of µs (I); stock backstop 500 ms | log ch, `DMA_STAT`, `DMA_CTRL` |
| TX complete | 1 ms | 1518 B at 1 Gb/s = 12 µs + DMA, tens of µs for 64 B (I) | drop frame, count, log |
| Link up (AN) | 4 s | 1000BASE-T AN typically 1.5-3 s (I), ×1.25 | print "no link", leave NIC down |
| `udelay` source | 1 µs granularity from the timer | | |

### MAC address source
- `board_late_init` / `eth_ops.read_rom_hwaddr`: `sf read` 6 B at `0x1ff0000` (EEPROM region, read-only, C board.md `ubnthal`), reject all-0/all-F/multicast, else random locally-administered MAC + warning.
- Stock env default `ethaddr 00:90:4c:06:a5:72` (uboot.md §3) is a Broadcom placeholder: never used.
- Env in our U-Boot: `ENV_IS_NOWHERE` (RAM, as st1008f); `saveenv` disabled. Defaults `ipaddr=192.168.25.207`, `serverip`, `netmask`, `autoload=no`.

### Code size estimate (I)

| Part | Lines C | Text |
|---|---|---|
| S-channel + register/table accessors | ~250 | 2 KiB |
| Chip reset + init (steps 1-10, TDM table) | ~500 | 4 KiB |
| VLAN/STG/PORT_TAB writes (section 6) | ~200 | 2 KiB |
| GPHY + MIIM | ~250 | 2 KiB |
| CMIC DMA RX/TX | ~300 | 3 KiB |
| DM_ETH glue + `ethstat` + MAC read | ~250 | 3 KiB |
| Board / mach / DT / defconfig | ~300 | 1 KiB |
| **Driver total** | **~2000** | **~15-20 KiB** |

- `u-boot.bin` total ~450-550 KiB (net, bootm, sf, loady) vs 456 KiB at st1008f (C). Register names via a small header derived from `regdb/` (offsets only, no vendor text).

## 3. Reuse plan

### Mainline U-Boot (store `/mnt/1tnvme/git-mirrors/u-boot/u-boot`, v2026.07-class; `git worktree add`, never clone)

| Piece | Status in mainline | Use |
|---|---|---|
| Arch/mach for iProc / HR2 / NS2 | **none**: `arch/arm` has `mach-bcm283x`, `mach-bcmbca`, `mach-bcmstb` only (C, listing) | new minimal `ARCH_BCMHR2` scaffold (like st1008f `mach-otto`: `uboot-port/` tree + `scripts/build_uboot_mainline.py` patching hooks idempotently into a pinned worktree) |
| NS16550 serial (DM) | yes | UART1 `0x18000400` DT node |
| SPI flash | `drivers/spi/iproc_qspi.c` (`brcm,iproc-qspi`, BSPI+MSPI) (C) | `sf probe/read` for EEPROM MAC and flash slot. Check it against the stock-used window `0x18027000` (uboot.md §5). Read-only guard (no `sf write/erase` except dev opt-in) as st1008f |
| GPIO | `drivers/gpio/iproc_gpio.c` (C) | optional (system LED GPIO 6/7) |
| Timer | `drivers/timer/arm_global_timer.c`, `arm_twd_timer.c` (C) | A9 global timer at PERIPHBASE `0x19020200` (stock's private watchdog is at `0x19020620`, same PERIPHBASE, C) for `udelay`; the clock rate is PERIPHCLK (stock assumes 200 MHz, I: uboot.md Open items) |
| PCIe | `drivers/pci/pcie_iproc.c` (`paxb-v2`, `paxc-v2` only, C) | **not for M1-M6** (unit 0 is AXI). For M7: add an HR2/PAXB-v1 variant (Linux `pcie-iproc.c` register table; OpenWrt patches 950-952 already adapt Linux, openwrt-port.md) |
| Net stack | `NET_LEGACY`: `dhcp`, `tftpboot`, `ping`, `wget` | as st1008f; lwIP optional later |
| bootm / uImage | yes | LZMA only if kernels shrink; OpenWrt initramfs image is uncompressed uImage today |
| Watchdog | `wdt` uclass | A9 private wdt is not mainline (arm_twd is timer only): small `wdt` driver or a `wdt` command (I) |

### From st1008f (`/mnt/2tb/git/awto-tl-st1008f`)
- **Scaffold + build script pattern**: `uboot-port/` staged into a pinned tree, hooks patched idempotently, out-of-tree build, outputs under `tmp/tftp/`, log `tmp/logs/build_uboot_mainline.log`; store reached through a gitignored symlink (`tmp/u-boot-store`, env override), as CLAUDE.md #45.
- **`ramload` flow** (`scripts/st1008_ramload.py`): power-cycle → stock prompt → load → `go`/`bootm` → match banner. Adapt: stock `loady` + `bootm` (above) instead of stock TFTP + `go`; reuse `Console.run`, `send_checked` (long lines lose chars at 115200, st1008f #31), banner regex.
- **Chain-image format**: st1008f builds a raw slot image with CRC verified by stock `crc32`/`cmp`. Here the format is a plain legacy uImage (`mkimage`-style header, OpenWrt's `mkimage` already used for the initramfs image), so stock `bootm` does the CRC and handoff.
- **Net test**: `uboot-test net` = ping gateway, `dhcp`, `tftpboot` + CRC32 vs host, repeated 8x; port as `dev.py uboot-test net` (TFTP of the U-Boot image itself). Reference rate 10.6-19.8 MiB/s at 10G (C, their doc); expect lower at 1G polled (~5-10 MiB/s, I) = a 5.6 MB kernel in ~1 s.
- **Debug lessons to copy** (C, their doc): all RX rings armed or none (null ring left a runout bit), release RX counters per frame, doorbell loss on TX (re-kick after a bounded wait), never reset the chip with the NIC reset (separate reset bits), `ethstat` command, `ethrxany` debug switch.
- **Shared-core idea** (#83): one `us48-cmic.h` for Linux (#47) and U-Boot. Only after the first packet works.

### Broadcom iProc U-Boot (clayface MX64 tree, `arch/arm/cpu/armv7/hurricane2`, `include/configs/hurricane2.h`; same base as stock, prior-art.md)
- **`lowlevel_init` / HR2 CPU code**: reference for UART, QSPI, clock-reading, PERIPHCLK; DDR/PLL parts not needed (stock did them). Licence: Open Broadcom ISC-style + GPLv2 U-Boot: read, don't copy wholesale.
- **`bcmiproc_eth*.c`**: AMAC driver. **Not usable** for the network (AMAC SerDes never locks, not wired on this board: #46 comments, prior-art answer 1). Useful only as a DMA-ring/DM_ETH skeleton; the CMIC DCB layout is different (DI §3, §5).
- Anything it has for PCIe RC init on HR2 is the template for M7 (stock U-Boot already does RC init + SerDes via CCB-MDIO `phy_read/phy_write`@0x1c023c64, C uboot.md §5).
- OpenMDK (`bmd/PKG/chip/bcm56150_a0`, `bmd/PKG/arch/xgsm/xgsm_dma.c`) is the primary source for the sequence (DI sources); Broadcom permissive licence (use/modify with notices, no binary RE).

## 4. `dev.py` integration

New commands in `scripts/dev.py` (`@command`, `describe`), helper in `scripts/`:

| Command | Does | Notes |
|---|---|---|
| `dev.py build-uboot` | stage `uboot-port/` into the pinned worktree, build, write `tmp/tftp/us48-uboot.bin` + `us48-uboot.img` (uImage, load=entry `0x04000000`) | log `tmp/logs/build_uboot_mainline.log`; fails if `.bin` > slot budget |
| `dev.py uboot-chain [--from-flash]` | reboot → stock prompt (`_session_at_uboot`) → `loady 0x01000000` at 460800 (or `sf read` the kernel0 slot) → `bootm 0x01000000` → wait for our banner + `us48-uboot>` prompt | no flash write by default; refuses to run if the console shows stock Linux up (coordinator sessions) |
| `dev.py ramload <image> [--bootargs ...]` | needs the second stage up (calls `uboot-chain` first if at stock): `setenv ipaddr/serverip`, `tftpboot 0x01000000 <image>`, `bootm`, then the existing boot watch (`_bootm_watch`) | image from `tmp/tftp/` served by a host tftpd; replaces `boot-ram` (`loady`) for kernels |
| `dev.py uboot-chain-install` (later) | `sf write` the chain image to the head of `kernel0` + read-back CRC | same guards as `flash-kernel0`; never `kernel1`/bootsel/u-boot/EEPROM |
| `dev.py uboot-test net` | ping, `dhcp`, `tftpboot` + CRC32 vs host, 8 repeats | after M4 |

- Host side: tftpd for `tmp/tftp/` on the dev host (port 69, host IP on the 192.168.25.0/24 LAN; `us48-boot` .207 for the board). Link path: front 1 → sw1 → house LAN (current cabling, 2026-11-12). Our port is isolated (only CPU + front 1), so no loop even though front 1 and the uplink (50) are both cabled.
- `serverip` / `ipaddr` set per boot by `dev.py` (RAM env), not saved. Claim any other address in the registry first.
- Coordinator note: `uboot-chain` reboots the unit into stock U-Boot; never run it during live kernel tests.
- Keep the existing `boot-ram` / `flash-kernel0` / `boot-flash` paths as the fallback when the second stage is broken.

## 5. Milestones (each ends with a test and a log under `tmp/logs/`)

| M | Goal | Exit criterion | Needs |
|---|---|---|---|
| M0 | Pre-checks at the **stock** prompt (read-only, by the coordinator, not run) | `pci` output (is the RC/link up, BCM53346 `14e4:8346` seen); `mw.l 0x01000000 ...` + `md.l 0x61000000 1` alias test (uboot.md first-boot step 3); `md.l 0x19020200` (global timer present) | existing console |
| M1 | **Serial prompt**: scaffold, `ARCH_BCMHR2`, ns16550, global timer, RAM env, `loady`/`md`/`mw`/`bootm` | `dev.py uboot-chain` reaches `us48-uboot>`, `md` works, `bootm` of the OpenWrt initramfs (via `loady`) boots as today | build script, uImage wrapper |
| M2 | **SPI flash**: `iproc_qspi` + `sf probe/read`, EEPROM MAC read (read-only guard on) | `sf read` of `0x1ff0000` 6 B == stock `ubnthal` MAC; `crc32` of a kernel0 range == stock `mtd` CRC | M1 |
| M3 | **Net link**: S-channel read of `UMAC_REV_ID` (DI §1 worked example, `0x00030601`/`..0501`), chip init, GPHY, DMA; first RX frame (ARP broadcast from the LAN) in the RX DCB, first TX frame seen by `tcpdump` on the host | RX count > 0 and a host `tcpdump` shows our ARP reply; `ethstat` clean. **Resolves DI §12** (or triggers the unit-1 fallback) | M1, M2 |
| M4 | **DHCP / TFTP**: `ping`, `dhcp` (or static), `tftpboot` a 5.6 MB image, `crc32` == host | 8/8 repeat passes, no `T` timeouts, rate logged (target ≥ 5 MiB/s) | M3 |
| M5 | **Boot Linux from RAM**: `dev.py ramload us48-initramfs.bin`; DMA stopped on `bootm`; OpenWrt reaches `Starting kernel` and its console, with its own switch init working after our cold init | OpenWrt login on the serial console; kernel's `us48` switch driver does its own reset (DI §2) without conflict | M4 |
| M6 | Flash slot `kernel0` chain (`uboot-chain-install`), `--from-flash` | boot from kernel0 head to our prompt in seconds | M5 |
| M7 | (optional) 10G on unit 1 front 50: HR2 PAXB v1 in `pcie_iproc`, HOSTMEM remap, TSC init + ucode | TFTP ≥ 10 MiB/s over the DAC | M5; ucode blob policy |

## 6. Risks (ordered)

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| 1 | **GPHY→MAC path unresolved** (DI §12: PHY link up, GPORT0 MIB 0) | M3 blocked on unit 0 front 1 | §12.5 sequence + §12.3 checks logged at M3; fall back to unit 1 front 50 (M7 pulled forward) |
| 2 | **Cold chip init is unproven**: DI §2 is from source (OM + one stock trace), only ever run warm after stock (DI §5 "stock leaves config") | first-packet failures hard to localise | init in numbered steps, each with a read-back print (`ethstat init`); compare to a stock reg dump of the same registers; `reg-diff` approach as st1008f |
| 3 | `CMIC_CPS_RESET` side effects on unit 1 / PCIe / HiGig; does a CPU-only reset leave the chip configured on a later warm boot? | Linux (OpenWrt) switch driver sees a different state after our U-Boot than after stock | Skip the reset when the chip already answers (read `UMAC_REV_ID` first); verify Linux boots after M3 (M5) |
| 4 | DMA coherence unknown (CMIC AXI master vs A9 + PL310; PL310 is off after handoff) | silent RX/TX corruption | uncached DMA window; first test with 64-byte pings |
| 5 | **No watchdog, no reset command**: a hung init = power cycle on a remote unit | needs hand on the PSU | per-wait bounds (section 2); optional `wdt arm`; `bootm` handoff leaves wdt off; dev.py relay/power control if any |
| 6 | Heat: fans stay at power-on default in U-Boot (uboot.md §5), chip init raises dissipation | overheating in long sessions | short sessions; temp watch on the OpenWrt boot (CLAUDE.md); no fan access in U-Boot |
| 7 | Front 1 currently carries sw1 uplink / loop path (docs: copper 1 up 1000F to sw1, STP discarding) | cutting the link at reboot is expected; **a bridged U-Boot would loop** | VLAN 1 = CPU + front 1 only; never enable other ports; never run it with forwarding of other ports |
| 8 | Mainline for HR2 is a new mach: iProc QSPI/timer/serial DT, clocks not modelled | M1/M2 friction | take node shapes from `private/reference/bcm-hr2.dtsi`; fixed rates in DT, measured once (UART, PERIPHCLK; uboot.md Open items) |
| 9 | Stock `bootm` accepts our image only with valid CRCs + `Linux/ARM/kernel` header | no chain | `mkimage` header, host-side CRC check in `build-uboot` |
| 10 | EEPROM MAC / registry MAC column empty | DHCP reservation absent | static `ipaddr` (.207) for M4; fill the registry MAC from the EEPROM read |
| 11 | Vendor blob (TSC ucode) policy for M7 | licence/public-repo rule | decide before M7; M1-M6 need no blob |

## 7. Open items (to file as issues when work starts)

- Which watchdog OpenWrt uses (A9 private vs SP805), and the iProc chip-reset register for a real `reset`.
- Whether stock's `pci` already brings up the PAXB link at U-Boot (M0 check; matters only for M7).
- DMA coherency of the CMIC master (could allow cached buffers and a faster RX path).
- Gigabit TFTP rate with polled single-DCB RX; chain more DCBs if packets are lost.
