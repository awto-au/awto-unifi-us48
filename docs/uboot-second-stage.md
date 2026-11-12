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
- `bootm` on a Linux-type uImage ends with: IRQ+FIQ masked, I-cache off + invalidated, D-cache flushed + off, MMU off, PL310 L2 disabled, SVC mode, stock's own Cortex-A9 private watchdog stopped (`wdt_stop`), r0 = 0, r1 = 3000 (machid), r2 = `0x8000`.
- Our U-Boot therefore starts as if it were Linux: no cache/MMU clean-up needed. Unlike st1008f (`go`, cache hazards) this needs no custom handoff code.
- **The SP805 (`0x18039000`) is running at entry**: `dev.py` arms it from the stock prompt right before `bootm` (Watchdog section below). C
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
- **Skip**: DDR shmoo, QSPI re-init of stock's `mtd2` writes, stock's A9 private-watchdog arming (`wdt_arm`, stock's `board_init_r`; already stopped by `bootm`). The SP805 is not skipped: feed it (Watchdog section).
- **Do**: own page table + D-cache on (mainline `dcache_enable`), `ARM global timer` or SP804-class tick for `udelay` (see section 3), UART1 `0x18000400` NS16550 at 115200 (C, uboot.md §3). Console clock rate to be read from the stock divisor, not guessed (uboot.md Open items: UART input clock not measured).
- `reset`: stock's table handler is NULL (uboot.md §3). Ours = SP805 forced reset: unlock `0x1acce551` at `+0xC00`, load 1, control 3 (INTEN | RESEN). Same as UNVR `reset_cpu()` on its SP805 (UNVR `docs/uboot-port-plan.md` §4). `dev.py` already reboots the unit this way (SP805 expiry, `scripts/dev.py` `boot-flash` path). C. Whether the SP805 reset also resets the switch cores / unit 1: not known. I

### Watchdog (the **SP805 at `0x18039000`**; corrects the first draft, which named the A9 private watchdog)
- `dev.py` arms the SP805 from the stock prompt before **every** `bootm` (`_arm_sp805`): `mw.l 18039c00 1acce551` (unlock), `mw.l 18039000 1dcd6500` (load = 10 s at the 50 MHz DT `apb_clk`), `mw.l 18039008 3` (INTEN | RESEN). First expiry = IRQ (~10 s), reset at the second (~20 s). Linux `sp805-wdt` sees it running and keeps feeding it. Verified repeatedly (#11). C
- Stock's Cortex-A9 private watchdog (`0x19020620`, 30 s, reset mode) is stock-internal: armed in stock's `board_init_r`, stopped by `bootm` (uboot.md §2, §4). It is not our concern. C
- **Our stage starts with the SP805 counting.** First feed is due within 10 s of entry (conservative: the first expiry only raises an IRQ that we mask). I
- Policy:
  - Keep it armed at the prompt and during TFTP: a hang resets the unit (the design's old "hang = power cycle" is gone).
  - Feed from the mainline `sp805_wdt` driver (`drivers/watchdog/sp805_wdt.c` exists, C) with `WATCHDOG_AUTOSTART` (exists, C) and the U-Boot cyclic/`schedule()` service. Feed also at the top of every command. I
  - Never disarm before our own `bootm`; feed once immediately before it, Linux takes over. `wdt stop` exists for debugging only.
- Where it saves us: an AXI read of a dead or held-in-reset CMIC block can stall the core instead of faulting (UNVR hit SError on PCIe config reads, `uboot-boot-chain-state.md` #140). The SP805 fires only if feeding stops. A pure livelock in a loop that keeps feeding is caught by the bounded waits in section 2 instead. I
- Verified at M1 (2026-11-12, commit 03e51a9 image): `sleep 25` with SP805 armed at a ~20 s reset returns, `sp805_wdt` started at 10 s timeout, fed every 1 s. C

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

Shape: DM_ETH glue (`uboot-port/drivers/net/bcm_hr2_cmic_eth.c`, polled, legacy `NET_LEGACY` stack like st1008f) over the **shared `bcm-cmic` core** used unchanged by Linux (section 9). The "driver total" in the size table below is core + glue.

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

- `u-boot.bin` total ~450-550 KiB (net, bootm, sf, loady) vs 456 KiB at st1008f (C). Register names: compact table generated from `regdb/` (section 10, ~29-45 KB, offsets + names only, no vendor text).

## 3. Reuse plan

### Mainline U-Boot (store `/mnt/1tnvme/git-mirrors/u-boot/u-boot`, v2026.07-class; `git worktree add`, never clone)

| Piece | Status in mainline | Use |
|---|---|---|
| Arch/mach for iProc / HR2 / NS2 | **none**: `arch/arm` has `mach-bcm283x`, `mach-bcmbca`, `mach-bcmstb` only (C, listing) | `uboot-port/` tree staged into a pinned worktree by `scripts/build_uboot.py` (section 8). Board-only first (`TARGET_US48`, UNVR shape: one `arch/arm/Kconfig` hook); promote to an `ARCH_BCMHR2` mach dir (st1008f `mach-otto` shape) only if SoC code accumulates. I |
| NS16550 serial (DM) | yes | UART1 `0x18000400` DT node |
| SPI flash | `drivers/spi/iproc_qspi.c` (`brcm,iproc-qspi`, BSPI+MSPI) (C) | `sf probe/read` for EEPROM MAC and flash slot. Check it against the stock-used window `0x18027000` (uboot.md §5). Read-only guard (no `sf write/erase` except dev opt-in) as st1008f |
| GPIO | `drivers/gpio/iproc_gpio.c` (C) | optional (system LED GPIO 6/7) |
| Timer | `drivers/timer/arm_global_timer.c`, `arm_twd_timer.c` (C) | A9 global timer at PERIPHBASE `0x19020200` (stock's private watchdog is at `0x19020620`, same PERIPHBASE, C) for `udelay`; the clock rate is PERIPHCLK (stock assumes 200 MHz, I: uboot.md Open items) |
| PCIe | `drivers/pci/pcie_iproc.c` (`paxb-v2`, `paxc-v2` only, C) | **not for M1-M6** (unit 0 is AXI). For M7: add an HR2/PAXB-v1 variant (Linux `pcie-iproc.c` register table; OpenWrt patches 950-952 already adapt Linux, openwrt-port.md) |
| Net stack | `NET_LEGACY`: `dhcp`, `tftpboot`, `ping`, `wget` | as st1008f; lwIP optional later |
| bootm / uImage | yes | LZMA only if kernels shrink; OpenWrt initramfs image is uncompressed uImage today |
| Watchdog | `wdt` uclass + `drivers/watchdog/sp805_wdt.c` (C) | DT node `arm,sp805` at `0x18039000`, 50 MHz clock; no new driver (Watchdog section above). `cmd/wdt.c` gives `wdt` commands (C) |

### From st1008f (`/mnt/2tb/git/awto-tl-st1008f`)
- **Scaffold + build script pattern**: `uboot-port/` staged into a pinned tree, hooks patched idempotently, out-of-tree build, outputs under `tmp/tftp/`, log `tmp/logs/build_uboot_mainline.log`; store reached through a gitignored symlink (`tmp/u-boot-store`, env override), as CLAUDE.md #45.
- **`ramload` flow** (`scripts/st1008_ramload.py`): power-cycle → stock prompt → load → `go`/`bootm` → match banner. Adapt: stock `loady` + `bootm` (above) instead of stock TFTP + `go`; reuse `Console.run`, `send_checked` (long lines lose chars at 115200, st1008f #31), banner regex.
- **Chain-image format**: st1008f builds a raw slot image with CRC verified by stock `crc32`/`cmp`. Here the format is a plain legacy uImage (`mkimage`-style header, OpenWrt's `mkimage` already used for the initramfs image), so stock `bootm` does the CRC and handoff.
- **Net test**: `uboot-test net` = ping gateway, `dhcp`, `tftpboot` + CRC32 vs host, repeated 8x; port as `dev.py uboot-test net` (TFTP of the U-Boot image itself). Reference rate 10.6-19.8 MiB/s at 10G (C, their doc); expect lower at 1G polled (~5-10 MiB/s, I) = a 5.6 MB kernel in ~1 s.
- **Debug lessons to copy** (C, their doc): all RX rings armed or none (null ring left a runout bit), release RX counters per frame, doorbell loss on TX (re-kick after a bounded wait), never reset the chip with the NIC reset (separate reset bits), `ethstat` command, `ethrxany` debug switch.
- **Shared core** (st1008f #83 `shared/` + UNVR `stage_hal.py`): adopted, built in from the start, not after the first packet. See sections 8 and 9.

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
| M2 | **SPI flash** (built 2026-11-12, not yet run on the board): `iproc_qspi` + `sf probe/read`, EEPROM MAC read (read-only guard on) | `sf read` of `0x1ff0000` 6 B == stock `ubnthal` MAC; `crc32` of a kernel0 range == stock `mtd` CRC | M1 |
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
| 5 | **Hung init on a remote unit**: SP805 is running at entry (Watchdog section), so a stall resets in ~10-20 s only while we keep it fed correctly; a feed bug = spurious resets mid-TFTP | spurious reset, or hang if feeding sits in a loop that never exits | per-wait bounds (section 2); feed from `sp805_wdt` + cyclic, verified with a 25 s `sleep` at M1; `reset` = SP805 forced reset |
| 6 | Heat: fans stay at power-on default in U-Boot (uboot.md §5), chip init raises dissipation | overheating in long sessions | short sessions; temp watch on the OpenWrt boot (CLAUDE.md); no fan access in U-Boot |
| 7 | Front 1 currently carries sw1 uplink / loop path (docs: copper 1 up 1000F to sw1, STP discarding) | cutting the link at reboot is expected; **a bridged U-Boot would loop** | VLAN 1 = CPU + front 1 only; never enable other ports; never run it with forwarding of other ports |
| 8 | Mainline for HR2 is a new mach: iProc QSPI/timer/serial DT, clocks not modelled | M1/M2 friction | take node shapes from `private/reference/bcm-hr2.dtsi`; fixed rates in DT, measured once (UART, PERIPHCLK; uboot.md Open items) |
| 9 | Stock `bootm` accepts our image only with valid CRCs + `Linux/ARM/kernel` header | no chain | `mkimage` header, host-side CRC check in `build-uboot` |
| 10 | EEPROM MAC / registry MAC column empty | DHCP reservation absent | static `ipaddr` (.207) for M4; fill the registry MAC from the EEPROM read |
| 11 | Vendor blob (TSC ucode) policy for M7 | licence/public-repo rule | decide before M7; M1-M6 need no blob |

## 7. Open items (to file as issues when work starts)

- Whether an SP805 reset also resets the switch cores and unit 1 (matters for cold vs warm entry after `reset`).
- Whether stock's `pci` already brings up the PAXB link at U-Boot (M0 check; matters only for M7).
- DMA coherency of the CMIC master (could allow cached buffers and a faster RX path).
- Gigabit TFTP rate with polled single-DCB RX; chain more DCBs if packets are lost.
- Sections 8-11 (added 2026-11-12, #46 + #4): sibling setup, shared core, examination commands, ordered plan. Their open items are in section 11.

## 8. Sibling-project setup we copy

Sources read 2026-11-12: UNVR (`/mnt/2tb/git/awto-unvr`: `docs/uboot-port-plan.md`, `uboot-boot-chain-state.md`, `build.md`, `master-plan.md`, `uboot-hw-test-suite.md`, `uboot-port/`, `scripts/`, `dev.py`) and st1008f (`/mnt/2tb/git/awto-tl-st1008f`: `docs/UBOOT-MAINLINE.md`, `DEFAULT-BOOT.md`, `uboot-port/`, `shared/`, `scripts/`). C unless tagged I.

### Copy list

| # | Thing | Where it lives in the sibling | Ours | Change |
|---|---|---|---|---|
| 1 | Tracked scaffold staged into a pinned tree | UNVR `uboot-port/{arch/dts, board/annapurna/alpine, configs, drivers, include/configs}`; st1008f `uboot-port/{arch/mips/{mach-otto,dts,lib}, board/tplink/tl-st1008f, configs, drivers, include/configs}` | `uboot-port/{arch/arm/dts, board/ubiquiti/us48, cmd, configs, drivers/net, include/configs, patches}` | Board-only like UNVR (no mach dir yet, I). `cmd/` + `drivers/net/` added: they hold upstreamable files |
| 2 | Source tree = detached worktree of the machine store, never a clone | st1008f `scripts/build_uboot_mainline.py` (tag `v2026.07` at `tmp/u-boot-v2026.07`, gitignored symlink `tmp/u-boot-store`, `$ST1008F_UBOOT_STORE`, `--no-tags` fetch if the tag is missing). This repo's `scripts/build_mainline.py` already does it for Linux (`tmp/linux-store`, `$US48_LINUX_STORE`) | `tmp/u-boot-store -> /mnt/1tnvme/git-mirrors/u-boot/u-boot` (exists), `tmp/u-boot-v2026.07`, `$US48_UBOOT_STORE` | UNVR uses a standalone tree `/mnt/2tb/unvr-port-refs/u-boot-v2026.07` (738 MB, `master-plan.md`): not copied (awto-dan#50) |
| 3 | Build script | UNVR `scripts/uboot-build.py` (`stage()`, `unstage_stale()`, `verify_patch_state()`, `stamp_ident()`, `run()` with a stall timer); st1008f `scripts/build_uboot_mainline.py` (idempotent hooks, `SHARED` dict, out-of-tree build, ident `-st1008f-<last commit>[-mod]` in the banner) | `scripts/build_uboot.py`; build dir `tmp/uboot-build`; out `tmp/tftp/us48-uboot.{bin,img}`; log `tmp/logs/build_uboot.log` | UNVR's `unstage_stale()` (a file removed from `uboot-port/` otherwise stays in the tree, UNVR #140). st1008f's pinned-tree + ident. Stall timeout (awto-dan#53), not a fixed cap |
| 4 | Hooks into upstream files as patch files + marker check | UNVR `patches/uboot/0001..0008-*.patch`; `verify_patch_state()` FATALs naming the missing patch (11 markers) (`docs/build.md`) | `uboot-port/patches/0001-arm-add-TARGET_US48.patch`, `0002-net-wire-bcm-cmic.patch`, `0003-cmd-wire-bcm-cmic.patch` + marker list in `build_uboot.py` | st1008f patches 11 hooks inline in Python (diff invisible); UNVR moved off that (#224). Take UNVR's |
| 5 | Defconfig host-tool quirk | both defconfigs: `# CONFIG_TOOLS_LIBCRYPTO`, `# CONFIG_TOOLS_KWBIMAGE` (UNVR also `# CONFIG_FIT_SIGNATURE`); host OpenSSL 3.5 has no `engine.h` | `configs/us48_defconfig` | Verbatim |
| 6 | Entry / low-level | both `CONFIG_SKIP_LOWLEVEL_INIT`; TEXT_BASE clear of stock; relocate to top of RAM; st1008f `ENV_IS_NOWHERE` + `BOOTDELAY=-1`; UNVR own control DTB (`OF_CONTROL`, `u-boot-dtb.bin`, no prior-stage DTB) | section 1 | `bootm` of a uImage instead of `go`. UNVR's persisted env + CANARY not copied: a persisted env overrides defconfig defaults (`uboot-boot-chain-state.md`), and `saveenv` is forbidden here |
| 7 | Chain image | UNVR `scripts/mkuimage.py` (pure Python legacy uImage, no `mkimage`); st1008f `uboot_chain_image.py` + `uboot_chain_layout.py` (CRC trailer checked by stock `crc32; cmp`), `uboot_chain_install.py` | `scripts/mkuimage.py` (UNVR's), load = entry `0x04000000`. M6 flash slot: st1008f's read-back CRC | No trailer in RAM mode: stock `bootm` checks hcrc/dcrc. st1008f's power-up auto-chain (`uboot_chain_env.py`, `DEFAULT-BOOT.md`) not copied: needs `saveenv` (forbidden) |
| 8 | `dev.py` commands | UNVR `chainload`, `uboot-test`, `uboot-chainload-from-ssd`, `flash-awto-uboot`, `power-cycle`, `_ensure_tftpd()` (`dev.py`:1193), `scripts/catch-uboot.py`; st1008f `ramload`, `uboot-test [net]`, `uboot-chain-install`, `reg-diff`, `scripts/tftpd.py` | section 4 table plus `uboot-test [hw\|net]`, `reg-diff`; tftpd helper from UNVR | Names follow this repo (`boot-ram`, `flash-kernel0`) |
| 9 | Test harness | st1008f `scripts/uboot_mainline_test.py`: `CHECKS = {name: (command, regex, cap_s)}`, one PROMPT regex, explicit-only checks, `refuse_*` negative checks, log `tmp/logs/uboot_mainline_test.log`, `Console.send_checked` (16-char chunks, st1008f #31). UNVR `docs/uboot-hw-test-suite.md` (hardware inventory -> command table) | `scripts/uboot_test.py` over this repo's `scripts/console.py`; checks in section 10 | Structure copied |
| 10 | Register diff / dump twin | st1008f `scripts/reg_diff.py` (`capture <label>` at the live prompt, `compare a b`, `--sdk` names). UNVR `scripts/regdump.py` ("userspace twin of in-U-Boot `aldiag`, keep in sync") | `scripts/reg_diff.py` parses `regdump` lines; Linux twin = debugfs from the same core (section 10) | UNVR's twin is synced by hand; ours is one core + one name table |
| 11 | Shared-code staging | UNVR `scripts/stage_hal.py` (single source in repo, byte-identical read-only copy into a gitignored dest, rewritten every build; a build failure is fixed in the shim, never in the staged copy), `hal/al_hal_plat_contract.h` + `uboot-port/drivers/net/al_hal_shim/al_hal_plat_services.h` (one shim, contract fails the build naming the missing primitive), `scripts/hal-drift-check.py`, `scripts/hal-host-portability.py` (each staged file compiled alone under U-Boot). st1008f `shared/rtl930x-nic/*.h`, `SHARED` dict in `build_uboot_mainline.py`, includer-defined `rd/wr/udelay`, Linux `static_assert`s | `common/bcm-cmic/` + `scripts/sync_bcm_cmic.py` (`stage`, `check`) + compile-alone check | Section 9 |
| 12 | Wait table "Wait / Limit / Basis / On expiry" | st1008f `UBOOT-MAINLINE.md` Ethernet | section 2 (done) | - |
| 13 | Interface table "rated max / board allows / we run / why the gap" | st1008f `UBOOT-MAINLINE.md` SPI-NOR | rows for MDC, QSPI, CMIC DMA added at M2/M3 | Global rule |
| 14 | `reset` | UNVR `reset_cpu()` = SP805 unlock/load/control (`uboot-port-plan.md` §4) | section 1 | Same register sequence, our SP805 |

### Gotchas lifted from the siblings
- `tio` file log lacks the unterminated prompt: judge state from the live console socket, not the log file (UNVR `uboot-boot-chain-state.md`). Our `dev.py` console already works on the socket. C
- Long lines lose characters at 115200: send in checked chunks (st1008f #31). C
- DM_ETH lessons (st1008f): arm all RX rings or none; release the RX counter per frame; re-kick a lost TX doorbell after a bounded wait; never reset the chip with the NIC reset. Apply as hypotheses to the CMC0 DMA path, not as facts about it. C (st1008f), I (applicability)
- `serverip`/`ipaddr` baked into a persisted env drift with DHCP: ours is set per boot by `dev.py` (RAM env). C

### Deliberately not copied
- Persisted env, `saveenv`, bootsel/auto-chain (CLAUDE.md forbids `saveenv`, bootsel record, u-boot partition).
- UNVR `go`-chain (we `bootm`), DDR/SPL, NAND, SATA, serdes; st1008f `highmem`, `rtk` command family, clock overclock code, SPI read-only opcode allowlist (we do not use QSPI writes at all; revisit at M6).

## 9. Shared core (Linux + U-Boot)

Goal: one portable C core ("bcm-cmic") for the HR2 switch chips, compiled **unchanged** by the Linux driver (#4: `kernel/us48-cmic/us48_cmic.c` today) and the U-Boot net driver and commands. The core holds hardware facts and register sequences. The OS glue holds everything else.

### Boundary

| In the core | In the glue |
|---|---|
| S-channel op + abort, reg/mem accessors, address/header encode | bus discovery (Linux PCI/platform + DT; U-Boot DM + DT, board table), unit 1 PCIe link |
| CMIC MIIM C22/C45, AER lane select | `mii_bus` / DM `mdio` registration, phylib/phylink, DM `phy_connect` |
| chip reset + init (DI §2), TSC lane enable/status (DI §13.5), GPHY power-up (DI §4, §7, §12.4), port enable/STP/VLAN (DI §5-6, §12.5) | netdev/NAPI/ethtool/DSA (Linux), `eth_ops` + legacy net stack (U-Boot), commands, debugfs |
| CMC0 DCB layout, RX/TX channel setup, arm/poll/abort, HOSTMEM remap | IRQs and NAPI (Linux, later), DMA mask and allocation policy, cache policy |
| register-name table + `regdump` line formatter | `seq_file`/debugfs (Linux), `printf` (U-Boot) |

- v1 is polled in both OSes; Linux IRQ-driven RX uses the same DCB helpers from glue later. I

### Mechanism options for sharing the files

| Option | Verdict | Why |
|---|---|---|
| Symlink from each tree into `common/` | no | Trees are disposable detached worktrees (`tmp/u-boot-v2026.07`, `tmp/linux-mainline`) and the OpenWrt kernel dir is regenerated by `make clean`; a symlink dirties `git status` and dangles. UNVR's own review of symlinks into trees: absolute paths, tools that do not follow links, dangling on another checkout (`tmp/issue-vendoring-symlinks.md`). Kbuild `M=` also cannot take `../` objects reliably (I). C/I |
| Git submodule / separate repo | no | Third repo to keep in lock-step; both consumers need patches to land together while the core is still moving. I |
| **Single source in `common/bcm-cmic/`, byte-identical read-only copy staged into each tree by `scripts/sync_bcm_cmic.py`, `--check` in `dev.py gate`** | **chosen** | Both siblings already do this (UNVR `stage_hal.py`, st1008f `SHARED`). The staged copy is read-only so an edit there fails at the edit. Same files become the upstream Linux patch (`git format-patch` needs real files) and the U-Boot sync. C (siblings), I (upstream use) |
| Upstream-first: Linux lib is the source, U-Boot copy is a periodic sync | end state, not the dev flow | U-Boot already carries Linux-derived code in two forms (git grep of the store, 2026-11-12). Compat headers it provides so Linux-dialect code compiles: `include/linux/{bitfield,iopoll,mdio,mii,dma-mapping,printk,delay,io}.h`; "from Linux" notes in `lib/crc32c.c`, `lib/hexdump.c`, `drivers/net/phy/{broadcom,smsc,et1011c}.c`. Forks with `#ifndef __UBOOT__` ifdefs: `drivers/mtd/ubi/*`, `drivers/mtd/nand/spi/*`, `drivers/usb/musb-new/*`, `lib/rbtree.c`, `lib/list_sort.c`, `lib/crypto/*`, `drivers/spi/spi-mem.c`. The fork form is what we avoid; a verbatim sync is the form maintainers can review against Linux (I). Until the Linux side lands, `common/` stays the source |

- Rule: edit only `common/bcm-cmic/`. Never `#ifdef __UBOOT__` / `#ifdef __KERNEL__` in the core (Linux would reject it, and that is what makes the U-Boot copy a sync instead of a fork). I

### Directory tree

```
common/bcm-cmic/                      SINGLE SOURCE (GPL-2.0, SPDX per file), tracked
  bcm_cmic.h                          public API, struct bcm_cmic_unit, struct bcm_cmic_os
  bcm_cmic_regs.h                     GENERATED subset of regdb the core uses (not the 2 MB hr2-regs.h)
  bcm_cmic_dcb.h                      DCB (64 B, 16 words) + static_assert(sizeof == 64)
  bcm_cmic_schan.c  _miim.c  _tsc.c  _gphy.c  _port.c  _dma.c  _init.c  _dump.c
  bcm_cmic_names.c                    name lookup; data = generated bcm_cmic_names_data.h (optional, Kconfig)
  tests/host_test.c                   host build: fake CMIC (S-channel handshake, MIIM, DMA) on x86
  linux/Kconfig  linux/Makefile       per-OS build files (staged next to the core, NOT byte-identical-checked as core)
  uboot/Kconfig  uboot/Makefile

uboot-port/                           as UNVR/st1008f (section 8)
  arch/arm/dts/us48-uboot.dts
  board/ubiquiti/us48/{Kconfig,Makefile,MAINTAINERS,board.c,ports.c}   board port table, MAC read, DMA window
  cmd/bcm_cmic.c                      commands (section 10): board-independent, upstreamable
  drivers/net/bcm_hr2_cmic_eth.c      DM_ETH glue; drivers/net/bcm_hr2_mdio.c = DM mdio over core MIIM
  configs/us48_defconfig  include/configs/us48.h
  patches/000{1,2,3}-*.patch          hooks into upstream Kconfig/Makefile

kernel/us48-cmic/                     consumer 1 (OOT module, exists): us48_cmic.c becomes glue over the core
scripts/sync_bcm_cmic.py              stage <linux|uboot|oot> | check
scripts/gen_bcm_cmic_regs.py          regdb -> bcm_cmic_regs.h (names listed in common/bcm-cmic/regs.used)
scripts/gen_bcm_names.py              regdb -> bcm_cmic_names_data.h (section 10)
scripts/bcm_cmic_host_test.py         builds + runs tests/host_test.c
```

### Wiring (where the staged copies land)

| Consumer | Staged to | Hook | Notes |
|---|---|---|---|
| Linux mainline branch (`tmp/linux-mainline`) | `drivers/net/ethernet/broadcom/bcm-cmic/` | `broadcom/Kconfig`: `source ".../bcm-cmic/Kconfig"`; `broadcom/Makefile`: `obj-$(CONFIG_BCM_CMIC) += bcm-cmic/`; consumer Makefile `ccflags-y += -I$(srctree)/drivers/net/ethernet/broadcom/bcm-cmic`. Hook patch `mainline/0008-*.patch` (hooks only; the core is staged, never duplicated into a patch) | Same shape as `microchip/fdma/`: `config FDMA bool "FDMA API" if COMPILE_TEST`, consumers `select FDMA`, `obj-$(CONFIG_FDMA) += fdma/`, `ccflags-y += -I.../fdma` in lan966x and sparx5 (lan969x builds inside `sparx5/`, I). C |
| OpenWrt hr2 kernel (OOT module build, `scripts/build_us48_cmic.py`) | `kernel/us48-cmic/bcm-cmic/` (gitignored) | `us48-cmic-y := us48_cmic.o bcm-cmic/bcm_cmic_schan.o ...` in the module Makefile (subdir objects in `M=` builds, I) | One `.ko`, core not exported. Matches the kernel in the flashed image (`build_us48_cmic.py` rule) |
| U-Boot worktree (`tmp/u-boot-v2026.07`) | `lib/bcm-cmic/` (built: `drivers/net/` needs `ETH`, we run `NO_NET`; patch 0002 hooks `lib/Kconfig` + `lib/Makefile`, 0003 `cmd/Makefile`) | patch 0002: `drivers/net/Kconfig` `source "drivers/net/bcm-cmic/Kconfig"`; `drivers/net/Makefile` `obj-$(CONFIG_BCM_CMIC) += bcm-cmic/`; `ccflags-y += -I$(srctree)/drivers/net/bcm-cmic` for `bcm_hr2_cmic_eth.o` and `cmd/bcm_cmic.o` | `uboot/Makefile` lists flat objects (`obj-y += bcm_cmic_schan.o ...`); Linux Makefile uses the `bcm-cmic-y` composite form, so the two Makefiles are the only per-OS files |

- `sync_bcm_cmic.py stage <target>`: copy core files, set read-only, write a manifest with SHA-256. `check`: every staged copy equals `common/`; fails `dev.py gate` (UNVR `hal-drift-check.py` pattern). C (pattern), I (script)
- Compile-alone check per OS (UNVR `hal-host-portability.py` pattern): each core `.c` built alone against the Linux tree (`make drivers/net/ethernet/broadcom/bcm-cmic/` with `COMPILE_TEST`) and the U-Boot tree. A failure is a shim/contract finding, never an edit to the core copy. C (pattern)
- Linux `static_assert`/U-Boot `static_assert` (`include/linux/build_bug.h`, C) tie DCB size, field offsets and `HR2_BLKn_SCHAN` constants (generated from `regdb/hr2_blocks.tsv`) to the core.

### OS contract (the thin shim)

Principle: write the core in the Linux kernel dialect and use only the subset U-Boot already provides in `include/linux/*` (checked in the store, 2026-11-12). The shim is then only what genuinely differs. Linux maintainers reject an OS-abstraction layer that wraps `readl`/`writel` (I); calling the native API is the form that is upstreamable.

| Need | Linux | U-Boot | Form | Basis |
|---|---|---|---|---|
| MMIO read/write | `readl()/writel()` on `unit->base + off` | same (`asm/io.h`; `__iomem` defined in `compiler_types.h`) | native API, no shim | C |
| Delay | `udelay()` only | same (`include/linux/delay.h`: `void udelay(unsigned long)`) | native API | C. No `usleep_range`/`msleep` in core (absent or unchecked in U-Boot, I). Waits are short; Linux busy-waits under its mutex (longest = 10 ms reset settle, probe only, I) |
| Bounded wait | core-owned `bcm_cmic_wait(unit, off, mask, val, limit_us, what)`: loop of `udelay(1)` + `readl` | same | core code | `readl_poll_timeout()` differs: Linux `(addr, val, cond, delay_us, timeout_us)` (`include/linux/iopoll.h:227`), U-Boot `(addr, val, cond, timeout_us)` (`iopoll.h:61`); `readl_poll_timeout_atomic` and `dma_wmb` are absent in U-Boot (git grep). C. On expiry it logs what, limit, elapsed (iterations x 1 us, lower bound) and returns `-ETIMEDOUT` (awto-dan timeout rule) |
| Logging | `pr_err/pr_warn/pr_info/pr_debug` + `pr_fmt("bcm-cmic: ")` | same (`include/linux/printk.h`: `pr_err`, `pr_debug` present) | native API | C. Linux glue may add `dev_*` around calls |
| Locking | `mutex_lock/unlock` on a mutex in glue state, passed as `os_priv` | none (`lock`/`unlock` = NULL) | **ops** | Linux shares the S-channel/MIIM between the driver, the MDIO bus and debugfs. Each public entry locks once; internal `_locked` variants never relock. I |
| DMA memory + bus address | `dma_alloc_coherent(dev, size, &bus, GFP_KERNEL)` with `dma_set_mask_and_coherent` from `unit->dma_bits` | `noncached_alloc(size, align)` (`include/cpu_func.h`) from the non-cacheable 1 MiB DMA window of section 1; `bus = phys` (AXI master, no IOMMU) | **ops**: `dma_alloc(priv, size, align, &cpu, &bus)`, `dma_free` | `dma_alloc_coherent` is not in U-Boot's `include/` (git grep). C. Bus = phys on both units (no IOMMU: I) |
| Cache / barrier sync | `dma_wmb()` before arming, `dma_rmb()` after DONE (coherent memory) | `dsb()` when uncached; flush/invalidate when the window is cached | **ops**: `dma_sync(priv, cpu, bus, len, dir)`; NULL = nothing needed | I |
| Byte order | `cpu_to_le32()/le32_to_cpu()` on DCB and CMIC DMA words | same (`byteorder/generic.h`) | native API | C |
| Bit/field helpers, asserts | `BIT`, `GENMASK`, `FIELD_PREP/GET`, `static_assert` | same (`bitops.h`, `bitfield.h`, `build_bug.h`) | native API | C |

- `struct bcm_cmic_os` = `{ lock, unlock, dma_alloc, dma_free, dma_sync }` (5 entries), passed once in `bcm_cmic_unit_init()`. All other OS access is the native API above. I
- Watchdog service in long waits is not a shim item: U-Boot feeds the SP805 from its own cyclic/`schedule()` (Watchdog section); verified at M1 (25 s `sleep`, no reset). C

### Unit handle (unit 0 AXI, unit 1 PCIe)

| Field | Unit 0 (BCM53344) | Unit 1 (BCM53346) | Tag |
|---|---|---|---|
| `base` | AXI `0x48000000` (DT/board constant) | PCIe BAR 2 of `14e4:8346` function 0 (`CMIC_BAR 2` in `us48_cmic.c`; function 1 shares the ID and has no CMIC) | C |
| CMC0 offsets | `base + 0x31000...` (DI §1, §5) | same offsets from the BAR base (DI §13: `B + off`) | C |
| `flags` | none | `BCM_CMIC_F_HOSTMEM_REMAP`: `bcm_cmic_unit_init()` writes `CMC0_HOSTMEM_ADDR_REMAP_0..3` (`+0x31474/78/7c`, `+0x314ac`) with the DI §8 case A values; DMA buffers must be below the remapped window, so `dma_bits` is set from it | C (values), I (window size) |
| DMA endianness | `DMA_CTRL[5:4] = 0` (LE host) | same, flag `BCM_CMIC_F_DMA_SWAP` if the PAXB swaps | I |
| Chip data | block table `HR2_BLKn_SCHAN` (generated from `hr2_blocks.tsv`), per-chip port/lane tables | same table, other port map | C |
| Board data | passed by glue: `struct bcm_cmic_port_cfg[]` = front -> {unit, phys, logical, GPHY bus/addr or TSC lane}. Linux later from DT, U-Boot from `board/ubiquiti/us48/ports.c` (data from `scripts/us48_portmap.py`) | same | I |
| Header flavour | `bcm_cmic_unit_probe()` first reads `UMAC_REV_ID` and picks xgsm (`0x2c200200`) vs xgsd (`0x2c100200`) header (DI §1: decide by first live read) | same | C (procedure), I (xgsm expected) |

- Unit 1 only differs by `base`, the remap, the chip port map and who enumerates PCIe. In U-Boot that needs HR2 PAXB v1 in `pcie_iproc` (M7); in Linux it is the existing `iproc` PCIe host. C

### Core modules

| Module | Public functions (names are the design, not code) | Facts | Linux user | U-Boot user |
|---|---|---|---|---|
| `unit` | `unit_init/probe/fini`, `rd/wr` inlines on `readl/writel` | DI §1, §8 | `us48_cmic` probe | board init, `cmic` |
| `schan` | `schan_op` (raw), `reg_read32/64`, `reg_write32/64`, `mem_read/mem_write` (entry words), `schan_abort`, address/header builders | CMC0 `SCHAN_CTRL/ERR/MESSAGE[22]`, opcodes 0x07-0x0e, handshake incl. abort-pulse recovery, 22-word max (DI §1); mem tables DI §6 | all register work | `schan`, `smem`, `regdump` |
| `miim` | `miim_read/write(unit, sel{int,ext}, bus, phy, c45?, devad, reg)`, `tsc_acc(unit, A, lane, addr32)` doing the AER write first | `MIIM_PARAM` `INTERNAL_SEL` b25, `BUS_ID` [24:22], `C45_SEL` b21; one transaction = CTRL/PARAM/ADDRESS/STAT sequence; AER sticky per PHY address, rewrite every access (DI §13.1) | `mii_bus` read/write | `miim`, DM `mdio` |
| `tsc` | `tsc_status(unit, port, &st)` (DI §13.2 checks), `tsc_lane_enable/disable(unit, port)` | the eight verified writes of DI §13.5 (SIGDET force clear, TX lane, RX lane, XLPORT/XLMAC enable, ANATX, credits, TX lane reset pulse after credits); credits before the reset release. Warm start only (uC alive): `-EOPNOTSUPP` when `ANAPLL_STATUS` PLL is not locked (cold = ucode download, M7, blob policy) | front 49/50 bring-up, phylink PCS later | `tsc` |
| `gphy` | `gphy_status(unit, port, &st)`, `gphy_powerup(unit, port, cfg)`, RDB enter/exit helpers | DI §4, §7, §12.4: top reset, DSP clock, `DSP_TAP10`, SUPER_ISOLATE=0, `MODE_SEL`, `POWER_DOWN=0`, `MII_CTRL=0x1340`, EEE off | copper ports | `gphy`, first port |
| `port` | `port_enable(unit, port, vlan, stg)`, `port_disable`, `stp_set`, `port_status` | `PORT_TAB`, `EGR_PORT`, `EGR_ENABLE`, MAC reset pulse, VLAN/STG/`EGR_VLAN`, `EPC_LINK_BMAP_64` (DI §5, §6, §12.5) | switch driver | `port`, DM_ETH `start` |
| `dma` | `dma_init(unit, cfg)`, `rx_arm`, `rx_poll(&frame{ptr,len,src_port})`, `rx_release`, `tx(bus,len,dst_port)`, `dma_abort(ch)`, `dma_stop`, `dma_status` | CMC0 DMA channels, DCB 64 B / 16 words / 16-byte aligned, RX ch1 + TX ch0, abort recipe, `RXBUF_EP_RLS_CRED`, `COS_CTRL_RX` (DI §3, §5) | netdev RX/TX | `eth_ops`, `dma rx-test` |
| `init` | `chip_reset`, `chip_init(unit, &cfg{ports})` | DI §2 steps 1-10 (reset, SBUS ring, TOP release, pipe reset, port maps, TDM, MIIM clock, MMU, VLAN profile, port bring-up) | OpenWrt boots with the unit-0 core **unconfigured** (#46 comment, AMAC tests), so Linux needs this too | cold init at M3 |
| `dump` | `dump_next(unit, &iter, buf, len)`: one `NAME[port] addr = val` line | section 10 | debugfs | `regdump` |
| `names` | `name_lookup`, `name_iter` | section 10 | debugfs (optional) | commands |

- Raw write functions are unguarded in the core; guard policy (GPIO/SPI/MSPI/reset regs) lives in the command layer (section 10). I
- Source of sequences: OpenMDK (permissive licence) and the project's own live verification (DI). No stock-SDK text is copied. C
- Host test: fake CMIC model drives `schan`/`miim`/`dma` on x86 (`tests/host_test.c`, pattern of st1008f `udivdi3_host_test.py`, `sfp_decode_host_test.py`). First golden: the DI §1 worked example (header `0x2c200200`, UMAC_REV_ID read) encodes bit-exact. C (pattern), I (case)

### Upstream acceptability

| Project | Shape | Why |
|---|---|---|
| Linux | `drivers/net/ethernet/broadcom/bcm-cmic/`, hidden `bool` selected by consumers (visible under `COMPILE_TEST`), headers in-dir + `-I`, GPL-2.0, posted together with the first in-tree consumer (the HR2 switch driver; DT/PCI patches 0001-0007 already in `mainline/`) | exact `microchip/fdma/` precedent; Linux requires a user for a library. C (fdma), I (reception) |
| U-Boot | verbatim copy at `drivers/net/bcm-cmic/` + own glue (`drivers/net/bcm_hr2_cmic_eth.c`, `cmd/bcm_cmic.c`, board) | submitted as "sync from Linux <commit>" after the Linux side lands, or with the first U-Boot series if Linux is slow; no `__UBOOT__` forks. I |
| This repo meanwhile | `common/` is the source; both trees get staged copies | sections above |

- Linux-first ordering avoids U-Boot carrying a fork that Linux review then changes. I
- Command names are short here for the bench; for U-Boot upstream they get one prefix (`hr2sw ...`) because `port`/`dma` are generic. No such names exist in `cmd/` today (store listing). C/I

## 10. U-Boot hardware-examination commands

Purpose: the second stage doubles as a no-Linux bring-up shell for the switch (UNVR `uboot-hw-test-suite.md` idea). All commands are thin wrappers over the core in section 9, so each one is also the first test of the library code Linux runs. They land at M1-M3, **before** the NIC works: the AXI CMIC needs no PCIe and no network. C (AXI CMIC `0x48000000`, DI §8), I (ordering)

### Conventions
- `-u <0|1>` or `cmic unit <n>` selects the unit (default 0). Unit 1 exists in U-Boot only after M7 (PCIe); until then the command says "unit 1 not mapped".
- `<port>` = front number 1-52, or `ge<n>`/`xe<n>` in the selected unit's numbering (unit 1: `xe0` = front 49, `xe1` = front 50, DI §13), or `l<n>` logical. Resolved through the board port table. C (xe naming), I (global map)
- Register/memory arguments accept a regdb name (`XLMAC_CTRL`) or a number; names resolve to block + offset, so `<blk>` is optional when the name is unique.
- Output is one stable line per register: `UNIT NAME[port] addr = 0xVALUE` (host diff-able, section 10 Linux twin).
- Writes are plain (no confirmation prompts: normal device modes are settings, awto-unifi-us48 memory rule). The only refusals: GPIO/SPI/MSPI-block registers without `-f` (CLAUDE.md "don't guess GPIO/I2C/SPI writes"; the fan/LED chains share those blocks) and no write path at all in `poe`. The prefix list is matched against regdb at build time and the build fails if it matches nothing. I

### Command list

| Command | Does | Core call | Notes |
|---|---|---|---|
| `cmic r <off> [n]` / `cmic w <off> <val>` / `cmic unit [n]` | direct CMIC register access, unit-relative offset (names accepted for `CMIC_*`) | `unit rd/wr` | `CMIC_CPS_RESET` etc. are ordinary writes; `dev.py` knows the unit is cold-resettable |
| `schan r <reg\|blk addr> [port] [-f]` / `schan w <reg\|blk addr> [port] <val>` | S-channel register op; `schan r XLMAC_CTRL xe1` resolves block (XLPORT), lane (low address byte, DI §13.5) and unit | `reg_read32/64`, `reg_write*` | `-f` = decode fields (needs host decoder, below). 64-bit regs from regdb width. `schan r UMAC_REV_ID 1` = the DI §1 worked example |
| `smem r <mem> <idx> [n]` / `smem w <mem> <idx> <w0> [w1..]` | table entry read/write with regdb name; checks `index_min/max`, entry words from `bytes` | `mem_read/mem_write` | e.g. `smem r VLAN_TAB 1`, `PORT_TAB`, `EGR_PORT`, `STG_TAB`. 197 mems in `hr2_mems.tsv` |
| `miim r\|w [int\|ext] [c45] <bus> <phy> <reg> [val]` | one MIIM transaction; `c45`: `reg` = `devad:reg16`; `-l <lane>` writes AER first (TSC) | `miim_*`, `tsc_acc` | `int` bus 0 = GPHYs, bus 1 = TSC (DI §4, §13.1). Addition: `miim scan [int\|ext] <bus>` reads ID1/ID2 of 0-31 (read-only; same as `scripts/miim.py` live scan) |
| `tsc status <port>` | DI §13.2 read-only checks in dependency order (PLL, uC, SIGDET, PMD, PCS, MAC LSS), latched bits read once | `tsc_status` | unit 1 `xe0/xe1`; latched bits clear on read, printed once |
| `tsc enable <port>` | DI §13.5 sequence, verified live on xe1 (front 50, 10G DAC) | `tsc_lane_enable` | warm start only; refuses when the PLL is not locked (cold needs ucode, M7). Addition: `tsc disable` |
| `gphy status <port>` | BMCR/BMSR/ID, AN result, speed/duplex, RDB regs, EXT_PWRDOWN bit of `TOP_QGPHY_CTRL_0`, DI §12.3 checks 1 and 12 | `gphy_status` | read-only; first port = front 1 (bus 0 addr 9) |
| `gphy up <port>` (addition) | DI §4/§7/§12.4 power-up + 1000FD AN | `gphy_powerup` | needed for M3 |
| `port status <port>` | `MAC_MODE` link, UMAC/XLMAC enable, `PORT_TAB`/STG state, `EPC_LINK_BMAP_64` bit, MIB (`GRPKT`, `GTPKT`) | `port_status` | read-only |
| `port enable <port>` / `port disable <port>` | DI §12.5 / §6: PORT_TAB, EGR_PORT, EGR_ENABLE, MAC reset pulse, VLAN 1 = {CPU, port}, STG forwarding | `port_enable` | prints once: "port joins the VLAN with the CPU only; other ports untouched" (loop safety, risk 7). Any further port is the operator's call |
| `dma rx-test [-n frames] [-t ms]` (+ `dma status`, `dma abort`, `dma tx-test <port> <len>` additions) | init DMA window, arm RX ch1, poll DONE; per frame: src port, length, EtherType, first 16 bytes; aborts DMA on exit and on Ctrl-C | `dma_*` | default `-t 5000`: a LAN ARP broadcast arrives within a few seconds (I); expiry prints "no frame in 5000 ms" + `DMA_STAT`/`DMA_CTRL`. Needs `port enable` first |
| `poe status [card]` | read-only BCM59111 status: for each chip 0x28-0x2d on card 0/1 (mux select `0x10`/`0x20`, deselect `0x00` after): IC id `0x1b`, device id `0x43`, fw rev `0x41`, fw status `0x75` (`0xaa` = running), die temp `0x2c` (`367 - 2*raw` C when raw > `0x53`), power status `0x10`, mode `0x12`, per-port detect/class `0x0c-0x0f`, V/I `0x30..0x3e` | `i2c` uclass | **no write command exists.** The only writes are the PCA9548 channel select/deselect (mux at `0x70`). Registers `0x00` (interrupt status), `0x03` and `0x07` are clear-on-read: never read. Chip state with no stock fw loaded is expected (PoE is off at boot). Needs the HR2 I2C master (`drivers/i2c/iproc_i2c.c` exists in U-Boot, C; HR2 compat I) + `pca954x` mux driver (exists, C). Register map: `docs/board.md` PoE (C); a PoE chip that answers differently is a finding |
| `regdump [block...] [-u n] [-c]` | dumps named registers per block (`CMIC`, `TOP`, `GPORT0..2`, `XLPORT0/1`, `MMU`, `EPIPE`, `IPIPE`, per-port instances) as stable lines | `dump_next` | skips counter regs by default (regdb `ctr_idx`; `-c` includes), `-u` per unit. Feeds `reg_diff.py` (stock-prompt dump vs ours vs Linux debugfs) |

- Existing U-Boot commands we reuse as is: `md`/`mw` (CPU address space, for the A9/IPROC side), `i2c`, `wdt`, `pci`, `mdio`/`mii` if the DM `mdio` driver (`bcm_hr2_mdio.c`) is built; `miim` is the superset that also does C45 + AER + internal/external select. C (cmd/ listing: `mdio.c`, `mii.c`, `i2c.c`, `wdt.c`, `pci.c`; no `cmic`, `schan`, `smem`, `miim`, `tsc`, `gphy`, `port`, `dma`, `poe`, `regdump`)
- Cost: command layer ~600 lines C, ~10 KB text (I).

### Register-name table (`regdb/` -> compact table in the binary)

- Input: `regdb/hr2_regs.tsv` (6758 regs), `hr2_mems.tsv` (197), `hr2_blocks.tsv` (13 block rows) (generated by `scripts/hr2_regdb.py`, committed, #43). New `scripts/gen_bcm_names.py` writes `bcm_cmic_names_data.h`. C
- Format (I): names sorted, front-coded (shared-prefix length byte + suffix + NUL); entry = {u32 block-relative offset, u8 block index, u8 flags (64-bit, port-instanced, counter, array)}, ~6 B; mems = {name, base u32, index max u16, entry bytes u8, block u8}. Lookup = binary search on the decoded index (build once into a small RAM index at first use). Fields are not in the table: field decode is done on the host from `hr2_regs.py` (`reg_diff.py --fields`), so the 17 120 field records never enter the binary.
- Size, measured 2026-11-12 from `hr2_regs.tsv` (front-coding computed with a Python script, entries assumed 6 B; not yet built into U-Boot):

| Subset (`Kconfig` choice) | Regs | Names front-coded | + entries | Total |
|---|---|---|---|---|
| `BRINGUP`: CMIC, TOP, XLPORT, GPORT | 1869 | 17.9 KB | 11.2 KB | **~29 KB** |
| `CORE`: + MMU, EPIPE, IPIPE | 2501 | 25.6 KB | 15.0 KB | ~41 KB |
| non-IPROC (adds SER, OTPC) | 2815 | 28.0 KB | 16.9 KB | ~45 KB |
| everything (IPROC IDM boilerplate is 3943 of 6758) | 6758 | 50.2 KB | 40.5 KB | ~91 KB |
| memories (all) | 197 | 3.0 KB raw | ~2 KB | ~5 KB |

- Choice: `BRINGUP` + all memories (~34 KB) in the default image; `CORE` in dev builds. Against the 450-550 KiB baseline (section 2) that is +6-8 % for the default (I). Raw (unencoded) names would be 48 KB for `BRINGUP` alone, so the front-coding is worth it. C (arithmetic), I (final size)
- IPROC registers (3943) stay out: they are reachable with `md`/`mw` at their CPU addresses and `reg_diff.py` names them on the host. I
- **Linux debugfs: same table, same formatter.** `CONFIG_BCM_CMIC_DEBUGFS` (default n) exposes `cmic/<unit>/{reg,schan,mem,miim,regdump}` using `name_lookup` + `dump_next`: the `regdump` file is line-for-line the U-Boot output, so a Linux dump diffs directly against a U-Boot dump. This removes UNVR's "twin, keep in sync" problem (`regdump.py` vs `aldiag`). The ~35-45 KB table is `.rodata` under an option and can be dropped from an upstream submission without touching the core. I

### Checks (`scripts/uboot_test.py hw`, st1008f pattern: name -> (command, regex, cap))
- Read-only, run by default: `version`, `bdinfo` (256 MiB at `0x60000000`), `timer` (`sleep 5` vs host clock, +/-3 %), `wdt_alive` (`sleep 25`, no SP805 reset), `schan_rev` (`schan r UMAC_REV_ID 1` equals the DI §1 value), `regname` (`schan r XLMAC_CTRL` resolves), `smem_range` (out-of-range index refused), `miim_scan` (PHY id of front 1 = octal GPHY, I), `gphy_status`, `port_status`, `regdump_stable` (two `regdump CMIC` dumps equal on non-counter regs).
- Explicit only: `dma_rx` (needs `port enable` + cable; expects >= 1 frame, src port = the enabled logical port), `tsc_enable` (unit 1, after M7), `net`.
- Negative: `refuse_gpio` (`cmic w <GPIO reg>` refused without `-f`), `refuse_poe_write` (`poe enable` -> unknown subcommand).
- Caps: reply of a few hundred bytes is ~30 ms at 115200; 1 s names a hang, 5 s for `dma_rx`. Log `tmp/logs/uboot_test.log`. All runs on the board are the coordinator's, never started from this design.

## 11. Implementation plan (ordered)

Order rule: infrastructure and shared code first (written once, tested on the host), U-Boot as examination tool at M1 (before the NIC), Linux consumption in parallel, unit 1 last. "HW" = on the board, coordinator only.

| # | Step | Files | Exit criterion | HW |
|---|---|---|---|---|
| 1 | Shared-code infrastructure | `common/bcm-cmic/` skeleton (`bcm_cmic.h`, `bcm_cmic_os` contract), `scripts/sync_bcm_cmic.py`, `scripts/gen_bcm_cmic_regs.py`, `scripts/bcm_cmic_host_test.py` + fake CMIC, `mainline/0008-*` hook, `uboot-port/patches/0002` | `sync_bcm_cmic.py check` clean; each core file compiles alone under Linux (`COMPILE_TEST`) and U-Boot; host test passes; `dev.py gate` runs the three | no |
| 2 | Core read side | `schan`, `miim`, `dump`, `names` (+ `gen_bcm_names.py`); `us48_cmic.c` reduced to glue calling the core for its current register dump (same output) | host golden = DI §1 worked example; staged OOT module builds; module output unchanged vs today | module run |
| 3 | U-Boot scaffold, M1, read-only exam commands | `scripts/build_uboot.py`, `uboot-port/` (board, dts, defconfig, `ports.c`), `scripts/mkuimage.py`, `dev.py build-uboot / uboot-chain`, `cmd/bcm_cmic.c`: `cmic r`, `schan r`, `smem r`, `miim r`, `miim scan`, `regdump` | M1 criterion + `schan r UMAC_REV_ID 1` right + `wdt_alive` (25 s, no reset) | yes |
| 4 | Core write side + remaining commands | `gphy`, `port`, `tsc` (status + 13.5 enable), `init` modules; `cmic w`, `schan w`, `smem w`, `miim w`, `gphy`, `port`, `tsc status`, `poe status` (I2C + mux drivers in DT); M2 QSPI; `uboot_test.py hw` | checks green; `poe status` matches `docs/board.md` register facts on a chip that answers | yes |
| 5 | CMC0 DMA + first packet | core `dma`; `dma rx-test`; `bcm_hr2_cmic_eth.c` (DM_ETH), `bcm_hr2_mdio.c` | M3: RX frame in the DCB, TX seen by `tcpdump`; resolves DI §12 or triggers the unit-1 fallback | yes |
| 6 | Linux consumes the core (#4) | `us48_cmic.c` -> netdev/switch glue over `init`/`port`/`dma`/`tsc`/`gphy`; `CONFIG_BCM_CMIC_DEBUGFS`; `build_us48_cmic.py` stages the core | Linux first packet with the same core code that ran in U-Boot; `regdump` Linux vs U-Boot diff empty on config regs | yes |
| 7 | TFTP + Linux from RAM | `ramload`, `uboot-test net`, M4, M5 | section 5 criteria | yes |
| 8 | Flash slot | `uboot-chain-install`, M6 | section 5 | yes |
| 9 | Unit 1 / 10G | HR2 PAXB v1 in `pcie_iproc` (U-Boot), unit-1 `BCM_CMIC_F_HOSTMEM_REMAP` path, `tsc enable` in U-Boot | M7; blob policy decided before ucode | yes |
| 10 | Upstream | Linux RFC (`bcm-cmic` + first consumer + existing `mainline/` series), then U-Boot sync + `cmd` + board | review feedback folded into `common/` | no |

### New open items (file as issues when work starts)
- `udelay()` -> watchdog service in U-Boot: unverified; decides whether long waits need an explicit feed hook in `bcm_cmic_os`. I
- SBUS address composition for port-instanced registers (lane in the low byte for XLPORT, block per port for GPORT): needs one table per block type from `regdb` flags, or a small hand table verified against DI §1/§13. I
- Posted-write ordering of `writel` on the PCIe unit 1 before a DMA kick (read-back vs `dsb`): decide at step 9. I
- Whether the GPIO/SPI deny-list prefixes match regdb names at all (build-time check will say). I
- U-Boot `pca954x` + `iproc_i2c` on HR2 DT: untested; `poe status` depends on them. I
- Which of `drivers/net/bcm-cmic/` vs `lib/` U-Boot maintainers prefer for the upstream copy. I

## M3 as built: Ethernet on unit 1 xe1 (front 50, logical port 27) (build only, not yet run on the board)
- Unit 1, not the unit-0 front-1 plan above: the Linux driver (`kernel/us48-cmic/us48_cmic.c`) already pings on this port.
- Chip programming = shared core `common/bcm-cmic/bcm_cmic_hr2.c` (xe1 TSC/XLMAC enable, `EGR_ENABLE`, `EPC_LINK_BMAP`, private VLAN 48, `PORT_VID`, `CML_FLAGS` 0, static L2X entry, DMA init/abort, RX ring, 2-DCB TX). Linux refactor onto it is later.
- S-channel now waits for DONE only (error bits checked after); backstop 10 ms.
- Driver `uboot-port/drivers/net/us48_cmic_eth.c` (UCLASS_ETH, polled), DT node `ethernet@8040000` (`ubnt,us48-cmic-eth`), wired by `patches/0005`.
- Fixed addresses: CMIC `0x08040000` (PCIe BAR2, enumerated by stock; no PCI driver); DMA region `0x03000000`: RX DCBs `+0x0`, TX DCBs `+0x1000`, TX buf `+0x2000`, RX bufs (16 x 2 KiB) `+0x10000`. Below the chip's `0x08000000` window, outside relocated U-Boot (`0x6ffbe000`).
- Chip is touched on the first `start()` only (not at boot/probe): `dma_init` aborts stock's CMC0 ch0/ch1, then xe1 bring-up (skipped if XLMAC already enabled), L2 add, RX arm.
- `stop()` (also before `bootm`): abort both channels, L2 delete. VLAN 1 untouched (HiGig links to unit 0).
- Env defaults (defconfig, `USE_IPADDR` etc.): `ipaddr=192.168.25.207`, `netmask=255.255.255.0`, `gatewayip=192.168.25.254`, `serverip=192.168.25.30`; `ethaddr=02:75:73:34:38:50` (`include/configs/us48.h`). Build-time override: `US48_IPADDR` / `US48_SERVERIP` / `US48_GATEWAYIP`; run time: `setenv`.
- Link wait: none. sw1 STP can take 30+ s; `ping`/`dhcp`/`tftpboot` retries cover it.
- Timeouts: S-channel 10 ms, TX completion 2 ms (abort + log), DMA abort 10 ms, MIIM 65 us.
- `sf` stays read-only; the net driver never calls `sf probe`.

### M3 hardware test (coordinator)
1. `dev.py boot-flash --slot uboot` (or the `loady` + `bootm` chain), prompt `us48-uboot>`.
2. `ping 192.168.25.11` (expect `is alive`, first tries may time out while STP converges).
3. `tftpboot 0x01000000 us48-initramfs.bin` (host serves `192.168.25.30`; override with `setenv serverip`).
4. `iminfo 0x01000000` (header + data CRC OK), then `bootm 0x01000000` as before.
5. On failure: `cmic r 0x31140 4` (DMA_CTRL/STAT), `smem r 10 0x04000000+27 8` (PORT_TAB[27]), report the log lines `bcm-cmic:` / `us48-eth:`.

## M4 result (2026-11-12, #46)
- `tftpboot 0x01000000 us48-initramfs.bin` over xe1 (front 50): 8,496,209 B, 5,788 blocks, ~85 s, server log "sent" (`scripts/tftp_serve.py`, `setenv tftpdstp 6969`). iminfo header OK ("ARM OpenWrt Linux-6.18.55").
- Needed: fresh stock-initialised chip + a U-Boot built after the `CML_FLAGS = 0` change (a stale 17:29 build kept CML 1 and failed).
- Open: `iminfo` checksum / `bootm` of the RAM image reset the board (SP805 armed by `dev.py` for 20 s); not yet diagnosed. Speed ~100 kB/s, lock-step TX.
- U-Boot has no `ping` command in this build.
