# Mainline Linux port (issue #15)

- Develop on latest mainline. OpenWrt (`openwrt/`, 6.18) is parked and not used by the image build.
- Base: torvalds/linux tag `v7.4-rc1` (`BASE` in `scripts/build_mainline.py`); branch `awto/us48/mainline-v7.4-rc1` (local, not pushed). Rebased from v7.3-rc7, all 7 patches apply clean.
- Tree: `tmp/linux-mainline` = `git worktree` of the machine-shared Linux store (#45), not a clone.
  - Store reached via gitignored symlink `tmp/linux-store` (or `$US48_LINUX_STORE`, `--store`); `build_mainline.py` creates the worktree if missing.
  - Never `--depth`, `git gc` or `git repack` in the store or its worktrees (machine timer does gc).
- Series: `mainline/0001..0009-*.patch` (`git format-patch`, `Signed-off-by: TODO` = unsigned on purpose).
- Config: `mainline/us48_defconfig` on `allnoconfig` (explicit list, ~520 `=y`; debug profile as OpenWrt `hr2/config-6.18`, #4, #64). gzip kernel + initramfs.
  - `build_mainline.py` aborts if any listed symbol is missing from the final `.config` (unmet dependency).
  - Slot limit: `kernel0` linux slot = 0xD00000 B (13 MiB, `SLOTS["linux"]` in `dev.py`).

## Build

- `python3 scripts/build_mainline.py [--profile normal|debug]` (default debug) — rebuilds the branch from `mainline/*.patch` (`git am`), configures, builds, packs.
- Config: `allnoconfig` + `mainline/us48_defconfig` + `mainline/us48_<profile>.config` + initramfs = `tmp/mainline/rootfs` (stage of `mainline/rootfs/`, below) + modules + firmware + `/dev/console` node, root-owned.
- Kernel + modules toolchain: Fedora `arm-linux-gnu-gcc` (dnf `gcc-arm-linux-gnu binutils-arm-linux-gnu`). No mkimage: `build_mainline.py` writes the legacy uImage header itself.
- Userspace toolchain: musl-cross-make (gcc 14.3, musl 1.2.6, soft-float, Cortex-A9; Fedora's cross gcc is hard-float only, the HR2 A9 has no VFP). Built once by `build_rootfs.py` into `/mnt/1tnvme/toolchains/musl-14.3.0-arm-cortex-a9-soft` (symlink `tmp/musl-arm`, `$US48_MUSL_TC`); mcm clone in `/mnt/1tnvme/git-mirrors/richfelker/musl-cross-make`, tarballs in `/mnt/1tnvme/toolchains/dl`.
- Output: `tmp/mainline/us48-mainline-<profile>.bin` (build dir `build-<profile>`, stage `rootfs-<profile>`) = legacy uImage, load/entry `0x00018000`, zImage + appended DTB; boots via stock U-Boot `loady 0x01000000; bootm` or awto-uboot TFTP (`dev.py u2-boot --from tftp`).
- `CONFIG_ARM_ATAG_DTB_COMPAT` off; DT has no bootargs, so `CONFIG_CMDLINE="console=ttyS0,115200 earlycon"` applies.
- Flags: `--keep-branch` (build tree as is), `--save-patches` (branch -> `mainline/`), `--checkpatch`, `--dtbs-check`, `--prepare-only`.
- Logs: `tmp/logs/build_mainline.log`, `build_mainline-make.log`, `build_mainline-dtbs_check.log`.
- Edit flow: change `tmp/linux-mainline` on the branch, commit, `--save-patches`, commit `mainline/`.

## Rootfs (`mainline/rootfs/`, `scripts/build_rootfs.py`)

- Contents: busybox 1.37.0 static (`busybox.config`, regenerate with `build_rootfs.py --regen-busybox-config`), dropbear 2026.94 static multi-binary (ssh server, key only), `overlay/` copied as is.
- Pinned tarballs (sha256 in the script) in `/mnt/1tnvme/toolchains/dl`.
- `/init`: a2 role (DT compatible `ubnt,us48-a2`) = banner + console shell; a1 = `exec /sbin/init` (busybox, `/etc/inittab`: `sysinit` = `/etc/rc.sh`, `askfirst` shell on the console).
- `/etc/rc.sh`: mounts (the SP805 awto-uboot arms, ~46 s, is fed by the kernel driver: `WATCHDOG_HANDLE_BOOT_ENABLED`, #84), hostname `us48`, `syslogd -O /tmp/messages` (no SysV IPC in the kernel: no `logread`), then `/etc/rc.d/S*` in order:
  - `S04us48-net`: insmod `us48_cmic unit0=1`, `us48_pci`, `us48_cmic_xe`; ge1/xe49/xe50 up, ge1 `192.168.25.207/24` (ip-registry `us48-boot`).
  - `S10us48-dma`: insmod `us48_idm_reset` (reset controller, DT `dmac_idm`), then `pl330` (DT `resets`; #84). Explicit insmod like the other scripts: no udev/depmod in the image, so DT/modalias autoload does not exist. Board 2026-11-16: `Num_Chans-8` probed on a cold boot.
  - `S14us48-thermal`: insmod `us48_cmic_gpio`, `us48_cmic_mspi`, `us48_pvtmon`, `us48_fan` (fans: `docs/fan.md`).
  - `S20dropbear`: `dropbear -R -s -g -p 22`; `/root/.ssh/authorized_keys` = `mainline/rootfs/authorized_keys` (public keys).
  - `S90us48-selftest`: `us48-selftest` in the background (`docs/selftest.md`).
- `/usr/sbin/us48-poe`, `us48-poe-off`: present, not run at boot (bench image keeps the PoE state stock left; run `us48-poe-off` by hand).
- Modules: `/lib/modules/<release>/*.ko` (loaded by explicit insmod order in the rc scripts; `us48_a2` and `us48_bustest` are loaded by hand). Firmware: `/lib/firmware/us48-a2*.elf` (from `tmp/a2fw`).
- initramfs = a gen_init_cpio list of the stage (all uid/gid 0), written by `build_mainline.py`; modules are stripped of debug info.
- Debug profile only: static test tools in `/usr/bin` (`build_rootfs.py --tools`, docs/core-bringup.md); in-tree `=m` modules (`pl330`, `dmatest`, torture, `test_*`) are staged into `/lib/modules/<release>` by `build_mainline.py`.
- Stage is rebuilt on every `build_mainline.py` run (`tmp/mainline/rootfs`); the busybox/dropbear builds are a few seconds each.

## Module reload (#78, #56)

- `us48_cmic` (core + unit 0 ge1) and `us48_cmic_xe` (unit 1 xe49/xe50/ge40) are reloadable on the fly.
- Every load resets the chip and runs the full cold init, whatever state it is in (no ring-map guard: `BCM_HR2_INIT_FORCE`; awto-uboot keeps its guard). A load briefly resets the switch: traffic drops (ge1 = ssh), expected.
  - Unit 0: `bcm_hr2_u0_cold_init` (stock write list), unit 1: `bcm_hr2_u1_cold_init` with the minimum list `bcm_cmic_hr2_init1_min_data.h` (5541 ops, `scripts/gen_bcm_hr2_init1_min.py` from `init_phases.py --emit-min`).
  - Order: abort both DMA channels (logged as "left by bootloader" if one was running), TOP reset + cold init, credit release (`bcm_hr2_dma_init`), rings, ports.
- Unload (`rmmod`): link work cancelled, netdevs unregistered (ndo_stop stops the poll timer, NAPI, both channels), both channels aborted and `DMA_DESC` cleared, skb pool purged; rings and netdev memory freed by devres afterwards. A self-check at the end logs `unbind self-check` (and `WARN`s) if DMA_CTRL/STAT, `nopen`, the TX ring or the skb pool are not clean. Reboot/shutdown keeps `us48_quiesce`.
- Board 2026-11-16 (#78, normal profile):
  - `us48_cmic_xe` reload x3: clean (2.7 s each, ge1/ssh stay up, unit 1 cold init + TSC download 1.6 s + CRC a081, no WARN, no leaks).
  - `us48_cmic` (unit 0) reload FAILS: chip init + link up OK, ssh does not return, TX dead (TX counter frozen, `TX stuck: DMA_STAT 00000320` from the netdev watchdog; ch0 abort "still active"). RX still counts. Same with `gaps=1`.
  - Same signature on the warm path (awto-uboot already initialised the chip, then Linux's forced init): any second full unit 0 init after a first one leaves TX hung; only a power cycle clears it. Fixed: CMIC_CPS_RESET hook in the unit 0 init (#78).
  - Module param `gaps` (#34) exists since 2026-11-16 (was documented, not wired).
- `us48_cmic` cannot go while `us48_cmic_xe` is loaded (symbols); `scripts/mod_reload.py` handles the chain.
- `scripts/mod_reload.py <module> [args]`: one detached `rmmod; insmod` script on the board (log `/tmp/mod_reload.log`), waits for ssh (30 s first-run limit), fetches the log; console-socket fallback. Log `tmp/logs/mod_reload.log`.
- 10G microcode: `hwdb/firmware/us48-tsc.bin` -> `/lib/firmware`, loaded by `us48_cmic_xe` (driver-internals 9, Linux loader).
- awto-uboot: `remove` of both eth devices (`DM_FLAG_ACTIVE_DMA`) aborts the channels at bootm, and the common abort clears `DMA_DESC`. The earlier Linux log `stock left ch0 CTRL 00000001` had EN clear (bit 0 = TX direction only): no DMA was running.

## Profiles

| | `normal` | `debug` (default) |
|---|---|---|
| Use | final performance tests | general work |
| Fragment | `us48_normal.config` (`LOCALVERSION=-norm`, no debug options) | `us48_debug.config` (`-dbg`) |
| Image | `tmp/mainline/us48-mainline-normal.bin` | `tmp/mainline/us48-mainline-debug.bin` |
| Fits 13 MiB slot | must (build aborts otherwise) | not required: RAM-boots via TFTP (`dev.py u2-boot --from tftp --image ...`) |

- Base `us48_defconfig` = hardware + userspace needs; CPU-speed mitigations (Spectre) stay off in both.
- Release string differs per profile (`7.4.0-rc1-norm+` / `7.4.0-rc1-dbg+`); modules are built per build dir and staged under that release.
- Debug fragment: memory (KFENCE, SLUB_DEBUG_ON, DEBUG_PAGEALLOC, PAGE_POISONING, INIT_ON_ALLOC/FREE, DEBUG_VM, KMEMLEAK, DEBUG_SG/LIST/PLIST/NOTIFIERS, HARDENED_USERCOPY, FORTIFY, STACKPROTECTOR_STRONG), locking (PROVE_LOCKING, PROVE_RCU, DEBUG_OBJECTS*, mutex/rwsem/spinlock/ww checks), DMA_API_DEBUG, DEBUG_SHIRQ/DEVRES, softlockup panic, hung task, WQ_WATCHDOG, UBSAN (bounds, shift, unreachable), KALLSYMS_ALL, DWARF5, ftrace, kprobes, perf, dynamic debug, debugfs, IKCONFIG, PANIC_ON_OOPS. `DEBUG_DRIVER` off (noisy).
- Lockdep tables sized down (`LOCKDEP_*_BITS`): lowmem was 64 MiB while the a2 block was `no-map`; with the reusable pool (patch 18) it is 128 MiB, of which 64 MiB is movable-only until the early allocations are done. Tables not yet re-grown.
- KASAN generic outline (`KASAN_STACK`) is in the debug fragment since patch 18, first boot unverified (#84). History: with the `no-map` a2 block lowmem was 64 MiB and inline panicked at memmap allocation (kernel 35 MiB + 8 MiB shadow + 8 MiB CMA + 14 MiB memmap), outline hung before earlycon output. Risk now:
  - Early allocations (shadow = 1/8 of all lowmem incl. the pool = 16 MiB, memmap) must fit in the 64 MiB of lowmem outside the pool + default CMA: the pool is `memblock_reserve`d until `cma_activate_area()`. Sizes: docs/core-bringup.md (Memory map).
  - A silent hang before earlycon would be the old outline failure again (not explained by memory): then boot with the normal profile and bisect `KASAN_STACK`, `KASAN_OUTLINE`.
- Not enabled:
  - KASAN inline: not tried since the pool change (outline only, below).
  - KCSAN, KMSAN, KASAN_SW_TAGS/HW_TAGS: no ARM32 support.
  - RCU stall detector: `TINY_RCU` (SMP off) has none.
  - `DMA_API_DEBUG_SG`, `DEBUG_TIMEKEEPING`: no longer exist in 7.4.
  - BTF: not enabled (DWARF5 only; vmlinux debug info is not loaded to the board).

## Patches

| # | Subject | Purpose |
|---|---|---|
| 1 | dt-bindings: arm: bcm: hr2: Add Ubiquiti UniFi US-48-500W | `brcm,hr2.yaml` only allowed `ubnt,unifi-switch8` + `brcm,bcm53342`; second `oneOf` entry with `brcm,bcm53344` |
| 2 | dt-bindings: PCI: brcm,iproc-pcie: Add Hurricane 2 compatible | `brcm,iproc-hr2-pcie` + `brcm,iproc-pcie` fallback |
| 3 | PCI: iproc: Skip APB error disable on Hurricane 2 | HR2 PAXB has no `APB_ERR_EN` (0xf40); touching it aborts during bus scan. Keyed on the compatible |
| 4 | PCI: iproc: Return all-ones for aborted config reads on Hurricane 2 | absent function (01:00.2 of BCM53346) aborts instead of all-ones; fault hook as pci-imx6, pcie0 + pcie1 windows. `hook_fault_code` is `__init`, not exported: built-in only (`!MODULE`), ARM mode only |
| 5 | ARM: dts: broadcom: bcm-hr2: Add 50 MHz APB clock for UART/SP804/SP805 | UART divisor was computed from 25 MHz osc; SP804/SP805 had no clocks, never probed, no restart handler. Also affects UniFi Switch 8 (untested there) |
| 6 | dt-bindings: mfd: syscon: Add brcm,hr2-dmu | `syscon` alone fails dtbs_check ("too short"); needed by patch 7 |
| 7 | ARM: dts: broadcom: Add Ubiquiti UniFi US-48-500W | board DTS: 2x128 MiB RAM, SPI-NOR map, CMIC SMBus (0x48000000) + PCA9548 + 4x LM75 on ch6, pcie0, DMU syscon + syscon-reboot (0x200, mask 0x2, value 0), UART1 console; no bootargs |
| 8 | ARM: dts: broadcom: bcm-hr2: Fix PMU interrupts for the single CPU | two PMU IRQs + one `interrupt-affinity` entry made `armv7-pmu` fail with -EINVAL; one IRQ |
| 9 | hwrng: bcm2835: Allow building on Hurricane 2 (ARCH_BCM_IPROC) | HR2 `brcm,bcm-nsp-rng` is handled by this driver; Kconfig did not list ARCH_BCM_IPROC |
| 10-13 | us-48-500w a2 reserved memory, auxiliary bus selectable, lm75 `#thermal-sensor-cells`, fans + thermal zones | see the patch files |
| 14 | ARM: dts: broadcom: bcm-hr2: Add the APB clock to the PL330 DMA controller | `apb_pclk` missing, node never probed; `dma` label; fixed PERIPHID (#84) |
| 15 | ARM: dts: broadcom: us-48-500w: Enable the PL330 DMA controller | `okay`; `pl330` is a module (#84, docs/core-bringup.md) |
| 16 | ... Release the PL330 IDM reset through a reset controller | `dmac_idm` node for `kernel/us48-idm-reset` |
| 17 | ... Read the boot flash with 1-1-4 quad | `spi-rx-bus-width = <4>` |
| 18 | ... Make the a2 memory a reusable CMA pool | `a2@4000000` `shared-dma-pool` + `reusable` (was `no-map`), consumer node `a2-rproc` with `memory-region` and the offsets (#84 item 11) |
| 19 | ... Add the on-chip SRAM as mmio-sram | `sram@1b000000` 16 KiB, one `dma-desc` pool; `CONFIG_SRAM=y` (#84 item 12) |

Differences to the OpenWrt DTS (`openwrt/0001-*.patch`):
- APB clock moved into `bcm-hr2.dtsi`; no per-board clock overrides.
- No `bootargs`; `stdout-path` only.
- LM75 nodes named `temperature-sensor@`; `dmu` compatible `brcm,hr2-dmu`, `syscon`.

## Checks (2026-10-14, v7.3-rc7)

- checkpatch `--strict`, every commit: 0 findings except the intended unsigned-off trailer, and for patch 7
  `WARNING: added, moved or deleted file(s), does MAINTAINERS need updating?` (false positive: `arch/arm/boot/dts/broadcom/` is already covered).
- `dt-doc-validate` + `yamllint` on `brcm,hr2.yaml`, `brcm,iproc-pcie.yaml`, `syscon.yaml`: clean.
- `dtbs_check` (`CHECK_DTBS=y W=1`, dtschema from `tmp/dtschema-venv`) on `bcm53344-ubnt-unifi-us-48-500w.dtb`:
  0 findings in board nodes. 9 remaining are pre-existing in `bcm-hr2.dtsi` and equally hit `bcm53340-ubnt-unifi-switch8`:
  mpcore bus nodename, `brcm,hr2-armpll` (#clock-cells, clock-output-names), `dma@` nodename, qspi `interrupt-names`,
  `arm,sp804` missing `arm,primecell`, `nor@` / `cfi-flash`.
- `iproc_hr2_abort_*` symbols present (built-in). Image must stay < kernel0 (0xf00000 B). Not booted (#15).

## Before upstream submission

- Real `Signed-off-by` (and `From:`), remove `TODO`.
- Boot-test the image on the bench board (not done; `US48_IMAGE=tmp/mainline/us48-mainline.bin python3 scripts/dev.py boot-ram` / `flash-net` + `boot-flash`, #15): UART baud, SP805 restart, syscon-reboot, PCIe enumeration without oops, LM75 hwmon.
- Patch 4 on hardware: confirm the oops-free scan of 01:00.2 and that a PCIe error on pcie1 stays fatal elsewhere.
  Expect review pushback on the open-coded instruction decoder; alternative is a config-read path that avoids the abort (needs PAXB register that reports absent function).
- Patch 5: boot-test on UniFi Switch 8 (Florian) or get a Tested-by; UART 50 MHz is from US-48 stock logs only.
- Fix the 9 pre-existing `bcm-hr2.dtsi` schema findings or leave them (separate series; `arm,primecell` on SP804 changes probing).
- `scripts/get_maintainer.pl` per patch for To/Cc; send via `git send-email`; PCI patches to linux-pci (Bjorn Helgaas, Lorenzo/Krzysztof), DT to Florian Fainelli + linux-arm-kernel/bcm lists.
- Clock `clock-apb` naming and the `brcm,hr2-dmu` compatible are my choices; check with maintainers.
- Public repo: patches carry no private paths; confirm again before any send.
