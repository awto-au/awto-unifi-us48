# hwdb: single source of truth for hardware facts (#72)

- Facts live in `hwdb/*.toml`; code, headers, Ghidra scripts and doc tables are generated from them.
- Loader: `scripts/hwdb.py` (stdlib `tomllib`). The SDK regdb (`regdb/hr2_*.tsv`) is merged in as a bulk source, never converted.
- Step 1 of #72 only: schema, loader, generators, lint, first content. Script/C migration follows in small commits.

## Layout
- `hwdb/soc/*.toml`: spaces, regions, blocks, registers (memmap, clocks, periph, a9, paxb, cmic).
- `hwdb/board/ports.toml`: front-panel port map (52 front + internal HiGig 28/29 on both chips).
- `hwdb/fw/<binary>.toml`: vendor-binary functions (`uboot1`). Restricted TOML subset, see Ghidra round-trip.
- `hwdb/gen/`: generated, committed: `us48_hwdb.h`, `hwdb.rs`, `ghidra_import_hwdb.py`, `ghidra_export_hwdb.py`.
- `docs/hwdb-map.md`: generated tables. `docs/port-map.md`: port table between markers is generated.

## Schema
- Provenance on every fact: `src` (doc section / datasheet page / disassembly PC / SDK file:line / live read + date) and `conf` = `"C"` (confirmed) or `"I"` (inferred). Docs disagree: `conf = "I"` plus `note`.
- Regs inherit `src`/`conf` from their block unless set. Superseded facts are deleted, not annotated.
- `[space.ID]`: `desc`. Spaces: `axi0` (unit 0 AXI/physical as seen by a1), `axi1` (unit 1 AXI as seen by a2), `bar0u1` (unit 1 BAR0 offsets).
- `[region.NAME]`: `space, base, size?, kind (ram|rom|alias|flash|device|pcie|l2lock|window|hazard), unit?, desc, src, conf, note?`.
- `[block.NAME]`: `space, base, size?, unit?, desc, src, conf, like?, note?`. `like = "other"` copies its registers (second instance).
- `[block.NAME.reg.REG]`: `offset` (from block base), `width=32, access=rw, reset?, count=1, stride=width/8, desc, src?, conf?, note?, sdk?`.
  - `fields = ["NAME hi:lo desc", "NAME bit desc"]`, or `[block.N.reg.R.field.F]` with `bits`, `desc`, `enum = {0 = "x"}`.
  - `sdk = "SDK_REG_NAME"`: `check` fails if hwdb address differs from the regdb address (cross-check against the bulk source).
- `[binary.NAME]` (`desc, base, arch`), `[binary.NAME.func.FN]`: `name, addr, size?, role, regs = ["block.REG" | ...], src, conf`; `[[binary.NAME.comment]]`: `addr, kind (eol|pre), text, src, conf`.
- `[[port]]`: `front` (0 = internal), `id, label, unit, logical, phys, phy (gphy|octal|none|higig), phy_bus?, phy_addr?, lane, sdk?, linux?, src, conf`.
- SDK merge (loader): `sdk_iproc` (absolute AXI offsets, regtype iprocreg), `sdk_cmic` (offsets from `cmic0`), S-channel regs/memories in `db.sdk_sbus_regs` / `db.sdk_sbus_mems`.

## Use
- Python: `sys.path.insert(0, scripts dir); import hwdb; hwdb.addr("ccu.POLICY_FREQ")`, `hwdb.default().field("ccu.PLLARMA.PDIV").mask`, `.port(front)`, `.find(0xaddr)`.
- CLI: `python3 scripts/hwdb.py check|stats|addr NAME|find 0xADDR`.
- C: `#include "hwdb/gen/us48_hwdb.h"`: `HW_<BLOCK>_BASE`, `HW_<BLOCK>_<REG>` (offset), `..._<FIELD>_SHIFT/_MASK`, `HW_<REGION>_BASE/_SIZE`. Rust: `hwdb/gen/hwdb.rs`, same names.

## Add a fact
1. Edit the TOML (reg in an existing block, or a new block); give `src` and `conf`; add `sdk =` when the regdb has the register.
2. `python3 scripts/hwdb.py check` (schema + sdk cross-check).
3. `python3 scripts/hwdb_gen.py` and commit the TOML plus generated files. `--check` fails on stale output.

## Lint
- `python3 scripts/hwdb_lint.py [--by-file] [--all]`: lists hard-coded 0x18/19/1b/1c/48xxxxxx literals that match an hwdb entry. Report only.
- Context collisions exist (0x1c000000 is also the L2X S-channel table id).

## Ghidra round-trip (not yet run inside Ghidra; export merge logic unit-checked with plain Python)
- Import: run `hwdb/gen/ghidra_import_hwdb.py` in a program: memory blocks for axi0 regions/blocks, labels per register, struct `<block>_regs` for dense blocks, function names + role comments from `hwdb/fw` for the binary matching the program name (or arg 1).
- Export: run `hwdb/gen/ghidra_export_hwdb.py [binary] [repo-root]`: writes non-default function names, sizes, function comments and EOL/PRE comments to `hwdb/fw/<binary>.toml`; keeps `role/regs/src/conf` of known addresses; new functions get `src = "ghidra <program> <date>"`, `conf = "I"`. Source: `scripts/ghidra/hwdb_export.py`.
- Ghidra is a consumer and contributor, not the store.

## Port map
- `hwdb/board/ports.toml` is the source; `scripts/us48_portmap.py [switchdrvr]` verifies the FastPath table (VA 0x1d8ecb4) and the OpenBCM p2l rule against it.
- Regenerate the docs table: `python3 scripts/hwdb_gen.py ports`.
