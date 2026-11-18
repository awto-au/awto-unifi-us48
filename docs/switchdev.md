# switchdev L2 offload (us48_cmic)

Linux bridge -> ASIC mapping for both BCM5334x units; base for the L3/NAT offload ([route-nat-fw.md](route-nat-fw.md)). Issue [#57](https://github.com/awto-au/awto-unifi-us48/issues/57). Code: `kernel/us48-cmic/us48_cmic_sw.c`. Board test: `scripts/switchdev_test.py`.

- Decision (switch-model.md stage 4): switchdev in the CMIC driver, not DSA. The CPU port is a DMA ring per unit, not an Ethernet MAC a DSA conduit needs; both units already have their own netdevs.
- Stage 1 (this): VLAN-unaware bridges, one unit per hardware domain. Stage 1.5: HW-learned/aged entries reported to the bridge (L2 change FIFO, below). Stage 2: HiGig cross-unit offload, VLAN-aware bridges, MDB.
- Board-tested 2026-11-18 on unit 0 (`switchdev_test.py` all + mgmt, results in #57); unit 1: L2X walk timing (#57) and the `burst` step (#134).

## Model

| Linux | ASIC (per unit) | Source |
|---|---|---|
| port outside a bridge | private VLAN {CPU, port} (copper 100 + front, xe = front), STG of VLAN 1, CML 0: unchanged | `ge_vlan_init` / `port_vlan_init`, [driver-internals.md](driver-internals.md) 14.4 |
| bridge `brX` (slot i of 8) | ASIC VLAN 4000 + i, STG 2 + i, FID_ID = 4000 + i | VLAN 4000-4007 and STG 2-9: no write in `tmp/schan_stock_writes*.txt`; free check at create (`VLAN_TAB.VALID`) |
| port joins | VLAN_TAB/EGR_VLAN bitmap bit, untagged egress, `PORT_TAB.PORT_VID` = bridge VLAN, CML 8 | below |
| port STP state | `SP_TREE_PORT<lport>` in STG_TAB + EGR_VLAN_STG | SDK `include/soc/ptable.h` |
| bridge-local MAC (`is_local`, on br dev) | static L2X entry -> CPU (port 0, CPU bit) | `l2_op_cpu` pattern |
| user static FDB on a port | static L2X entry -> {modid, lport} | regdb L2X fields |
| dynamic FDB | HW learning (CML 8) + HW ageing; the bridge's software-learned entries are not pushed. HW entries -> bridge as `extern_learn offload` from L2_MOD_FIFO | #57 learn run; L2 change FIFO below |
| ageing_time | `L2_AGE_TIMER` (global per unit, last writer wins) | SDK `soc/esw/firebolt.c` |
| RX from an offloaded port | `skb->offload_fwd_mark = 1` | net/bridge `br_switchdev.c` hwdom check |
| switch id | `ndo_get_port_parent_id` = {0x48, modid}: one hwdom per unit | |

- Module params (`/sys/module/us48_cmic/parameters/`): `switchdev` (Y default; N = CPU-path bridges, applies to joins after the change), `mgmt_bridge` (N default: ge1 join refused with EPERM + extack + log).
- No bridge: no table write, no change in RX/TX. `ndo_fdb_dump` falls back to `ndo_dflt_fdb_dump`; `phys_switch_id` becomes readable.
- `stp_loop_test.py` sets `switchdev` N for its CPU-path run and restores it.

## Tables (addresses: `common/bcm-cmic/bcm_cmic_hr2_memtab.h` / `regtab.h`, generated from `regdb/hr2_mems.tsv` / `hr2_regs.tsv`; not in hwdb)

| Table | Blk / base | Fields used (lsb:width, regdb) | Write source |
|---|---|---|---|
| `VLAN_TAB` | IPIPE 10 `0x14000000` + vid, 4 w | PORT_BITMAP 0:30, VALID 38, STG 39:9, FID_ID 48:12, EVEN_PARITY 126 | clone of VLAN 1 as `ge_vlan_init`; FID_ID = vid (below) |
| `EGR_VLAN` | EPIPE 11 `0x100c0000` + vid, 3 w | UT_BITMAP 0:30, PORT_BITMAP 30:30, STG 60:8, VALID 68, EVEN_PARITY 76 | clone of VLAN 1 as `ge_vlan_init` |
| `STG_TAB` / `EGR_VLAN_STG` | 10 `0x14040000` / 11 `0x10100000` + stg, 2 w | SP_TREE_PORTn 2n:2, EVEN_PARITY 60 | values: PVP_STP_DISABLED 0, BLOCKING 1 (also listening), LEARNING 2, FORWARDING 3 (SDK 6.5.16 `include/soc/ptable.h:17-20`); init all 0 (SDK `bcm/esw/firebolt/stg.c` `_bcm_xgs3_stg_stp_init`), CPU 3 as `ge_vlan_init` |
| `PORT_TAB` | 10 `0x04000000` + lport, 8 w | PORT_VID 24:12, CML_FLAGS_NEW 121:4, CML_FLAGS_MOVE 125:4 | CML 8 = HW learn + forward (SDK `bcm/esw/trx/port.c` `_bcm_trx_port_cml_flags2hw`; #57 `--learn` run, driver-internals 15.4) |
| `L2X` | 10 `0x1c000000`, 16384 x 4 w, hashed | KEY_TYPE 0:2, VLAN_ID 2:12, MAC_ADDR 14:48, PORT_NUM 62:6, MODULE_ID 68:7, T 75, CPU 91, STATIC_BIT 97, VALID 99, EVEN_PARITY 100 | TABLE_INSERT/DELETE (0x24/0x26) as `l2_op_cpu`; index reads (0x07) for walks; MODULE_ID = PORT_TAB.MY_MODID (below) |
| `L2_MOD_FIFO` | 10 `0x1c1c0000`, 128 x 6 w | OPERATION 0:4, ENTRY_INDEX 5:3, BUCKET_INDEX 8:11, L2_ENTRY_DATA 20:101 (= L2X bits 0-100), REPLACED_ASSOC_DATA 121:37, STATION_MOVE 161 | FIFO_POP (0x2a, SDK `include/soc/schanmsg.h`); count `L2_MOD_FIFO_CNT` 10 `0x1e011300` NUM_OF_ENTRIES 0:8; enable `AUX_ARB_CONTROL` 10 `0x02000000` |
| `L2_USER_ENTRY` | 10 `0x1c100000`, 128 x 6 w, TCAM | VALID 0, MAC_ADDR 1:48, MASK 70:69 (KEY layout: MAC mask 70:48, KEY_TYPE mask 130), CPU 142, PORT_NUM 148:6, MODULE_ID 154:7, BPDU 162 | SDK 6.5.16 `bcm/esw/l2cache.c` `bcm_esw_l2_cache_init` (887-897) + `_bcm_l2_cache_to_l2u` (565-683) |
| `L2_AGE_TIMER` | 10 reg `0x02000400` | AGE_VAL 0:20 (s), AGE_ENA 20 | SDK `soc/esw/firebolt.c` `soc_firebolt_age_timer_set`; stock 0x0010012c = 300 s (trace line 12427 u0, 32253 u1). Cold init writes it on both units (#131): unit 0 final-value list, unit 1 after the minimum list (stock u1 write at 83485 ms in `tmp/bde_stock_init.log`, after VLAN 1; its B3 write is 0) |

- L2 key: `ING_CONFIG_64.SVL_ENABLE` (bit 14) is 1 in stock (0x2080f042, trace line 1587); #57 found unit 0 keyed by FID (0) and unit 1 by VLAN. FID_ID = vid makes both keys = vid. C for the rule, I for SVL on our cold init.
- L2_USER_ENTRY: not in the stock S-channel capture (capture partial, or written by table DMA). The driver reuses a matching valid entry, else writes the first free index, and removes it with the unit's last bridge.
- L2_USER_ENTRY is a TCAM stored X/Y: HR2 has `soc_feature_xy_tcam` + `xy_tcam_direct` (SDK `soc/common/feature.c` bcm56150), so `soc_mem_write` encodes KEY = K & M, MASK = K | ~M (40 nm, `soc/common/mem.c` `_soc_mem_tcam_dm_to_xy`). Written raw (data/mask) the unmasked VLAN_ID bits read as "must be 0" and the trap never matched in a bridge VLAN (#57 board run). The driver encodes (`l2u_dm_to_xy`).

## Sequences (rtnl held throughout; S-channel ops under `schan_seq`)

- PRECHANGEUPPER link: refuse ge1 (unless `mgmt_bridge`), refuse a VLAN-aware bridge (stage 2). Slots full / VLAN not free / no BPDU trap -> no offload, CPU-path bridge, logged.
- Bridge create (first port on the unit): BPDU trap; STG entries (CPU 3, rest 0); EGR_VLAN, then VLAN_TAB (ingress last = live); L2_AGE_TIMER from `br_get_ageing_time`.
- Join: STG state 0 (bridge sends the state after the join), VLAN bitmaps (egress first), PORT_TAB PORT_VID + CML; RX tag strip follows `rx_vid`; `switchdev_bridge_port_offload` (ctx = port, no TX forward offload).
- STP attr: STG_TAB + EGR_VLAN_STG; leaving learning/forwarding -> flush the port's dynamic L2X entries.
- Leave: unoffload at PRECHANGEUPPER (unlink, as DSA); at CHANGEUPPER PORT_TAB back to private VID + CML 0, out of the bitmaps, STG 0, flush all (static too) L2X entries of the port in the VLAN. Last port: host entries deleted, VLAN_TAB/EGR_VLAN/STG zeroed, BPDU trap removed with the last bridge.
- Copper port joined before its first `ip link set up`: `copper_enable` rewrites PORT_TAB; `us48_sw_port_reapply` puts the bridge VLAN back.
- Brport flags: only BR_LEARNING (CML 8 / 0 + flush). Flood flags off, or hairpin/isolated/locked/MAB/mcast-to-unicast on: -EINVAL (the VLAN always floods). The refusal sits on the atomic notifier: the bridge sends PRE_BRIDGE_FLAGS with `call_switchdev_notifiers` (`net/bridge/br_switchdev.c`).
- FDB to device: atomic notifier -> ordered workqueue (rtnl): vid 0 only (VLAN-unaware; vid 1 entries are the same key), dynamically learned entries skipped, foreign ports skipped (stage 2). Port static entries reply `SWITCHDEV_FDB_OFFLOADED`.
- L2X walk (flush, `ndo_fdb_dump`): 16384 index reads per walk per port, under rtnl. PPA (`PER_PORT_AGE_CONTROL`, stock writes 0x10000002 u0 line 19821, 0x1000005b u1 line 39646) is the HW alternative; not used: static-entry handling unknown.

## L2 change FIFO: HW learn/age -> bridge (stage 1.5, #134)

- Decision (Dan 2026-11-18): L2_MOD_FIFO, not the SDK's HR2 default (L2MODE_POLL, `soc/esw/l2xmsg.c` `soc_l2x_mode_cfg_get`; taken as convenience, not a HW limit). Table walk only as resync/check.
- Enable: `AUX_ARB_CONTROL` |= LEARN 1, PPA_REPLACE 2, AGE 4, PPA_DELETE 5 (0x36) while the unit has an offloaded bridge; cleared + leftovers popped with the last bridge. SDK set (`l2mod.c` `_soc_l2mod_fifo_enable`) minus L2_DELETE 6: CPU deletes (flush) notify the bridge where they are made; no FIFO-full refusal of our own ops (`SCHAN_GEN_RESP_L2_MOD_FIFO_FULL`).
- Drain: S-channel FIFO_POP (SDK `soc_mem_pop`; HR2 has `soc_feature_mem_push_pop` via bcm56624_a0, `feature.c`), header data_byte_len 0 as `_soc_mem_pop`; NACK = empty. Not the SDK's CMIC FIFO DMA (`fifo_dma_hu2`). No IRQ in the driver: delayed work every 10 ms reads `L2_MOD_FIFO_CNT`, pops that many (max 128), applies under rtnl.
- 10 ms: 128 entries / 10 ms = 12800 changes/s; idle cost one register read per period.
- Decode (SDK `_soc_hu2_l2mod_fifo_process`): op 5 LEARN / 3 INSERT / 4 PPA_REPLACE -> entry; 2 AGE / 0 DELETE / 1 PPA_DELETE -> gone; 7 = hash bucket full (not learned). L2X index = BUCKET_INDEX x 8 + ENTRY_INDEX (I; 0 index mismatches in every board run).
- Shadow (16384 records) = what the bridge was told. Reported: valid, KEY_TYPE 0, dynamic, not CPU/trunk, MODULE_ID = PORT_TAB.MY_MODID, VLAN of a live slot (4000-4007), port a member. Private VLANs never. Changed record -> `SWITCHDEV_FDB_DEL_TO_BRIDGE` old + `ADD_TO_BRIDGE` new (vid 0, offloaded). Static user add drops the record (bridge owns it).
- FIFO full (count 128) or a pop error -> one L2X walk (8 chunks, FIFO drained before each chunk read) diffs table vs shadow; at most one per 3 s. Same walk every 300 s as a consistency check.
- Board 2026-11-19 (#134, RAM boot build #132 + this module, both units):
  - FIFO full **pauses HW learning**: 512 new sources back to back (4-11 ms) -> 139-305 learned over 6 runs, resync 0-1 corrections, bridge = L2X. Sources beyond 12800/s learn at a later frame.
  - Learn latency (probe frame TX -> `bridge -timestamp monitor` extern_learn): 0.7-10.6 ms. Ageing 10 s: bridge Deleted 17.6-19.1 s after the last frame.
  - Idle CPU with one bridge vs none (6 s windows): unit 1 7.7 vs 7.5 %, unit 0 7.2 vs 7.2 % (4ea3394 table poll: ~5 % on unit 1).
- MODULE_ID: unit 1 learns MODULE_ID 0 (PORT_TAB.MY_MODID 0 after our cold init), not the driver's modid 1. The L2X/L2_USER_ENTRY fields use MY_MODID read at the first join (`l2_modid_check`); the switch id keeps modid.

## offload_fwd_mark

- Set on every frame from an offloaded port: the ASIC already delivered known unicast, and flooded unknown/broadcast/multicast to the unit's other members (+ CPU).
- The bridge then forwards only to ports in another hwdom (other unit, non-us48 ports). Link-local frames (BPDU) are not forwarded by the bridge either way.
- Gap (I): a frame trapped to the CPU only (L2_USER_ENTRY, CPU-bit entries) is marked too. Fine for BPDUs and host MACs; DCB reason bits (w13/w14) would separate trap from flood copies (stage 2).

## STP

- BPDUs: L2_USER_ENTRY {01:80:c2:00:00:00/48, CPU, BPDU, dest CMIC port, own modid}, X/Y encoded. The BPDU bit lets the frame past a blocking STG state (SDK BCM_L2_CACHE_BPDU): C, `bpdu` step (roots agree 0.5 s after the joins, both ports SP_TREE 1). Without it a blocking port hears no BPDU and goes forwarding after max_age.
- CPU-sent BPDUs leave by the SOBMH module header (`us48_xmit`): egress STG does not drop them, C (`bpdu` step: GTPKT counts the netdev TX of a listening port).
- Not mapped: MST (`SWITCHDEV_ATTR_ID_PORT_MST_STATE`), MRP.

## Cross-unit bridges

- Now: allowed, logged ("cross-unit frames go through the CPU"). Each unit offloads its own members (own VLAN slot, own hwdom); frames between units cross the CPU (unit A ASIC floods to the CPU, the bridge forwards to unit B's ports). Unknown unicast keeps flooding on the source unit (unit B's MACs are never learned there).
- Stage 2 (HiGig, stock values from driver-internals 15.2/15.4):
  - Same bridge slot (VLAN, STG) on both units; HiGig trunk ports 28 + 29 tagged members of the VLAN on both units, STG forwarding.
  - Driver-owned stacking: unit 1 `PORT_TAB/EGR_PORT[28/29]` + `MY_MODID` 1 on front ports, `MODPORT_MAP_SW/IM/EM`, `HG_TRUNK_BITMAP/GROUP`, `E2EFC_*_RMODID`, `EPC_LINK_BMAP` 28/29 (the `asic_fwd_test.py setup` list).
  - One parent id for both units -> one hwdom; L2X entries learned across the stack (mod 1 port N on unit 0).
  - Host entries: CPU of the unit that owns the bridge MAC; the other unit reaches it through MODPORT_MAP.

## VLAN-aware bridges (stage 2 design)

- Bridge VID v -> ASIC VLAN v; conflicts: VLAN 1 (HiGig default), private VIDs 49-51 + 101-148, slots 4000-4007. Needs a VID map or moving the private VLANs out of the range.
- PORT_VLAN objects: VLAN_TAB/EGR_VLAN bitmap per port, UT_BITMAP from BRIDGE_VLAN_INFO_UNTAGGED, PORT_TAB.PORT_VID from PVID, `PORT_TAB.EN_IFILTER` (5) + `EGR_PORT` EN_EFILTER for filtering; one STG per bridge for all its VLANs.
- `CONFIG_BRIDGE_VLAN_FILTERING` is off in `us48_defconfig`: VLAN filtering cannot be enabled today.

## Open (#57)

- Answered (board run in #57): BPDU trap passes listening/blocking; SOBMH TX not dropped by egress STG; CML 8 forwards unicast in the bridge VLAN (`fdb` ping).
- L2X walk under rtnl: one per STP-leave/port-leave; unit 1 (PCIe) ~2x unit 0 (numbers in #57). PPA static-entry behaviour untested.
- iproute2 `bridge` (7.3.0 static, `build_rootfs.py`) in the rootfs: static FDB add/del + learned/aged entries tested (`fdb` step).

## Board test (`scripts/switchdev_test.py`, dry-run unless `--apply`)

| Step | Setup | Pass |
|---|---|---|
| preflight | read only | params Y/N; VLAN 4000-4007 free; L2_AGE_TIMER 0x0010012c on both units; PORT_TAB ge8/ge9/ge17 private, CML 0; logs L2_USER_ENTRY, SVL_ENABLE |
| bpdu | brA{ge8}, brB{ge17}, STP on, forward_delay 6 s (4 s counter window in listening); two VLANs, no loop | same root_id while both ports listen (SP_TREE 1), both forwarding within 16.25 s, STG 3/3, bitmaps {CPU, port}; logs GRPKT/GTPKT + netdev rx/tx of the listening window |
| fdb | brsw{ge8, ge9} STP off; ge17 in a netns = host A 10.48.30.2/24, brsw 10.48.30.1/24 | ping; VLAN_TAB/EGR_VLAN bitmap {0, 9, 10}, STG/FID; STG ge8 3, ge9 0; PORT_TAB VID + CML 8; L2 A on lport 9 dynamic; A in `bridge fdb` extern_learn + offload; brsw MAC -> CPU static; static `bridge fdb add` on ge9 in L2X; learning off -> CML 0 + A flushed; flood off refused; probe frame (DA = the ge9 static) TX -> `bridge -timestamp monitor` extern_learn <= 15 ms; ageing 10 s: A + probe gone from L2X + bridge within 1.25 x (2 x 10 s + 1 s); statics kept; `bridge fdb del`; 300 s restored; FIFO off line: 0 index mismatches |
| burst | per unit: brbu{ge8, ge9} + host ge17 (u0), brbu{ge32, ge33} + host ge41 (u1); static sink on ge9/ge33 | probe latency <= 15 ms; idle CPU with the bridge - without < 5 points; 512 sources back to back (`tools/l2_burst`, DA = sink: no frame reaches the CPU or a wire) -> 'L2 resync (FIFO full)' within 4.4 s; bridge extern_learn set = driver L2X dump (`self`), > 128; statics kept; 0 index mismatches |
| stp | runs only after bpdu passes; brstp{ge8, ge17} = loop cable, STP on before the joins | one blocking (STG 1) one forwarding (STG 3); blocking port rx >= 1 BPDU/s; < 2000 pps while held 5 s; states stable |
| mgmt | brmg + ge1 (named only here), 5 s deadman | join refused, ge1 has no master, dmesg "refused join" |

- Every step: snapshot before; teardown verifies VLANs invalid, PORT_TAB private + CML 0, test MACs gone, ports = snapshot.
- Guards as `stp_loop_test.py`: host sampler 0.5 s (> 2000 pps, CPU > 90 % twice, both loop ports forwarding; fdb: CPU guard paused around L2X walks, no loop possible there), board loop 1 s steps with the same pps limit and a 40 s deadman (fdb 130 s) (bridges deleted, netns holder killed). Temps stop: die >= 108 C, LM75 >= 78 C.
- Order: `preflight`, `bpdu`, `fdb`, `burst`, `stp` (`all`); `mgmt` on its own.
