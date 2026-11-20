# XLMAC MIB addresses and TX loopback proof, unit 0 xe51/xe52 (#78)

- Offline derivation 2026-11-21, branch `wip/sfp-1g-unit0` (cf1a456). No board access. C = read in source/log, I = inferred.
- Context: 1000BASE-X link up, sw2 sees no good frames from us ([tx1g-framing.md](tx1g-framing.md) on `wip/tx1g-framing-78`). Needs (1) trustworthy MAC counters, (2) a local TX proof.
- S-channel syntax (`scripts/xlport_cmp.py`): `echo "0 rreg 5 <addr hex> 2" > /sys/module/us48_cmic/parameters/schan; cat ...`; `wreg 5 <addr> 2 <w0> <w1>` (reads old, writes, reads back). Block 5 = XLPORT0, unit 0, lane l adds l to the address (xe51 = lane 0, xe52 = lane 2). 64-bit = two words, word 0 = low.

## 1. Counter addresses

- The earlier read at `0x64c00` etc. was wrong: it added the XLMAC register base 0x60000 to the counter offset. MIB counters have no 0x60000 base.
- Derivation: `regdb/hr2_regs.tsv` (generated from the SDK reg tables): counters are regtype 2 (port) regs in block XLPORT (5,6), `offset` column = full address, flags 0x3 (counter), field `COUNT` 40 bits (byte counters 48). Counters sit at 0x0000-0x7000 (stride 0x100, in `ctr_idx` order); XLMAC control regs sit at 0x60000+; no overlap.
- Cross-check, XLMAC control regs from the same table (all verified live): CTRL 0x60000, MODE 0x60100, TX_CTRL 0x60400, RX_CTRL 0x60600 = the tsv `offset` column as is. Counters use the same rule, so TPKT = 0x4c00, not 0x60000 + 0x4c00. C
- Live proof the addresses work: the driver's own dump (`tsc_dump_one`, `us48_cmic_core.c:211`) reads `RPKT 0xb00 | lane`, `TPKT 0x4c00 | lane`, `RFCS 0xf00 | lane`. `tmp/logs/console/us48-20261120-202348.log` shows them moving: xe51 TPKT 2111405672 (8087 s) -> 2111405749 (13351 s) -> 2111405759 (16090 s) -> ...765 (17044 s); RPKT 972590371 -> ...386 -> ...398 -> ...404. C
- Lane stride confirmed: `docs/driver-internals.md` 13.5 "XLPORT MIB is lane-indexed by the low address byte (RPKT lane 0 = 0, lane 2 live)". C

| Counter | Address (+ lane) | Width | Meaning |
|---|---|---|---|
| TPKT | 0x4c00 | 40 | all TX packets (good + bad) |
| TPOK | 0x4b00 | 40 | TX good packets |
| TBYT | 0x6f00 | 48 | TX bytes |
| TFCS | 0x5300 | 40 | TX frames with FCS error |
| TERR | 0x5d00 | 40 | TX frames with error (poisoned / underrun) |
| TFRG | 0x5c00 | 40 | TX fragments (< 64 B, bad FCS) |
| TUFL | 0x6100 | 40 | TX FIFO underflow |
| TOVR | 0x5500 | 40 | TX oversize |
| TRPKT | 0x6000 | 40 | TX runt (< 64 B) packets |
| TJBR | 0x5200 | 40 | TX jabber |
| T64 / T127 / T255 / T511 | 0x4000 / 0x4100 / 0x4200 / 0x4300 | 40 | TX size bins |
| T1023 / T1518 | 0x4400 / 0x4500 | 40 | TX size bins |
| TUCA / TMCA / TBCA | 0x4d00 / 0x4e00 / 0x4f00 | 40 | TX unicast / multicast / broadcast |
| RPKT | 0x0b00 | 40 | all RX packets |
| RBYT | 0x3d00 | 48 | RX bytes |
| RFCS | 0x0f00 | 40 | RX FCS-error frames |
| RFRG | 0x3500 | 40 | RX fragments |
| RUND | 0x3400 | 40 | RX undersize, good FCS |
| ROVR | 0x1a00 | 40 | RX oversize |
| RERPKT | 0x1800 | 40 | RX packets with error (RERR) |
| RRPKT | 0x3f00 | 40 | RX runt packets |
| RJBR | 0x1b00 | 40 | RX jabber |
| RALN | 0x1600 | 40 | RX alignment error |
| RFLR | 0x1700 | 40 | RX frame length out of range |
| RMTUE | 0x1c00 | 40 | RX MTU exceeded |
| RUCA / RMCA / RBCA | 0x0c00 / 0x0d00 / 0x0e00 | 40 | RX unicast / multicast / broadcast |
| R64 / R127 / R255 / R511 / R1023 / R1518 | 0x0000 / 0x0100 / 0x0200 / 0x0300 / 0x0400 / 0x0500 | 40 | RX size bins |

- Reading: 2 words, `rreg 5 <addr|lane> 2`; value = `w1 << 32 | w0`.
- Other fifo/status regs (not counters): `XLMAC_FIFO_STATUS` 0x61700 (TX_PKT_UNDERFLOW b2, TX_PKT_OVERFLOW b3, RX_MSG_OVERFLOW b1), cleared by `XLMAC_CLEAR_FIFO_STATUS` 0x61800 (same bits written 1, then 0); `XLMAC_RX_LSS_STATUS` 0x60b00; `XLPORT_TXFIFO_OVERFLOW` 0x2022600 (core, bit = lane); `XLMAC_TXFIFO_CELL_CNT` 0x62b00. All from the tsv. C

### Clear semantics

- Not clear-on-read: counters rise monotonically across repeated reads (log above). C
- Clear: `XLPORT_MIB_RESET` 0x2022c00 (core reg, no lane in the address), CLR_CNT [3:0] = one bit per lane. SDK `hurricane2.c:1966-1968` writes 0xf then 0; stock A5 phase does the same on both units (`init-phases-u0.md` line 153). C
- Our unit 0 driver never pulses it (no `MIB_RESET` in `kernel/`): counters start at garbage (xe51 RPKT 972590371, RFCS 569067734 at boot; xe52 RFCS 3214247915 constant). Use deltas, or pulse first:
  - xe51: `echo "0 wreg 5 2022c00 1 1" > .../schan` then `... 2022c00 1 0`.
  - xe52: values 4 then 0. (0xf then 0 resets all four lanes, harmless here; unit 1 is another chip.)
  - Single-lane pulse is I (SDK only uses 0xf).
- The garbage RFCS and RUND at boot are therefore not errors; compare before/after.

## 2. Loopback controls

### XLMAC (`XLMAC_CTRL` 0x60000 + lane, 64-bit)

| Bit | Field | Use |
|---|---|---|
| 0 / 1 | TX_EN / RX_EN | 0x1003 = TX_EN, RX_EN, SW_LINK_STATUS (live value) |
| 2 | LOCAL_LPBK | MAC TX -> RX loopback inside the XLMAC (SDK `mac_xl_loopback_set`, `tmp/sdk/xlmac.c:1388`) |
| 4 | REMOVE_FAILOVER_LPBK | not a loopback |
| 6 | SOFT_RESET | |
| 8 | LOCAL_LPBK_LEAK_ENB | with LOCAL_LPBK, also send the frames to the line |
| 12 | SW_LINK_STATUS | |

- No REMOTE_LPBK bit in this XLMAC (fields list in `regdb/hr2_regs.tsv`: only LOCAL_LPBK b2, LOCAL_LPBK_LEAK_ENB b8, REMOVE_FAILOVER_LPBK b4). Remote loopback is a TSC function (below). C
- SDK also turns LSS local/remote fault checking off while in MAC loopback (`XLMAC_RX_LSS_CTRL` 0x60a00 b0 LOCAL_FAULT_DISABLE, b1 REMOTE_FAULT_DISABLE) and notifies the PHY (`phyEventMacLoopback`). Optional at 1G (SDK writes fault status 0 for <= 1G).
- MAC loopback only proves the MAC and its counters, not the PCS or SerDes. It is the quick check that TPKT/RPKT/RFCS work.

### TSC (`MAIN0_LOOPBACK_CONTROL` 0x9009, devad 0, core reg: AER 0)

| Bits | Field | Meaning |
|---|---|---|
| 15:12 | REMOTE_PCS_LOOPBACK_ENABLE | bit per lane, RX -> TX at PCS (remote) |
| 11:8 | REMOTE_PMD_LOOPBACK_ENABLE | bit per lane, RX -> TX at PMD (remote) |
| 7:4 | LOCAL_PCS_LOOPBACK_ENABLE | bit per lane = 1 << (4 + lane), TX -> RX at the PCS/PMS parallel interface (digital, "gloop") |
| 3:0 | reserved | |

- Sources: OpenMDK `tmp/openmdk/bcmi_tsc_xgxs_defs.h:1331-1338`; SDK `tscmod_tx_loopback_control` (`tscmod_cfg_seq.c:2105`, "Modified registers 0x9009 bit 7:4", lane_select 0 / this_lane 0 = AER 0); stock binary `hwdb/fw/tscmod_static_writes.txt:60` writes 0x9009 with mask 0xf0. C
- OM `bcmi_tsc_xgxs_loopback_set` (`bcmi_tsc_xgxs_drv.c:4117`): PHY disable notify, RMW LOCAL_PCS_LOOPBACK_ENABLE with the lane mask, for CL73 AN also CL73_UCTRL1 NONCE_MATCH_OVER = 1, PHY enable notify. CL37 at 1G has no equivalent. C
- Precedent: unit 1 lane 0, 0x9009 bit 4 with credits/TX/RX off around it: PCS_LIVE 0x0003, no remote fault, "our TX stream decodes in our own RX" (`driver-internals.md` 13.6). C
- The lane field applies to the lane's PCS, so one write per lane: xe51 lane 0 = 0x10, xe52 lane 2 = 0x40.
- The loop is above the SerDes: the analog TX is not exercised. Loopback good + sw2 sees nothing = fault is analog/PMD TX; loopback bad = MAC/PCS TX path.

## 3. Recipe: prove the TX path locally (xe51 lane 0; xe52 = lane 2, bit 0x40, addresses |2)

Tools: `tscwr <front> <aer> <reg> <mask> <val>` is a masked RMW of one 16-bit register (other lanes' bits kept); `tscreg <front> <lo> <hi> [aer]` logs registers (dmesg). Both under `/sys/module/us48_cmic_xe/parameters/`.

1. Baseline (read only):
   - `echo "51 9009 9009 0" > .../tscreg` (record, expect 0x0000 loopback bits) and `51 c14c c154 0`.
   - XLMAC_CTRL/MODE/TX_CTRL: `0 rreg 5 60000 2`, `60100`, `60400` (expect 0x1003 / 0x20 / 0xc802 hi 0x10).
   - MIB set M = TPKT 4c00, TBYT 6f00, TFCS 5300, TERR 5d00, TFRG 5c00, TUFL 6100, TRPKT 6000, RPKT 0b00, RBYT 3d00, RFCS 0f00, RFRG 3500, RUND 3400, ROVR 1a00, RERPKT 1800. Optionally pulse `XLPORT_MIB_RESET` first (section 1).
   - `XLMAC_FIFO_STATUS` `0 rreg 5 61700 2`.
2. Quiet the line: `ethtool -s xe51 autoneg off` (forced 1000/full) so CL37 pages do not mix with the test. I: phylink may drop carrier and the driver may stop TX; if `tx_packets` does not rise, note it and hold off (no silent workaround).
3. Enable local PCS loopback (one RMW, no word-1 issue, 16-bit):
   - `echo "51 0 9009 10 10" > /sys/module/us48_cmic_xe/parameters/tscwr`
   - Confirm: `tscreg 51 9009 9009 0` reads bit 4 set; `c154` (PCS link) b1 = 1 and `c14c` sync set with the far end dark or not (it now follows our own TX).
4. Send a burst of known frames from the CPU on xe51 (e.g. 10 `ping -c 10 -I xe51` or `arping`; ARP = 42 B, ND 86 B, plus one `-s 100` ping to cover sizes). Link idle on the far side is fine: nothing leaves the PCS.
5. Re-read M and diff:
   - TPKT +N, RPKT +N, RFCS/RUND/RFRG/RERPKT/ROVR 0, TFCS/TERR/TFRG/TUFL 0: MAC + 8b/10b PCS path emits and decodes whole frames. Fault is analog TX, SerDes rate or beyond (go to FIR/polarity/rate tests with xe51 -> xe52).
   - TPKT +N, RPKT +N but RFCS or RFRG or RUND rise: frames corrupted between MAC and PCS decode (credits `c100`/`c103`, TX_CTRL PAD/CRC mode, TXFIFO). Size bins T64..T1518 vs R bins show the class.
   - TPKT +N, RPKT 0: PCS loop not engaged or RX path dead; check 9009 and `c154`, `XLMAC_RX_LSS_STATUS` 0x60b00.
   - TPKT 0 while `tx_packets` rose: frames die before the MAC (the earlier "TPKT stayed 0" was the wrong address 0x64c00).
   - `XLMAC_FIFO_STATUS` b2/b3 set: TX underflow/overflow at the MAC.
6. Restore (same register, same lane, mask only the lane bit):
   - `echo "51 0 9009 10 0" > .../tscwr`
   - Confirm `tscreg 51 9009 9009 0` equals the baseline, then `ethtool -s xe51 autoneg on`; carrier 1 within about 3 s and `c154` b1 as before; `XLMAC_FIFO_STATUS` clear via `0 wreg 5 61800 2 c 0` if b2/b3 got set (words: lo 0xc, hi 0).
7. Optional MAC-only loopback (no TSC): RMW both words of `XLMAC_CTRL`:
   - `0 rreg 5 60000 2` -> `lo hi` (expect 1003 0).
   - `0 wreg 5 60000 2 <lo|4> <hi>`; word 1 must be the old hi (a zero in word 1 cleared bit 0x10 of TX_CTRL in the earlier mistake; same class of 64-bit write).
   - Burst, read M, then restore `0 wreg 5 60000 2 <lo> <hi>` with the recorded values and read back.

- Cautions:
  - Do not write XLMAC regs with `words` = 1 and do not write 0 into word 1; always replay the read value.
  - 0x9009 is shared by the four lanes of the TSC: never write it unmasked (xe52 lane 2 and unit 1 are separate PHYs, but lanes 1/3 of this TSC may be in use).
  - Test is 1-2 s per run; no soak.
