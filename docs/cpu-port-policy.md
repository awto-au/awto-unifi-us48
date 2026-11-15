# CPU-port meters and CoS map (HR2 MMU)

Offline decode for #71. Stock capture: `tmp/schan_stock_writes.txt` (unit 0, 2026-11-15 capture). Decoder: `scripts/cpu_port_policy.py` (prints the tables below, log in `tmp/logs/cpu_port_policy.log`).

## Registers (regdb/hr2_regs.tsv)

| Register | Block / offset | Replicas | Fields |
|---|---|---|---|
| MAXBUCKETCONFIG | MMU blk 12, 0x19c00, step 0x4 | 8 (CoS) per port | EAV_MODE:30:1, MAX_REFRESH:12:18, MAX_THD_SEL:0:12 |
| MAXBUCKET | MMU blk 12, 0x1bc00 | per port/CoS | IN_PROFILE_FLAG:29:1, MAX_BUCKET:0:29 (fill state, not config) |
| MINBUCKETCONFIG | MMU blk 12, 0x15c00 | 8 | MIN_REFRESH:12:18, MIN_THD_SEL:0:12 |
| MINBUCKET | MMU blk 12, 0x17c00 | 8 | IN_PROFILE_FLAG:29:1, MIN_BUCKET:0:29 |
| PKTMAXBUCKETCONFIG | MMU blk 9, 0x2130400 | 8 | PKT_MAX_REFRESH:12:20, PKT_MAX_THD_SEL:0:12 (packets/s shaper) |
| PKTPORTMAXBUCKETCONFIG | MMU blk 9, 0x2130200 | 1 | PKT_PORT_MAX_REFRESH:12:20, PKT_PORT_MAX_THD_SEL:0:12 |
| MISCCONFIG | MMU blk 9, 0x2025a00 | 1 | METERING_CLK_EN:5:1 (stock 1) |
| EGRMETERINGCONFIG | MMU blk 12, 0x15600 | 1 | REFRESH:12:18, THD_SEL:0:12 (egress port meter, not CPU) |
| PKTSHAPECONFIG | not in this decode | 1 | ENABLE (SDK sets it for packet shaping) |

- `MAXBUCKETCONFIG` has no granularity field in regdb. Upstream code writes `MAX_METER_GRAN` only on the MTRO memories (triumph3/trident2 `cosq.c`), not on this register. Source of the granularity for HR2 port/CoS meters: unknown (I).
- `hwdb.py addr MAXBUCKETCONFIG` fails (regs are not in hwdb regions). `hwdb.py find 0x19c00` resolves to `ddr_alias`, so blk-relative MMU addresses are not named. The decoder reads regdb directly.

## Stock values (port 0 = CPU, unit 0)

Captured at `tmp/schan_stock_writes.txt` line 40288 onward. Decoded by `scripts/cpu_port_policy.py`.

| CoS | Address | Raw | EAV | MAX_REFRESH | MAX_THD_SEL |
|---|---|---|---|---|---|
| 0 | 0x19c00 | 0x0004808d | 0 | 72 (0x048) | 141 (0x08d) |
| 1 | 0x19d00 | 0x0001802f | 0 | 24 (0x018) | 47 (0x02f) |
| 2 | 0x19e00 | 0x0001802f | 0 | 24 | 47 |
| 3 | 0x19f00 | 0x0001802f | 0 | 24 | 47 |
| 4 | 0x1a000 | 0x0001802f (I) | 0 | 24 | 47 |
| 5 | 0x1a100 | 0x000c0177 (I) | 0 | 192 (0x0c0) | 375 (0x177) |

- Cos4 (0x1a000) is labelled `CMIC_RXBUF_EPINTF_RELEASE_ALL_CREDITS` in the capture. The address math makes it cos4 of MAXBUCKETCONFIG (I).
- Cos5 is unlabelled in the capture (I).
- Port 0 cos0 and cos1-3 are the values from #71.
- Stock `MINBUCKETCONFIG` = 0 on port 0 (no minimum). `MAXBUCKET` = 0.
- `cpu_meter_off` in `common/bcm-cmic/bcm_cmic_hr2.c` writes 0 to cos0-3 of port 0 at bring-up.
- Stock `MISCCONFIG` = 0x1fed (METERING_CLK_EN bit 5 = 1).
- Stock core PLL: `TOP_CORE_PLL_CTRL4` = 0x0006c0c9, MSTR_CH0_MDIV (bits 3:10) = 0x19 = 25, which is the 135 MHz setting (`[OM]` `esw/hurricane2` clock code). C for the register, C for the mdiv-to-MHz mapping per `docs/clocks.md`.

## Units (upstream OpenBCM 6d2330c, sdk-6.5.16)

Source: `src/bcm/esw/trident/meter.c` (`_bcm_td_rate_to_bucket_encoding`, `_bcm_td_granularity_params`, `_bcm_td_bucket_encoding_to_rate`). Not in `/vendors/broadcom/openbcm` (that copy has only hurricane2.c clock and CPU_COS_MAP TCAM init). Mode and granularity are not in the regdb row, so the mapping is (I) for HR2.

- Byte (bandwidth) mode, MAXBUCKETCONFIG: `rate_kbps = MAX_REFRESH x 8 x 2^g`, `burst_bits = MAX_THD_SEL x 2048 x 2^g` (g = granularity, 0..7).
- Packet mode, PKTMAXBUCKETCONFIG (HR2 path `src/bcm/esw/hurricane2/cosq.c`): `pps = PKT_MAX_REFRESH` (max 0xfffff), `burst_pkts = PKT_MAX_THD_SEL` (max 0xfff). No conversion. Also sets `PKTSHAPECONFIG.ENABLE`.
- Value 0 for both fields = disabled (trident encoding: rate 0 and burst 0).
- Metering clock: the kbps formula does not use the core clock. Core clock affects the refill clock only indirectly (I).
- Sibling chip datasheet (`/vendors/broadcom/bcm56150-datasheet-whycan/56150-DS06-RDSS.pdf`): "Metering granularity from 8 Kbps", "8 CoS queues for CPU", "traffic shaping on CPU queues: bandwidth based and packet-per-second based". Consistent with the table above.

## Stock rate and burst (I)

With g unknown, the two values in the table above give:

| CoS | g=0 rate / burst | g=3 rate / burst |
|---|---|---|
| 0 (72, 141) | 576 kbit/s (72 kB/s) / 36 KB | 4.6 Mbit/s (576 kB/s) / 288 KB |
| 1-3 (24, 47) | 192 kbit/s (24 kB/s) / 12 KB | 1.5 Mbit/s (192 kB/s) / 96 KB (about 63 frames) |

- Measured TFTP to CPU ~185 KiB/s (#34), about 130 frames/s.
- 130 frames/s x 1514 B = 1.58 Mbit/s. g=3 on cos1-3 gives 1.54 Mbit/s = 127 frames/s at 1514 B. Match within 3 %.
- g=0 gives 24 kB/s, which does not match the measurement.
- So: cos1-3 at g=3 explains ~130 frames/s (I). Granularity is inferred from this fit, not read from the chip.
- Which CoS the TFTP path lands in is open (see CPU_COS_MAP below).

## CPU CoS queues and CPU_COS_MAP

- 8 CPU CoS queues (regdb numels 8; datasheet).
- `CPU_COS_MAP` (regdb/hr2_mems.tsv, IPIPE blk 10, 0x444c0000, 128 entries, 142 bits in 5 words):
  - `VALID:0:1`
  - `KEY:1:69`, `MASK:70:69` (SW_PKT_TYPE_KEY:6:2, SW_PKT_TYPE_MASK:75:2, MIRR_PKT_KEY:5:1, MIRR_PKT_MASK:74:1)
  - `REASONS_KEY_LOW:8:32`, `REASONS_KEY_HIGH:40:20` (52 reason bits), mask at 77 and 109
  - `INT_PRI_KEY:1:4`, `INT_PRI_MASK:70:4`
  - `COS:139:3` = CPU queue (0..7)
- Selection: first valid entry in index order whose key/mask match. Mask bit 1 = compare, 0 = don't care (upstream `esw/rx.c` decode: mask 0 key 0 = match all).
- Stock capture: 29 of 128 entries captured, all VALID.

| Entry | COS | Match (decoded) |
|---|---|---|
| 0 | 5 | reason bit 22 exact |
| 1 | 5 | reason bit 11 exact |
| 2 | 5 | reason bit 3 exact |
| 3 | 1 | reason bit 18 exact |
| 4 | 0 | no reason bits, INT_PRI mask 0 (any), SW_PKT_TYPE 0/3 |
| 5-11 | 1-7 | INT_PRI key n (mask n, I), after entry 4 |
| 12-19 | 0 | INT_PRI 8-15 |
| 119-127 | mixed | INT_PRI rows (captured, partial) |

- Reason-bit names are not in the repo. Map bits to frame types from the upstream `bcm/rx.h` reason enum before using entries 0-3 (I).
- Entries 5-11 sit after the wildcard entry 4 (I). Whether their INT_PRI rows are reachable depends on the SW_PKT_TYPE and reason fields. Not resolved.
- `SW2_RAM_CONTROL_4.CPU_COS_MAP_TCAM_TM` = 0x10 (SDK, `openbcm-hurricane2.c` 3831-3835).

## Knobs for #71

- Unmetered (current bring-up): MAXBUCKETCONFIG[0][c] = 0. Stock is metered; zero removes protection.
- Byte-rate cap per CoS: MAXBUCKETCONFIG[0][c] = (MAX_REFRESH, MAX_THD_SEL) with g fixed by the encoding. Needs the granularity source for HR2 before use (I).
- Frames/s cap per CoS: PKTMAXBUCKETCONFIG[c] = (pps, burst_pkts), plus PKTSHAPECONFIG.ENABLE=1. Direct units, no encoding table. Preferred for the management role (low pps, high priority).
- Route a frame class to a queue: CPU_COS_MAP row with COS field (139:3), VALID=1, reason/INT_PRI/SW_PKT_TYPE key+mask.
- Min guarantee per CoS: MINBUCKETCONFIG (stock 0).
- Metering clock: MISCCONFIG.METERING_CLK_EN must be 1 (stock 1; `cpu_meter_off` does not touch it).

## Open (filed on #71)

- Which CoS carries the TFTP path (entries 0-3 vs cos0 default).
- HR2 granularity source for MAXBUCKETCONFIG.
- Reason-bit names for entries 0-3.
