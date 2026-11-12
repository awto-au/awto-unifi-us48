# Stock driver internals for the Linux switch driver (issue #47)

Facts the BCM53344/BCM53346 (HR2, BCM56150 family) driver needs. Reference only; no SDK code copied.
Tags: C = confirmed in binary or source, I = inferred. Sources:
- OM = OpenMDK `/vendors/broadcom/openmdk/db9c67869680/` (`cdk/PKG/arch/xgsm`, `cdk/PKG/chip/bcm56150`, `bmd/PKG/chip/bcm56150_a0`, `phy/PKG`).
- SW = stock `switchdrvr` 7.5.15, Ghidra export `tmp/ghidra/out/switchdrvr/switchdrvr.c` (VAs below).
- RDB = `regdb/hr2_regs.tsv`, `hr2_blocks.tsv`. OBCM = `private/reference/openbcm-hurricane2.c`.
- Helper: `python3 scripts/ghidra_regseq.py <VA>` lists the named register/field sequence of one stock function.

## 1. S-channel (SBUS)

### CMIC flavour
- Register level = **CMICm layout** (OM arch `xgsm`; `bcm56150/PKGINFO` `ARCH:xgsm`; no vendored chip uses `xgsd`). C
- SW has `_soc_schan_op_cmicm` @0x127a59c and a legacy `_soc_schan_op_no_cmicm` @0x127aea0; CMC0 regs at +0x31000 are what the cmicm path uses. C
- BDE `IPROC_CMICD` (base 0x48000000, IRQ 194, compat `brcm,iproc-cmicd`) is the iProc platform glue name; stock strings also say "CMICd" for the iProc block. Not a statement about the S-channel header. C (strings), I (reading)
- `xgsd` differs only in header: acc_type 5 bits at [18:14], dst block 7 bits at [25:19] (`xgsd_schan.h`). SW header builder @0x1279200 has both layouts, picked by a feature bit (`+0x124d38` bit 1). Which one the HR2 feature set selects is not derived statically. I
- Decide by first live read of UMAC_REV_ID (below): xgsm header `0x2c200200` vs xgsd `0x2c100200`. A NACK/timeout on one means try the other. Expect xgsm (OM validated on bcm56150). I

### CMC0 registers (RDB: `CMIC_CMC0_SCHAN_*`; abs = AXI 0x48000000 + off)
| Reg | Off | Use |
|---|---|---|
| SCHAN_CTRL | +0x31000 | bit0 MSG_START, bit1 MSG_DONE, bit2 ABORT, bit20 SER_CHECK_FAIL, bit21 NACK, bit22 TIMEOUT |
| SCHAN_ACK_DATA_BEAT_COUNT | +0x31004 | bits 4:0 |
| SCHAN_ERR | +0x31008 | bit0 NACK, 5:4 ERR_CODE, bit6 ERRBIT, 13:7 DATA_LEN, 19:14 SRC_PORT, 25:20 DST_PORT, 31:26 OP_CODE |
| SCHAN_MESSAGE[22] | +0x3100c + 4*i | message buffer, 22 words |
| CMC1 / CMC2 | +0x32000 / +0x33000 | same layout, +0x1000 per CMC (OM `CDK_XGSM_CMC_OFFSET`) |
- All C (RDB + OM `xgsm_cmic.h:1050-1193` + SW @0x127a59c/0x127a0b0).

### Handshake (C: OM `xgsm_schan.c`, SW @0x127a59c, @0x127a0b0)
1. Take a lock (SW mutex around the whole op).
2. Write `dwc_write` words to `SCHAN_MESSAGE[0..]`.
3. Write `1` to SCHAN_CTRL (SW: plain write; OM: read-modify-write of bit 0).
4. Poll SCHAN_CTRL until bit1 MSG_DONE (SW can instead wait on a CMC interrupt, soc property `schan_intr_enable`; polling default is the simple path).
5. Error check on the same value: bit21 NACK, bit20 SER_CHECK_FAIL, bit22 TIMEOUT. Then read SCHAN_ERR; bit6 ERRBIT = error.
6. Read `dwc_read` words from `SCHAN_MESSAGE[0..]` (word 0 = ack header).
7. Write `0` to SCHAN_CTRL (SW) / clear MSG_DONE (OM). Clears DONE and the error bits.
- Poll timeout recovery: set ABORT (bit2), then clear it (RMW). SW @0x1278fe0, OM `_schan_reset`. C
- SW poll limit comes from soc property `schan_timeout_usec` (SW stores it at soc_control+0xb4c8). C
- Max message 22 words (SW asserts `dwc_write`, `dwc_read` <= 22 at @0x1279cec). C
- SW also has a Fast S-channel (RDB `CMIC_CMC0_FSCHAN_*`): write ADDRESS (+0x31070), read/write DATA32 (+0x31074; DATA64 +0x31078/+0x3107c), poll STATUS.BUSY (+0x31068 bit0). No header; block comes from address bits 23:20/31:30. Gated by soc property `fschan_enable` (feature `+0x124d50` bit 15), reg32 read path @0x12688cc. C (code), unknown if on in this build.

### Header word (xgsm layout; OM `xgsm_schan.h:22-37`, SW builder @0x1279200, parser @0x1279410)
| Bits | Field | Notes |
|---|---|---|
| 31:26 | OPCODE | 6 bits |
| 25:20 | DST_BLK | SBUS block number (table below) |
| 19:17 | (zero) | SW old layout, other branch puts SRC_BLK in 19:14 instead of ACC_TYPE |
| 16:14 | ACC_TYPE | 3 bits, 0 for plain register/memory ops used here |
| 13:7 | DATA_LEN | bytes of data; matters for write/read-reg (4 for 32-bit, 8 for 64-bit) |
| 6 | ERR (EBIT) | set in ack on error |
| 5:4 | ECODE | |
| 3 | DMA | SW name (OM treats 3:1 as COS) |
| 2:1 | bank ignore mask (SW) / COS[1:0] | 0 for normal ops |
| 0 | CPU / NACK in ack | OM `SCMH_CPU` bit 0; SW debug names it NACK in the ack |
- C for the field positions above (OM + SW debug dump format @0x1279410 "HDR[NACK BANK DMA ECODE ERR DLEN ACC DPORT OPC]"). I for the 19:17 / SRC_BLK variant on HR2.

### Opcodes (OM `xgsm_schan.h:245-291`; SW reg read @0x12688cc uses 0x0b, checks ack 0x0c)
- READ_MEM 0x07 / ACK 0x08, WRITE_MEM 0x09 / ACK 0x0a, READ_REG 0x0b / ACK 0x0c, WRITE_REG 0x0d / ACK 0x0e. C
- TABLE_INSERT 0x24/0x25, DELETE 0x26/0x27, LOOKUP 0x28/0x29, FIFO_POP 0x2a/0x2b, FIFO_PUSH 0x2c/0x2d. C (OM)
- Message shapes: read = {hdr, addr} -> {ack hdr, data...}; write reg = {hdr, addr, data...}, no reply words read (SW reads 0, OM reads 0; ack still produced, DONE signals it). Memory read/write same shapes with entry data words. C

### SBUS block numbers (OM `bcm56150_a0_chip.c:31-44` == RDB `hr2_blocks.tsv` col schan) C
| Block | No. | Ports (logical = SBUS port number) |
|---|---|---|
| GPORT0/1/2 | 2/3/4 | 2-9 / 10-17 / 18-25 |
| XLPORT0/1 | 5/6 | 26-29 / 30-33 |
| IPIPE, EPIPE, MMU | 10, 11, 12 | all |
| OTPC | 13 | |
| CMIC | 14 | |
| IPROC | 15 | |
| TOP | 16 | |
| SER | 19 | |
- Ring map programmed at init (both OM and SW): `CMIC_SBUS_RING_MAP_0_7=0x11122200`, `_8_15=0x00430000`, `_16_23=0x00005004`, `_24_31=0`. Rings: 0 IP/EP, 1 XLPORT, 2 GPORT, 3 MMU, 4 OTPC/TOP, 5 SER. `CMIC_SBUS_TIMEOUT=0x7d0`. C

### Address word
- Register offsets in RDB/OM `*_defs.h` are identical (e.g. UMAC_REV_ID 0x12200, XLPORT_ENABLE_REG 0x2020b00). C
- SW `soc_reg_addr` @0x126cfbc builds `offset | (blk & 0xf) << 20 | (blk >> 4) << 30` and derives the header DST_BLK from those bits (@0x12688cc: `(addr & 0xffffff) >> 20 | (addr >> 30) << 4`). The address word sent is that full value. C
- OM sends `offset | blkport` only, block only in the header (`xgsm_blockport_addr.c`, `xgsm_port_addr.c`). C
- **HW: use the OM form.** Address word = `offset | blkport`, block only in the header. Live 2026-11-12 (#47): `UMAC_REV_ID` via `0x00012200` → 0x00030601; the SW form `0x00212200` → 0x00000000 (another register). C
- **Port index = low byte** (OM `blkport`). Live: `MAC_0` written via `0x00010301` read back only at 0x10301; `0x11300` is another register (0x00018100). SW's `portword << 12` is not a port index on HR2. C

### Worked example: read UMAC_REV_ID, unit 0, logical port 2 (front 1)
- Register: `UMAC_REV_ID` (RDB idx 51657, type port, block GPORT, offset 0x12200, reset 0x00030601; fields PATCH 7:0, MINOR 15:8, MAJOR 23:16).
- Logical port 2 = first port of GPORT0 (block 2), block-port index 0. Physical port is 9 (see section 4) but SBUS uses the logical number.
- DST_BLK = 2, OPCODE = 0x0b, DATA_LEN = 4, ACC_TYPE = 0:
  - header = `0x0b << 26 | 2 << 20 | 4 << 7` = **0x2c200200**
  - address = **0x00012200** (offset only; port index 0)
- Steps (AXI base 0x48000000, CMC0), verified on HW 2026-11-12:
  1. write `0x2c200200` -> 0x4803100c
  2. write `0x00012200` -> 0x48031010
  3. write `0x00000001` -> 0x48031000
  4. read 0x48031000 until bit1 set; fail if bit20/21/22 set (then read 0x48031008, abort per above)
  5. read 0x4803100c = ack header **0x30E00200** (opcode 0x0c, src blk 14 CMIC, 4 bytes), read 0x48031010 = **0x00030601**
  6. write `0x00000000` -> 0x48031000
- Header layout is xgsm (CMICm): confirmed by the ack above. C
- Write: header `0x0d<<26 | blk<<20 | 4<<7` (blk 2: 0x34200200), words {hdr, `offset|port`, value}, CTRL start, poll, clear. Ack 0x38E00000. Verified on `MAC_0`. C
- Console lines over ~400 chars get cut; keep on-box helper commands short.

## 2. Chip init order

Stock = `soc_reset` -> `soc_reset_bcm56150_a0` @0x100827c, then `soc_misc_init` @0x1209500 -> `soc_hurricane2_misc_init` @0x1039668. OM = `bcm56150_a0_bmd_reset.c` + `_init.c`. Both agree on order; SW adds PLL programming and GPHY power-up that OM leaves blank.

### Reset (SW @0x100827c, OM `_bmd_reset`)
1. (OM) CMIC init for endianness; write `CMIC_CPS_RESET.CPS_RESET=1` (RDB `CMIC_CPS_RESET` 0x10220, regtype cpureg); wait 10 ms; redo endian init. C (OM), SW path not traced
2. (SW, only if soc property `xgxs_lcpll_xtal_refclk`) program TOP XG PLL0-3 CH0/3/4/5 MDIV, NDIV_INT 0x28, PDIV 2. C
3. Write the SBUS ring map (4 words, above); wait (SW 10 ms, 250 ms if flag `0x10000` set). C
4. PTP TS PLL: `TOP_PLL_BYP_AND_REFCLK_CONTROL.TS_PLL_REFCLK_SEL`, `TOP_TS_PLL_CTRL_0..4` (VCO_DIV2, KA/KI/KP, NDIV_INT/FRAC, PDIV, CH0_MDIV) from a table keyed on property `ptp_ts_pll_fref` (default 25 MHz row). Broadsync PLL0/1 same via `TOP_BS_PLL0/1_CTRL_*`, `TOP_BROAD_SYNC0/1_LCPLL_FBDIV_CTRL_*`, with PLL resets held in `TOP_SOFT_RESET_REG_2` then released (RST_L then POST_RST_L). C
   - Not needed for a switch-only driver unless PTP is used; OM skips it. I
5. `TOP_SOFT_RESET_REG` release: XLP0/1, QSGMII2X0-2 (+FIFO), GP0-2, `TOP_QGPHY_RST_L = 0xf`; wait. Then TS, SPARE; `TOP_SOFT_RESET_REG_2.TS_PLL_RST_L` then `TS_PLL_POST_RST_L`. C (SW + OM)
6. `CMIC_SBUS_TIMEOUT = 0x7d0` (2000). C
7. `TOP_SOFT_RESET_REG`: EP, IP, MMU released; wait. C
8. PVTMON: `TOP_PVTMON_CTRL_0` (FUNC_MODE_SEL 0, MEASUREMENT_CALLIBRATION 5, BG_ADJ 3), pulse `TOP_SOFT_RESET_REG_2.TEMP_MON_PEAK_RST_L` 0->1, `TOP_PVTMON_CTRL_1` (SELECT 0, RESET_N 0->1). C
9. (SW only) XG/BS PLL `CML_2ED_OUT_EN=0`, `TOP_MISC_CONTROL_1.*_SW_OVWR`. C
10. **(SW only) GPHY power-up**: `TOP_QGPHY_CTRL_0` (IDDQ_BIAS 0xc cleared, EXT_PWRDOWN 0xff00 cleared, per quad), `TOP_QSGMII2X_CTRL.IDDQ` cleared per QSGMII2X block (loop of 4 iterations in the trace). Exact per-iteration values not extracted (chip-id dependent branches). C (registers), I (values)
11. (OM) for each XLPORT first port: `XLPORT_XGXS0_CTRL_REG` REFIN_EN=1, PWRDWN=0, wait 1.1 ms, RSTB_HW=1, wait 1.1 ms, RSTB_REFCLK=1, RSTB_PLL=1; then `XLPORT_MAC_CONTROL.XMAC0_RESET` 1 then 0. C

### Init (SW @0x1039668, OM `_bmd_init`)
1. Pipe reset: `ING_HW_RESET_CONTROL_1=0`; `ING_HW_RESET_CONTROL_2` {RESET_ALL, VALID, COUNT=0x8000}; `EGR_HW_RESET_CONTROL_0=0`; `EGR_HW_RESET_CONTROL_1` {RESET_ALL, VALID, COUNT=0x4000}. Poll `ING_HW_RESET_CONTROL_2.DONE` and `EGR_HW_RESET_CONTROL_1.DONE` (OM: 5 ms limit, 1 ms steps), then write both control regs to 0. C
2. Port maps: `ING_PHYS_TO_LOGIC_MAP[phys]` (invalid = 0x1f), `EGR_LOGIC_TO_PHYS_MAP[logical]` (invalid = 0x3f), `MMU_LOGIC_TO_PHYS_MAP[logical]` (invalid = 0x3f). C (OM)
3. Core PLL: `TOP_MISC_CONTROL_1.CMIC_TO_CORE_PLL_LOAD=1`, `TOP_CORE_PLL_CTRL4.MSTR_CH0_MDIV=0x19`, `TOP_CORE_PLL_CTRL2.LOAD_EN_CH0/1=1` (OM comment: 135 MHz when any TSC port > 1G). SW does the same with MDIV 0x19 or 0x87 depending on config. C
4. TDM: `IARB_TDM_CONTROL` {DISABLE=1, TDM_WRAP_PTR=83}; fill `IARB_TDM_TABLE[i].PORT_NUM` (physical) and `MMU_ARB_TDM_TABLE[i].PORT_NUM` (logical, 63 = empty; last entry WRAP_EN=1) from a per-config table; then `IARB_TDM_CONTROL` {DISABLE=0, WRAP_PTR=size-1}. OM tables: `bcm56150_a0_bmd_init.c:33-189` (embedded, powersave, cascade, ...). Which one stock uses for `bcm5615x_config=10` not extracted. I
5. `MISCCONFIG.METERING_CLK_EN=1`. `CMIC_RATE_ADJUST` and `CMIC_RATE_ADJUST_INT_MDIO` DIVISOR=40, DIVIDEND=1 (5 MHz MDC from 200 MHz). C (OM)
6. `RDBGC0_SELECT.BITMAP=0x0400ad11`. C (OM)
7. MMU: `CFAPCONFIG.CFAPPOOLSIZE=max`; `IP_TO_CMICM_CREDIT_TRANSFER` {TRANSFER_ENABLE, NUM_OF_CREDITS=32}; `HOLCOSPKTSETLIMIT[mport][cos 0..7] = (XQ0 entries)/8`; `PKTAGINGTIMER=0`, `PKTAGINGLIMIT=0`; `MMUPORTENABLE` = bitmap of MMU ports (CPU + GPORT + XLPORT). C (OM)
8. Default VLAN/tables: `VLAN_PROFILE_TAB[max]` (L2_PFM=1, L3 flags), `ING_VLAN_TAG_ACTION_PROFILE[0]`, `EGR_VLAN_TAG_ACTION_PROFILE[1]` (HiGig). C (OM)
9. `GPORT_RSV_MASK=0x70` (all GPORT blocks); `GPORT_CONFIG`: CLR_CNT=1, then GPORT_EN=1, then CLR_CNT=0. C
10. Per GPORT: `PORT_TAB[lport]` {PORT_VID=1, FILTER_ENABLE, OUTER_TPID_ENABLE, CML_FLAGS_NEW/MOVE=8}; `EGR_PORT.EN_EFILTER`; `EGR_VLAN_CONTROL_1`; `EGR_ENABLE[port].PRT_ENABLE`; MAC: `COMMAND_CONFIG` SW_RESET=1, then RX/TX/LOOP off, then SW_RESET=0; `TX_IPG_LENGTH=12`; then PHY attach/init. C (OM)
11. XLPORT (first port of block): `XLPORT_SOFT_RESET`, `XLPORT_MODE_REG` (quad/dual/single by lanes), `XLPORT_CNTMAXSIZE=0xde8`, EGR credit reset pulse + `XLPORT_TXFIFO_CTRL` clear counts (1 ms), `XLPORT_ENABLE_REG` bits; XLMAC: CTRL soft reset, RX_CTRL, TX_CTRL (AVERAGE_IPG 0xc, CRC_MODE 2), PAUSE, PFC refresh, RX_MAX_SIZE 0x3fe8, MODE (SPEED_MODE 4 = 10G), LSS drop-on-fault, then RX_EN/TX_EN. SW also sets `XLPORT_CONFIG.HIGIG_MODE=1` on HiGig ports. C
12. SW only: EEE ref/timers (`UMAC_EEE_REF_COUNT=500`, `GE*_EEE_CONFIG`, `MII/GMII_EEE_*_TIMER`), `EGR_CONFIG_1.RING_MODE=1`, `EPC_LINK_BMAP_64`, LED (`TOP_PARALLEL_LED_CTRL`). C
13. Switching init (OM `_switching_init.c`): create default VLAN, add each port untagged + STP forwarding, set port mode, CPU port added tagged, finally `EPC_LINK_BMAP_64.PORT_BITMAP_LO` = all ports. C (OM)

## 3. CMIC packet DMA (CMC0)

Registers (OM `xgsm_cmic.h`, offsets in CMC0; RDB has the `CMIC_CMC0_*` equivalents):
| Reg | Off | Notes |
|---|---|---|
| CMC_DMA_CTRL[ch] | +0x31140 + 4*ch | bit0 DIRECTION (1 = mem->SOC i.e. TX), bit1 DMA_EN, bit2 ABORT_DMA, bit3 SEL_INTR_ON_DESC_OR_PKT, bit4 PKTDMA_ENDIANESS, bit5 DESC_ENDIANESS, bit6 DROP_RX_PKT_ON_CHAIN_END, bit7 RLD_STS_UPD_DIS, bit8 DESC_DONE_INTR_MODE, bit9 ENABLE_CONTINUOUS_DMA |
| CMC_DMA_STAT | +0x31150 | [3:0] CHAIN_DONE, [7:4] DESC_DONE, [11:8] DMA_ACTIVE, then per-channel decode/ECC error nibbles |
| CMC_DMA_DESC[ch] | +0x31158 + 4*ch | descriptor bus address |
| CMC_DMA_STAT_CLR | +0x311a4 | [3:0] DESCRD_CMPLT_CLR, [7:4] INTR_COALESCING_CLR, [11:8] DESC_CONTROLLED_INTR_CLR |
| CMC_COS_CTRL_RX_0/1 | +0x31168 / +0x3116c | COS bitmap admitted per RX channel (low byte; per-channel step 8) |
| CMC_HOSTMEM_ADDR_REMAP[0..3] | see RDB | PCI host memory remap |
- 4 channels per CMC; OM uses `CMIC_NUM_PKT_DMA_CHAN=4`. C
- OM PCI remap values when unit is behind PCIe (unit 1 case): `0x144D2450, 0x19617595, 0x1E75C6DA, 0x1f` into `CMIC_CMC_HOSTMEM_ADDR_REMAP[0..3]`. C (OM `_bmd_init.c:846`). Relevant to unit 1 over PCIe, to be confirmed against `libbde/shbde_iproc.c`.
- The OM DMA helper (`bmdi/arch/xgsm_dma.c`: start/poll/abort) is not in the vendor subset; channel start sequence (write DESC, then CTRL with DMA_EN, poll CHAIN_DONE) is the standard CMICm flow. I

DCB (descriptor), 64 bytes = 16 words, little index = word number (OM `bcm56150_a0_defs.h:76476+`, `:102260+`). C
| Word | Bits | RX_DCB | TX_DCB |
|---|---|---|---|
| 0 | 31:0 | ADDR (bus address of buffer) | same |
| 1 | 15:0 | BYTE_COUNT (max bytes) | same |
| 1 | 16 | CHAIN (next sequential DCB valid) | same |
| 1 | 17 | SG (packet continues in next DCB) | same |
| 1 | 18 | RELOAD (next DCB from ADDR) | same |
| 1 | 19 | - | HG (module header present) |
| 1 | 20 | - | STAT |
| 1 | 21 / 22 | - | PAUSE / PURGE |
| 2-5 | | module header (HiGig2 format, 16 bytes; big-endian field order) | same; TX: `[0]=0x81000000, [1]=logical port, [2]=(txq<<8)\|0x00040000` per OM `_dcb_init` |
| 7 | 29:16 | PKT_LENGTH (after EP modification) | |
| 12 | 7:0 | SRC_PORT (logical) | |
| 15 | 15:0 | BYTES_TRANSFERRED | same |
| 15 | 16 / 17 / 18 | END_BIT / START_BIT / ERROR | |
| 15 | 31 | DONE | DONE |
- RX status words 6-14 carry PBE fields: REASON (CPU opcode, 48 bits at big-endian bits 224..271), COS, VFI, timestamp (802.1AS, always 32-bit), tag status, etc. Full list in `bcm56150_a0_defs.h:76476-76540`.
- TX (OM `_bmd_tx.c`): two DCBs per packet: DCB0 = first 16 (or 12 untagged) bytes with SG, CHAIN, STAT, HG + module header; DCB1 = rest starting at offset 16 (VLAN tag stripped), STAT. For HiGig frames the offset grows by the HiGig/HiGig2 header size. C
- RX (OM `_bmd_rx*.c`): one DCB per buffer, ADDR+BYTE_COUNT; start channel; CPU port must be in `EPC_LINK_BMAP_64` (bit 0) for packets to reach the CPU; stop = clear that bit then abort DMA. C
- Open: whether unit 1 CPU traffic uses unit 1's own CMC over PCIe or HiGig to unit 0. Not answerable from OM/stock static; stock BDE modules are the next place to look. **Open**

## 4. Internal GPHY and MIIM mapping

OM `phy/PKG/bus/bcm56150_miim_int/bcm56150_miim_int.c`, `phy/PKG/chip/bcm56150/bcm56150_drv.c`. C
- Logical -> physical port map (OM `bcm56150_a0_chip.c:58`): logical 2..9 -> physical 9..2 (reversed), logical 10..33 -> physical 10..33. Physical 0 = CPU. C
- MIIM address from **physical** port `p` (OM `_phy_addr`):
  - p 2..17: internal bus 0, addr = `(p-1) + ((p-2) >> 2)`. 
  - p 18..25: internal bus 1, addr = `((p-2) & ~7) + 1`.
  - p 26..33 (XLPORT/TSC): internal bus 1, addr = `1 + (p-26)`.
- Live scan (issue #4) matches: p2..17 -> internal bus 0 addrs 1-4, 6-9, 11-14, 16-19 (16 PHYs). Logical 2 = phys 9 = addr 9 = front 1. C
  - Front 1-8 = logical 2-9 = phys 9..2 = addr 9, 8, 7, 6, 4, 3, 2, 1.
  - Front 9-16 = logical 10-17 = phys 10..17 = addr 11, 12, 13, 14, 16, 17, 18, 19.
  - Front 17-24 = logical 18-25: external octal PHY (live: ext bus 0, addr 0x14-0x1b), OM expects internal bus 1 SerDes (QSGMII) for these ports. Mapping of 0x14-0x1b to ports not verified. I
- Probe ID: `0x600d` / `0x85b0` (rev nibble masked) = "Internal Octal QSGMII Gigabit PHY" (BCM54282 family; driver reuses `bcm54282_drv`). C
- PHY instance number (lane in octal): `inst = 9 - p` for p < 10, else `p - 2`. C
- GPHY init sequence (OM `bcm56150_phy_init`, register names; addresses are in `phy/PKG/chip/bcm54282_defs.h`, not in the vendor subset): C
  1. `TOP_MISC_GLOBAL_RESET`: TOP_MII_REG_SOFT_RST=1, RESET_1588=1.
  2. `MII_AUX_CTRL`: ENABLE_DSP_CLOCK=1, RESERVED=1; `DSP_TAP10 = 0x011b` (recommended for all revs).
  3. `POWER_MII_CTRL.SUPER_ISOLATE=0`; `LED_GPIO_CTRL.CURRENT_LED_DISABLE=0`; `MODE_CTRL.MODE_SEL=0` (SGMII to copper).
  4. **`MII_CTRL.POWER_DOWN=0`** (BMCR bit 11 clear; this is the stock fix for the power-down state seen live), then `MII_GB_CTRL` {SWITCH_DEV, CAP_1000BASE_T_FD}, `MII_CTRL` {FULL_DUPLEX, SPEED_SEL1, AUTONEG, RESTART_AN}.
  5. `SGMII_SLAVE.AUTO_DETECT=1`; `AUTO_DETECT_MEDIUM` (QFSD_WITH_SYNC_STATUS=1, rest 0); `MII_PHY_ECR.LED_TRAFFIC_EN=1`; `MII_AUX_CTRL.EXT_PKT_LEN=1`.
  6. EEE off: `EEE_803D` LPI_FEATURE_ENABLE=0, `EEE_ADV` 1000BASE-T/100BASE-TX = 0, `EEE_STAT_CTRL` all 0, `MII_BUFFER_CONTROL_0.DISABLE_LOC_LPI_HALF_DUPLEX=1` (on instance 0 of the octal).
  7. LEDs: `LED_SELECTOR_1/2`, `LED_CTRL=8`, `EXP_LED_SELECTOR=0`, `SPARE_CTRL` link LED modes 0; `LED_GPIO_CTRL.CURRENT_LED_DISABLE=0xf`.
- No errata writes beyond DSP_TAP10 in OM. Stock `soc_hurricane2` side adds the TOP QGPHY power control (reset section step 10). C / I
- QSGMII / TSC SerDes init for XLPORT ports 26-33 via `bmd_phy_mode_set("tsc")` + `bmd_phy_fw_base_set(0x0f00)` + `bmd_phy_init` (OM `_bmd_init.c:808-833`); TSC register-level sequence is in `phy/PKG/chip/tsc`, not yet read. **Not done**

## 5. Not done / open
- Port-index bits in the SBUS address for ports > first in a block (section 1).
- Which header variant HR2 uses at runtime (section 1): resolve with the first live read.
- Stock TDM table choice for `bcm5615x_config=10`; per-iteration QGPHY/QSGMII IDDQ values; unit-1 DMA path; TSC SerDes sequence; BDE module behaviour; GPL archive fetch.
