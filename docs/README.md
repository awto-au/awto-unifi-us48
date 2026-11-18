# docs index

- One line per doc, grouped. `dev.py doc-check` fails on a broken link or anchor and on a doc missing from (or listed twice in) this index.
- Findings and open questions live in GitHub issues, not here.

## Start here

- [mainline.md](mainline.md): mainline Linux port: base, patch series, config, build, rootfs (#15)
- [DECISIONS.md](DECISIONS.md): standing decisions, one line each, with issue links
- [hardware.md](hardware.md): board hardware, console, U-Boot, flash map
- [port-map.md](port-map.md): front panel -> chip -> software port names
- [hwdb.md](hwdb.md): hwdb schema, single source of truth for hardware facts (#72)
- [hwdb-map.md](hwdb-map.md): hwdb map (generated)

## Boot chain and U-Boot

- [uboot.md](uboot.md): stock U-Boot `usw-v1.1.6.122` (uboot-1)
- [uboot-second-stage.md](uboot-second-stage.md): awto-uboot live design (#46)
- [uboot-self-boot.md](uboot-self-boot.md): boot straight into awto-uboot without uboot-1 (#63)
- [autochain.md](autochain.md): uboot-1 auto-chains awto-uboot on every power-on (#63)
- [uboot-cpu-clock.md](uboot-cpu-clock.md): uboot-2 CPU clock (#17)
- [uboot-emu.md](uboot-emu.md): uboot-1 host emulator (#69)

## Second chip (unit 1) and a2

- [a2.md](a2.md): a2, the unit 1 Cortex-A9
- [a2-design.md](a2-design.md): a2 Linux driver design (#54, #70)
- [a2-linux.md](a2-linux.md): Linux on a2 (#79)
- [a2-uboot.md](a2-uboot.md): awto-uboot on a2 (#83)
- [pcie-bringup.md](pcie-bringup.md): PCIe RC bring-up to unit 1 from cold (#76)
- [pcie-harness.md](pcie-harness.md): PCIe bus harness unit 0 <-> unit 1 (#80)
- [pci-layer.md](pci-layer.md): PCI layer for the second chip (`us48_pci`)

## Clocks, memory, SoC

- [clocks.md](clocks.md): rated max vs what runs (#17, #20, #55)
- [clock-audit.md](clock-audit.md): clock / DDR / die-temperature audit
- [cpu-sweep.md](cpu-sweep.md): A9 clock sweep under Linux (#17)
- [sram-clock-switch.md](sram-clock-switch.md): SRAM-resident clock switch with rollback (#17)
- [ddr.md](ddr.md): DDR3 shmoo, boot behaviour, clock headroom (#55)
- [sram.md](sram.md): on-chip memory (SRAM, L2-as-RAM, ROM)
- [core-bringup.md](core-bringup.md): core SoC bring-up (#84)

## Switch driver and forwarding

- [switchdev.md](switchdev.md): switchdev L2 offload in `us48_cmic`
- [switch-model.md](switch-model.md): Linux switch model
- [cpu-port-policy.md](cpu-port-policy.md): CPU-port meters and CoS map
- [route-nat-fw.md](route-nat-fw.md): copper routing, NAT, firewall tests (#40)
- [driver-internals.md](driver-internals.md): stock driver internals for the Linux switch driver (#47)

## Board peripherals

- [board.md](board.md): board peripherals from stock static RE
- [fan.md](fan.md): fan control and the quiet rule
- [leds.md](leds.md): system LED, reset button, LEDUP port LEDs (driver, sysfs, board test)
- [ledup-stock.md](ledup-stock.md): stock LEDUP program, analysed; chain format source
- [poe.md](poe.md): PoE (BCM59111): stock sequence and a safe Linux sequence
- [kernel-pse.md](kernel-pse.md): stock kernel PSE platform driver, board code, gpiodev
- [selftest.md](selftest.md): boot self-test `us48-selftest`

## Stock firmware RE

- [vendor-stack.md](vendor-stack.md): stock vendor stack: what we hold, RE status, traces
- [stock-debug-cli.md](stock-debug-cli.md): stock debug CLI (switchdrvr 7.5.15)
- [stock-bootlog-facts.md](stock-bootlog-facts.md): stock boot-log facts
- [init-phases-u0.md](init-phases-u0.md): stock init phases, unit 0
- [init-phases-u1.md](init-phases-u1.md): stock init phases, unit 1
- [edgeswitch.md](edgeswitch.md): EdgeSwitch ESWH 1.12.2 cross-check
- [REGISTERS.md](REGISTERS.md): HR2 register / memory naming DB (#30)

## References

- [prior-art.md](prior-art.md): prior art (HR2 relatives, drivers, bootloaders)
- [sources.md](sources.md): sources
