# US-48 port map (front panel -> chip -> software names)

Detail and evidence: [board.md](board.md) (Port map, SFP ports 49-52), [driver-internals.md](driver-internals.md) 13. Generator: `scripts/us48_portmap.py`.

## Front -> chip
- The panel has no numbers 49-52: it is labelled SFP+ 1, SFP+ 2, SFP 1, SFP 2 (four cages; Dan says all four are 10G capable). 49-52 are the stock/SDK numbers in this doc.
| Front | Panel label | Chip | Logical (SDK) | PHY / lane | Software names |
|---|---|---|---|---|---|
| 1-16 | RJ45 | unit 0 (BCM53344, host SoC, AXI CMIC 0x48000000) | 2-17 | integrated GPHY, int bus 0 | uboot-2 + Linux (stock) |
| 17-24 | RJ45 | unit 0 | 18-25 | octal PHY, ext bus 0 `0x14-0x1b` | |
| 25-40 | RJ45 | unit 1 (BCM53346, PCIe 01:00.0) | 2-17 | integrated GPHY | |
| 41-48 | RJ45 | unit 1 | 18-25 | octal PHY, ext bus 0 `0x14-0x1b` | |
| 49 | SFP+ 1 | unit 1 | 26 (`xe0`, phys 26, TSC0 lane 0) | SFP+, no PHY | OpenWrt `xe49` |
| 50 | SFP+ 2 | unit 1 | 27 (`xe1`, phys 28, TSC0 lane 2) | SFP+, no PHY | OpenWrt `xe50` |
| 51 | SFP 1 | unit 0 | 26 (`ge24`, phys 26, TSC lane 0) | SFP, no PHY | uboot-2 port 26 |
| 52 | SFP 2 | unit 0 | 27 (`ge25`, phys 28, TSC lane 2) | SFP, no PHY | |
| (internal) | | both | 28, 29 | 2 x 13G HiGig between the chips | |

- Logical = physical except fronts 1-8 (logical 2-9 = phys 9-2) and phys 26/28/30/32 -> logical 26/27/28/29.
- The same logical number (26, 27) exists on both chips: always say which chip.

## Current cabling
- Lives in [scripts/cabling.json](../scripts/cabling.json) (module, far end, link state per port); this doc is the fixed map only.
- sw1 has three links to the US-48 (1G copper, SFP, SFP+): stock bridges them with STP off. Test with one link up, or set the others down on sw1 (runtime). Carrier is independent of forwarding.

## Which software drives which chip
- uboot-2 drives unit 0 (AXI CMIC): `BCM_HR2_XE1_PORT 26` lane 0 = SFP 1 (front 51).
- Linux `us48_cmic` drives unit 1 over PCIe: `xe49` = SFP+ 1, `xe50` = SFP+ 2.
- uboot-2 on unit 1 (SFP+ 1/2) = M7 in [uboot-second-stage.md](uboot-second-stage.md).
