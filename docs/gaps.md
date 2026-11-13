# Gaps: what is missing for a full Linux/OpenWrt port

Ranked by blocking effect. Snapshot 2026-11-14. Detail lives in the linked docs and issues.

| # | Gap | Why it matters | Evidence | Issue | Cheapest next step | Board? |
|---|---|---|---|---|---|---|
| 1 | Forwarding model: switchdev/DSA, VLAN/FDB/STP offload, bridge across both chips (HiGig 28/29) | Today each port is a private CPU-only VLAN; no switching in Linux | [driver-internals.md](driver-internals.md), [openwrt-port.md](openwrt-port.md) m4 | #57 (NEW), #4 | design doc `docs/switch-model.md`; software-bridge baseline | doc n, bench y |
| 2 | Large-frame RX loss on ge1 (>5 Mbit/s) | Management/CPU path unusable for real traffic | #56 | #56 | read GPORT RDBGC/RDROP + MMU drop counters, CPU-port COS limits | y |
| 3 | Copper PHY/octal PHY Linux driver + CMIC MDIO bus; only fronts 1 and 40 verified | No link on 46 of 48 ports from our code | [hardware.md](hardware.md) unit 1 scans, [port-map.md](port-map.md) | #58 (NEW), #4, #31 | PHY ID + AN table for all ports, both units | y |
| 4 | SerDes (QSGMII/TSC4) cold init, 10G SFP+/SFP, sfp/phylink (presence, TX-disable, EEPROM) | 4 cages incl. the 10G house uplink work only on a stock-initialised chip | [board.md](board.md) SFP, port-map | #58 (NEW), #31 | per-cage read-only status; identify bus-1 IDs 0x8770/0xbff0 | y |
| 5 | Unit 1 (PCIe) path: IRQ instead of polling, RX stall, driver structure | Half the ports sit behind it | #4 comments, #46 | #4, #46 | RX counters on unit 1 vs unit 0 | y |
| 6 | PoE: BCM59111 PSE driver, firmware load, per-port control, power budget (436 W) | Core product feature; today PoE is only turned off | [poe.md](poe.md), [kernel-pse.md](kernel-pse.md) | #26, #23, #24, #28 | userspace prototype on one card (needs owner OK) | y |
| 7 | Flash layout, rootfs, sysupgrade A/B | No installable image; initramfs only | [openwrt-port.md](openwrt-port.md) m6, [hardware.md](hardware.md) flash map | #59 (NEW) | `docs/flash-layout.md`, RAM-boot squashfs test | doc n, test y |
| 8 | LEDs: port link/act (LEDUP program, serial pins), PoE LEDs (MSPI), front 49-52, system LED | No user feedback; stock logic known only statically | [board.md](board.md) LED section | #14, #60 (NEW, system LED) | LEDUP pin mux + per-LED toggle table | y |
| 9 | Buttons, reset, system LED, PoE IRQ/reset GPIOs in DT; panic/last-reset reason | No factory reset, no failsafe trigger | [board.md](board.md) GPIO map | #60 (NEW), #29 | `gpio-keys` GPIO 15, `gpio-leds` 10/11 in DT | y |
| 10 | Fans/thermal: preset-only, no tach, no off, policy daemon is bench-only | Heat/noise; safety net for all other work | [fan.md](fan.md) | #12, #19, #21, #22 | temperature-driven policy with fail-safe | y |
| 11 | QSPI at 12.5 MHz DT, 4-byte-mode reset hazard after panic | Boot time; board can hang until power-cycle | #20 | #20 | `sysrq-c` test + rated-clock DT | y |
| 12 | Watchdog/reboot path: SP805 fed by procd, DMU restart; no recovery policy | Hangs in the field | [openwrt-port.md](openwrt-port.md) bring-up | #20, #60 | covered by #20/#60 tests | y |
| 13 | DDR shmoo/clock headroom; CPU 400 MHz vs 800 MHz | Performance ceiling (NAT/CPU path) | [hardware.md](hardware.md) rates | #55, #17 | RAM-only clock trial | y |
| 14 | Boot time (59 s to shell, no forwarding stage) | Field behaviour, restarts | [openwrt-port.md](openwrt-port.md) | #34 | `boot_profile.py` | y |
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
