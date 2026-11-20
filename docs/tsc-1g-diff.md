# Unit 0 TSC0 1000BASE-X: ours vs stock (#78)

- Offline analysis 2026-11-21, branch `wip/sfp-1g-unit0` (24a1710). No board access. Tags: C = read in source/dump, I = inferred.
- Symptom: xe52 (lane 2) light reaches the peer (sw2 lan5, GLC-SX-MM), no CL37 page, no link, AN on or forced.
- Sources: stock unit 0 TSC0 1G dump `tmp/higig/stock.txt` (lines `0 tsc a1/l0|l2/...`, a1 = TSC0, both lanes identical), `tmp/logs/sfp_1g_test.log` (our post-init reads), OM `tmp/openmdk/bcmi_tsc_xgxs_drv.c`, SDK `tmp/tscmod-sdk/`, `us48_tsc_fw.c`, `us48_tsc_init.h`, `us48_cmic_core.c` `port_enable`.

## 1. What our reads say about the two ends

- RX works: xe52 `c150`=000f (PMD_LOCK b3), `c14c`=0006 and `c154` b0 sync with AN on. Our CDR locks sw2's 1.25 Gbaud stream, so PLL div66 + `c137` OS_MODE 7 give 1.25 Gbaud on RX. C
- TX is the suspect: sw2 sees optical power only (SerDes 6 1/29 b8 follows our laser), never a /C/ ordered set, in AN, forced, both polarities, FIR and credit variants. C
- Steady laser power with a dark/idle serializer looks like "light, no data". I
- AN or forced makes no difference to what sw2 sees, so the fault is below the PCS (analog TX, TX lane enable/reset, TX clocking), not CL37 setup. I

## 2. Register diff, unit 0 lane 2 (lane 0 identical in stock)

Ours = value after `us48_tsc_init` + `port_enable` + AN enable (`sfp_1g_test.log` 2026-11-21 00:03/00:23, plus values from #78 comment). Stock = `tmp/higig/stock.txt` unit 0, no module, AN off.

### 2.1 Core / PLL (AER 0, shared by lanes 0-3)

| Reg | Stock | Ours | Verdict |
|---|---|---|---|
| 0x9000 SETUP | 6a80 | 7a80 | bit 12 FORCE_PLL_MODE_AFE_SEL: OM sets it always (`:2368`, `:3390`); SDK AN/CL37_10G path leaves it 0 (`tscmod_cfg_seq.c:1798-1870`, mask drops it). Same div66 either way (`9010` ee0a both). Unlikely cause |
| 0x9001 RESET | 0000 | aa00 (unit 1 reading) | lanes 1/3 analog held in reset (stage 0 mask 0x5); lanes 0/2 released. Tested no effect at 10G (13.7). Not on lane 2 |
| 0x9002 / 0x9003 | 0010 / e4e4 | same | equal |
| 0x9010 PLL status | ee0a | ee0a | equal (div66, locked) |
| 0x9012 | 01ff | 01ff | equal |
| 0x9111/9112 | 03f0 / 5a54 | same | equal |
| 0x9113 | 83f0 | stage 0 writes 03f0 mask 7fff | bit 15 kept from reset: read it, not verified |
| 0x9248 RAM_BASE | 0e10 | 0ee0 | stock unit 1 (10G, works) also 0ee0; differs on unit 0 only. Unit 0 10G ran with 0ee0, not the cause |
| 0x9250/9251/9256 CL37/CL73 timers | 0350 | 029a | OM default vs stock. Affects AN timing only, cannot cause "no light data" |
| 0x9252 / 0x9254 | 0000 / absent | 055d / 1a10 | OM defaults; AN only |
| 0xf014 TEMPERATURE | 0a3e (live) | a2a6 (stage 0 force) | not relevant |

### 2.2 Lane 2 (AER 2)

| Reg | Stock 1G | Ours | Verdict |
|---|---|---|---|
| 0xc010 TX FIR applied | 03f0 | not read | stock = KX default taps. Ours should be KR 5a54 (see 0xc253) |
| 0xc017 ANATX_DRIVER | 0990 | **8990** | **bit 15 ELEC_IDLE set (TX idle)**. `port_enable` seq_1g writes 0x0990 whole; stage 0 sets b15 on all lanes (`us48_tsc_init.h` stage 0). Never cleared. SDK: ELEC_IDLE is real electrical idle (`tscmod_cfg_seq.c:5233-5256`; `c01a` is only gearbox) |
| 0xc01a ANATX_CONTROL6 | 0 | not read | read it |
| 0xc100 CREDIT0 | 40a5 | **4019** | 4019/0018 = SDK 1G plldiv-40 pair (`tscmod_cfg_seq.c:4108-4115`); we never write it, `tx_credit_cycle` writes 40a5. Rewritten after AN enable (uC or AN engine) |
| 0xc101 | 0 | 0 | reads 0 both (written 0x8200/0x0082, read-back 0) |
| 0xc103 | 0029 | **0018** | same plldiv-40 origin |
| 0xc111 / 0xc114 | 000c / 0004 | same | equal |
| 0xc113 TX_X4_MISC | 01cb | 01cb (00:03), **01c8** (00:23) | 01c8 = ENABLE_TX_LANE 0, RSTB_TX_LANE 0. No driver path leaves that; second dump came after live experiments: check |
| 0xc130 / 0xc134 / 0xc136 | 2cb0 / 0073 / 0001 | same | equal |
| 0xc137 | 003a | 00bb | b7 OVERRIDE_OS_MODE (AN), b0 RSTB_LANE; OS_MODE 7 kept. Intended (OM CL37_10G `:1547`) |
| 0xc138 | 0009 | 0009 | equal |
| 0xc180 / 0xc181 / 0xc188 | 4000 / 0016 / 0 | 4040 / 0012 / 0002 | AN on (CL37 b6, PD_KX b1); c181 b2 SGMII_FD 0 per OM |
| 0xc186 CL73 abil | 0020 | 0068 | CL73 off, unused |
| 0xc252 CL72_TX_FIR_TAP | 83f0 | **0000** (reads 0, 83f0 after live write) | stage 2 writes 83f0; something zeroes it after (uC lane start / uC stop-resume). 10G rewrites it in stage 3 inside the uC-stopped window; 1G skips that row (`tsc_1g_skip`) |
| 0xc253 CL72_MISC1 | 3f80 | 3fb0 | b7 FORCE + FORCEVAL [6:4]: stock 0 (KX taps 03f0), ours 3 (KR taps 5a54). OM decode_set picks 0 for OS_MODE != 0 (`:1350-1370`); 1G patch has no c253 row. Docs 9.4 call it "CL72 only": wrong, it selects the TX FIR default |
| 0xc254 | 0220 | 0220 | equal |
| 0xc260 FIRMWARE_MODE | unreadable | 0 | SFP_OPT=1 in SDK comment; OM uses 0 for SFI optic |
| 0xc20x / 0xc21x uC state | c205 fe00, c206 3000, c20a/b 0063, c212 1080, c21d 0025, c21e 8800 | not dumped | 10G unit 1 differs (b401, 33dd, 0021/00a5): uC state depends on speed. Dump ours for the comparison |

## 3. Ordering and resets (stock vs OM vs ours)

- Stock MIIM write order for TSC is not captured (tracer fails, 13.7). End state only. OM/SDK order is the reference.
- OM `speed_set(1000)` (`:3280-3440`): RX_SIGDET override 1/0 -> uC stop -> AN_DISABLE -> **PLL/lane analog reset only if plldiv != cur** (66 stays 66: none) -> c254 -> c260 -> credit/encode/decode -> txdrv -> PD_KX 0 -> uC resume -> RX_SIGDET release -> PMD training off. No PMD lane reset after speed set. Ours = same minus the SIGDET override; no extra reset needed. C
- OM `_tsc_serdes_stop(0)` enable order (`:2046-2110`): RXSEQSTART, 1 ms, DISABLE_TX_R 0, **ELEC_IDLE 0**, 1 ms, uC rx lane ctrl / RSTB_LANE, ENABLE_TX_LANE 1, RSTB_TX_LANE 1, then (AN) CREDIT0 CLOCKCNT0 0x21 + CREDITENABLE. Ours `port_enable` seq_1g: c138, c101/c100, c017 0x0990, c01a 0, c113 pulse, c137 0x3b. Same content, no 1 ms gaps, c01a/c017 swapped. If `port_enable` is skipped (XLMAC_CTRL already 0x..03: log "MAC already enabled ... skip enable"), only `tx_credit_cycle` runs and ELEC_IDLE from stage 0 stays. I
- uC: stage 2 starts the lane uC (`TSC_UC_RX` 1) before stage 3; the uC rewrites c252 (and possibly more). OM writes txdrv inside the uC-stopped window for that reason. 1G skips the stage 3 rewrite of c252/c017 (`tsc_1g_skip`).
- PLL: OM waits for PLL lock only after a plldiv change; ours waits once in stage 0 (`ANAPLL_STATUS` b11, 200 ms). Lock holds (`9010` ee0a). Equal.
- TX_DISABLE: the kernel sfp driver owns the cage TX_DISABLE; light reaches sw2, so asserted-low is not the issue.

## 4. 1.25 Gbaud arithmetic

- 156.25 MHz x 66 = 10.3125 GHz VCO. OS_MODE 7 = 8.25. 10.3125 / 8.25 = 1.25 Gbaud. C (SDK `:2711`, OM `_tsc_decode_set`)
- RX evidence: PMD_LOCK and sync on sw2's stream at that setting. C
- TX rate: no register in the lane config selects a separate TX OS ratio (SDK/OM write only RX `c137`); TX follows PLL + SW_ACTUAL_SPEED (`c254` 0x02). Not provable without a loop. I

## 5. Ranked hypotheses and single live tests

Tools: `echo "52 <aer> <reg> <mask> <val>" > /sys/module/us48_cmic_xe/parameters/tscwr` (AER 2 = lane 2, AER 0 = core), read `tscreg`. Judge by sw2 `2/5` (CL37 word), our `c198`/`c154` b1, `c14c`.

1. **TX held in electrical idle / TX lane off.** Evidence: `c017` 8990 vs 0990; `c113` 01c8 in the second dump; idle gives light without data; sw2 sees nothing in every variant; 10G (where `c017` = 2020) transmitted. 
   - Read first: `tscreg 52 c017 c017`, `c01a`, `c113`, and dmesg `xe52: enable sequence done` vs `MAC already enabled`.
   - Test: `52 2 c017 8000 0`. If `c113` b1:0 = 0 also: `52 2 c113 3 0` then `52 2 c113 3 3`.
   - Confirms: sw2 `2/5` becomes non-zero within 1-3 s, our `c198` non-zero, `c154` b1 = 1.
   - If confirmed: fix = `port_enable` clears b15 on every path (also the skip path), or stage 3 clears it.
2. **TX PCS never encodes / wrong credits.** Evidence: `c100` 4019, `c103` 0018 (rewritten, plldiv-40 pair, not ours); precedent 13.7 (`c120` ENCODE_STATUS 0 until credits are re-armed after MAC up); our earlier 40a5/0029 live write had no 0-then-armed cycle.
   - Test: read `c120`/`c121`; then `52 2 c100 ffff 0` then `52 2 c100 ffff 40a5` (and `c103` 0029); re-read `c120`.
   - Confirms: `c120` goes 0 -> 0x4xxx (stock 0x4871/0x4811 at 10G) and sw2 sees a page. Do after (1) or both effects mix.
3. **Analog/serializer rate or PMD not actually switched (TX baud != 1.25 G).** Evidence: only RX rate is proven; uC rewrote `c252`, so uC state may differ from stock's 1G state.
   - Test (digital, one write, AN off via `ethtool -s xe52 autoneg off`): local PCS loopback lane 2: `52 0 9009 40 40` (LOCAL_PCS_LOOPBACK_ENABLE b4+lane). Read `c154`, `c14c`.
   - Confirms PCS chain OK (fault is analog) if `c154` b1 = 1 with loopback and 0 without. Then analog TX = (1) or rate. If loopback fails: encoder/decoder config (`c111`, `c130`, `c134`).
   - Rate check needs both ends ours: fibre xe52 -> xe51 (GLC-SX-MM in both, both 1G): `c150` b3 + `c14c` on xe51.
4. **TX FIR/default taps wrong (KR taps at 1G).** Evidence: `c253` 3fb0 (FORCEVAL 3 = KR 5a54) vs stock 3f80 (KX 03f0); `c252` = 0 so taps come from the MUXSEL default, stock forces 83f0. Live `c252` 83f0 alone did not help, consistent with (1) masking it.
   - Test: `52 2 c253 f0 80` and `52 2 c252 ffff 83f0` after (1). Read `c010`: expect 5a54 -> 03f0.
   - Confirms: link only after (1)+(4), or marginal link improves (sw2 BER).
5. **FORCE_PLL_MODE_AFE_SEL (9000 b12) and AN timers.** Evidence: 7a80 vs 6a80 and 029a vs 0350; weakest, both div66 and `9010` equal.
   - Test: `52 0 9000 1000 0` (core, also touches xe51), then read `9010` (must stay ee0a) and `c150` b3.
   - Confirms only if the PLL status changes or lock appears/vanishes. Expect no change. Timers: `52 0 9250 ffff 0350`, `9251`, `9256`: AN timing only.

## 6. Doc corrections for driver-internals 9.4

- `c253` is not "CL72 only": FORCEVAL selects the TX FIR default; 1G should be 0 (OM `:1350-1370`, stock 3f80). Add a `0xc253` row to `tsc_1g_patch` (mask 0x00f0, val 0x0080) after test (4).
- `c101` 0x8200 has no effect (reads 0 on stock and ours).
- `port_enable` 1G path assumes it runs: it must also clear ELEC_IDLE on the "MAC already enabled" path.
