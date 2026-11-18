# Gaps: what is missing for a full native Linux port

Ranked by blocking effect. Snapshot 2026-11-17. Detail lives in the linked docs and issues.

| # | Gap | Why it matters | Evidence | Issue | Cheapest next step | Board? |
|---|---|---|---|---|---|---|
| 1 | Forwarding model: switchdev/DSA, VLAN/FDB/STP offload, bridge across both chips (HiGig 28/29) | Today each port is a private CPU-only VLAN; no switching in Linux | [switch-model.md](switch-model.md), [driver-internals.md](driver-internals.md) | #57, #4 | isolated software-bridge baseline, then same-chip and cross-HiGig hardware forwarding | bench y |
| 2 | Large-frame RX loss on ge1 (>5 Mbit/s) | Management/CPU path unusable for real traffic | #56 | #56 | read GPORT RDBGC/RDROP + MMU drop counters, CPU-port COS limits | y |
| 3 | Octal-PHY carrier and QSGMII2X/GPORT2 link | Representative 1G pairs on unit 0 (fronts 17/8) and unit 1 (fronts 41/32) both link; individual jacks are not cable-tested | [switch-model.md](switch-model.md), [driver-internals.md](driver-internals.md) §12.6 | #58, #4, #31 | Continue to ASIC forwarding; investigate individual copper ports only if inventory or use exposes an anomaly | y |
| 4 | SFP+/TSC link, peer mapping, and cage control (presence, TX-disable, EEPROM) | `xe49` has PCS sync/link in mainline; `xe50` is down; `xe49` far end and packet path remain unverified | [switch-model.md](switch-model.md), [board.md](board.md) SFP | #58, #31 | Keep `xe49` as the representative SFP+ link; identify its far end and verify traffic if a known peer is available; investigate other cages only on anomalies | y |
| 5 | Unit 1 (PCIe) path: IRQ instead of polling, RX stall, driver structure | Half the ports sit behind it | #4 comments, #46 | #4, #46 | RX counters on unit 1 vs unit 0 | y |
| 6 | PoE: BCM59111 PSE driver, firmware load, per-port control, power budget (436 W) | Core product feature; today PoE is only turned off | [poe.md](poe.md), [kernel-pse.md](kernel-pse.md) | #26, #23, #24, #28 | userspace prototype on one card (needs owner OK) | y |
| 7 | Flash layout, rootfs, sysupgrade A/B | No installable image; initramfs only | [hardware.md](hardware.md) flash map | #73 (from #59, #74) | flash map + rootfs placement design, RAM-boot squashfs test | doc n, test y |
| 8 | LEDs: port link/act (LEDUP program, serial pins), PoE LEDs (MSPI), front 49-52, system LED | No user feedback; stock logic known only statically | [board.md](board.md) LED section | #14, #60 (NEW, system LED) | LEDUP pin mux + per-LED toggle table | y |
| 9 | Buttons, reset, system LED, PoE IRQ/reset GPIOs in DT; panic/last-reset reason | No factory reset, no failsafe trigger | [board.md](board.md) GPIO map | #60 (NEW), #29 | `gpio-keys` GPIO 15, `gpio-leds` 10/11 in DT | y |
| 10 | Fans/thermal: preset-only, no tach, no off, policy daemon is bench-only | Heat/noise; safety net for all other work | [fan.md](fan.md) | #12, #19, #21, #22 | temperature-driven policy with fail-safe | y |
| 11 | QSPI at 12.5 MHz DT, 4-byte-mode reset hazard after panic | Boot time; board can hang until power-cycle | #20 | #20 | `sysrq-c` test + rated-clock DT | y |
| 12 | Watchdog/reboot path: SP805 armed by awto-uboot, fed by the kernel driver (`WATCHDOG_HANDLE_BOOT_ENABLED`); DMU restart; no recovery policy | Hangs in the field | [mainline.md](mainline.md) rc.sh | #20, #60 | covered by #20/#60 tests | y |
| 13 | DDR shmoo/clock headroom; CPU 400 MHz vs 800 MHz | Performance ceiling (NAT/CPU path) | [hardware.md](hardware.md) rates | #55, #17 | RAM-only clock trial | y |
| 14 | Boot time (59 s to shell, no forwarding stage) | Field behaviour, restarts | [autochain.md](autochain.md) boot timing | #34 | `autochain_time.py` | y |
| 15 | Unit 1 A9 (a2): controllable, no payload; no Linux loader | Possible offload engine | [a2.md](a2.md) | #53, #54 | remoteproc-style loader design | y |
| 16 | Sensors/telemetry: LM75 x4 done; BCM59111 temps, PVTMON, SFP DDM, SNMP | Monitoring parity | [board.md](board.md) | #21, #35, #36 | selftest + hwmon nodes | y |
| 17 | Power-down of unused blocks (PHYs, SerDes, SFP lasers, unit 1) | Heat and idle power | #25 | #25 | PHY BMCR power-down once MDIO driver exists | y |
| 18 | Security: Spectre/errata config, no secure boot, debug packages in image, switch-plane DoS | Hardening before any release image | config-6.18, [hardware.md](hardware.md) U-Boot RSA note | #61 (NEW) | `docs/security.md` audit | n |
| 19 | NAT/routing: L3 in ASIC (64 LPM), NAT on CPU | Performance expectations | #40 | #40 | blocked by #1, #2 | y |
| 20 | Controller adoption (Omada/UniFi), feature parity with stock | Managed use | #32, #33 | #32, #33 | enumerate stock features | n |
| 21 | Stock GPL source / vendor store, boot log mining | Reduces RE risk | #49, #50, #51 | #49, #50, #51 | owner decision on #51 | n |
| 22 | Upstream: mainline 7-patch series never fully upstreamed; mainline boots + mgmt Ethernet only on unit 0 front 1 | Long-term maintainability | [mainline.md](mainline.md) | #41, #15 | held for owner go-ahead | n |
| 23 | Unknown board facts: BCM59111 reg 0x2c raw, MSPI power-on default, EEPROM fields, unit 1 SMBus/CCB I2C | Small RE leftovers | [board.md](board.md) open questions | #9, #31 | scan unit 1 CMIC SMBus | y |

## Notes
- NEW issues filed 2026-11-14: #57 forwarding model, #58 PHY/SerDes/SFP, #59 flash/sysupgrade, #60 buttons/GPIO/LED, #61 security.
- Hierarchy: rows 1-5 gate everything else visible to a network user; rows 7 and 9 gate a shippable image.
