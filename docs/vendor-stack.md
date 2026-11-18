# Stock vendor stack: what we hold, RE status, traces

Inventory 2026-11-18 (stock 7.5.15+17146, kernel 3.6.5, uClibc 1.0.41, ARM soft-float). Open trace/RE work: #132.

## Where things are
- Only local stock rootfs: `tmp/initramfs-full/` (listing `tmp/re/initramfs.list`, 502 entries). `/vendors/ubiquiti`, `/vendors/broadcom` moved to the NAS (`NAS-MOVED.tsv`); `private/firmware` symlink dangles.
- Ghidra: exports `tmp/ghidra/out/<name>/` (`.c`, `.funcs.tsv`, `.calls.tsv`, `.strings.tsv`), projects `tmp/ghidra/proj/<name>`; regenerate with `scripts/ghidra_export.py`. Exported: switchdrvr, eswh_switchdrvr, libubnt, ubnthal, gpiodev, ubnt_common, vmlinux, ubntapp, uboot, uboot1.
- SDK source: OpenBCM 6.5.16 + OpenMDK on the NAS; local pieces `tmp/sdk/`, `tmp/sdk-l2/`, `private/reference/openbcm-*.c`.
- switchdrvr function addresses: cited in docs only (not in hwdb). hwdb/fw holds ubntapp + uboot1 tables.

## Components
| Component | Path (in `tmp/initramfs-full/`) | Role | Docs | RE |
|---|---|---|---|---|
| switchdrvr (FastPath + Broadcom SDK, static, 36.3 MB) | `bin/switchdrvr` | init of both chips, linkscan, VLAN/FDB/STP, PoE host, fan, LED, sensors | driver-internals.md, board.md, poe.md, fan.md, stock-debug-cli.md, init-phases-u0/u1.md | deep: init, board, PoE, fan, linkscan; none: L2 runtime (mod FIFO, learn/age handlers), STP runtime, L3 |
| linux-kernel-bde.ko | `lib/modules/3.6.5/` | CMIC map (u0 AXI 0x48000000, u1 PCIe BAR2), IRQ 194/218, DMA pool | stock-debug-cli.md (params), stock-bootlog-facts.md (MPS) | strings + OpenBCM compare; no Ghidra |
| linux-user-bde.ko | same | LUBDE ioctls for switchdrvr; Ubiquiti-only 0x4c1d-0x4c1f (anti-clone devID / QSPI JEDEC / SFDP) | `tmp/issue66_feas.md` (6 ioctls `_IOR` vs upstream `_IO`) | partial (ABI); no Ghidra |
| ubnthal.ko | same | EEPROM parse, board table (28 entries), /proc/ubnthal | board.md | deep |
| gpiodev.ko | same | reset button GPIO 15, sys LED GPIO 10/11 | kernel-pse.md, board.md | deep |
| ubnt_common.ko | same | vendor helpers | kernel-pse.md | light |
| kernel 3.6.5 (PSE `pse_plt`) | `tmp/ghidra/in/vmlinux-us48-7.5.15.elf` | PoE glue, board code | kernel-pse.md | partial/deep (PSE, GPIO) |
| libubnt.so (+ librpcclt, libopenclt, libproc_libs) | `lib/` | RPC client to switchdrvr (usmDb*), PoE, fan | fan.md, poe.md, hardware.md | deep for fan/PoE |
| ubntbox multicall (swctrl, poed, ubntconf, cfgmtd, factorytest, hwcheck, ...) | `bin/ubntbox` | CLI tools, PoE daemon | poe.md, fan.md, stock-debug-cli.md | deep: poed, swctrl fan; factorytest/hwcheck/egtool unread; not in Ghidra targets |
| procmgr, syncdb, mcad, utermd, ... | `bin/` | process manager, controller agent | stock-debug-cli.md | strings |
| rc scripts, `usr/etc/syswrapper.sh` | `usr/etc/` | module order, switch start script | kernel-pse.md, stock-bootlog-facts.md | read |
| snake .soc (`usr/etc/switch/snake.es48.{0,1}.soc`) | | burn-in HiGig trunk / modport / egress_mask | board.md, edgeswitch.md | read; only stock HiGig config files |
| SOC properties | compiled into switchdrvr (board code 0x5fb5e0) | no config.bcm in the image | board.md, edgeswitch.md, hwdb/board/ports.toml | deep for known keys; `bcm_linkscan_pbmp` unread |
| ESWH 1.12.2 switchdrvr (symbols) | `tmp/eswh/` | names UniFi functions | edgeswitch.md | deep cross-check |

Not in the image: config.bcm, libbcm*.so, gdbserver, devshell_symbols.gz, published GPL source for this build.

## Stock captures
All large traces = `pm app-restart switchdrvr` on running stock, tracer attached, stop at `switchdrvr_ready`. **No capture covers runtime events on a settled switch** (learn/age, link change, VLAN/STP change, PoE, LED). Phase C (49-125 s) has init-time link-up reactions only.

| File (`tmp/`) | Tool | Covers | Units |
|---|---|---|---|
| `bde_stock_init.log` (2026-11-15, 241,707 lines) | `tools/bde_trace.c` (BDE vtable + S-channel) | restart to ready, 125.6 s | u0+u1 |
| `schan_stock_init.log` (2026-11-12, 252,781 lines) | `tools/schan_trace.c` | restart to ready, 139.9 s | u0+u1 |
| `schan_stock_writes{,_u1}.txt` | `scripts/schan_summarize.py` | named writes of the above | u0 / u1 |
| `mmio_stock_init.log` (2026-11-15, 10,111 lines) | `tools/mmio_trace.c` | direct CMIC MMIO; dies STEPERR at 39.8 s | u0 |
| `mmio_u1_stock_init.log` | mmio_trace `-p 0x0a000000` | empty: STEPERR after 8 s, switch hung | u1 |
| `higig/stock.txt` (2026-11-18) | `scripts/higig_dump.py stock` | static snapshot: stacking, TSC1, XLPORT/XLMAC, PORT_TAB, trunk | u0+u1 |
| `tsc_dump/` | `scripts/tsc_lane_dump.py` | static xe49/xe50 lane regs, stock vs ours | u1 |
| `hwprobe/{cold,warm}.json` | `scripts/hwdb_probe.py` | register probes | u0+u1 |
| `logs/console/us48-2026*.log` (28) | console | stock boot text | both |
| `private/live/` (2026-10) | strace, shell dumps | fan RPC, status dumps | n/a |

Blind spots of every capture: MIIM (CMC0 page 0x31000), DMA/DCB payloads (L2 mod FIFO, table DMA), CMIC SMBus (PoE), poed.

## Tracing tools
| Tool | Limits | Run |
|---|---|---|
| `tools/bde_trace.c` | vtable accesses only; addresses fixed to switchdrvr sha d76ef864; `bde_trace_run.py` has no attach mode | `scripts/build_bde_trace.py`, `scripts/bde_trace_run.py` |
| `tools/schan_trace.c` | S-channel request words only; has `-p PID` (attach) | `scripts/build_schan_trace.py` |
| `tools/mmio_trace.c` | ARM mode only; STEPERR `pc=TRAMP+4` kills it, needs a power cycle | `scripts/build_bde_trace.py --mmio`, `bde_trace_run.py --tool mmio_trace` |
| `tools/tsc_read.c` | read-only MIIM + S-channel | used by higig_dump.py, tsc_lane_dump.py |
| stock helpers | FastPath CLI unprivileged; bcmsh/devshell need the admin flag (session+300) | `stock_ramboot.py`, `stock_wait_ready.py`, `stock_cli.py` |

Stock kernel 3.6.5: no debugfs/kprobes/uprobes/ftrace; ptrace is the only route.

## Coverage by driver area
| Area | Stock covered by | Missing |
|---|---|---|
| L2 learn/age reporting | config values (L2_AGE_TIMER, PER_PORT_AGE_CONTROL, SVL); SDK `tmp/sdk-l2/` | how stock learns/reports (mod FIFO vs scan): runtime trace |
| STP/STG | SDK mapping; init STG 1 writes | runtime STG / L2_USER_ENTRY writes |
| VLAN-aware bridges | init VLAN/PORT_TAB layout | runtime VLAN add/remove |
| HiGig cross-unit | static stacking state | MODPORT_MAP writes, cross-stack flooding |
| L3/NAT offload | none (stock leaves L3 untouched) | read-only L3 table probe (#13) |
| PoE | deep static RE | runtime SMBus (mmio_trace fix or poed strace) |
| Fan | complete | — |
| LEDs | LEDUP loader location 0x120ed64, MSPI frames | LEDUP program bytes (read-only dump) |
| SFP/DAC #105, 10G-T #110 | SDK TSC source, static lane dumps | stock MIIM/TSC writes (mmio_trace fix), hot-plug on stock |
| Linkscan | algorithm (driver-internals.md 13.4) | poll cadence, linkscan pbmp |
