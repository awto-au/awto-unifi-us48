# Unit 0 xe51/xe52 1000BASE-X: TX data malformed on the wire (#78)

- Offline analysis 2026-11-21, branch `wip/sfp-1g-unit0` (cf1a456). No board access. Tags: C = read in source/dump, I = inferred.
- Symptom: link + CL37 AN up against sw2 (lan4/lan5), RX from sw2 fine. sw2 counts a few RX bytes, 0 packets, 1 parser error + 1 `rx_MacDiscard` per port. Our `tx_packets` moves (software count at DMA reap, `us48_cmic_core.c:1116`; not a wire count).
- Reading: AN /C/ pages and link-up go through (LP word `c198`=41a0 carries ack), so serializer, 8b/10b ordered sets and analog TX work at 1.25 Gbaud. Only MAC data frames are bad. Fault sits between XLMAC TX and the PCS encoder, or in the MAC's own framing.
- Sources: `tmp/sdk/xlmac.c`, `tmp/tscmod-sdk/tscmod_cfg_seq.c` (`tscmod_credit_set` :4041), stock `tmp/higig/stock.txt` + `docs/init-phases-u0.md`, `regdb/hr2_regs.tsv`, `tmp/logs/sfp_1g_test.log`, `kernel/us48-cmic/us48_tsc_fw.c`, `us48_cmic_core.c` (`port_enable`, `us48_xe_start_xmit`).

## 1. MAC-side state vs stock vs SDK (all equal where it matters)

| Reg (lane in low addr byte) | Ours xe51/52 | Stock unit 0 | SDK `xlmac.c` | Verdict |
|---|---|---|---|---|
| `XLPORT_MODE_REG` 0x2020a00 | 0 (quad) | 0 | n/a | equal |
| `XLPORT_ENABLE_REG` 0x2020b00 | 5 | 5 | n/a | equal |
| `XLPORT_TXFIFO_CTRL` 0x20300 | 4 (DELAY_PKT=1) | 4 | n/a | equal |
| `XLMAC_MODE` 0x60100 | 0x20 (SPEED_MODE 2) | 0x20 | 2 = 1000 (:55, :1241) | equal |
| `XLMAC_TX_CTRL` 0x60400 | 0xc802 | 0xc802 / hi 0x10 | IPG = 96/8 = 12 (:509), CRC_MODE 3 per-packet (:511) | IPG equal; CRC_MODE 2 (replace) = stock, differs from SDK (+4 length hack in `start_xmit`) |
| `XLMAC_TX_CTRL` PAD_EN b4 | 0 | 0 | not set | equal; runts (<64 B incl. FCS) not padded |
| `XLMAC_RX_LSS_CTRL` 0x60a00 | 0x73 | 0x73 | b0/b1 set for <5G (:1243) | equal; b3 LINK_INTERRUPTION_DISABLE 0, b6 DROP_TX_DATA_ON_LINK_INTERRUPT 1 |
| `XLPORT_FAULT_LINK_STATUS` 0x20600 | not written | n/a | 0/0 for <=1G (:1252-1263) | not written at 1G; SDK writes REMOTE/LOCAL_FAULT 0 |
| `XLMAC_CTRL` | 0x1003 | 0x1003 after link-up | SW_LINK_STATUS b12 | equal |

- No TX-side MAC register differs from stock. MAC config is not the lead.

## 2. Evidence that points at the TSC TX credits

- Every post-link dump (`sfp_1g_test.log` xe51 16103 s, xe52 13390 s, 14564 s, xe51 15959 s) reads `c100`=**4019**, `c103`=**0018**. C
- We write `c100` 0x40a5 / `c103` 0x0029 (`tsc_1g_patch`, `port_enable`, `tx_credit_cycle`); stock 1G reads 40a5 / 0029. C
- 4019/0018 = SDK `tscmod_credit_set` 1000M **plldiv 40** pair (clockcnt0 0x19, mac_credit 0x18; `tscmod_cfg_seq.c:4108-4112`). Our PLL is div66 = 0xa5 / 0x29 (:4116-4119). C
- Nothing in our driver writes 0x19 / 0x18, so the rewrite comes from the TSC (AN engine or lane uC) after link/AN. I
- Meaning: CLOCKCNT0 paces MAC->PCS data credits. 0x19 vs 0xa5 = 6.6x too fast, MAC credit 0x18 vs 0x29. The 8b/10b encoder is fed faster than 1.25 Gbaud drains: overrun, truncated/corrupt frame. AN /C/ pages are PCS-originated and ignore MAC credits, which matches "link ok, data bad". I

## 3. Step 0: classify at the MAC before touching anything

- MIB reads (unit 0, block 5, lane in low byte; xe51 = lane 0, xe52 = lane 2): `echo "0 rreg 5 <addr> 2" > /sys/module/us48_cmic/parameters/schan; cat /sys/module/us48_cmic/parameters/schan` (as `scripts/xlport_cmp.py`).
- Addresses: TPKT 0x4c00, TBYT 0x6f00, T64 0x4000, T127 0x4100, TFCS 0x5300, TFRG 0x5c00, TERR 0x5d00, TUFL 0x6100, TXFIFO_CELL_CNT 0x62b00, `XLPORT_TXFIFO_OVERFLOW` 0x2022600 (core, bit = lane), `XLMAC_RX_LSS_STATUS` 0x60b00.
- Read before and after 10 pings. Reading:
  - TPKT rises, TFRG/TUFL/TERR 0, no overflow: MAC emits clean frames; corruption is in the TSC (go to 1, 5).
  - TUFL/TERR/TFRG rising or TXFIFO overflow bit set: MAC-side underrun (credits starving the MAC: 1, 2).
  - TPKT flat while `tx_packets` rises: frames die before the MAC (EP/credit, hypothesis 3 or `EGR_*`).

## 4. Ranked hypotheses

Tools: `tscwr` = `echo "<front> <aer> <reg> <mask> <val>" > /sys/module/us48_cmic_xe/parameters/tscwr` (AER = lane: 0 for xe51, 2 for xe52); reads `echo "<front> <lo> <hi>" > .../tscreg` then dmesg. Link up and AN on for all tests; judge by sw2 `rx` counters/packets on lan4/lan5 (and our RPKT of the reply).

### H1. TSC rewrote TX credits to the plldiv-40 pair (`c100` 4019, `c103` 0018)
- Register: `TX_X4_CREDIT0` 0xc100 [13:0] CLOCKCNT0, `TX_X4_MAC_CREDITGENCNT` 0xc103.
- Stock 0x40a5 / 0x0029 (div66). Ours after link 0x4019 / 0x0018.
- Why top: only post-link TX register that differs from stock and from what we wrote; explains control frames fine, data frames cut.
- Test (xe52): `tscreg 52 c100 c103` (confirm 4019/0018), then `tscwr 52 2 c100 ffff 0000`, `tscwr 52 2 c100 ffff 40a5`, `tscwr 52 2 c103 1fff 0029`, ping from xe52.
- Confirms: sw2 `rx` packets non-zero and parser error stops growing; our `c100` stays 40a5 (if it flips back to 4019 within seconds, the writer is periodic: AN engine/uC, go to H5).
- Fix if confirmed: re-apply credits after AN complete (in `pcs_get_state` link-up edge) or stop the writer.

### H2. XLMAC TX FIFO underrun at 1G (DELAY_PKT / EP feed rate)
- Register: `XLPORT_TXFIFO_CTRL` 0x20300 [3:2] DELAY_PKT, MIB `TUFL` 0x6100, `XLPORT_TXFIFO_OVERFLOW` 0x2022600.
- Stock and ours 4 (DELAY_PKT 1). Not tested at other values; SDK has no use of it.
- Mechanism: CPU DMA is two DCBs (12 B header + payload); a store-and-forward delay larger than the 1G drain would still be a stall, not corruption, so lower probability.
- Test: step 0 counters. If TUFL > 0: `wreg 5 20300|lane` value 0xc (DELAY_PKT 3) and 0x0 in turn, re-ping.
- Confirms: TUFL stops, sw2 counts packets.

### H3. XLMAC framing: CRC replace + padding
- Register: `XLMAC_TX_CTRL` 0x60400 CRC_MODE [1:0], PAD_EN b4. Ours 0xc802 = stock (replace, no pad). SDK `mac_xl_init` uses CRC_MODE 3 (per packet).
- Our `start_xmit` adds 4 bytes so the replaced FCS lands on padding (`us48_cmic_core.c:1231`). Works at 10G on unit 1.
- Risk: frames < 60 B (ARP 42 B, MLD) are runts on the wire with PAD_EN 0; sw2 counts a runt as parser error. Does not explain 86 B ND frames, so only partial.
- Test: `wreg 5 60400|lane 0xc812 0x10` (PAD_EN on), ping with `-s 100` and an ARP; step-0 T64/T127 bins tell which size class leaves.
- Confirms: sw2 packets for short frames only: pad issue; none for all sizes: not this.

### H4. TX FIR taps (KR default 5a54 vs stock KX 03f0)
- Register: `0xc253` CL72_MISC1 FORCEVAL (ours 3fb0, stock 3f80), `0xc252` (ours reads 0, stock 83f0). See `docs/tsc-1g-diff.md` (branch `wip/tsc1g-diff-78`) section 5.4.
- Why low now: AN pages at the same baud pass; marginal taps give ISI on dense data only, a 850 nm MM link of metres tolerates it.
- Test: `tscwr 52 2 c253 00f0 0080`, `tscwr 52 2 c252 ffff 83f0`, `tscreg 52 c010 c010` expect 03f0, ping.
- Confirms: sw2 counters move only after this write.

### H5. AN engine still owns the TX path / rewrites TX regs
- Register: `0xc180` ENABLES b6 CL37, `0xc1ac` AN complete, `0xc104/0xc105` PCS credit pair (SDK sets 0xa5/0x29 only for 10/100 SGMII speed switch; we leave 0).
- Test (isolates H1's writer): `ethtool -s xe51 autoneg off` on our side and sw2 forced 1000 FD (or forced both), re-read `c100`/`c103`, ping.
- Confirms: forced mode keeps 40a5/0029 and frames pass: writer is the AN/speed-resolve logic (fix: apply credits after AN, or run forced). Frames still bad with credits intact: not AN, fall back to H2/H3 via step 0.

## 5. Not suspected (checked, equal to stock/SDK)

- `XLPORT_MODE_REG` quad 0, ENABLE 5, `XLMAC_MODE` SPEED_MODE 2, TX_CTRL IPG 12, TX_PREAMBLE_LENGTH 8 (hi word 0x10), `c111` 0x0c, `c113` 0x01cb, `c114` 0x0004, `c112` 0x01b4, `c130/c134/c136`, `c254` 0x0220: all stock.
- `EGR_MTU_SIZE` reset 0x3fff; SDK programs it per port, ours leaves reset value.
- `XLMAC_RX_CTRL` 0x40c (strict preamble b3, strip CRC b2) is RX only; stock 0x400.
- Note: ports.toml records unit 0 xe51 at 10G also drops TX after sync (#78): if H1 test fixes 1G, check `c100`/`c103` at 10G too (4021 / 0004 expected).
