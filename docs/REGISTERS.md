# HR2 register / memory naming DB (issue #30)

Offline DB of every `soc_reg_info` and `soc_mem_info` entry of the stock SDK (hurricane2 driver in `switchdrvr` 7.5.15). Names, offsets, bit positions only. Tags: C = confirmed in the binary, I = inferred.

## Regenerate

- `python3 scripts/hr2_regdb.py [BIN]` → `regdb/hr2_regs.tsv`, `hr2_mems.tsv`, `hr2_blocks.tsv`. Default BIN = `private/firmware/rootfs-selected/bin/switchdrvr` (sha256 `d76ef864…8593c7`). Log `tmp/logs/hr2_regdb.log`.
- `python3 scripts/gen_hr2_regs.py` → `regdb/hr2-regs.h` (`HR2_<REG>_OFFS`, `HR2_<REG>_<FIELD>_MASK` GENMASK, `HR2_MEM_<MEM>_BASE/_INDEX_MAX/_BYTES/_<FIELD>_BP/_LEN`) and `regdb/hr2_regs.py` (`REGS`, `MEMS`, `BLOCKS`, `decode()`, `by_offset()`).
- Lookup: `python3 regdb/hr2_regs.py <NAME|0xOFFSET> [VALUE]` → field decode.
- `regdb/` is generated and committed (#43); regenerate, never hand-edit.
- Only for the 7.5.15 binary: name/field table VAs are constants in `scripts/smbus_regdb.py`; the reg_info array and `soc_driver_t` are located by scan/check, a mismatch aborts.

## Table layout (C)

- `soc_driver_t` (chip string `hurricane2`, type `0x52`) at VA `0x1fb4fe8`: `+0x18` reg_info array, `+0x28` mem_info pointer array, `+0x38` block_info.
- reg_info: pointer array `0x1fb6628`, index = `soc_reg_t`, 0xcf16 slots, 0x40-byte structs. Layout is older than OpenBCM 6.5 `register.h` (no `first_array_index`, no `flags1`):
  - `+0` block-type list ptr (-1 terminated), `+4` regtype, `+8` numels, `+0xc` offset, `+0x10` flags, `+0x14` nFields, `+0x18` fields ptr.
  - `+0x1c/+0x20` reset value lo/hi, `+0x24/+0x28` reset mask lo/hi, `+0x2c` counter idx (-1 if not a counter).
- Field record, 12 bytes: field id (index into field-name table `0x200ad70`), `len | bp << 16`, flags.
- mem_info: pointer array `0x1fadfa8` (7184 slots, index = `soc_mem_t`), 0x40-byte structs from `0x1ea5f54`:
  - `+0` flags, `+4` cmp_fn, `+8` null_entry, `+0xc` index_min, `+0x10` index_max, `+0x14` minblk | maxblk << 16, `+0x18` block mask, `+0x20` base, `+0x24` gran, `+0x28` bytes | nFields << 16, `+0x2c` fields ptr.
  - I: block mask bits index **block_info rows**, not block types (L2X = row 5 = IPIPE; EGR_* = row 1 = EPIPE; MMU_* = row 7).
  - Entry bytes confirmed against known sizes (L2X = 13 bytes, 104 bits).
- mem names: 3 tables of 7185 pointers ending at the first mem struct, all indexed by `soc_mem_t`: `[0]` short alias (sparse, e.g. `PEM`), `[1]` user name (`VLAN`), `[2]` full SDK name (`VLAN_TAB`). `hr2_mems.tsv` uses `[2]` as `name`.

## Counts (stock 7.5.15, 2026-10-14)

- Regs: 6758 of 53014 slots (rest NULL for this chip). 17120 fields. No duplicate names.
- Mems: 197 of 7184 slots. 2628 fields. All have a full name.
- Block rows: 13 (below). Reg block-type list always has exactly one entry.
- Regs by regtype: 3959 iprocreg(14), 1417 cpureg(9), 950 gen(1), 432 port(2). No cos(4) or mcsreg(0xd) regs on this chip.
- Flags (OpenBCM `register.h` bits): RO `0x10` 1276, WO `0x20` 1557, COUNTER `0x2` 224, 64BIT `0x1` 162, ED_CNTR `0x40` 47, ARRAY `0x4` 67; 67 regs have numels > 1. No bits above `0x7f` set.

## Regtypes

| Value | Name | Meaning | Regs |
|---|---|---|---|
| 1 | gen | SBUS register, block-wide | 950 |
| 2 | port | SBUS register, per port in block | 432 |
| 4 | cos | per-COS SBUS register | 0 |
| 9 | cpureg | CMIC BAR offset (PCI memory), no block base | 1417 |
| 0xd | mcsreg | MCS indirect | 0 |
| 0xe | iprocreg | iProc AXI absolute address (e.g. `0x18119a00`) | 3959 |

- Enum is an older `soc_regtype_t` than OpenBCM 6.5 (`cpureg` = 9, `iprocreg` = 14; 6.5 header has 11 and 16). C.
- 16 of the 3959 iprocreg regs sit in block type 6 (CMIC AXI).

## Blocks (`hr2_blocks.tsv`)

| Row | Type | Number | schan | Label (I) | Regs of type | Mems |
|---|---|---|---|---|---|---|
| 0 | 6 | 0 | 0xe | CMIC | 1433 | 0 |
| 1 | 0x12 | 0 | 0xb | EPIPE | 119 | 27 |
| 2..4 | 0xe | 0..2 | 2, 3, 4 | GPORT | 138 | 0 |
| 5 | 0x10 | 0 | 0xa | IPIPE | 364 | 106 |
| 6 | 0x64 | 0 | 0xf | IPROC | 3943 | 0 |
| 7 | 9 | 0 | 0xc | MMU | 149 | 56 |
| 8 | 0x23 | 0 | 0xd | OTPC | 18 | 0 |
| 9 | 0x30 | 0 | 0x13 | SER | 296 | 7 |
| 10 | 0x2f | 0 | 0x10 | TOP | 90 | 0 |
| 11, 12 | 0x26 | 0, 1 | 5, 6 | XLPORT | 208 | 1 each (same mem) |

- schan, cmic columns are equal on every row. C.
- Labels are inferred from reg names inside each block type (`CMIC_*`, `EFP_*`/`EGR_*`, `GE*_EEE_*`, `ING_*`/`L2_*`, `OTPC_*`, `SER_*`, `TOP_*`, `XLMAC_*`). The binary's own `soc_block_name` table uses a different numbering (e.g. `cmic` = 7, `top` = 0x30) and does not match the chip tables; not used.
- No block base addresses exist in the tables: SBUS blocks are reached by schan number, CMIC by BAR offset, iProc by the absolute AXI offset in the reg entry. gen/port reg offsets are the within-block part. Composition of the full SBUS address from schan + offset: not verified.

## Findings: stock side vs OpenBCM

- Local OpenBCM material is partial (`allregs_*.i`, `allmems_*.i`, `bcm56150_a0.c` are listed in the tree but not present). The full by-name join (issue #30 step 3) is not done.
- Used instead: `openbcm-hurricane2.c` (HR2 driver, names as `<NAME>r` / `<NAME>m` tokens, `READ_`/`WRITE_` macro prefixes dropped) against the DB.
  - 332 reg tokens: 199 in DB; most misses are macro wrappers. Real misses: `L3_IIF_PARITY_*`, `MPLS_ENTRY_PARITY_*`, `PORT_CBL_TABLE_PARITY_*`, `SOURCE_VP_PARITY_*`, `VFI_PARITY_*`, `VFI_1_PARITY_*`.
  - 113 mem tokens: 91 in DB. Real misses: `L3_IIF`, `MPLS_ENTRY`, `VFI`, `VFI_1`, `SOURCE_VP`, `PORT_CBL_TABLE`, `L3_TUNNEL`, `EGR_IP_TUNNEL`, `EGR_IP_TUNNEL_IPV6`, `EGR_IP_TUNNEL_MPLS`, `L3_DEFIP_128`, `L3_DEFIP_128_DATA_ONLY`, `ING_PW_TERM_COUNTERS`, `MMU_XQ1`.
  - The names exist in the stock name tables (e.g. `L3_IIF` idx 3460, `VFI` idx 7067, `MPLS_ENTRY` idx 5709) but their mem_info slot is NULL. I: stock build prunes L3-interface / MPLS / VFI / tunnel tables (feature-gated or Ubiquiti-patched SDK).
  - Present and populated: `L2X`, `L3_ENTRY_IPV4_UNICAST`, `FP_TCAM`, `VLAN_TAB` (idx 7101), `PORT_TAB` (idx 6011), `EGR_VLAN`.
- Not checked: ESWH 1.12.2 `switchdrvr` (second binary, FastPath SDK; table VAs differ, constants in `smbus_regdb.py` do not apply).

## Not decoded / open

- Reset values: stock `CMIC_I2CM_SMBUS_CONFIG` reset value in the table is `0x000f0000`, live read is `0x400f0c00` (bits 11:10 and `SMB_EN` set by init; bits 11:10 have no field).
- Mem flags word, mem `+0x1c` (always 0) and `gran` (always 1) not mapped to OpenBCM `SOC_MEM_FLAG_*`.
- Which regs each driver touches (issue #30 plan item): not tabulated yet.
- `scripts/dev.py reg <name|offset>` lookup CLI: not added (`dev.py` is owned by another session); `hr2_regs.py` provides the same as a standalone.
