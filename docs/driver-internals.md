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
- Logical port 2 = GPORT0 (block 2) lane 7 (lane = phys - 2; per-lane MAC registers use the lane, PORT_TAB/EGR_PORT/EPC use the logical port; see #31 comment 6842848670). Physical port is 9 (see section 4) but SBUS uses the logical number.
- DST_BLK = 2, OPCODE = 0x0b, DATA_LEN = 4, ACC_TYPE = 0:
  - header = `0x0b << 26 | 2 << 20 | 4 << 7` = **0x2c200200**
  - address = **0x00012200** (offset only; the example reads lane 0 = logical 9, not front 1; front 1 = 0x00012207)
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

Stock = `soc_reset` -> `soc_reset_bcm56150_a0` @0x100827c, then `soc_misc_init` @0x1209500 -> `soc_hurricane2_misc_init` @0x1039668. OM = `bcm56150_a0_bmd_reset.c` + `_init.c`. Both agree on order; SW adds PLL programming that OM leaves blank (and IDDQ power-down for 53342/53343 only).

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
10. **(SW only) QGPHY/QSGMII2X IDDQ**: SETS `TOP_QGPHY_CTRL_0` IDDQ_BIAS=0xc, EXT_PWRDOWN=0xff00 and `TOP_QSGMII2X_CTRL.IDDQ` (3 or 7) only when the device id is 0x8342/0x8343 (BCM53342/53343 powersave parts, unused quads powered down). BCM53344/53346 (0x8344/0x8346) take no IDDQ writes: all PHYs stay at reset-default power. Details in section 10. C
11. (OM) for each XLPORT first port: `XLPORT_XGXS0_CTRL_REG` REFIN_EN=1, PWRDWN=0, wait 1.1 ms, RSTB_HW=1, wait 1.1 ms, RSTB_REFCLK=1, RSTB_PLL=1; then `XLPORT_MAC_CONTROL.XMAC0_RESET` 1 then 0. C

### Init (SW @0x1039668, OM `_bmd_init`)
1. Pipe reset: `ING_HW_RESET_CONTROL_1=0`; `ING_HW_RESET_CONTROL_2` {RESET_ALL, VALID, COUNT=0x8000}; `EGR_HW_RESET_CONTROL_0=0`; `EGR_HW_RESET_CONTROL_1` {RESET_ALL, VALID, COUNT=0x4000}. Poll `ING_HW_RESET_CONTROL_2.DONE` and `EGR_HW_RESET_CONTROL_1.DONE` (OM: 5 ms limit, 1 ms steps), then write both control regs to 0. C
2. Port maps: `ING_PHYS_TO_LOGIC_MAP[phys]` (invalid = 0x1f), `EGR_LOGIC_TO_PHYS_MAP[logical]` (invalid = 0x3f), `MMU_LOGIC_TO_PHYS_MAP[logical]` (invalid = 0x3f). C (OM)
3. Core PLL: `TOP_MISC_CONTROL_1.CMIC_TO_CORE_PLL_LOAD=1`, `TOP_CORE_PLL_CTRL4.MSTR_CH0_MDIV=0x19`, `TOP_CORE_PLL_CTRL2.LOAD_EN_CH0/1=1` (OM comment: 135 MHz when any TSC port > 1G). SW does the same with MDIV 0x19 or 0x87 depending on config. C
4. TDM: `IARB_TDM_CONTROL` {DISABLE=1, TDM_WRAP_PTR=83}; fill `IARB_TDM_TABLE[i].PORT_NUM` (physical) and `MMU_ARB_TDM_TABLE[i].PORT_NUM` (logical, 63 = empty; last entry WRAP_EN=1) from a per-config table; then `IARB_TDM_CONTROL` {DISABLE=0, WRAP_PTR=size-1}. OM tables: `bcm56150_a0_bmd_init.c:33-189` (embedded, powersave, cascade, ...). Stock for `bcm5615x_config=10` = `hu2_tdm_cascade` (section 10). C
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
- OM DMA helper `bmd/PKG/arch/xgsm/xgsm_dma.c` now in the vendor store; exact recipe in section 5. C

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
- Unit 1 CPU path: own CMC over PCIe (stock starts RX on every unit), see section 8. C

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
- QSGMII / TSC SerDes init for XLPORT ports 26-33 via `bmd_phy_mode_set("tsc")` + `bmd_phy_fw_base_set(0x0f00)` + `bmd_phy_init` (OM `_bmd_init.c:808-833`); TSC register-level sequence: section 9. GPHY register addresses: section 7.

## 5. First-packet recipe: CMC0 packet DMA (RX ch1, TX ch0)

Sources: OM `bmd/PKG/arch/xgsm/xgsm_dma.c`, `bmd/PKG/chip/bcm56150_a0/bcm56150_a0_bmd_{rx,tx,init}.c`, `cdk/PKG/arch/xgsm/xgsm_cmic.h`. SW = `soc_dma_init` @0x1207c00, `soc_dma_chan_config` @0x1206d48, `soc_dma_start_chain` @0x120290c, `soc_dma_abort_channel` @0x12034f4, `soc_cmicm` hostmem remap @0x120c598, rx COS map @0x13cdf78/0x13ce5f4 (`bcm/common/rx.c`).
All addresses are AXI abs = 0x48000000 + CMC0 offset.

### Channels and registers
| Item | Value | Tag |
|---|---|---|
| TX channel | ch0, DIRECTION=1 (OM `XGSM_DMA_TX_CHAN 0`; SW `chan_config(0, type 1, 0x11)`) | C |
| RX channel | ch1, DIRECTION=0 (OM `XGSM_DMA_RX_CHAN 1`; SW `chan_config(1, type 2, 0x11)`); ch2/ch3 unused (type 0) in SW | C |
| DMA_CTRL[ch] | `0x48031140 + 4*ch` (ch0 `0x48031140`, ch1 `0x48031144`) | C |
| DMA_DESC[ch] | `0x48031158 + 4*ch` (ch0 `..158`, ch1 `..15c`) | C |
| DMA_STAT | `0x48031150`: [3:0] CHAIN_DONE, [7:4] DESC_DONE, [11:8] DMA_ACTIVE, [15:12] STWT addr-decode err, [19:16] PKTWRRD addr-decode err, [23:20] DESCRD addr-decode err, [27:24] STWR ECC err, [31:28] PKTWR ECC err (per channel bit) | C |
| DMA_STAT_CLR | `0x483111a4`: bit ch = DESCRD_CMPLT_CLR, bit 4+ch = INTR_COALESCING_CLR | C |
| DMA_CURR_DESC[ch] | `0x483111a8 + 4*ch` (RDB; read-only progress) | C |
| COS_CTRL_RX_0/1[ch] | `0x48031168 + 8*ch` / `0x4803116c + 8*ch` (ch1: `..170` / `..174`); bit n of RX_0 = CPU COS n (0..31), RX_1 = COS 32..47 | C (OM stride `4*i*2`, SW @0x13ce5f4 `(chan&3)*8`) |
| CMIC_RXBUF_EP_RLS_CRED | `0x4801a000` (not in CMC), bit0 RELEASE_ALL_CREDITS | C (OM) |
| PKT_COUNT per channel | `0x48031480 + 8*ch` (+0 RXPKT, +4 TXPKT), total RX `0x483114a0`, TX `0x483114a4`; use to confirm traffic | C (RDB), I (which counter a TX/RX channel bumps) |
| IRQ_STAT0 | `0x48031400`: ch0 DESC_DONE b14, CHAIN_DONE b15; ch1 DESC_DONE b12, CHAIN_DONE b13 | C (RDB) |
| Host IRQ mask | `CMC0_PCIE_IRQ_MASK0/1` `0x48031414/0x48031418`; OM clears both (polled); SW unmasks CHAIN_DONE (`0x8000 >> (2*ch)`) | C (OM), I (mask reg SW uses for the ARM host) |

DMA_CTRL bits: b0 DIRECTION, b1 DMA_EN, b2 ABORT_DMA, b3 SEL_INTR_ON_DESC_OR_PKT, b4 PKTDMA_ENDIANESS, b5 DESC_ENDIANESS, b6 DROP_RX_PKT_ON_CHAIN_END, b7 RLD_STS_UPD_DIS. C

### One-time init (OM `bmd_xgsm_dma_init`, SW `soc_dma_init` + `chan_config`)
1. DMA_CTRL[0..3]: keep only bits 5:4 (SW: `val & 0x30`); set endianness bits only for a big-endian host (OM). ARM-LE host, AXI: leave 5:4 = 0. Warm boot from stock: read them first and keep what stock left. C (SW), I (LE value)
2. `CMIC_RXBUF_EP_RLS_CRED` <- 1. C (OM)
3. `COS_CTRL_RX_0[ch1]` <- 0xffffffff, `COS_CTRL_RX_1[ch1]` <- 0xffff (all 48 CPU COS to the RX channel). C (OM)
4. ch0: `DMA_CTRL[0]` = (old & 0x30) | DIRECTION(1) = 0x01. ch1: `DMA_CTRL[1]` = (old & 0x30) | 0 = 0x00. SW additionally clears DMA_EN and writes `DMA_STAT_CLR` bit ch first; SW sets b3 only for flag INTR_ON_DESC (not used on ch0/1). C
5. Chip side (already set by stock on warm boot, else init section 2 step 7/9): `IP_TO_CMICM_CREDIT_TRANSFER` {TRANSFER_ENABLE b6, NUM_OF_CREDITS=32} (= 0x60), `MMUPORTENABLE` bit for CPU mport, `EGR_ENABLE[phys 0].PRT_ENABLE`, `PORT_TAB[0]` as other ports. C (OM)
6. PCIe host only (unit 1): HOSTMEM remap, section 8. Not needed for unit 0 (AXI). C

Warm-boot hazard (I): stock leaves ch0/ch1 DMA_EN=1 with DESC pointing into stock memory. Before step 1 read `DMA_CTRL[0..1]` and `DMA_STAT`; if DMA_ACTIVE or DMA_EN set, run the abort sequence below, else stale writes land in freed RAM.

### Abort (SW @0x12034f4, OM `bmd_xgsm_dma_chan_abort`)
1. If `DMA_STAT.DMA_ACTIVE[ch]` (bit 8+ch) set: write `DMA_CTRL[ch] |= DMA_EN|ABORT` (SW writes EN then EN|ABORT; OM sets ABORT only).
2. Poll `DMA_STAT` until bit 8+ch clear. SW: 1 ms steps, limit `pdma_timeout_usec` default 500000 us (SW backstop). Expected: one in-flight descriptor, tens of us (I). On expiry log `ch, DMA_STAT, DMA_CTRL`.
3. `DMA_CTRL[ch] &= ~(DMA_EN|ABORT)`; `DMA_STAT_CLR |= 1<<ch`. C

### DCB (64 bytes, 16 words, 16-byte aligned, bus address; same layout both directions)
| Word | Bits | Field |
|---|---|---|
| 0 | 31:0 | ADDR: bus address of the buffer |
| 1 | 15:0 | BYTE_COUNT (RX: buffer size; TX: bytes of this fragment) |
| 1 | 16 / 17 / 18 | CHAIN (next DCB follows in memory) / SG (packet continues in next DCB) / RELOAD |
| 1 | 19 / 20 / 21 / 22 | TX only: HG (module header in w2-w5 valid) / STAT / PAUSE / PURGE |
| 2-5 | | module header, 16 bytes (TX: stream-of-bytes header, RX: HiGig2-format header written by HW) |
| 6-14 | | RX status written by HW: w7[29:16] PKT_LENGTH, w12[7:0] SRC_PORT (logical), REASON/COS/VFI in the PBE fields (`bcm56150_a0_defs.h:76476`) |
| 15 | 15:0 | BYTES_TRANSFERRED (written by HW) |
| 15 | 16 / 17 / 18 | RX only: END / START / ERROR |
| 15 | 31 | DONE (HW sets; zero the DCB before use) |
- C (OM defs `RX_DCB_*`, `TX_DCB_*`).

### RX recipe, one packet (OM `rx_start/rx_poll/rx_stop`)
1. Allocate coherent DCB (64 B) + packet buffer (stock pkt_size 0x4000; 2 KB is enough for a test). Zero DCB; `w0` = buffer bus addr; `w1` = BYTE_COUNT; CHAIN=SG=RELOAD=0. C
2. Flush DCB; `DMA_DESC[1]` <- DCB bus addr; `DMA_CTRL[1] |= DMA_EN` (read-modify-write; value 0x02 when step 4 above left 0x00). OM inserts a memory barrier between the two writes. C
3. Set `EPC_LINK_BMAP_64` bit 0 (CPU). OM does this after the DMA start. C
4. Detect: poll `DMA_STAT` bit 5 (CH1 DESC_DONE; OM) or bit 13 of IRQ_STAT0 (SW uses CHAIN_DONE, `DMA_STAT` bit 1). Then invalidate DCB: `w15` bit 31 DONE, `w15[15:0]` length, `w12[7:0]` source logical port, `w7[29:16]` PKT_LENGTH. C
5. Clear: `DMA_STAT_CLR` <- 1<<1; `DMA_CTRL[1] &= ~DMA_EN`. (OM `_clear_chan`.) C
6. Stop: clear `EPC_LINK_BMAP_64` bit 0, then abort. C
- Frame length = `w15[15:0]` (BYTES_TRANSFERRED), compare with `w7[29:16]` PKT_LENGTH; whether the 4 CRC bytes are included and how the VLAN tag appears (per `EGR_VLAN` UT_BITMAP bit 0) is not stated in OM. I
- Continuous RX = chain of DCBs (CHAIN=1, final DCB RELOAD or controlled by DROP_RX_PKT_ON_CHAIN_END b6); SW chains 4 DCBs (`pkts_per_chain` 4, cfg in hpcHardwareInit). C

### TX recipe, one packet to a specific port (OM `bmd_tx`, HG-header form)
1. Two DCBs in one 128-byte coherent block, zeroed. Packet in a buffer P (DA, SA, tag if any, payload; CRC appended by MAC).
2. DCB0: `w0` = P, `w1` = `0x001B0000 | 16` (BYTE_COUNT 16, or 12 if untagged and tag stripped; flags SG b17, CHAIN b16, HG b19, STAT b20), module header `w2 = 0x81000000`, `w3 = dest logical port` (front 1 = 2), `w4 = (txq << 8) | 0x00040000` (txq 0), `w5 = 0`. C (OM `_dcb_init`)
3. DCB1: `w0` = P + 16, `w1` = `0x00100000 | (len - 16)` (STAT only; no HG/SG/CHAIN), module header words zero. For untagged-strip case the 4 tag bytes at P+12 are skipped (hdr 12, offset 16). C
4. Flush; `DMA_DESC[0]` <- DCB0 bus addr; `DMA_CTRL[0] |= DMA_EN` (0x03 with DIRECTION). C
5. Done = `DMA_STAT` bit 0 (CH0 CHAIN_DONE). Then `DMA_STAT_CLR` <- 1, `DMA_CTRL[0] &= ~DMA_EN`. DCB1 `w15` bit 31 DONE. C
6. Timeout: one 64 B frame should complete in tens of us (I); poll up to 1 ms then abort (stock step uses 1 ms steps).
- Normal switched TX (no HG, pkt->port < 0 in OM): HG=0 on both DCBs, module header unused, frame enters the ingress pipeline as from the CPU port. C (OM branch), I (behaviour)
- Egress of the TX frame needs: `EPC_LINK_BMAP_64` bit for the dest port, dest port in the VLAN it uses (EGR_VLAN), `EGR_ENABLE`. I

### What gets a frame from front 1 (unit 0, logical 2, phys 9) to the CPU (port 0)
Checks in order; items marked "stock" are already set after a warm boot from stock (live 2026-11-12: switch config survives).
1. MAC/link: GPORT0 port 0 (`MAC_0` 0x802AA89C live) RX enabled, GPHY link up (BMCR power-down bit 11 cleared). C (OM init 10)
2. `PORT_TAB[2]` (IPIPE blk 10, base `0x04000000`, 8 words): PORT_VID=1 (w0 bits 24+, spans w0/w1 bits 35:24), FILTER_ENABLE b0, OUTER_TPID_ENABLE=1. C (OM `_port_init`)
3. `VLAN_TAB[vid]`: VALID (w1 b6), PORT_BITMAP (w0 bits 29:0) has bit 2 (ingress filter) and bit 0 (CPU), STG id (w1 bits 15:7). C (OM `vlan_create`, `vlan_port_add`)
4. STP: `STG_TAB[stg].SP_TREE_PORT2` (w0 bits 5:4) = 3 forwarding. 0 disabled, 1 blocking/listening, 2 learning (stock had front 1 discarding). Same for `EGR_VLAN_STG`. Recipe in section 6. C
5. L2 flood: DA unknown or broadcast, `VLAN_PROFILE_TAB[VLAN_TAB.VLAN_PROFILE_PTR].L2_PFM`=1 (OM profile: L2_PFM=1; `L2_MISS_TOCPU` b18 would force a copy for DLF) so the frame is flooded to the VLAN's PORT_BITMAP, which includes port 0. A unicast DA learnt at port 0 (L2X entry, MODULE_ID/PORT_NUM 0) goes only to the CPU. C (OM profile), I (flood semantics of PFM=1)
6. Egress side: `EGR_VLAN[vid]` VALID (w2 b4), PORT_BITMAP bit 0 (field starts at bit 30, so port p = bit 30+p: p0 = w0 b30), `EGR_ENABLE[0].PRT_ENABLE`. OM never sets the CPU's `EGR_VLAN_STG`/`STG_TAB` state (stays 0). C (OM)
7. `EPC_LINK_BMAP_64` (IPIPE reg `0x46002700`, bits 29:0, bit = logical port) bit 0 = 1. OM `rx_start` sets it only for the CPU; `switching_init` sets all front ports. C
8. MMU: `MMUPORTENABLE` includes the CPU mport, HOL limits for CPU COS queues, `IP_TO_CMICM_CREDIT_TRANSFER` enabled, `CMIC_RXBUF_EP_RLS_CRED` released. C (OM)
9. CMIC: RX DCB armed on ch1 and `COS_CTRL_RX_*[ch1]` admits the packet's CPU COS (0xffffffff/0xffff admits all). C
- Reason code in the RX status (w6-w14, REASON bits: bit2 DLF, bit0 CPU-learn/unknown VLAN, bit4 L2 table copy) says why the frame was copied; a flooded frame needs no copy-to-CPU reason. I

## 6. First-packet recipe: STP/VLAN state via S-channel memory ops

Memory read/write on xgsm (OM `cdk_xgsm_mem_read/write`, `xgsm_mem.c:28-163`; same transport as section 1, block only in the header):
- READ_MEMORY 0x07: send {hdr, addr}; reply {ack hdr 0x08, data words}. `hdr` = `0x07<<26 | blk<<20 | datalen<<7`, datalen = entry words * 4, ACC_TYPE 0. C
- WRITE_MEMORY 0x09: send {hdr, addr, data words...}, reads nothing back (OM reads 0 words; DONE still signals). `hdr` = `0x09<<26 | blk<<20 | datalen<<7`. C
- `addr` = memory base (RDB `base`) + entry index, plain add, no port bits, no block bits. C (OM `xgsm_blockport_addr.c`: `addr = offset + idx`), I (HW accepts the OM form for memories like for registers)
- Ack header expectation for a mem read: `0x08<<26 | 14<<20 | datalen<<7` (CMIC src block 14), e.g. 0x20e00400 for 8 bytes. I (by analogy with the reg ack 0x30E00200)
- Entry word order: data words are the entry little-endian by 32-bit word, bit n of the entry = word n/32 bit n%32 (`cdk_field32_set` on word arrays). C
- Parity: STG_TAB/VLAN_TAB/EGR_VLAN/EGR_VLAN_STG have EVEN_PARITY fields; OM writes them as read-modify-write without touching parity, so HW generates it. I

### Tables (RDB `hr2_mems.tsv`, OM defs; blk 10 = IPIPE, 11 = EPIPE)
| Memory | blk | base | entries | bytes / words | Fields used |
|---|---|---|---|---|---|
| `VLAN_TAB` (RDB `QVLAN`) | 10 | `0x14000000` | 4096 | 16 / 4 | PORT_BITMAP [29:0]; VALID b38 (w1 b6); STG [47:39] (w1 b15:7); VLAN_PROFILE_PTR [63:60] (w1 b31:28); EVEN_PARITY b126 |
| `STG_TAB` (RDB `STG`) | 10 | `0x14040000` | 256 | 8 / 2 | SP_TREE_PORTn = bits [2n+1:2n], n 0..29 (w0 = ports 0-15, w1 b27:0 = ports 16-29); EVEN_PARITY b60 (w1 b28) |
| `EGR_VLAN` | 11 | `0x100c0000` | 4096 | 10 / 3 | UT_BITMAP [29:0]; PORT_BITMAP [59:30] (port p = bit 30+p); STG [67:60]; VALID b68 (w2 b4) |
| `EGR_VLAN_STG` | 11 | `0x10100000` | 256 | 8 / 2 | same layout as STG_TAB |
| `PORT_TAB` | 10 | `0x04000000` | 30 | 29 / 8 | PORT_VID [35:24], FILTER_ENABLE b0, OUTER_TPID_ENABLE [88:85] |
| `EGR_ENABLE` | 11 | `0x345c0000` | 34 | 1 / 1 | PRT_ENABLE b0, index = physical port (OM `_port_init`) |
- STP state values (OM `xgsm_stp_xlate.c`): 0 disabled, 1 blocking and listening, 2 learning, 3 forwarding. C
- `bmd_port_stp_set` (OM) writes STG_TAB[1] then copies the same 8 bytes to EGR_VLAN_STG[1]; OM assumes VLAN 1 uses STG 1. C. Use `VLAN_TAB[vid].STG` instead.
- Logical port index only; SP_TREE_PORT0 is the CPU and OM leaves it 0. OM stops at port 29 (HR2 table has 30 slots). C

### Exact words: set logical port 2 to forwarding in VLAN 1 (STG id read from VLAN_TAB)
All values hex. `W` = write to `CMC0 SCHAN_MESSAGE[i]` +0x3100c+4i, then `SCHAN_CTRL` handshake from section 1.
1. Read VLAN_TAB[1]: send `{1ca00800, 14000001}`; reply = ack + 4 words v0..v3. `stg = (v1 >> 7) & 0x1ff`, check `v1 & 0x40` (VALID), check v0 bit 2 (port 2 member) and bit 0 (CPU member).
2. Read STG_TAB[stg]: send `{1ca00400, 14040000+stg}`; reply ack + s0, s1.
3. `s0 = (s0 & ~0x30) | 0x30` (port 2 = forwarding). Write STG_TAB: send `{24a00400, 14040000+stg, s0, s1}`.
4. Read EGR_VLAN_STG[stg]: send `{1cb00400, 10100000+stg}`; reply e0, e1. `e0 = (e0 & ~0x30) | 0x30`. Write: send `{24b00400, 10100000+stg, e0, e1}`.
5. If port 2 / CPU are not in the VLAN: VLAN_TAB write `{24a00800, 14000001, v0 | 0x5, v1, v2, v3}` (bits 2 and 0). EGR_VLAN[1]: read `{1cb00600, 100c0001}` -> g0,g1,g2; `g0 |= 1<<30` (CPU, port 0), `g1 |= 1<<0` (port 2, field bit 32); optionally `g0 |= 0x4` for untagged egress on port 2; write `{24b00600, 100c0001, g0, g1, g2}`.
6. Verify by reading back both STG entries; `SCHAN_CTRL` error bits must stay 0 (NACK = bad block/addr).
- Header derivations: 0x07<<26 = 0x1c000000, blk 10<<20 = 0x00a00000, blk 11<<20 = 0x00b00000, 8 B<<7 = 0x400, 16 B<<7 = 0x800, 12 B<<7 = 0x600, 0x09<<26 = 0x24000000. C (arithmetic)
- Register-side companions: `EPC_LINK_BMAP_64` read `{2ca00200, 46002700}`, set bits 0 and 2, write `{34a00200, 46002700, val}`. C (OM), I (bit = logical port)
- Expected live starting state: stock had front 1 discarding, so `s0 bits 5:4` is 0 or 1 now.

## 7. BCM54282-family GPHY (BCM56150 internal octal) register definitions

OM `phy/PKG/chip/bcm54282/bcm54282_defs.h` (now in the vendor store), driver `bcm56150/bcm56150_drv.c` calls these. Access class is encoded in the high bits of each define. C
| Register | Address form | Fields used |
|---|---|---|
| `MII_CTRL` | clause 22 reg 0x00 | RESET b15, LOOPBACK b14, SPEED_SEL0 b13, AUTONEG b12, POWER_DOWN b11, ISOLATE b10, RESTART_AN b9, FULL_DUPLEX b8, SPEED_SEL1 b6 |
| `MII_GB_CTRL` | reg 0x09 | SWITCH_DEV b10, CAP_1000BASE_T_FD b9, CAP_1000BASE_T_HD b8 |
| `MII_PHY_ECR` | reg 0x10 | LED_TRAFFIC_EN b5 |
| `MII_AUX_CTRL` | reg 0x18 shadow 000 | EXT_PKT_LEN b14, ENABLE_DSP_CLOCK b11, RESERVED b10 |
| `SPARE_CTRL` | reg 0x1c shadow 0x02 | LINK_SPEED_LED_MODE b2, LINK_LED_MODE b0 |
| `LED_CTRL` | reg 0x1c shadow 0x09 | whole reg = 0x8 |
| `LED_SELECTOR_1/2` | reg 0x1c shadow 0x0d / 0x0e | 1 = 0x10 (LED1 sel 0, LED2 sel 1), 2 = 0x63 (LED3 sel 3, LED4 sel 6) |
| `LED_GPIO_CTRL` | reg 0x1c shadow 0x0f | CURRENT_LED_DISABLE [3:0]: 0 then 0xf at end of init |
| `SGMII_SLAVE` | reg 0x1c shadow 0x15 | SGMII_SLAVE_AUTO_DETECT b0 |
| `AUTO_DETECT_MEDIUM` | reg 0x1c shadow 0x1e | QFSD_WITH_SYNC_STATUS b5; others 0 |
| `MODE_CTRL` | reg 0x1c shadow 0x1f | MODE_SEL [2:1] = 0 (SGMII to copper) |
| `POWER_MII_CTRL` | RDB 0x02a | SUPER_ISOLATE b5 = 0 |
| `DSP_TAP10` | RDB 0x125 | write 0x011b |
| `MII_BUFFER_CONTROL_0` | RDB 0x800 | DISABLE_LOC_LPI_HALF_DUPLEX b15 = 1, AUTOGREEEN_ENABLE b0 = 0; written on lane/instance 0 of the octal |
| `TOP_MISC_GLOBAL_RESET` | RDB 0x82b | TOP_MII_REG_SOFT_RST b15, RESET_1588 b10 (both written 1, rest 0) |
| `EXP_LED_SELECTOR` | expansion reg 0x0f04 via reg 0x15 | whole = 0 |
| `EEE_STAT_CTRL` | expansion reg 0x0faf | all 0 |
| `EEE_ADV` | clause 45 dev 7 reg 0x3c | EEE_1000BASE_T b2, EEE_100BASE_TX b1 = 0 |
| `EEE_803D` | clause 45 dev 7 reg 0x803d | LPI_FEATURE_ENABLE b15, EEE_CAPABILITY_USING_QSGMII_AN b14 = 0 |
- Access encodings in the defines: `PHY_REG_ACC_RAW` (plain), `_BRCM_SHADOW` (low byte = reg, bits 15:8 = shadow select), `_BRCM_RDB` (RDB offset), upper bits of EEE/EXP defines = dev/expansion address. The generic accessors live in `phy/phy_reg.c`, not vendored: standard BRCM scheme assumed (reg 0x18/0x1c shadow select; RDB via reg 0x1e address + 0x1f data; expansion via reg 0x17 select + 0x15 data). I
- Init-order (as in section 4): top reset -> AUX_CTRL (0x0c00) -> DSP_TAP10 -> SUPER_ISOLATE -> LED current -> MODE_SEL -> `MII_CTRL.POWER_DOWN=0` -> GB_CTRL -> MII_CTRL {FD, SPEED_SEL1, AUTONEG, RESTART_AN} (= 0x1340) -> SGMII_SLAVE -> AUTO_DETECT -> ECR -> EXT_PKT_LEN -> EEE off -> LEDs. `MII_AUX_CTRL` first write = `b11|b10` = 0x0c00. C (derived from fields)

## 8. Unit 1 (BCM53346) CPU path

- **Own CMIC over PCIe.** Stock `hpcHardwareInit` (`broad_hpc_binds.c`, FUN_001299b8) loops over every unit and calls `bcm_rx_init(unit)` + `bcm_rx_start(unit, cfg)` (`FUN_00a72a44`, `FUN_00a739ac`), cfg pkt_size 0x4000, 4 pkts per chain. No cross-unit CPU routing in that loop. C
- Both units go through the same SDK DMA code (`soc_dma_*` take a `unit`); unit 1's CMC base is the PCIe BAR (`0000:01:00.0`, board.md), DCB/packet memory = host RAM reached by PCIe bus master. C (code), I (stock actually sees RX on unit 1; CPU ports of unit 1 are also in the cpudb)
- HiGig 2x13G between the units (unit 0 p28/29 <-> unit 1 p28/29) carries stacking/trunk traffic; stock uses the `cpudb`/`bcm_stk_*` stack code (`modmap`, `modport`), i.e. traffic for another unit's ports may be HiGig-forwarded, but CPU-bound frames from unit 1 ports go to unit 1's own CMIC. I
- PCIe host memory remap (SW `FUN_0120c598`, gated by feature bit `+0x124d50 & 0x80000` and a PCI-bus test): case A writes `CMC0_HOSTMEM_ADDR_REMAP_0..2` (`+0x31474`, `+0x31478`, `+0x3147c`) = `0x144d2450`, `0x19617595`, `0x1e75c6da`, `_3` (`+0x314ac`) = `0x1f`; case B (flag 0x4000 set) = `0x248e2860`, `0x29a279a5`, `0x2eb6caea`, `0x2f` then a `+0x31470` field update. OM uses case A. Field layout, case A/B meaning and the PAXB side: section 14. C (values)
- Endianness: OM `cmic_xgsm_endian_flags_get` inverts DMA endianness if the iProc PCIe bridge byte-swaps; for an LE ARM host with plain PCIe leave DMA_CTRL 5:4 = 0. I
- Unit 1 is reached after unit 0 config; use unit 0 CMC0 for the first packet, repeat the same recipe on unit 1 via PCIe BAR + remap (section 5) later. I
- BDE: stock uses `/dev/linux-user-bde` (strings); no `bcm-knet` netif. The OpenBCM `linux-kernel-bde` (`private/reference`) registers the AXI CMIC `0x48000000` (IRQ 194) and PCIe devices as separate BDE units. C

## 9. TSC SerDes init (unit 1 xe0/xe1, SFI)

- Driver = **`bcmi_tsc_xgxs`** (OM `phy/PKG/chip/bcmi_tsc_xgxs/`), not `bcmi_tsce_xgxs` (Eagle-based): `phy/PKG/chip/bcm56150/phy_depend.def` includes `BCMI_TSC_XGXS` (+ QSGMII serdes), TSCE is not selected for BCM56150. C
- Access, address decode, lane select (AER) and the unit-1 MIIM recipe: section 13.1. The `0x0010xxxx` defs are `phy_tsc_iblk` addresses, not `xgsm_miim_iblk` ones. C
- OM entry per port (`_bmd_init.c:808`): `bmd_phy_probe`; `bmd_phy_mode_set(port, "tsc", SERDES, 0|1)` (4-lane: 0; 1/2-lane: 1 + `2LANE` flag); `bmd_phy_fw_base_set(.., 0x0f00)` on subport 0; `bmd_phy_init`. Board reset callback for 10G config: Rx lane map 0x0123, Tx lane map 0x3210, polarity 0 (no inversion) (`board_bcm56150_10g.c`). C

### `_tsc_init_stage_0` (before firmware; primary lane only)
Registers (block:reg): `SETUP` 0x1090:00, `RESET` 0x1090:01, `POWER` 0x1090:02, `ANAPLL_STATUS` 0x1090:10, `ANATX_DRIVER` 0xc017, `TX_X4_MISC` 0xc113, `RX_X4_PMA_CONTROL_0` 0xc137, `CONTROLS` 0xc188, `DSC_MISC_CTRL0` 0xc21e, `PRBSCTRL` 0xc031, `FIRMWARE_MODE` 0xc260, `UC_CTRL` 0xc20e. C
1. `SETUP.PORT_MODE_SEL` [6:4] / `SINGLE_PORT_MODE` b3: 4 x 1-lane = 0 (`pm_4port_3_2_1_0`), 2 x 2-lane = 3, 1 x 4-lane = 4 (+ SPM=1). (Mode for xe0/xe1 on phys 26/28 = lanes 0 and 2: 1-lane mode if SFI is single-lane; unknown which, I.)
2. `ANATX_DRIVER.ELEC_IDLE` b15 = 1 on all lanes; `RESET.RESET_PLL` b7 = 0; `POWER.POWER_ON_TIMER_ENABLE` b4 = 1; `POWER.TX_PWRDWN_ANALOG` [11:8] = 0, `RX_PWRDWN_ANALOG` [15:12] = 0; wait 1 ms.
3. `DSC_MISC_CTRL0.RXSEQSTART_AN_DISABLE` = 1 (all lanes); `TEMPERATURE` 0x10f014: FORCE_TEMPERATURE=1, TEMP_IDX=8, TEMPERATURE=678.
4. `RESET.RX_RESET_ANALOG` [15:12] |= lane mask, `TX_RESET_ANALOG` [11:8] |= lane mask; `DSC_MISC_CTRL0.RXSEQSTART` = 1; wait 1 ms.
5. `SETUP.START_SEQUENCER` b7 = 0; `SETUP.REFCLK_SEL` [15:13] = 3 (156.25 MHz); wait 2 ms. `LANETEST.LFCK_BYPASS` = 0. `ANAPLL_TIMER1.VCOSTEPTIME` = 1.
6. Timers (all registers in block 0x1090.. via defs): CL73_BREAK_LINK 0x10ed, CL37_RESTART 0x029a, CL37_ACK 0x029a, CL37_ERROR 0x055d, CL73_ERROR 0x1a10, CL73_DME_LOCK 0x14d4, IGNORE_LINK 0x07d0, LINK_FAIL_INHIBIT_CL72 0x8236, _NOT_CL72 0x1a10, PD_SD 0x029a, LINK_UP 0x029a, CL72_MAX_WAIT 0x8236, CL72_WAIT 0x01fe; `PRBSCTRL` PRBS_INV_TX/RX = 1 (all lanes).
7. TX FIR defaults: KX main 0x3f; OS post/main/pre = 0/0x3f/0; BR 0x16/0x25/0x4; KR 0x16/0x25/0x4; 2P5 0/0x3f/0.
8. `SETUP.FORCE_PLL_MODE_AFE_SEL` b12 = 1, `DEFAULT_PLL_MODE_AFE` [11:8] = 0xa (div66); wait 1 ms. `TX_X4_CREDIT0` = 0, `TX_X4_PCS_CLOCKCNT0` = 0 (all lanes).
9. **Microcode download** (`bcmi_tsc_xgxs_firmware_set`): `COMMAND` (0x10ffc2) INIT_CMD b15 = 1; poll `DOWNLOAD_STATUS` (0x10ffc5) INIT_DONE b15, 1 ms steps, limit 200 ms (OM); `COMMAND2` (0x10ffca) TMR_EN [3:0] = 0xf; `RAMWORD` (0x10ffc0) = aligned_len-1 (aligned 16 B); `ADDRESS` (0x10ffc1) = 0; `COMMAND` = WRITE b3 | RUN b0; write every 16-bit LE word of the image to `WRDATA` (0x10ffc3); `COMMAND` = STOP b1; check `DOWNLOAD_STATUS` ERR0 b0 / ERR1 b1 = 0; `CRC` (0x10f015) = 0 (CRC calc enabled); `COMMAND` = MDIO_UC_RESET_N b4 (starts the 8051). Image = OM `bcmi_tsc_xgxs_ucode.c` (`tsc_ucode_bin`, 32728 bytes), not copied here. C
10. `SETUP.START_SEQUENCER` = 1; poll `ANAPLL_STATUS.PLLSEQPASS` b11, 1 ms steps, limit 200 ms (OM `PLL_LOCK_MSEC`).
- Stage 1 (`firmware_check`): poll `CRC` (0x10f015) != 0 up to 100 ms (OM `CRC_CALC_MSEC`), then read `VERSION` (0x10f010) for the log; no compare against an expected value. C

### `_tsc_init_stage_2` (after firmware)
1. Wait 1 ms; read `ANAPLL_STATUS`. `CONTROLS.PD_KX4_EN`, `PD_KX_EN` = 0 (no parallel detect).
2. Credits off: `TX_X4_CREDIT0.CREDITENABLE` = 0, `TX_X4_PCS_CLOCKCNT0.PCS_CREDITENABLE` = 0; `TX_X4_MISC.ENABLE_TX_LANE` = 0, `RSTB_TX_LANE` = 0; uC rx lane control off (`0x4d0/0x5d0/0x6d0/0x7d0` RAM-command writes `0x0d00`, offset `| 3<<20`); `RX_X4_PMA_CONTROL_0.RSTB_LANE` = 0.
3. AN abilities (CL73_BASE_ABIL_0, OVER1G_ABIL_0/1, CL37_BASE_ABIL, CL72_MISC2 SW_ACTUAL_SPEED 0x1b); for a 1-lane port: CL73 10GBASE_KR + 1000BASE_KX; `FIRMWARE_MODE` = 0; encode/decode: `TX_X4_ENCODE_0.ENCODEMODE` = 5, `TX_X4_MISC.SCR_MODE` = 3, `RX_X4_PCS_CONTROL_0` DECODERMODE 1 / DESC2_MODE 1, `RX_X4_DECODE_CONTROL_0.BLOCK_SYNC_MODE` = 1; credits `TX_X4_CREDIT0` CLOCKCNT0 0x21 + CREDITENABLE 1; `TX_X4_CREDIT1` 0x84; LOOPCNT0 1; MAC_CREDITGENCNT 4; CL49/CL48/CL82 RF/LF/LI enables = 1.
4. TX amplitude defaults: `ANATX_DRIVER` IDRIVER [11:8] = 9, IPREDRIVER [7:4] = 9, POST2_COEFF [14:12] = 0; `CL72_TX_FIR_TAP` (0xc?) FORCE b15 = 1, POST [14:10] 0, MAIN [9:4] 0x3f, PRE [3:0] 0.
5. `RESET.RX_RESET_ANALOG` / `TX_RESET_ANALOG` &= ~lane mask (release); wait 1 ms; uC rx lane control on (`0x0e00`); notify ChangeToFiber.
- All steps C (OM source), values for registers outside those listed are in `bcmi_tsc_xgxs_drv.c:2143-2776`.

### SFI + per-port settings (stock soc properties for unit 1 xe: `serdes_if_type_xe.1=9`, `serdes_preemphasis_xe.1=0x5a54`, `serdes_driver_current_xe.1=0x2020`)
- OM `_tsc_txdrv_set` driver profile `SFI`: POST2 2, IDRIVER 2, IPREDRIVER 2, preemph word `0x12<<10 | 0x2d<<4 | 1<<15` = `0xcad0` (OM SFI default). C
- `CL72_TX_FIR_TAP` field layout: FORCE b15, POST [14:10], MAIN [9:4], PRE [3:0]. Stock value **`0x5a54` = FORCE 0, POST 0x16, MAIN 0x25, PRE 0x4** = the KR/BR default taps from stage 0 (so the uC/CL72 default set applies, no force). C (layout, arithmetic), I (that the property is written to this register)
- `0x2020` for `serdes_driver_current`: not decodable from OM. If written as the whole `ANATX_DRIVER` it would be POST2 2, IDRIVER 0, IPREDRIVER 2 (IDRIVER 0 looks wrong); if per lane nibbles {0,2,0,2}. Open. I
- Lane/port facts for the board: unit 1 xe0/xe1 are SDK ports 26/27 on TSC0 (phys 26 and 28 per board.md), HiGig phys 30/32 on TSC1 (13G). I

## 10. TDM table for `bcm5615x_config=10`, IDDQ

- Stock (BCM53344 and BCM53346, config 10, also 1/11/12) selects **`hu2_tdm_cascade`**, 75 entries (OpenBCM `_soc_hu2_tdm_init`, `private/reference/openbcm-hurricane2.c:3200-3475`; OM `tdm_cascade` identical). C (source), I (stock binary built from the same code; not re-traced)
```
2 14 26 28 30 32 | 3 15 26 28 30 32 | 4 16 26 28 30 32 | 5 17 26 28 30 32 | 0
6 18 26 28 30 32 | 7 19 26 28 30 32 | 8 20 26 28 30 32 | 9 21 26 28 30 32 | 63
10 22 26 28 30 32 | 11 23 26 28 30 32 | 12 24 26 28 30 32 | 13 25 26 28 30 32 | 63
```
- Values are physical ports (0 = CPU, 63 = empty slot). Same 75 values go to `IARB_TDM_TABLE[i].PORT_NUM`; `MMU_ARB_TDM_TABLE[i].PORT_NUM` = logical (`p2l[phys]`, 63 for 63; logical 2..9 = phys 9..2); last entry `WRAP_EN`=1. C
- Sequence: `IARB_TDM_CONTROL` {DISABLE=1, TDM_WRAP_PTR=83}; write both tables; `IARB_TDM_CONTROL` {DISABLE=0, TDM_WRAP_PTR=74}. C
- Phys 26/28 = the 2 uplinks (1G/10G), 30/32 = the 2 HiGig 13G ports (option 2: `p2l_mapping_24_2_2`). C (board.md)
- QGPHY IDDQ: see section 2 step 10. For BCM53344/53346 no `TOP_QGPHY_CTRL_0` / `TOP_QSGMII2X_CTRL` writes occur (those only run for device ids 0x8342/0x8343; condition is the device id returned by `soc_cm_get_id`, local var in `soc_reset_bcm56150_a0` @0x100827c, branches at ~0x1009f44). Reset defaults: QGPHY_RST_L=0xf released, IDDQ/EXT_PWRDOWN cleared. C (branch), I (reset defaults, regdb shows reset value 0)
- Other device-id gated writes in the same function that apply to 0x8344/0x8346: `TOP_XG_PLL0/1_CTRL_3` and `TOP_BS_PLL0/1_CTRL_3` `CML_2ED_OUT_EN` = 0, `TOP_MISC_CONTROL_1` `CMIC_TO_XG_PLL0/1_SW_OVWR`, `CMIC_TO_BS_PLL0/1_SW_OVWR` = 1. C

## 11. Not done / open
- Whether the S-channel address word needs more than `offset + index` for memories (live check: read STG_TAB entry 1 and compare with a SBUS-DMA-free sanity register).
- Which xgsm/xgsd header: resolved (xgsm).
- Unit 1: xe port mode (1-lane vs 2-lane SFI), `serdes_driver_current` decode, firmware download over MIIM bus 1 timing.
- BDE module behaviour (stock `.ko`), GPL archive fetch, per-lane TSC address mapping.

## 12. Internal GPHY <-> GPORT path (QSGMII)

Question: front 1 (logical 2, phys 9, GPORT0 MAC 0, GPHY internal bus 0 addr 9) has PHY link and PHY RX counter, GPORT MIBs stay 0 with UMAC RX/TX enabled (#47). Tags: C = read in source/stock decompile, I = inferred, not verified on HW.

### 12.1 Findings (no QSGMII2X init exists for these ports)
- **No OpenMDK or stock code configures a SerDes between the integrated GPHYs and GPORT0/1.** C
  - OM `bcm56150_a0_bmd_reset.c`: only releases `TOP_SOFT_RESET_REG` QSGMII2X0-2 (+FIFO), GP0-2, `QGPHY_RST_L=0xf` (section 2 step 5). Never writes `TOP_QSGMII2X_CTRL`, `TOP_QGPHY_CTRL_0/1`, `GPORT_MAC_CRS_SEL`, `GPORT_SGN_DET_SEL`, `GPORT_MODE_REG`.
  - OM GPHY driver `bcm56150_drv.c` (section 4/7) has no SerDes access.
  - Stock: `TOP_QSGMII2X_CTRL` (id 0xc495) / `TOP_QGPHY_CTRL_0` (0xc493) written only in the 0x8342/0x8343 branch (section 10). No constant reference to `GPORT_MAC_CRS_SEL` (0x581a) or `GPORT_SGN_DET_SEL` (0x5822) in the whole export. BCM53344/53346 take reset defaults.
- **QSGMII SerDes cores serve GPORT2 (phys 18-25, front 17-24), not GPORT0/1.** C/I
  - OM `bcm56150_miim_int`: p18-25 -> internal bus 1 addr `((p-2)&~7)+1` = 17 (lanes share one address); p2-17 -> bus 0 GPHYs.
  - OpenBCM `soc_hu2_get_port_mapping` (`private/reference/openbcm-hurricane2.c:2155`, `:2688-2760`): property `serdes_qsgmii_sgmii_override=2` ("QSGMII forced to SGMII") keeps only phys 18 and 22 of 18-25 and leaves 2-17 untouched.
  - TOP clock-valid status text: "QGPHY_inst0/1 **or** QSGMII2X_inst0", "QGPHY_inst2/3 or QSGMII2X_inst1", "QSGMII2X_inst2" alone. GPORT0 = QGPHY inst 0/1, GPORT1 = inst 2/3, GPORT2 = QSGMII2X2. I
  - The "QSGMII" in the GPHY driver name / EEE field `EEE_CAPABILITY_USING_QSGMII_AN` refers to the GPHY's MAC-side interface, which is internal and has no MDIO-visible SerDes core in OM.
- **Internal bus 1 addrs 2/4/6/8 (ID 0x600d8770) are TSC cores, not QSGMII.** C
  - `bcmi_tsc_xgxs_drv.c`: `BCM_SERDES_PHY_ID0 0x600d`, `ID1 0x8770`, SERDES_ID model 0x11 (section 9).
  - QSGMII 65nm core ID is `0x0143`/`0xbff0` (reg 2/3), SERDES_ID0 model 0x07 (`bcmi_qsgmii_serdes_drv.c`). Not seen in the live scan (bus 1 addr 17 silent).
  - OM maps TSC lanes to bus 1 addr `1+(p-26)` (odd addrs for p26/28/30/32); live answers are at 2/4/6/8, i.e. +1. Offset unexplained (I).
- Lane mapping for phys 9 (logical 2): C
  - GPORT0 MAC lane 7 (static analysis of the stock trace, #31; the earlier lane-0 reading was wrong and probably explains the zero MIBs; untested on HW).
  - GPHY: bus 0 addr 9, octal instance `9-p` = 0 (OM; lane 0 of the octal phys 2-9; the one OM writes `MII_BUFFER_CONTROL_0` on).
  - TOP bit for this lane in `QGPHY_CTRL_0.EXT_PWRDOWN[15:0]`: not documented, likely bit 0 (I).

### 12.2 QSGMII SerDes (GPORT2 only), register facts
- Addressing (I, accessor `phy_aer_iblk_*` not vendored): define `0xBBBBRR` = block `BBBB`, MIIM reg 0x1f <- block, then reg `RR & 0x1f`; lane via AER reg `0xffde` (lane in low bits, `inst & 7`).
- Regs (C, `bcmi_qsgmii_serdes_defs.h`): `MIICNTL` 0x0000, `MIISTAT` 0x0001 (link b2), `XGXSSTATUS` 0x8001 (TXPLL_LOCK b11, SEQUENCER_DONE b9, SEQUENCER_PASS b8), `PLL_STATUS` 0x8050 (PLLSEQPASS b11), `TX_DRIVER` 0x8067, `RX_SIGDET` 0x80b4 (RX_SIGDET b4), `RX_CONTROL_PCIE` 0x80ba, `INDCOMBCTRL` 0x8105, `CONTROL1000X1` 0x8300, `CONTROL1000X2` 0x8301, `STATUS1000X1` 0x8304 (SGMII_MODE b0, LINK b1, DUPLEX b2, SPEED [4:3]), `GP_1000XSTATUS1` 0x8124 (same layout), `SERDES_ID0` 0x8310 (MODEL [5:0] = 7).
- OM init (C): `TX_DRIVER` DRIVER_FIXED_ENB=1, DRV_AMP=5; `INDCOMBCTRL` DISPARITY_EN_VAL, DISPERROR_EN_SYNC_VAL, DISPERROR_EN_VAL, DISPERROR_EN_FORCE = 1; `RX_CONTROL_PCIE` COMMA_ADJ_EN_R, COMMA_ADJ_EN_FORCE_R = 1; mode notify -> `CONTROL1000X1.FIBER_MODE_1000X` = 1 (fiber) or 0 (passthru). Stop: `MIICNTL` PWRDWN_SW only for lane > 1.
- Stock `phy_qsgmii65_init` (`0x10a58b4`, C): `MIICNTL` bit15 reset, poll clear (10 ms stock limit); property `serdes_qsgmii_sgmii_override` 1/2 -> `0x8015` hi byte 0x1100 under `0x8000` bit13 toggles (default 0 skips); `MIICNTL` = 0x0140 (or 0x1340 + reg 4 = 0x01a0 for AN); `0x8300` value 0x40 (|0x10 `serdes_automedium`, |1 `serdes_fiber_pref`, |0x20 `serdes_sgmii_master`) mask 0x51/0x71; `0x8301` value 1 (5 if AN) mask 0x25; `0x8302` value 0x60 mask 0x66 only with `serdes_fifo_low_latency`.
- TOP regs (C, OM defs + regdb): `TOP_QSGMII2X_CTRL` 0x02033400: REFCLK_PD [2:0], PWRDWN [5:3], IDDQ [8:6], INT_REFCLK_EN [11:9], QSGMII_DEF [17:12] ("configures the lane into QSGMII mode"), REF_CLKFREQ_SEL [20:18]; one bit per instance 0-2. Reset values all 0.

### 12.3 Read-only checks
S-channel words (header, address): READ_REG op 0x0b, header = `0x2c000000 | blk<<20 | 0x200`. TOP (blk 16) `0x2d000200`; GPORT (blk 2) `0x2c200200`; IPIPE (blk 10) `0x2ca00200`. Address of a port register = offset | port index (section 1). P = port index used for logical 2.
| # | What | Header | Address | Bits / expected |
|---|---|---|---|---|
| 1 | `MAC_MODE` (UMAC status) | 0x2c200200 | 0x00011100 \| P | **b5 LINK_STATUS** = link input from the PHY side to the MAC; [1:0] MAC_SPEED (2 = 1G); b2 duplex (0 = full). LINK_STATUS=0 means the GPHY->MAC sideband link is not seen. C (reg), I (it gates RX) |
| 2 | `COMMAND_CONFIG` | 0x2c200200 | 0x00010200 \| P | b0 TX_ENA, b1 RX_ENA, [3:2] ETH_SPEED, b13 SW_RESET, b22 ENA_EXT_CONFIG (1 = speed/duplex from PHY pins, ETH_SPEED ignored). Last seen 0x0180005B |
| 3 | `TOP_QGPHY_CTRL_0` | 0x2d000200 | 0x02033800 | EXT_PWRDOWN [15:0] (1 = port in low power; expect 0 for our lane), IDDQ_BIAS [19:16] (1 = bias off; expect 0), FORCE_DLL_EN [23:20], CK25_DISABLE [27:24] (expect 0), PLL_SEL_DIV5 b28 (0 = 25 MHz ref) |
| 4 | `TOP_MISC_STATUS` | 0x2d000200 | 0x02031800 | QGPHY_PLL_LOCK [4:1] expect 0xf (value & 0x1e = 0x1e) |
| 5 | `TOP_SOFT_RESET_REG` | 0x2d000200 | 0x02030400 | QGPHY_RST_L [19:16] = 0xf, QSGMII2X0-2 b9-11, FIFO b12-14, GP0-2 b6-8 (live 0x000FFFFF) |
| 6 | `TOP_QSGMII2X_CTRL` | 0x2d000200 | 0x02033400 | IDDQ/PWRDWN/REFCLK_PD expect 0; informational for GPORT0/1 |
| 7 | `TOP_QGPHY_CTRL_1` | 0x2d000200 | 0x02033c00 | EEE defaults per quad; informational |
| 8 | `TOP_L1_RCVD_CLK_VALID_STATUS_1` | 0x2d000200 | 0x0204c800 | QC0 [7:0] = link of the 8 ports of QGPHY inst 0/1 (GPORT0), QC1 [15:8] GPORT1, QC2 [23:16] GPORT2. Front 1: bit 0, or bit 7 if `GPORT_LINK_STATUS_TO_CMIC.TRANSPOSE` swizzles (I). Cross-check against MAC_MODE.LINK_STATUS |
| 9 | `GPORT_MAC_CRS_SEL`, `GPORT_SGN_DET_SEL` | 0x2c200200 | 0x02022200, 0x02022300 | 8 bits, one per MAC, PHY vs SerDes source for CRS / signal-detect; reset 0, never written by OM/stock; expect 0 |
| 10 | `GPORT_CONFIG`, `GPORT_MODE_REG`, `GPORT_LINK_STATUS_TO_CMIC` | 0x2c200200 | 0x02020000, 0x02020100, 0x02020500 | GPORT_EN b0 = 1; TRANSPOSE b0 |
| 11 | `LINK_STATUS_64` (IPIPE) | 0x2ca00200 | 0x46002c00 | b2 = logical 2 link (1 = up); compare with SW-set `EPC_LINK_BMAP_64` 0x46002700. Source of the HW bitmap not traced (I) |
| 12 | GPHY MIIM bus 0 addr 9, no RDB needed | n/a | reg 1, reg 0x19 (AUX_STATUS), reg 0x10 (ECR), reg 0x1c shadow 0x1f (MODE_CTRL = 0x7CA0 seen) | BMSR b2 link; MII_CTRL b10 ISOLATE = 0, b11 PDN = 0 |
| 13 | GPHY RDB 0x02a `POWER_MII_CTRL` (after 12.4 enter-RDB) | n/a | n/a | **b5 SUPER_ISOLATE** must be 0; stock sets it at reset (0x6e2) then clears, so a 1 here isolates the MAC interface (I) |
- Reading checks 1/8/13 first discriminates: link bit 0 in MAC_MODE with PHY link up = sideband path (SUPER_ISOLATE / EXT_PWRDOWN / EN bits); link bit 1 but MIB still 0 = MAC datapath (reset/clock) or PORT_TAB/EP side, not the PHY.

### 12.4 RDB access on the BCM54282-family GPHY
- RDB must be **enabled first**. C (stock `0x10c9be4`, `0x10cb564`, `0x10cbc98`, `0x10d0ca0`; same pattern in all four, around every RDB access):
  1. enter: MIIM reg **0x17 <- 0x0F7E**, reg **0x15 <- 0x0000** (expansion select 0x0f7e, data 0).
  2. access: reg **0x1E <- RDB address**, then reg **0x1F** read (data) or write (data); read-modify-write keeps the 0x1E pointer.
  3. exit: reg **0x1E <- 0x0087**, reg **0x1F <- 0x8000** (restores legacy meaning of 0x1E/0x1F).
- Without step 1, reg 0x1E is the legacy `TEST1` (OM `BCM54282_TEST1r` = 0x1e RAW) so RDB reads return junk/0: matches the live "RDB 0x02a / 0x125 read 0". I
- Legacy regs (0x00-0x1d) stay reachable in RDB mode; stock does enter/exit per access, so do not leave RDB mode on across other MIIM users.
- Stock `phy_bcm542xx_reset_setup` (`0x10d116c`, C), RDB writes in order: 0x82b = 0x8400; rev-dependent block (0x28 = 0xc00, 0x152 = 0x8787, 0x156 = 0x17d, 0x28 = 0x400, 0x1c0 = 0xd771, 0x1c2 = 0x72, 0x1cf = 6, 0xd8 = 0x20, 0x1cc = 0x500, 0x1c5 = 0xc100, 0xff = 0x1000, 0xfb = 0x7c00 then 0, 0x2a = 0x6e2 or 0x6c2 per property `port_super_isolate` default 2); then always 0x28 = 0xc00, **0x125 = 0x011b**, **0x2a &= ~0x20**, 0x1f = 0xbc00. OM has the 0x82b, 0x125, 0x2a subset only (section 7). Meaning of the rev block and 0x28 is unknown (I).
- Stock also uses a second address per octal (`phy_port_primary_and_offset`: primary lane addr and offset) for some RDB regs (0x83c); lane 0 of the octal = front 1 (addr 9). I

### 12.5 Minimal write sequence for one port (warm boot from stock)
Order matters: PHY side first, then MAC. Skip steps whose read-only check already shows the target value.
| Step | Write | Tag |
|---|---|---|
| 1 | GPHY bus 0 addr 9 MII reg 0 <- 0x1340 (clears PDN; done already) | C (OM), done live |
| 2 | RDB enter: reg 0x17 <- 0x0F7E, reg 0x15 <- 0x0000 | C (stock) |
| 3 | RDB 0x02a: reg 0x1E <- 0x002A; reg 0x1F RMW `&= ~0x0020` (SUPER_ISOLATE=0) | C (stock + OM) for the write, I that it is set after warm boot |
| 4 | RDB 0x125: reg 0x1E <- 0x0125; reg 0x1F <- 0x011B (DSP_TAP10) | C (stock + OM) |
| 5 | RDB exit: reg 0x1E <- 0x0087; reg 0x1F <- 0x8000 | C (stock) |
| 6 | Only if check 3 shows the lane's EXT_PWRDOWN bit set: S-channel RMW of `TOP_QGPHY_CTRL_0` (write header 0x35000200, address 0x02033800) clearing that bit (and IDDQ_BIAS if set) | I (stock never writes it for 53344/6) |
| 7 | MAC reset pulse (OM `_bmd_port_mode_set`; write header 0x34200200, address 0x00010200 \| P): `0x01802058` (SW_RESET=1, RX/TX off) -> `0x0180205B` (speed 1G, RX+TX on) -> `0x0180005B` (SW_RESET=0) | C (OM sequence), I (our earlier single write skipped the SW_RESET pulse) |
| 8 | Re-read checks 1, 2 and the MIB (`GRPKT`, GPORT port reg offset 0x1b00 per regdb, address \| P) | n/a |
- No QSGMII2X / TSC write is part of this sequence: none exists for GPORT0/1 in source or stock (12.1).
- If check 1 shows LINK_STATUS=1 and checks 3/13 are already clean, steps 3-6 are no-ops and the remaining suspects are MAC reset/clock (step 7) and the S-channel port index / block pick for the MIB read (verify the same `P` hits `MAC_0`). I
- Open: whether anything besides SUPER_ISOLATE / EXT_PWRDOWN gates the GPHY->MAC interface; stock's per-port enable (`phy_enable_set`, RDB reg 0 b11 + reg 0x1a bits) was seen but not fully traced (`0x10d0f54`, case 0x2b).

## 13. Unit-1 10G (TSC) status + recovery recipe

Scope: unit 1 (BCM53346, PCIe), xe0 = front 49 = phys 26 = TSC0 lane 0, xe1 = front 50 = phys 28 = TSC0 lane 2, XLPORT0 = SBUS block 5. Read-only checks first, then writes. Tags: C = read in OM / stock decompile / RDB, I = inferred, not run on HW. Added to the OM store 2026-11-12: `phy/util/phy_tsc_iblk_{read,write}.c`, `phy_aer_iblk_read.c`, `phy/include/phy/phy_{tsc_iblk,aer_iblk,reg}.h`, `bmd/shared/bmd_phy_probe_default.c`, `bmd_phy_ctrl.c`.
- Unit-1 CMIC base `B` = 0x0a000000 (PCIe BAR); every CMC0 offset below is `B + off`. S-channel / MIIM layouts equal unit 0 (sections 1, 4). C (S-channel verified live), I (MIIM on unit 1)

### 13.1 MIIM access to the TSC
- **The OM TSC driver uses clause 45 on the internal bus, not the `xgsm_miim_iblk` clause-22 block path.** C (`bcmi_tsc_xgxs_probe` sets `PHY_F_CLAUSE45 | PHY_F_ADDR_SHARE`; `phy_tsc_iblk_mdio_read`, `xgsm_miim.c`)
  - Section 9's earlier "block = addr>>8" decode was wrong (that is `xgsm_miim_iblk`, used by other cores).
- **MIIM_PARAM** (`B+0x31080`): INTERNAL_SEL b25 = 1, BUS_ID [24:22] = 1, C45_SEL b21, PHY_ID [20:16] = A, PHY_DATA [15:0].
  - C22: `0x02400000 | A<<16 | data`. C45: `0x02600000 | A<<16 | data`. C
- **PHY_ID A (TSC0 lane 0 / lane 2)**: A = 2 (xe0) and A = 4 (xe1). I
  - Source: live unit-0 scan, issue #4: bus 1 answers 0x600d/0x8770 at 2, 4, 6, 8 (phys 26, 28, 30, 32 = 2 + (p-26)).
  - OM `_phy_addr` gives 1 + (p-26) = 1 / 3 (off by one, section 12.1). Unit 1 not scanned: verify first with the probe below.
  - The TSC accepts the lane in AER as well (`PHY_F_ADDR_SHARE`), so A only has to hit the TSC0 core.
- **Probe (read-only, C22)**: ADDRESS = 2 then 3, expect `0x600d` / `0x8770` (rev nibble masked) at A = 2 and A = 4. Then C45 SERDESID `0x0010900e` [5:0] = model (0x11 per section 12.1). C
- **One MIIM transaction**, same polling as `scripts/miim.py`:
  - read: CTRL(`B+0x3108c`) <- 0; PARAM <- `0x02600000 | A<<16`; ADDRESS(`B+0x31088`) <- `devad<<16 | reg16`; CTRL <- 0x2 (RD_START); poll STAT(`B+0x31090`) b0 OPN_DONE; DATA = READ_DATA(`B+0x31084`) & 0xffff; CTRL <- 0.
  - write: PARAM <- `0x02600000 | A<<16 | data`; ADDRESS as above; CTRL <- 0x1 (WR_START); poll; CTRL <- 0. C
- **One TSC register access** `acc(A, addr32)` (`phy_tsc_iblk_mdio_*`, C45 branch): C
  1. `devad = (addr32 >> 16) & 0xf`, `reg16 = addr32 & 0xffff`, `copies = (addr32 >> 20) & 7`.
  2. `aer` = lane for `copies` 0; 0 for `copies` 1; `lane & ~1` for `copies` 2. Lane = `(phys-2) & 3`: xe0 = 0, xe1 = 2.
  3. MIIM write: ADDRESS = `devad<<16 | 0xffde`, data = `aer` (AER register).
  4. MIIM read/write: ADDRESS = `devad<<16 | reg16`.
  - AER is sticky per PHY address; OM rewrites it before every access, do the same.
  - `0x0010xxxx` defs (`copies` 1) = core-level registers, always AER 0. `0x0000cxxx` (`copies` 0) = per-lane, AER = lane. Bit 20 is the copies field, not part of devad.
- **Clause-22 fallback** (`phy_tsc_iblk_mdio_*`, non-C45 branch): `0x1f <- 0xffd0`, `0x1e <- aer`, `0x1f <- reg16 & 0xfff0`, then reg `(reg16 & 0xf) | (reg16 & 0x8000 ? 0x10 : 0)`. Bit 15 of the block value is kept. C
- Resolved addresses (A = 2 or 4; `AER` = 0 core / 0 or 2 lane; C45 = ADDRESS value for devad 0):

| Name | addr32 | C45 ADDRESS | C22 `0x1f` / reg | AER |
|---|---|---|---|---|
| SERDESID | 0x0010900e | 0x900e | 0x9000 / 0x1e | 0 |
| SETUP | 0x00109000 | 0x9000 | 0x9000 / 0x10 | 0 |
| RESET | 0x00109001 | 0x9001 | 0x9000 / 0x11 | 0 |
| ANAPLL_STATUS | 0x00109010 | 0x9010 | 0x9010 / 0x10 | 0 |
| VERSION | 0x0010f010 | 0xf010 | 0xf010 / 0x10 | 0 |
| CRC | 0x0010f015 | 0xf015 | 0xf010 / 0x15 | 0 |
| RAMWORD, ADDRESS, COMMAND, WRDATA | 0x0010ffc0..c3 | 0xffc0..c3 | 0xffc0 / 0x10..13 | 0 |
| DOWNLOAD_STATUS | 0x0010ffc5 | 0xffc5 | 0xffc0 / 0x15 | 0 |
| MDIO_UC_MAILBOX | 0x0010ffc8 | 0xffc8 | 0xffc0 / 0x18 | 0 |
| ANATX_DRIVER | 0x0000c017 | 0xc017 | 0xc010 / 0x17 | lane |
| ANATX_CONTROL6 | 0x0000c01a | 0xc01a | 0xc010 / 0x1a | lane |
| ANARX_STATUS1 | 0x0000c02b | 0xc02b | 0xc020 / 0x1b | lane |
| TX_X4_MISC | 0x0000c113 | 0xc113 | 0xc110 / 0x13 | lane |
| TX_X4_PCS_STATUS | 0x0000c121 | 0xc121 | 0xc120 / 0x11 | lane |
| RX_X4_PMA_CONTROL_0 | 0x0000c137 | 0xc137 | 0xc130 / 0x17 | lane |
| RX_X4_RX_SIGDET | 0x0000c138 | 0xc138 | 0xc130 / 0x18 | lane |
| RX_X4_CL49_LOCK_FSM_STATUS | 0x0000c145 | 0xc145 | 0xc140 / 0x15 | lane |
| RX_X4_PMA_PMD_LIVE_STATUS | 0x0000c150 | 0xc150 | 0xc150 / 0x10 | lane |
| RX_X4_PMA_PMD_LATCHED_STATUS | 0x0000c151 | 0xc151 | 0xc150 / 0x11 | lane |
| RX_X4_PCS_LATCHED_STATUS_1 / _0 | 0x0000c152 / c153 | 0xc152 / c153 | 0xc150 / 0x12, 0x13 | lane |
| RX_X4_PCS_LIVE_STATUS | 0x0000c154 | 0xc154 | 0xc150 / 0x14 | lane |
| DSC_DIAG_CTRL0 | 0x0000c20d | 0xc20d | 0xc200 / 0x1d | lane |
| UC_CTRL | 0x0000c20e | 0xc20e | 0xc200 / 0x1e | lane |
| DSC_MISC_CTRL0 | 0x0000c21e | 0xc21e | 0xc210 / 0x1e | lane |

### 13.2 Read-only status checks
Order = dependency order; stop at the first failing level. All TSC reads via `acc(A, ...)` with lane 2 for xe1 (lane 0 for xe0). Latched (LL/LH) bits clear on read: read once, record.
| # | Check | Register (addr16, AER) | Bit | Healthy / meaning |
|---|---|---|---|---|
| 1 | PLL lock | `ANAPLL_STATUS` 0x9010, 0 | PLLSEQPASS b11 (also PLLSEQDONE b15, FREQPASS b10, VCOPASS b9, LOSTPLLLOCK_SM b6) | b11 = 1 and b6 = 0: core PLL up. b11 = 0: core dead, go to 13.3(c). C |
| 2 | uC alive | `CRC` 0xf015, 0 | all 16 | != 0 (OM `firmware_check` polls it nonzero after start). 0 = ucode never ran or CRC not enabled. C |
| 2 | | `VERSION` 0xf010, 0 | all 16 | nonzero; value is informational. C |
| 2 | | `DOWNLOAD_STATUS` 0xffc5, 0 | INIT_DONE b15, ERR1 b1, ERR0 b0 | b15 = 1, b1:b0 = 0. C |
| 2 | | `COMMAND` 0xffc2, 0 | MDIO_UC_RESET_N b4 | 1 = 8051 out of reset. I (readback of a command reg) |
| 2 | | `UC_CTRL` 0xc20e, lane | READY_FOR_CMD b7, ERROR_FOUND b6 | b7 = 1 idle and b6 = 0. C (field map), I (idle value) |
| 3 | RX signal detect | `RX_X4_PMA_PMD_LIVE_STATUS` 0xc150, lane | AFE_SIGNAL_DETECT b1, SIGNAL_OK b0, EXTERNAL_SIGNAL_DETECT b2 | b1 = 1 = analog front end sees energy. Front 49 (no light): expect b1 = 0, b0 = 0; nothing below can pass. C |
| 3 | | `ANARX_STATUS1` 0xc02b, lane | SIGDET b15 | raw AFE signal detect, cross-check of b1 above. C |
| 3 | override | `RX_X4_RX_SIGDET` 0xc138, lane | OVERRIDE_SIGNAL_OK b2, OVERRIDE_VALUE_SIGNAL_OK b1 | b2 must be 0. OM PHY-disable sets b2 = 1, b1 = 0 (forces SIGNAL_OK low) and PHY-enable clears both; a port parked by a disable can keep it. C |
| 4 | PMD / CDR lock | `RX_X4_PMA_PMD_LIVE_STATUS` 0xc150, lane | PMD_LOCK b3 (RX_ANA_RESET b4, RX_ANA_POWER_DOWN b5 must be 0) | b3 = 1 = CDR locked on the data. b1 = 1 with b3 = 0 = signal present but adaptation not locked: go to 13.3(b). C |
| 4 | | `RX_X4_PMA_PMD_LATCHED_STATUS` 0xc151, lane | PMD_LOCK_LL b6, PMD_LOCK_LH b7 | LL = 1: lock dropped since last read. C |
| 5 | PCS block lock (CL49) | `RX_X4_PCS_LIVE_STATUS` 0xc154, lane | SYNC_STATUS b0, LINK_STATUS b1, HI_BER b2, REMOTE_FAULT b7, LOCAL_FAULT b8 | b0 = 1 = 66b block lock. b1 = 1 = PCS link up (no HI_BER). b8 = 1 = local fault (matches `XLMAC_RX_LSS_STATUS` below). C |
| 5 | | `RX_X4_CL49_LOCK_FSM_STATUS` 0xc145, lane | LSM_CURR_STATE [1:0], LSM_HIS_STATE [4:2] | lock-FSM state; stuck at the initial state = no sync headers seen. State names not in OM. C (fields), I (meaning) |
| 5 | | `RX_X4_PCS_LATCHED_STATUS_0` 0xc153, lane | SYNC_STATUS_LL b6, LINK_STATUS_LL b8, HI_BER_LH b11 | LL set = was down since last read. C |
| 6 | Link as OM sees it | `RX_X4_PCS_LIVE_STATUS` b1 and `RX_X4_PCS_LATCHED_STATUS_0` b8 | | OM `link_get`: up = live LINK_STATUS && !LINK_STATUS_LL. Read latched twice (first read clears). C |
| 7 | TX side | `ANATX_DRIVER` 0xc017 b15 ELEC_IDLE; `ANATX_CONTROL6` 0xc01a b0 DISABLE_TX_R; `TX_X4_MISC` 0xc113 b1 RSTB_TX_LANE, b0 ENABLE_TX_LANE | | expect 0, 0, 1, 1. A dark TX keeps the far end down and the far end then sends fault. C |
| 8 | PCS-level fault from TX | `TX_X4_PCS_STATUS` 0xc121 | TX_LOCAL_FAULT b8, TX_REMOTE_FAULT b7 | informational. C |
- MAC-level (S-channel block 5, port index = lane, lane 2 = `|2`; 8-byte regs: read header `0x2c500400`, ack data words {lo, hi}):
  - `XLMAC_CTRL` addr `0x00060002`: live 0x1041; fields TX_EN b0, RX_EN b1, SOFT_RESET b6, SW_LINK_STATUS b12. C
  - `XLMAC_RX_LSS_STATUS` addr `0x00060b02`: LOCAL_FAULT b0, REMOTE_FAULT b1, LINK_INTERRUPTION b2. C
  - `XLMAC_MODE` addr `0x00060102`: expect `0x40` (SPEED_MODE [6:4] = 4 = 10G, HDR_MODE [2:0] = 0 IEEE). C (RDB reset value), I (that stock leaves it)
  - `XLMAC_TX_CTRL` addr `0x00060402`: DISCARD b2 (lo word) should be 0 (OM sets DISCARD / EP_DISCARD while link is down and clears on link up). C
  - `XLPORT_LINKSTATUS_DOWN` addr `0x02022900` (block 5, 4-byte, header `0x2c500200`): PORT0..3 latched link-down bits. C (RDB), I (use)
  - `XLPORT_MODE_REG` live 0 = `XPORT_MODE_QUAD` = four 1-lane ports, so TSC `SETUP.PORT_MODE_SEL` [6:4] = 0 and 1-lane SFI; resolves the section 9 step 1 question. C (OM `bcm56150_a0_internal.h:20`, `_bmd_init.c:368-378`)
- Interpretation of the live state: LOCAL_FAULT on lane 2 with PCS RX unlocked = step 5 (or earlier) fails. The MAC soft reset hides nothing: the LSS fault is a PCS-side status. I

### 13.3 Recovery, minimal steps in order
Do 13.2 first; the failing level selects the step. xe0 (front 49) has no light: do not expect any step to lock it (13.2 check 3).

**(a) XLMAC out of soft reset, RX on (lane 2).** C for the stock sequence, I that it clears the symptom
- What stock does on link-up: `mac_xl_enable_set(1)` (`FUN_012abf7c`): read `XLMAC_CTRL`, set TX_EN=1, RX_EN=1; if the value changed: `FUN_01234964` (XLPORT_TXFIFO_CTRL MAC_CLR_COUNT/CORE_CLR_COUNT pulse), SOFT_RESET=0, write `XLMAC_CTRL`; then a port-bitmap update and `FUN_01235228(unit, port, 1)` (not decoded, I). Disable is the mirror: RX_EN=0, SOFT_RESET=1 = the live 0x1041. C
1. TXFIFO pulse (stock order, I that it is needed for a MAC that was only parked): RMW `XLPORT_TXFIFO_CTRL` addr `0x00020302` (offset 0x20300 | lane; 4-byte, header read `0x2c500200`, write `0x34500200`): OR in `0x3` (b1 MAC_CLR_COUNT, b0 CORE_CLR_COUNT), write; clear both bits (keep DELAY_PKT [3:2]), write.
2. `XLMAC_CTRL` 64-bit RMW (read first; live lo = 0x1041): new lo = `(lo & ~0x40) | 0x3` = **0x1003**, hi = 0. S-channel on unit 1:
   - read: `0x0a03100c <- 0x2c500400`, `0x0a031010 <- 0x00060002`, `0x0a031000 <- 1`, poll bit1 (fail on b20/21/22), ack hdr at `0x0a03100c` (I: 0x30E00400), lo at `0x0a031010`, hi at `0x0a031014`, `0x0a031000 <- 0`.
   - write: `0x0a03100c <- 0x34500400` (`0x0d<<26 | 5<<20 | 8<<7`), `0x0a031010 <- 0x00060002`, `0x0a031014 <- 0x00001003`, `0x0a031018 <- 0x00000000`, `0x0a031000 <- 1`, poll, check, `0x0a031000 <- 0`.
   - OM init writes the same bits with SW_LINK_STATUS = 1 at init, without waiting for link (`bcm56150_a0_xlport_init`). C
3. Clear latched LSS faults: `XLMAC_CLEAR_RX_LSS_STATUS` addr `0x00060c02`, 8-byte write lo `0x7` then lo `0x0`. I (pulse semantics not seen in OM). Re-read `XLMAC_RX_LSS_STATUS`: 0 only if the PCS is up (check 5).
4. If `XLMAC_TX_CTRL` DISCARD (b2) / EP_DISCARD (hi b5) are set, clear them (RMW, addr `0x00060402`). C

**(b) PCS still unlocked, firmware running: per-lane RX restart, no download.** I (sequence assembled from OM `_tsc_serdes_stop`/`_tsc_uc_rx_lane_control`; not run)
0. If `RX_X4_RX_SIGDET` b2 = 1: RMW clear b2 and b1.
1. uC lane stop: proxy write of uC RAM word `0x06d0` (lane 2; lane 0 = `0x04d0`) = `0x0d00`. Proxy write (`phy_tsc_iblk_proxy_write`, all with `acc`, AER 0 unless stated): `MDIO_UC_MAILBOX 0xffc8 <- 0`; `RAMWORD 0xffc0 <- 0x06d0`; `ADDRESS 0xffc1 <- lane` (2); `WRDATA 0xffc3 <- 0x0d00`; `MDIO_UC_MAILBOX 0xffc8 <- 0x0019` (REQ b0 | WR b4 | RAM b3); poll `0xffc8` b15 DONE; `0xffc8 <- 0`. C (OM)
   - Poll bound: OM `TSC_UC_PROXY_WAIT_TIME` = 600000 reads, no time base. First-run bound 10 ms (uC services a mailbox request within its main loop; not measured), log elapsed, tighten to 1.25x of the measured worst case. I
2. `RX_X4_PMA_CONTROL_0` (0xc137, AER lane) b0 RSTB_LANE: RMW to 0. C (OM stop path)
3. `DSC_MISC_CTRL0` (0xc21e, AER lane) b15 RXSEQSTART: RMW to 1 (OM stop and start paths both write 1 when firmware runs). C
4. uC lane start: same proxy write with data `0x0e00` to `0x06d0` (OM `_tsc_uc_rx_lane_control(1)`, used for forced, non-AN SFI). C
   - AN or loopback path in OM instead keeps the uC off (`0x0d00`) and sets `RSTB_LANE` = 1 by host. Use that variant only if the port runs CL73 AN. C
5. Poll `RX_X4_PMA_PMD_LIVE_STATUS` b3 PMD_LOCK, then `RX_X4_PCS_LIVE_STATUS` b1; if `RSTB_LANE` still reads 0 after the first poll interval, set it to 1 by host. I
   - Bound: 1 s total as a first-run backstop (no OM value for RX adaptation; PLL lock bound in OM is 200 ms). Log elapsed time of the first lock, then tighten to 1.25x of it.
- Alternative uC command (OM `_tsc_uc_cmd_seq`, only for revid < A2 there): per lane read `UC_CTRL` until READY_FOR_CMD b7; clear b7, b6, [5:4] (write); then write `UC_CTRL` = `cmd<<8 | 0x1` (cmd 0 graceful stop = `0x0001`, cmd 2 resume = `0x0201`). Skipped if `DSC_DIAG_CTRL0` (0xc20d) b0 = 1. `SERDESID` b13:11 = REV_NUMBER tells which applies. C (code), I (applicability)

**(c) Full TSC0 re-init.** Needed when check 1 fails (PLLSEQPASS = 0), check 2 shows no uC (CRC = 0 / INIT_DONE = 0), or (b) does not lock with signal present.
- Impact: TSC0 is one core: re-init drops xe0 and xe1 (front 49 and 50). TSC1 (HiGig, phys 30/32) unaffected. C
- Order, all C (OM), registers as in section 9 (addr16 in 13.1, AER 0 for core regs, all-lane writes where section 9 says "all lanes"):
  1. Stage 0 (section 9 steps 1-8): `SETUP` 0x9000 PORT_MODE_SEL = 0 (QUAD, 1-lane), REFCLK_SEL [15:13] = 3, FORCE_PLL_MODE_AFE_SEL b12 = 1, DEFAULT_PLL_MODE_AFE [11:8] = 0xa; `RESET` 0x9001, `POWER` 0x9002, timers, TX FIR defaults.
  2. Microcode download (section 9 step 9): `COMMAND` 0xffc2 INIT_CMD b15 -> poll `DOWNLOAD_STATUS` 0xffc5 b15 (1 ms steps, 200 ms limit in OM) -> `COMMAND2` 0xffca <- 0xf -> `RAMWORD` 0xffc0 <- len-1 -> `ADDRESS` 0xffc1 <- 0 -> `COMMAND` <- 0x9 (WRITE b3 | RUN b0) -> all LE words to `WRDATA` 0xffc3 -> `COMMAND` <- 0x2 (STOP) -> check ERR0/ERR1 = 0 -> `CRC` 0xf015 <- 0 -> `COMMAND` <- 0x10 (MDIO_UC_RESET_N). Image: OM `bcmi_tsc_xgxs_ucode.c`, 32728 bytes.
  3. `SETUP` START_SEQUENCER b7 = 1, poll `ANAPLL_STATUS` b11 (OM limit 200 ms); stage 1: `CRC` != 0 within 100 ms.
  4. Stage 2 (section 9): release `RESET` RX/TX_RESET_ANALOG, uC lane control on (`0x0e00` per lane as in (b) step 4), then (a) and the port link logic.
- If stock's warm state is intact (check 1 and 2 pass), (c) is not needed and a download over a running uC would wipe the stock state; prefer (b).

### 13.4 Does stock's link-scan do (a) by itself?
- **Yes, but only after the PHY reports link up; it will not unpark a MAC whose PCS has no lock.** C for the call chain, I for the exact PHY register
  - Thread `_bcm_esw_linkscan_thread` (`FUN_00af71a8`) -> `_bcm_esw_linkscan_update_port` (`FUN_00af4804`, per port): reads link with `FUN_00b28b88` (`_bcm_port_link_get`, SW mode) -> `FUN_0125fa88` -> PHY driver slot +0xc (`link_get`). Hardware-linkscan mode goes through `FUN_00af45a0` with `hw=1`; which mode unit 1 uses depends on `bcm_linkscan_pbmp` (soc property, not read). C / I
  - Link change -> `bcm_port_update` (`FUN_00b29258`) -> `FUN_00b1e768(link)`: link down: MAC slot +8 `enable_set(0)` then `soc_phyctrl_linkdn_evt`; link up: speed/duplex/pause MAC slots, then MAC slot +8 `enable_set(1)` = `mac_xl_enable_set` above. C
- Register it polls on unit 1: the TSC PHY driver's `link_get`, i.e. MIIM reads of `RX_X4_PCS_LIVE_STATUS` (0xc154) LINK_STATUS b1 and the latched `RX_X4_PCS_LATCHED_STATUS_0` (0xc153) LINK_STATUS_LL b8, as in OM. C (OM), I (stock identical; stock TSC driver not decompiled)
- What it writes on link-up: `XLPORT_TXFIFO_CTRL` pulse, then `XLMAC_CTRL` = TX_EN 1, RX_EN 1, SOFT_RESET 0 (0x1003 here), later `EPC_LINK_BMAP` and port forwarding state. C (MAC part), I (rest)
- Consequence: the live 0x1041 is what `enable_set(0)` leaves; it is consistent with "stock saw link down" and does not indicate a stock bug. A fix has to make the PCS lock (b) first, or do (a) by hand.

### 13.5 Verified on HW: bring a stock-disabled xe port up (2026-11-12, unit 1 xe1 = front 50, 10G DAC)
Warm boot from stock, uC alive (13.2 core checks pass). TSC MIIM A = 2, AER = 2 (lane) for every `0xc...` write; MAC via unit-1 S-channel (block 5, lane in low byte). C (all steps, live)
| # | Write | Before → after | Effect |
|---|---|---|---|
| 1 | `RX_X4_RX_SIGDET` 0xc138 ← 0x0009 (clear b2/b1) | 0x000D | PMD live 0x6 → 0xF (SIGNAL_OK + PMD_LOCK) |
| 2 | `TX_X4_MISC` 0xc113 ← 0xC1CB (ENABLE_TX_LANE + RSTB_TX_LANE) | 0xC1C8 | |
| 3 | `RX_X4_PMA_CONTROL_0` 0xc137 ← 0x0001 (RSTB_LANE) | 0x0000 | PCS live 0x0003 (SYNC + LINK) |
| 4 | `XLPORT_TXFIFO_CTRL` lane 2 ← 0x7, then 0x4; `XLMAC_CTRL` lane 2 ← 0x1003; `XLMAC_CLEAR_RX_LSS_STATUS` ← 7, 0 | XLMAC_CTRL 0x1041 | MAC on, LSS 0. Far end still sent RF: no RX counted |
| 5 | `ANATX_DRIVER` 0xc017 ← 0x2020 (clear b15) | 0xA020 | 0x2020 = stock `serdes_driver_current_xe` |
| 6 | 0xc01a ← 0x0000 (clear b0) | 0x0001 | |
| 7 | `TX_X4_CREDIT1` 0xc101 ← 0x0084; `TX_X4_CREDIT0` 0xc100 ← 0x4021 (CREDITENABLE b14) | 0x0000 / 0x0021 | PCS live 0x0083 (RF b7 from far end) |
| 8 | `TX_X4_MISC` 0xc113 ← 0xC1C8 then 0xC1CB (TX lane reset pulse after credits) | | **PCS live 0x0003, RF gone; RPKT/RBCA/RMCA/RBYT rising, RFCS 0** |
- Stock's port disable leaves all of: RX_SIGDET force, RX lane reset, TX lane disabled + in reset, ANATX b15, 0xc01a b0, TX credits off (CREDIT0.CREDITENABLE 0, CREDIT1 0). A driver's enable must undo each; order matters: credits before the TX lane reset release.
- `XLPORT_FAULT_LINK_STATUS` lane 2 stayed 0x1 after RF cleared: treat as latched, judge by PCS live b7 / `XLMAC_RX_LSS_STATUS`.
- XLPORT MIB is lane-indexed by the low address byte (RPKT lane 0 = 0, lane 2 live).

## 14. Unit-1 PCIe DMA addressing (issues #4, #47; offline analysis 2026-11-12)

Symptom was: RX ch1 armed, `CURR_DESC` advanced, nothing written. Resolved in 14.4 (VLAN/STG membership, not PCIe).
Sources: OM `cdk/.../bcm56150_a0_cmic.h:9743-10025` (RDB descriptions), OM `libbde/shared/shbde_iproc.c` (added to the vendor store, `/vendors/broadcom/openmdk/README.md`), OpenBCM `linux-kernel-bde.c:2571-2603`, stock `switchdrvr` `FUN_0120c598`, stock kernel `soc_pcie_init` @`0xc033ea84`, mainline `pcie-iproc.c`.

### 14.1 What `CMC0_HOSTMEM_ADDR_REMAP_*` does
- Register description (RDB, all CMCs): "Memory mapping using [31:28] of programmed host mem address". 16 entries, 6 bits each; entry index = `HOSTMEM_ADDR[31:28]`. C
  | Reg | Off | Fields (b[5:0], b[11:6], b[17:12], b[23:18], b[29:24]) = index |
  |---|---|---|
  | `_0` | `+0x31474` | ADDR_0..4 = 0..4 |
  | `_1` | `+0x31478` | ADDR_0..4 = 5..9 |
  | `_2` | `+0x3147c` | ADDR_0..4 = 10..14 |
  | `_3` | `+0x314ac` | ADDR_0 = 15 |
- Output is 6 bits replacing the 4-bit index: bus address `A[31:0]` leaves the CMC as a 34-bit AXI address `{entry[5:0], A[27:0]}`. I (width inferred from 4-bit index -> 6-bit entry; the RDB text gives only the index)
- Stock tables decode as identity plus a high bit: case A entry[i] = `0x10 + i` (bit 4 set = AXI bit 32), case B entry[i] = `0x20 + i` (bit 5 = AXI bit 33). Result: `AXI = 0x1_0000_0000 + A` (A) or `0x2_0000_0000 + A` (B). C (decoded from the 8 values in section 8), I (reading as "AXI bit 32/33")
- It is the host-address output stage of the CMC, so it applies to every host-memory access of that CMC: DCB fetch, DCB status write-back, packet write/read, FIFO/SBUSDMA. The RDB says nothing per access type. C (single stage per CMC in RDB), I (no per-type exemption)
- Case selector = which PAXB core the **endpoint** is wired to: `shbde_iproc_paxb_init` reads `PAXB_IMAP0_2` (BAR0+0x2c08) b12; set = PAXB_1 -> `dma_hi_bits` 2, flag `BDE_DEV_BUS_ALT`; clear = PAXB_0 -> `dma_hi_bits` 1. Stock `FUN_0120c598` picks case B iff `dev flag & 0x4000` and then also read-modify-writes `CMC0_PCIE_MISCEL` (`+0x31470`, regdb: ENABLE_MSI b4, MSI_ADDR_SEL b8, MSI_PACING_DELAY b3:0). C (code), I (flag 0x4000 = `BDE_DEV_BUS_ALT`; b8 MSI_ADDR_SEL is the field changed)
- Live: case A + `MISCEL` = 0x8 (pacing 8, MSI_ADDR_SEL 0) = PAXB_0, `dma_hi_bits` = 1. Consistent. C (live), I (reading)
- `dma_hi_bits` 1 = AXI bit 32, 2 = bit 33 is the same bit that case A/B sets: the remap's output is exactly what the endpoint's outbound window `OARR_2` is programmed to claim (14.2). C (values both sides), I (bit meaning)

### 14.2 Endpoint PCIe side (BCM53346 PAXB, reached through its own BAR0)
- BAR0 holds the endpoint's PAXB registers at `BAR0 + 0x2000 + PAXB_off` (mainline RC `CFG_IND_ADDR` 0x120 = BDE `0x2120`). The module only maps BAR2 (CMIC); it never touches BAR0. C (BDE offsets, module source), I (offset base rule)
- Outbound DMA path = CMC host address -> remap -> AXI `0x1_xxxx_xxxx` -> endpoint PAXB `OARR_2` window -> PCIe memory request with address `A[31:0]` (OMAP_2 never written by BDE, so base 0). C (BDE writes only OARR_2/UPPER), I (pass-through address)
- Stock/BDE `shbde_iproc_paxb_init` (run after enumeration on every BDE load) writes, offsets from BAR0 (C):
  | Reg | BAR0 off | Value |
  |---|---|---|
  | `PAXB_PCIE_EP_AXI_CONFIG` | `0x2104` | 0x0 ("enable iProc DMA to external host memory") |
  | `PAXB_OARR_2` | `0x2d60` | 0x1 (enable, lowest base/size cfg) |
  | `PAXB_OARR_2_UPPER` | `0x2d64` | `dma_hi_bits` (1 on PAXB_0) |
  | `PAXB_ENDIANESS` | `0x2030` | 0x01010101, read back; != 1 -> write 0 (LE) |
  | `PAXB_OARR_FUNC0_MSI_PAGE` | `0x2d34` | `|= 1` only if MSI |
- **Our module writes none of these.** Linux asserts then releases PERST# on every RC probe (`iproc_pcie_perst_ctrl(true)` / `(false)` before the link check, `pcie-iproc.c:1474`; stock `soc_pcie_init` does the same: CLK_CTRL `= 3` then `= 1`). PERST# resets the endpoint's PCIe core registers but not the CMIC (remap and DMA regs survived the warm boot). Stock re-arms `OARR_2` afterwards, from the BDE. Live under Linux `OARR_2` still reads 1 (14.4): PERST# does not clear it here. C
- Window size: `OARR_2` lower 0x1 = enable with size cfg 0; size not decoded. Stock DCBs sat at `~0x075f_xxxx`, so >= 128 MiB. Keep bus addresses < `0x08000000`. I

### 14.3 Host side (BCM53344 RC PAXB, `0x18012000`) and what mainline does
- PCIe address `A` arriving at the RC must be mapped inbound to AXI. Stock `soc_pcie_init` programs, on the RC PAXB base (C, decompiled): `+0xd08 = 1` (IARR1 lower, enable, PCIe base 0), `+0xc80 = 0x80`, plus the outbound `OARR0/1` (`+0xd20/0xd28`) and `OMAP0/1` (`+0xd40/0xd48`) windows and `+0x330 = 0xf` (INTX). I (meaning of `+0xd08` / `+0xc80`: IARR1 and an inbound map register by analogy with the v2 table; not decoded)
- Mainline `pcie-iproc` for `brcm,iproc-pcie` (type `IPROC_PCIE_PAXB`) has **no inbound programming**: `ib_map`/`nr_regions` are set only for `IPROC_PCIE_PAXB_V2` (`pcie-iproc.c:1399-1409`), and `IPROC_PCIE_PAXB` register table has no IARR/IMAP (`:319-332`). C
- `need_ib_cfg` is true only if the DT node has `dma-ranges` (`pcie-iproc-platform.c:177`). With it on a v1 node, `iproc_pcie_setup_ib` finds `nr_regions == 0` -> `-EINVAL` -> "unable to configure inbound mapping" -> probe fails (`:1487-1491`). **A DT `dma-ranges` entry is not a fix; it breaks the RC probe.** C
- Without `dma-ranges` the RC IARR/IMAP registers are left as found (`iproc_pcie_invalidate_mapping` skips them when `need_ib_cfg` is false). So inbound DMA works only if stock U-Boot left `+0xd08`/`+0xc80` set. Whether it does is unread. I
- RAM layout: DT `memory@0` = `0x0..0x08000000` + `0x68000000..0x70000000`. Stock ran `mem=128M@0x0` (second-stage doc) = only the low alias, consistent with one inbound region at PCIe base 0 of ~128 MiB. A buffer in the high bank (bus `0x68000000`, remap index 6) would miss it. RC outbound window is bus `0x08000000-0x0fffffff` (hr2 dtsi `ranges`): host RAM below `0x08000000` does not collide. C (DT, ranges), I (window size, stock intent)
- A 32-bit `dma_set_mask_and_coherent` does not keep allocations low: with two banks, `dma_alloc_coherent` may return `0x68xxxxxx`. I

### 14.4 Result (live 2026-11-12, commit after 03e51a9)
- Root cause: no frame was ever switched to the CPU. Unit-1 VLAN 1 held only ports 28/29: CPU (0) and xe1 (27, front 50) missing from `VLAN_TAB`/`EGR_VLAN`, and both `disabled` in STG 1. The PCIe DMA path was fine. C
- PCIe state as Linux finds it (read live, no writes needed): endpoint BAR0 `0x2030`=0, `0x2104`=0, `0x2c08`=0x18012001 (b12 clear = PAXB_0, case A), `OARR_2`/`_UPPER` = 1/1; RC `+0xd08`=1, `+0xd0c`=0, `+0xc80`=0x80; remap = case A. C
- `CURR_DESC` advancing + CMIC RX counter is not proof of a delivered frame; a 0xA5 pre-fill of DCB + buffer is the check. C
- Table writes (S-channel mem ops, unit 1, entry 1):
  | Table | Blk | Addr | Before | After |
  |---|---|---|---|---|
  | `VLAN_TAB` | IPIPE 10 | `0x14000001` | w0 `0x30000000` | w0 `0x38000001` (+CPU, +27) |
  | `STG_TAB` | IPIPE 10 | `0x14040001` | `0x0, 0x0F000000` | `0x3, 0x0FC00000` (CPU + 27 forwarding) |
  | `EGR_VLAN_STG` | EPIPE 11 | `0x10100001` | same as STG_TAB | same as STG_TAB |
  | `EGR_VLAN` | EPIPE 11 | `0x100c0001` | `0x30000000, 0x1C000000` | `0x70000000, 0x1E000000` (PORT_BITMAP +0, +27) |
  - Each adds an even bit count, so `EVEN_PARITY` stays valid. `PORT_TAB[27]` PORT_VID = 1, EN_IFILTER 0. C
- First frame: 68 B ARP broadcast, `w12` src port 27, DONE/start/end set, err 0, VLAN 1 tag kept (`81 00 00 01`), within 100 ms of arming. C
- Abort the channel before any `rmmod` (section 5 abort) so the engine never writes into freed RAM.
