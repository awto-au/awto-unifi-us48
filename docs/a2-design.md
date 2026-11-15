# a2 (unit 1 Cortex-A9): Linux driver design (#54, #70)

Dan, 2026-11-15: a2 is used only through proper kernel drivers and standard frameworks. No `/dev/mem`, raw BAR pokes from userspace, or one-off scripts in the product path. `rust/a2ctl` and `scripts/a2_*.py` stay as bring-up and RE tools only.

## Layers

| Layer | Linux framework | Owns | Today |
|---|---|---|---|
| `us48_pci` | PCI driver for `0000:01:00.0` + **auxiliary bus** parent | BARs, PAXB windows (OARR/IMAP), link state, shutdown, serialised IMAP window moves | exists with exported raw accessors (docs/pci-layer.md); convert to auxiliary devices |
| unit 1 AXI access | **regmap** (custom bus over the IMAP window) | every unit 1 register access; access table generated from hwdb (#72): readable/writable/volatile ranges, so a dead address returns `-EIO` instead of hanging AXI | new |
| `us48-a2` | **remoteproc** driver (aux device `us48_pci.a2`) | reset (CRU `ARM_RST_N`), halt/start via CoreSight (DSCR/ITR), ELF firmware load from `/lib/firmware/us48-a2.elf`, resource table, carveouts, crash recovery, coredump | stub loader logic in `us48_pci_a2_*`; move it here |
| a2 memory | DT `reserved-memory` carveout (`no-map`) in a1 DDR | a2 code/data in host DDR, cached on a2 (#53: same speed as SRAM) | today OARR_0 maps all of host RAM: narrow it to the carveout (OMAP = carveout base, window = carveout size) |
| messaging | **rpmsg** over virtio vrings in the carveout + **mailbox** doorbell | a1 ↔ a2 commands and streams; userspace through `rpmsg_char` | stub mailbox (cmd 1 call, 2 read32, 3 write32): replaced |
| doorbell a2 → a1 | mailbox controller: PCIe MSI raised by a2 if PAXB allows it (I, to check), else hrtimer poll of a doorbell word | wake a1 | new |
| a2 log | remoteproc **trace** resource (ring in carveout) → debugfs `trace0` | out-of-band log, including the uboot-1 replay (#69 2b) | new |
| a2 debug | remoteproc debugfs + our debugfs: halt, PC sample (PCSR), registers via ITR, breakpoints/watchpoints | the out-of-band watch from a1 | the `us48_pci_a2_halt/set_pc/pc_sample` code moves here |
| unit 1 clocks | **common clock framework** provider (aux `us48_pci.clk`): CCU ARM PLL, GENPLL ch3 (AXI)/APB | `clk_set_rate` with the lock wait and timeout; consumers: remoteproc (a2 CPU), cmic | new; replaces raw PLL pokes in trials |
| clock trials | `us48-a2` debugfs knob `cpu_mhz` / `axi_mhz`: stop a2 → `clk_set_rate` → lock → start a2 → self-test fw | probe-to-failure (#17) through the clk API | new; debug-only interface, documented |
| unit 1 sensors | **hwmon** (PVTMON both units) + **thermal** zone; fans as a **cooling device** | temperatures and fan policy (#19) | fand shell loop today |
| unit 1 switch | aux `us48_pci.cmic` → `us48_cmic_xe` netdev (later switchdev, #57) | CMIC, S-channel, packet DMA | separate PCI driver today (pci-layer.md migration steps 1-5) |

## Rules

- One owner per resource: `us48_pci` owns the PAXB windows. Sub-drivers ask through the aux/regmap API and never move a window themselves.
- Link down or PERST asserted → every API returns `-ENODEV`. No access is attempted (the awto-uboot hang, #76).
- Addresses and fields come from `hwdb/gen/us48_hwdb.h`, never literals (#72).
- Timeouts follow the CLAUDE.md rules: state what is waited for, the expected duration and source, ×1.25, and log on expiry.
- a2 firmware lives in the repo (`a2fw/`). It is an ELF with a resource table (carveout, vdev, trace) and is built by a script. The first firmware is a self-test (benchmark, heartbeat, S-channel read). The uboot-1 replay (#69) is a later firmware.
- Upstream shape: auxiliary bus + regmap + remoteproc + rpmsg + clk + hwmon are all standard, which keeps #41 possible.

## Order

1. `us48_pci` → auxiliary devices + regmap with the hwdb access table. `us48_cmic_xe` moves to the aux device.
2. `us48-a2` remoteproc with the carveout and self-test firmware: start/stop/reload without a power-cycle (#54 done criteria).
3. Trace ring, debugfs halt/PC-sample.
4. clk provider + the trial knob → probe to failure (#17).
5. rpmsg + doorbell.
6. hwmon/thermal/cooling (#19), shared with unit 0.

Related: #54, #70, #53, #17, #19, #57, #41, [pci-layer.md](pci-layer.md), [a2.md](a2.md).
