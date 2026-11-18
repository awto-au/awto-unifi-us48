# Linux switch model

Status: design and bench plan for the all-copper target in [#4](https://github.com/awto-au/awto-unifi-us48/issues/4), [#57](https://github.com/awto-au/awto-unifi-us48/issues/57), and [#58](https://github.com/awto-au/awto-unifi-us48/issues/58).

## Goal

Make all 48 RJ45 ports negotiate link and switch Ethernet frames in the ASIC:

- within either BCM5334x chip;
- between the two chips over their HiGig interconnect.

Ordinary port-to-port forwarding must not depend on Linux routing or bridging packets through the CPU. The CPU ports and CMIC packet DMA remain available for management and for traffic addressed to the host.

The first functional baseline is one shared untagged LAN across the 48 copper ports. VLAN configuration, STP control, and other switch features are a later control-plane milestone. SFP/SerDes, PoE, routing, and NAT are outside this copper milestone.

## Known topology and current behavior

- Front ports 1-24 are on unit 0 (BCM53344, AXI CMIC); ports 25-48 are on unit 1 (BCM53346, PCIe CMIC). See the generated [port map](port-map.md) and its source `hwdb/board/ports.toml`.
- The two chips are connected by HiGig links on logical ports 28/29 (physical ports 30/32) on each chip. Their 13G rate and physical mapping are documented; the Linux stacking/forwarding setup is not yet verified.
- Each chip has its own CPU packet-DMA path. Unit 1's CPU path is local to its PCIe CMIC; the HiGig fabric is the path for ASIC forwarding between chips.
- Linux exposes all 48 copper fronts: integrated-GPHY ports as `ge1`-`ge16` and `ge25`-`ge40`, and octal-PHY ports as `ge17`-`ge24` and `ge41`-`ge48`. Each still uses a private CPU-only VLAN. The stock-derived octal-PHY init is now present and its RDB register values have been verified in a RAM-booted test, but all 16 octal ports currently report no carrier. Unit 1 also exposes `xe49` and `xe50`; none of this proves ASIC forwarding.
- The stock initialization reference creates a default VLAN, adds front-panel ports as untagged forwarding members, adds the CPU port tagged, and enables the ports in `EPC_LINK_BMAP_64`. This is evidence for the per-chip baseline, not proof of the required two-chip stack configuration. See [driver internals](driver-internals.md).

## Staged implementation and proof

### 1. Establish the CPU-path baseline

On an isolated bench setup, bridge `ge1` and `ge40` in Linux and verify bidirectional packets. These are the currently supported copper ports on different chips, so this checks both CMIC paths and the Linux bridge without involving SFPs.

This is only a diagnostic baseline: packets cross the CPU, and success does not prove ASIC switching. Keep serial access available and preserve the current `ge1` management path while testing.

### 2. Inventory copper PHYs and test representative links

Run a read-only PHY inventory on both chips to verify PHY IDs at all configured MDIO addresses and capture current status without moving cables. Establish a known-peer link on one representative port for each copper PHY family (integrated GPHY and octal/QSGMII) on each chip; one pair may cover both families. Sample another GPORT block only if its configuration or diagnostics differ, or if an anomaly points there. Do not cable-test every jack. Start with autonegotiation and carrier; defer EEE, LED modes, cable tests, and power management.

The native driver initializes the 32 integrated-GPHY ports using the physical-port-to-MIIM formula in [driver internals](driver-internals.md). Octal-PHY fronts 17-24 and 41-48 use logical ports 18-25 and external MIIM addresses `0x14`-`0x1b`, with the stock-derived BCM54282-family init sequence in the shared CMIC code.

On mainline Linux 7.4, trigger the inventory on each chip with `echo 1 > /sys/module/us48_cmic/parameters/phy_scan` and `echo 1 > /sys/module/us48_cmic_xe/parameters/phy_scan`, then inspect `dmesg`. It reads ID1/ID2, BMCR, BMSR (twice for the latched link bit), ANAR/ANLPAR, and 1000BASE-T control/status on internal and external bus 0. For the octal PHY it also reads the documented shadow and RDB mode/status registers; the MIIM lock is held throughout. RDB accesses use the stock enter/access/exit sequence, and the diagnostic writes only access selectors, never PHY configuration data.

The initial RAM-booted scan on Linux 7.4.0-rc1-dbg+ (2026-11-17) found the same responders on both chips: internal bus-0 addresses `0x01`-`0x04`, `0x06`-`0x09`, `0x0b`-`0x0e`, and `0x10`-`0x13` identify as `0x600d:0x85b0`; external bus-0 addresses `0x14`-`0x1b` identify as `0x600d:0x845b`. External address `0x1c` responds with `0x0143:0xbff0`, the QSGMII SerDes ID, not a copper PHY. The initial link bits mapped to fronts 1, 8, and 10 on unit 0 and front 26 on unit 1. Before the front-17/front-8 loop test, the latest scan saw fronts 1, 8, and 10 linked; no integrated PHY on unit 1 reported link. Cable/partner state can change between boots.

All 32 integrated-GPHY ports and all 16 octal-PHY ports register as Linux netdevs. In an earlier RAM test, fronts 1, 8, 10, and 26 reached carrier and had CPU-DMA counter activity; this validates PHY-to-GPORT and CPU-DMA paths only, not ASIC forwarding. Before the front-17/front-8 loop test, all 16 octal netdevs were brought administratively up but had no carrier; their PHYs reported BMCR `0x1140`, ANAR `0x01e1`, GBCR `0x0600`, ANLPAR `0`, and GBSR `0`. Front 17 was reported connected to SW2 port 2, but that is not the actual peer: SW2 `lan2` contains an FS SFP-10G-T AQR113C copper transceiver with an RJ45 jack and BASE-T modes. The optical-looking LC/10GBASE-SR/850 nm EEPROM fields and laser/power readings are placeholders, not a fiber module. SW2 `lan2` is up at 10G and its forwarding table identifies the house gateway as its only learned peer; this is not a compatible test peer for the US-48 1G front port 17. A separate read-only US-48 check found `xe49` carrier up and `xe50` down; both local SFPs are LC/10GBASE-SR, so `xe49` cannot be linked to the SW2 copper module. SW1 `lan7` was down despite the historical FDB-verified `xe49` mapping. The current physical endpoint of `xe49` remains unidentified. No SW2 ports were bounced or reconfigured. Corrections and follow-ups are tracked in [awto-au/awto-tl-st1008f#169](https://github.com/awto-au/awto-tl-st1008f/issues/169). The SDK-derived mapping is in place; per-jack physical validation is reserved for anomalies.

The current read-only TSC dump finds `xe49` on an LC/SR optic with `PCS_LIVE=0x0003` (sync and PCS link; no remote-fault bit), while `xe50` has `PCS_LIVE=0x0000`. `xe49` packet counters were zero at the sample and its far end remains unidentified; count it as a representative SFP+ link indication, not as verified end-to-end traffic.

The first octal init test exposed a bug in the shared MIIM accessor: it accessed RDB addresses without the required enable handshake, so the apparent RDB reads were invalid. The corrected accessor now performs the stock RDB enter/access/exit sequence on each operation. In the latest RAM-booted image (Linux `7.4.0-rc1-dbg+` build `#105`), both chips report octal-PHY `POWER_MII=0x06c2` (SUPER_ISOLATE clear), `DSP_TAP10=0x011b`, and `GLOBAL_RESET=0x0000` after the reset pulse. `AUX=0x4c00`, `MODE=0x7e08`, `SGMII=0x5581`, and `DETECT=0x7820` are also readable and consistent across all eight PHYs per chip.

Read-only checks initially showed GPORT2 enabled and `TOP_QSGMII2X_CTRL=0x00080e00`, but no QSGMII2 received-clock-valid bits and no link at GPORT2 lane 0 while front 17 had no known compatible peer. After the user connected front 17 (`ge17`) directly to front 8 (`ge8`), both PHYs reported link and both MAC_MODE values were `0x2a` (1G, LINK_STATUS set). `TOP_L1_RCVD_CLK_VALID_STATUS_1` became `0x000f0281`, with QC2 low nibble `0xf`. The equivalent unit-1 loop from front 41 (`ge41`) to front 32 (`ge32`) also brought both PHYs and MACs up at 1G; MAC_MODE was `0x2a` on both ports and unit-1 clock-valid status was `0x000f0045`. These representative tests verify the integrated-GPHY and octal/QSGMII copper paths on both chips without testing every jack. They do not prove ASIC forwarding: each port remains in a separate private CPU-only VLAN. Although native Linux does not run stock `phy_qsgmii65_init()` for the SerDes at external MDIO `0x1c` ([driver internals](driver-internals.md) §12.6), that omission is not a demonstrated cause of the earlier no-link state. No PHY or SerDes configuration was changed.

### 3. Prove ASIC forwarding on each chip, then across HiGig

First prove a pair of copper ports on the same chip forwards in hardware. Then prove pairs spanning unit 0 and unit 1 forward across HiGig. Keep these tests separate from CPU-bridged tests so a Linux bridge cannot mask a missing ASIC forwarding path.

For the first test VLAN, explicitly configure and read back ingress membership, egress membership/untagged behavior, port VLAN IDs, STP state, egress enable, and the link bitmap. Use a single shared untagged service VLAN as the test behavior; do not assume VLAN 1 has the needed membership or STP state on either chip. Preserve the CPU management path and verify it after each change.

Before calling cross-chip forwarding complete, establish the HiGig port mode, TDM/credit setup, module/stack behavior, and remote-port forwarding rules from verified stock behavior or controlled bench evidence. The current documentation identifies the links but does not establish a safe Linux cold-init recipe for the stack.

### 4. Add Linux switch control

Once fixed-VLAN hardware forwarding is proven, select and implement the Linux control-plane model. The open choice is DSA (two chips with a cascade link and suitable HiGig handling) versus switchdev support in the existing CMIC driver. Compare both against the actual CPU-port and stacking behavior before committing to one; the fixed-VLAN proof alone does not resolve that design choice.

The initial managed-switch scope should cover bridge membership, VLAN filtering, FDB learning/aging, and STP state. MIB/ethtool statistics and features such as IGMP snooping, LAG, mirroring, storm control, and ACLs can follow.

## Acceptance criteria

- A read-only scan finds the expected PHY IDs at all configured copper MDIO addresses.
- At least one known-peer link succeeds for each copper PHY family on each chip; reuse the same patch lead and test pair where possible instead of testing every jack.
- At least one compatible SFP/SFP+ link succeeds for each distinct module/media mode in scope; do not require a separate cable test for every cage when the hardware path is shared.
- Known same-chip port pairs exchange unicast and broadcast frames with the Linux CPU bridge disabled.
- Known cross-chip port pairs exchange frames over HiGig with the Linux CPU bridge disabled.
- Representative forwarding tests cover both chips and relevant port classes; switch counters confirm forwarding is in the ASIC. Test individual ports only when mapping, inventory, or diagnostics indicate an anomaly.
- The CPU management path remains reachable, and no test depends on SFP/SerDes or PoE behavior.

Bench tests require the device. Use a RAM-booted test image, keep serial recovery available, and do not infer unverified register writes from the design notes.
