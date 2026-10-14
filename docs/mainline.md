# Mainline Linux port (issue #15)

- Develop on latest mainline, then backport to OpenWrt 6.18 (`openwrt/`, patches-6.18/950+951).
- Base: torvalds/linux tag `v7.3-rc7`; branch `awto/us48/mainline-v7.3-rc7` (local, not pushed).
- Tree: `tmp/linux-mainline` = `git worktree` of the machine-shared Linux store (#45), not a clone.
  - Store reached via gitignored symlink `tmp/linux-store` (or `$US48_LINUX_STORE`, `--store`); `build_mainline.py` creates the worktree if missing.
  - Never `--depth`, `git gc` or `git repack` in the store or its worktrees (machine timer does gc).
- Series: `mainline/0001..0007-*.patch` (`git format-patch`, `Signed-off-by: TODO` = unsigned on purpose).
- Config fragment: `mainline/us48.config`.

## Build

- `python3 scripts/build_mainline.py` — rebuilds the branch from `mainline/*.patch` (`git am`), configures, builds, packs.
- Config: `multi_v7_defconfig` + `mainline/us48.config` + initramfs from `tmp/openwrt-build/.../root-bcm53xx`
  (staged copy + `/init -> sbin/init` + `/dev/console` node, root-owned).
- Toolchain: OpenWrt gcc 14.4 musl (`tmp/openwrt-build/staging_dir`). mkimage from the OpenWrt host dir.
- Output: `tmp/mainline/us48-mainline.bin` = legacy uImage, load/entry `0x00018000`, zImage + appended DTB.
  Same format as `tmp/tftp/us48-initramfs.bin`; boots via stock U-Boot `loady 0x01000000; bootm`.
- `CONFIG_ARM_ATAG_DTB_COMPAT` off; DT has no bootargs, so `CONFIG_CMDLINE="console=ttyS0,115200 earlycon"` applies.
- Flags: `--keep-branch` (build tree as is), `--save-patches` (branch -> `mainline/`), `--checkpatch`, `--dtbs-check`, `--prepare-only`.
- Logs: `tmp/logs/build_mainline.log`, `build_mainline-make.log`, `build_mainline-dtbs_check.log`.
- Edit flow: change `tmp/linux-mainline` on the branch, commit, `--save-patches`, commit `mainline/`.

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
- Build (2026-10-14, 30 cores, ~2.5 min): `tmp/mainline/us48-mainline.bin` 15,499,356 B (zImage 15,491,584 incl. initramfs, DTB 7,708);
  uImage header load/entry `0x00018000`, no compression. Fits kernel0 (0xf00000 = 15,728,640 B) by ~229 KB.
  `iproc_hr2_abort_*` symbols present (built-in). Not booted.

## Before upstream submission

- Real `Signed-off-by` (and `From:`), remove `TODO`.
- Boot-test the image on the bench board (not done: board owned by another session): UART baud, SP805 restart, syscon-reboot, PCIe enumeration without oops, LM75 hwmon.
- Patch 4 on hardware: confirm the oops-free scan of 01:00.2 and that a PCIe error on pcie1 stays fatal elsewhere.
  Expect review pushback on the open-coded instruction decoder; alternative is a config-read path that avoids the abort (needs PAXB register that reports absent function).
- Patch 5: boot-test on UniFi Switch 8 (Florian) or get a Tested-by; UART 50 MHz is from US-48 stock logs only.
- Fix the 9 pre-existing `bcm-hr2.dtsi` schema findings or leave them (separate series; `arm,primecell` on SP804 changes probing).
- `scripts/get_maintainer.pl` per patch for To/Cc; send via `git send-email`; PCI patches to linux-pci (Bjorn Helgaas, Lorenzo/Krzysztof), DT to Florian Fainelli + linux-arm-kernel/bcm lists.
- Clock `clock-apb` naming and the `brcm,hr2-dmu` compatible are my choices; check with maintainers.
- Public repo: patches carry no private paths; confirm again before any send.
