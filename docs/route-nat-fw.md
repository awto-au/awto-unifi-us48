# Copper routing, NAT, firewall tests (#40)

- Goal: CPU-path routing/NAT/firewall numbers with the existing loop cables, no switch-table writes. Tool: `scripts/route_nat_fw_test.py` (dry-run by default, `--apply` acts over ssh on the board).
- Image: base `mainline/us48_defconfig` (both profiles) has network namespaces + nftables as modules; `nft` and `modprobe` are in the rootfs (below). Not yet run on the board.

## Topology

```
host A (netns)               router (root ns)                 host B (netns)
ge8 10.48.10.2/24  --cable-- ge17 10.48.10.1/24 | ge41 10.48.20.1/24 --cable-- ge32 10.48.20.2/24
gw 10.48.10.1                forwarding v4+v6                gw 10.48.20.1
v6: 2001:db8:48:10::/64 and 2001:db8:48:20::/64 (::1 router, ::2 hosts)
```

- Cables: front 8 - front 17 (unit 0), front 32 - front 41 (unit 1) (`scripts/cabling.json`).
- The copper ports stay in their private CPU VLANs: every packet goes to the CPU and back out (one A->B packet = 4 CPU-port DMA transfers: ge8 tx, ge17 rx, ge41 tx, ge32 rx; hosts A and B are on the same CPU).
- ge1 (management, 192.168.25.207) is never named in a write command (`Ctx.sh` refuses it). No bridge, no switch-table writes.
- Forwarding is set per router port only (`conf/ge17|ge41/forwarding`, v6 `force_forwarding`); `ip_forward`, `conf/all/*` and every other port stay as they are.

## Commands

- `route_nat_fw_test.py setup|route|nat|fw|flowtable|teardown|all [--apply] [--no-v6] [--mib N]`; log `tmp/logs/route_nat_fw_test.log`.
- Dry-run prints every board command. `all` = setup, route, nat, fw, flowtable, then teardown in a `finally`.
- Netns without `iproute2`: busybox `unshare -n sleep` holds a namespace, `ip link set geN netns PID` moves the port in, `nsenter -t PID -n` runs commands in it. Counters come from `/proc/net/dev` inside each namespace (sysfs shows the root namespace only).
- Modules (`modprobe`, `modules.dep` from host `depmod`, built by `build_mainline.py`): nf_conntrack, nf_nat, nf_tables, nft_ct, nft_chain_nat, nft_nat, nft_masq, nft_limit, nft_log, nft_reject(_inet), nf_flow_table(_inet), nft_flow_offload. They stay loaded after teardown until reboot.
- TCP test: `dd` 10 MiB through busybox `nc` between the namespaces, receiver counts bytes (must be exactly N MiB), MB/s from `/proc/uptime` (10 ms resolution).
- Limits: 15 s per board command, 30 s per 10 MiB transfer; expiry = step fails, logged with the command, restore runs.

| Subcommand | Proves |
|---|---|
| `setup` | snapshot (admin state, addresses, ipv6 disable, router-port forwarding, nft rule count) saved on the board in `/tmp/rnf/snapshot`; ports move into namespaces; bridge membership refused |
| `route` | v4/v6 ping both ways through the CPU; 10 MiB TCP both ways with MB/s; every one of the four ports saw rx and tx packets |
| `nat` | masquerade on ge41 for 10.48.10.0/24: B's socket table shows peer 10.48.20.1, conntrack holds the SNAT tuple, entry count, 10 MiB both ways |
| `fw` | stateless: TCP 5001 A->B dropped (`c_drop`), ping and TCP 5002 pass (`c_icmp`, `c_pass`). Stateful: policy drop, `ct established,related` accepted, new only A->B 5002 + ping; B->A new flows dropped (`c_drop_rev`) while replies pass (`c_est`) |
| `flowtable` | route and NAT, each without and with the nftables software flowtable (`flow add @ft`, devices ge17+ge41); MB/s per direction and the ratio; OFFLOAD-flagged conntrack entries |
| `teardown` | ports back in the root namespace, holders killed, our nft tables deleted (`flush ruleset` only if the ruleset was empty before), forwarding/addresses/admin state restored |

## Offload model for the US-48 (plan, not a claim)

Source model: awto-tl-st1008f `docs/NAT-STACKS.md`, `ROUTING-OFFLOAD.md`, `UPSTREAM-L3.md`, `DECISIONS.md` ("No netfilter guard on L3 offload"), `CPU-PORT.md` (read-only).

- Kernel stays the source of truth: FIB, neighbour table, conntrack, nftables. No TCP termination offload (TOE): not acceptable upstream.
- Routes reach the ASIC through FIB/neighbour notifiers (switchdev router pattern: mlxsw, prestera). On-link and unmatched traffic traps to the CPU.
- NAT flows: nftables flowtable. Their stack numbers: conntrack + masquerade without flowtable is about 3.5x slower than with the software flowtable (`NAT.md`, RTL9303, their conditions). `flowtable` above measures the same pair here first.
- Firewall stays nftables; nothing in the driver checks nft rules before offloading routes (their decision, in-tree routers do not either).
- HR2 mapping to check before any driver work (all from `regdb/hr2_mems.tsv`, generated from the stock SDK tables; sizes = index range):

| Need | HR2 table | Fields seen (regdb) | To verify |
|---|---|---|---|
| IPv4 LPM | `L3_DEFIP` (512 x 168 bit) | `IP_ADDR_L/U`, masks, `NEXT_HOP_INDEX0` (10 bit), `ECMP`, `ECMP_PTR0`, `DST_DISCARD0`, `MODE`, `DEFAULTROUTE0` | v4/v6 share rows (`MODE`)? `L3_DEFIP_128` has no mem_info (`docs/REGISTERS.md`): v6 prefix > 64 bit possible? |
| IPv4 host | `L3_ENTRY_IPV4_UNICAST` (4096 x 88 bit) | `IP_ADDR`, `NEXT_HOP_INDEX`, `VALID`, `HIT`, `DST_DISCARD` | hash bucket layout; `L3_ENTRY_ONLY` view |
| IPv6 host | `L3_ENTRY_IPV6_UNICAST` (2048 x 176 bit) | `IP_ADDR_UPR_64/LWR_64`, `NEXT_HOP_INDEX` | key type bits |
| Next hop (ingress) | `ING_L3_NEXT_HOP` (1024 x 42 bit) | `PORT_NUM`, `MODULE_ID`, `TGID`, `VLAN_ID`, `INTF_NUM`, `DROP`, `COPY_TO_CPU` | module id on unit 1 and HiGig egress (#57) |
| Next hop (egress rewrite) | `EGR_L3_NEXT_HOP` (1024 x 87 bit) | `MAC_ADDRESS` (DA), `INTF_NUM`, `OVID`, `IVID`, `L3_UC_DA/SA/TTL/VLAN_DISABLE` | entry type values |
| L3 interface (egress) | `EGR_L3_INTF` (512 x 95 bit) | `MAC_ADDRESS` (SA), `VID`, `OVID`, `IVID`, `TTL_THRESHOLD`, `L2_SWITCH` | MTU: `L3_MTU_VALUES` (768 x 15 bit) |
| L3 interface (ingress, my-station) | `L3_IIF` (named, `mem_info` NULL in the stock build: `docs/REGISTERS.md` lines 72-73); `VLAN_TAB` has `L3_IIF` field | not in regdb | does silicon have it, or pruned by the SDK/SKU? Read-only S-channel probe (#13 steps) |
| Routed-packet enable | `PORT_TAB`/`VLAN_TAB` L3 bits (`L3_IIF`, IPv4/IPv6 enables) | to read | which bits make a port route instead of bridge |
| ECMP | `ECMP_PTR0`/`ECMP_NH_INFO` (FP policy) | | likely unused |

- Rewrite capability (#13): routing rewrites DA (`EGR_L3_NEXT_HOP.MAC_ADDRESS`), SA (`EGR_L3_INTF.MAC_ADDRESS`), VLAN, TTL. `FP_POLICY_TABLE` (2048), `EFP_POLICY_TABLE` (512) and `VFP_POLICY_TABLE` (512) list actions for priority, DSCP/ECN, drop, copy to CPU, redirection, L2/VLAN change (`G_L3SW_CHANGE_MACDA_OR_VLAN`), mirror, meters, counters: no source/destination IP or L4 port rewrite action. Same conclusion as TL-ST1008F #58/#68. Plan: confirm with the #13 live probes; until then NAT is CPU + software flowtable, ASIC L3 only for plain routing.
- No `TC_SETUP_FT` hardware flowtable for NAT then: `nft flow add` stays software (`nf_flow_table`), as measured by `flowtable`.
- Steps, in order (each a separate issue/decision, none started):
  - Measure the CPU baseline with `route_nat_fw_test.py` (this tool) and the CPU-port budget (#40, #17).
  - Read-only probe: `L3_IIF`, `L3_DEFIP_128`, tunnel tables present or fused/pruned (#13).
  - Which stock init writes L3 tables / enable bits (`init-phases-u0.md`, `init-phases-u1.md`).
  - Driver: FIB/neighbour notifier mirror into `L3_DEFIP`/`L3_ENTRY_*` + next-hop tables; unit 1 needs module-id/HiGig next hops (#57).
