# Linux switch model

Design and bench plan for the all-copper target: [#4](https://github.com/awto-au/awto-unifi-us48/issues/4), [#57](https://github.com/awto-au/awto-unifi-us48/issues/57), [#58](https://github.com/awto-au/awto-unifi-us48/issues/58).

## Goal
- All 48 RJ45 ports negotiate link and switch frames in the ASIC: within each BCM5334x chip and between the chips over HiGig.
- Port-to-port forwarding never goes through Linux routing/bridging; the CMIC CPU path stays for management and host traffic.
- First baseline: one shared untagged LAN over the 48 copper ports. VLAN/STP control later. SFP/SerDes, PoE, routing, NAT: out of scope here.

## Current state (driver `kernel/us48-cmic`)
- Unit 0 (BCM53344, AXI CMIC): `ge1`-`ge24`. Unit 1 (BCM53346, PCIe CMIC, `us48_cmic_xe.ko`): `ge25`-`ge48`, `xe49`, `xe50`. All 50 netdevs register.
- Port tables generated from `hwdb/board/ports.toml` (`scripts/hwdb_gen.py ports_c` -> `hwdb/gen/us48_ports.h`); map: [port-map.md](port-map.md).
- PHYs: integrated GPHYs (`ge1`-`ge16`, `ge25`-`ge40`) on internal MIIM bus 0; BCM54282 octal (`ge17`-`ge24`, `ge41`-`ge48`) on external bus 0 `0x14`-`0x1b`; addressing [driver-internals.md](driver-internals.md) s4, octal init s7, s12.4.
- Copper bring-up (GPORT MAC, PHY AN, private VLAN, PORT_TAB, link bitmap) runs at the first `ip link set up`; block-wide GPORT init and the octal top reset once per GPORT block. Link poll (1 s) only for open ports.
- Every port is in a private VLAN with the CPU only (copper vid 100 + front, xe vid = front): no ASIC forwarding between ports yet.
- HiGig: logical 28/29 (physical 30/32) on each chip, 12.73G dual-lane HiGig2. Links up with the opt-in `higig=1` knob (driver-internals 15); stack/module-id/trunk tables for unit 1 not written yet (15.4).
- Read-only PHY inventory: `echo 1 > /sys/module/us48_cmic/parameters/phy_scan` (unit 0) or `.../us48_cmic_xe/parameters/phy_scan` (unit 1), then `dmesg`. No configuration writes; the shadow/RDB reads write selectors and the RDB mode enter/exit.

## Representative-test rule (Dan, 2026-11-17)
- One known-peer link per copper PHY family (integrated GPHY, octal) per chip; one link per SFP/SFP+ media type. No cable test of every jack.
- Test an individual port only when inventory, mapping or use shows an anomaly.
- Done for copper: loop cables front 8 <-> 17 (unit 0) and 32 <-> 41 (unit 1) link at 1G both ends ([driver-internals.md](driver-internals.md) s12.6, #123). These prove PHY/MAC link only.

## Stages
1. CPU-path baseline (diagnostic only): bridge two copper ports on different chips in Linux; frames cross the CPU, proves both CMIC paths, not ASIC switching. Keep `ge1` reachable.
2. PHY inventory + representative links: done (above).
3. ASIC forwarding, same chip, then across HiGig, with the Linux bridge out of the path.
   - Per test VLAN, set and read back: ingress membership (VLAN_TAB), egress membership/untagged (EGR_VLAN), PORT_VID (PORT_TAB), STP state (STG_TAB/EGR_VLAN_STG), EGR_ENABLE, EPC_LINK_BMAP. Do not assume VLAN 1.
   - Prove with per-port MIB counters that frames crossed the ASIC (not the CPU path). Restore the private VLANs after.
   - Never put both ends of a loop cable in one VLAN (loop through the ASIC). Never add `ge1` or the CPU port to a test VLAN.
   - Cross-chip: HiGig port mode, module ids and remote-port forwarding from stock evidence or controlled bench tests; no guessed writes.
4. Linux control plane: DSA (two chips, cascade over HiGig) vs switchdev in the CMIC driver; decide after stage 3 against the real CPU-port and stacking behaviour. Scope: bridge membership, VLAN filtering, FDB learn/age, STP; later MIB/ethtool stats, IGMP snooping, LAG, mirroring, storm control, ACLs.

## Acceptance
- Read-only scan finds the expected PHY IDs at all configured copper MDIO addresses.
- One known-peer link per copper PHY family per chip; one compatible link per SFP/SFP+ media type in scope.
- Same-chip and cross-chip (HiGig) port pairs exchange unicast and broadcast frames with the Linux bridge out of the path; switch counters show the ASIC forwarded them.
- CPU management path stays reachable; no test depends on SFP/SerDes or PoE.
- Bench tests: RAM-booted image, serial recovery available, no register writes inferred from design notes.
