# PCIe RC bring-up: link to unit 1 from a cold power-up (#76)

- Offline result (static RE + emulator); board-tested 2026-11-15: cold x2, warm, and via the env auto-chain, link up ~10.2 ms after PERST# release (#76).
- Registers: hwdb blocks `paxb0`, `rccfg0`, `idm_pcie_m0/s0` (`hwdb/soc/paxb.toml`); uboot-1 labels in `hwdb/fw/uboot1.toml`.

## Root cause
- `IPROC_PCIE_CLK_CONTROL` (`0x18012000`) bit 0 = `RC_PCIE_RST_OUTPUT`: 0 = PERST# asserted to unit 1. Cold reset value `0x4` (bit 0 clear). C (`docs/stock-bootlog-facts.md:15-19`)
- uboot-1 `soc_pcie_hw_init` (`0x1c0244b0`) never writes it. It only reads it for the "its value" print (`0x1c0245d0-0x1c0245e8`). C (disassembly; `tmp/uboot1_emu.tsv` seq 116850: one R, no W, no `0x18012xxx` write before the link check)
- What it does, in order (`0x1c024510-0x1c0245cc`, emu seq 116835-116848):
  - SerDes MDIO init (`mdio_write(0x1f, 0x8630)` + reads, `0x18032000/4`).
  - IDM `0x18101800` (M0) = 1, 2.5 ms, `0x1810b800` (S0) = 1, 2.5 ms, M0 = 0, 2.5 ms, S0 = 0, then `udelay(100000)`.
  - Print CLK_CONTROL; port struct fill; `udelay(1000)`; `soc_pcie_check_link` (`0x1c024084`).
- "Active" = RC header type 1 and `(LNKSTA & 0x3f0) != 0` (negotiated width; `tst r2,#0x3f0` @`0x1c0241f4`). LNKSTA = RC config `0xbe` via `CONFIG_IND_ADDR` (`0x120`) / `DATA` (`0x124`). Cap pointer walked from `0xe`; cap id `0x10` found at `0xac`.
- Cold: PERST# held low from POR, no training, LNKSTA `0x1001` (width 0) = "not active". Not active path returns before the bridge bus numbers, mem window and the unit 1 BAR assignment.
- After a stock reboot: CLK_CONTROL `0x1` survives the DMU soft reset (stock `soc_pcie_init` left PERST# released), the link retrains during the 100 ms IDM wait, LNKSTA `0x9012` = active, uboot-1 enumerates.
- After a mainline reboot (before `mainline/0024`): `pcie-iproc` shutdown asserted PERST#, CLK_CONTROL `0x0`, same failure as cold. Now the HR2 shutdown leaves PERST# released: uboot-1 prints `port 0 is active`, awto-uboot `link already up (warm)`.

## Stock kernel `soc_pcie_init` (`0xc033ea84`, vmlinux `tmp/ghidra/in/vmlinux-us48-7.5.15.elf`)
- `CLK_CONTROL = 3` (`0xc033eae8`; clears EP_PERST_SOURCE_SELECT, sets RC_PCIE_RST_OUTPUT: PERST# released), `udelay(250)`, `= 1` (`0xc033eb10`), 250 x `udelay(1000)` (`0xc033eb14`).
- No assert phase: from cold `0x4` the first write already releases PERST#.
- Then `OMAP_0 = base`, `OARR_0 = base | 1`, `OMAP_1/OARR_1 = base + 64 MiB (| 1)`, `IMAP1 = 0x80`, `IARR_1_LOWER = 1` (`0xc033eb60-0xc033ebac`), link check, bridge init, `pci_common_init`, `INTX_EN = 0xf`.
- Mainline `pcie-iproc.c:731-755,1473-1474`: assert = clear bits 2:0, `udelay(250)`; release = set bit 0, `msleep(100)`; link check = `LINK_STATUS` PHYLINKUP (bit 3) + DL_ACTIVE (bit 2).
- No LTSSM state register exists in the regdb or any doc; PHYLINKUP is the nearest signal.

## Minimal sequence (awto-uboot `board/ubiquiti/us48/pcie.c`, called from `board_init` before `us48_fan_boot`)
| # | Step | Register / value | Wait (source) | Expiry |
|---|---|---|---|---|
| 0 | Skip PERST if warm | `CLK_CONTROL` bit 0 set and `LINK_STATUS & 0xc == 0xc` | none | n/a |
| 1 | Assert PERST# | `CLK_CONTROL &= ~7` (mainline) | 90 us (CEM Tperst 100 us, -10 %) | none |
| 2 | Release | `CLK_CONTROL \|= 1` (final `0x1` = warm value) | n/a | n/a |
| 3 | Windows | `OMAP_0/OARR_0/OMAP_1/OARR_1/IMAP1/IARR_1_LOWER` as stock, written only if different | none | none |
| 4 | Link up | poll `LINK_STATUS` PHYLINKUP + DL_ACTIVE | expected <= 120 ms (100 ms + 20 ms Detect, PCIe Base r6 6.6.1) x 1.25 = 150 ms, 100 us poll | print `CLK_CTRL / LINK_STATUS / RESET_STATUS / STRAP / LINKSTA`, return -ETIMEDOUT |
| 5 | Config wait | none | 90 ms after release (Base r6 6.6.1 100 ms, -10 %) | none |
| 6 | RC bridge | if `SECONDARY_BUS != 1`: `0x18 = 0x00040100` (pri 0, sec 1, sub 4), `0x20 = 0x10000800`, `0x04 = 0x146` | none | none |
| 7 | EP present | `CFG_ADDR` bus 1 dev 0 fn 0 type 1, read `0x0` until != `0xffff0001` (RRS) and `== 0x834614e4` | expected 100 ms (I) x 1.25 = 125 ms | print regs, -ETIMEDOUT / -ENODEV |
| 8 | EP BARs | if different: BAR0 `0x08000000` (`0x10`), BAR2 `0x08040000` (`0x18`), BAR4 `0x09000000` (`0x20`), uppers 0, command `0x146` | none | -EIO if BAR2 readback differs |
- BAR layout = uboot-1 `pci_bar_assign` (`0x1c023468`): bottom-up from `0x08000000`, size-aligned, order BAR0 (32 KiB), BAR2 (256 KiB), BAR4 (16 MiB). BAR2 `0x08040000` is the CMIC (`us48_cmic_eth`, fan.c). I (derived from the algorithm and Linux BAR sizes; function 1 not touched by uboot-1)
- Not copied from uboot-1: bridge `0x3c` interrupt word, `0x28/0x2a` zeroing (no CPU-side effect expected).
- Unknowns for the board: cold link-up time of the BCM53346 after PERST# release; whether the SerDes needs more than uboot-1's MDIO init after a cold PERST#.

## Link-down guard (coordinator request, #63 blocker)
- `us48_pcie_up()` (`pcie.c`) = init succeeded and `LINK_STATUS` up, checked live.
- `fan.c`: no unit 1 BAR read, no frame on either group while down (select line B is unit 1 GP0, undriven); console says fans stay at the power-on state, default speed unknown (docs/fan.md). `fan <n>` re-checks live.
- `us48_cmic_eth` xe1 `start()` returns -ENODEV with a message; ge1 (unit 0) unaffected.
- A failed bring-up prints and continues to the prompt, so the SP805 never expires on this path.

## Board test (1-2 s, board agent)
- Cold power-up with the env auto-chain (#63). Expect on the awto-uboot console before the `fan:` lines:
  - `pcie: entry CLK_CTRL 0x4 LINK_STATUS 0x0 ...`, `pcie: PERST# cycled`, `pcie: link up <us> us after PERST# release`
  - `pcie: up ... LINKSTA 0x9012` (class `0x9012`), `pcie: unit 1 0x834614e4 BAR0 0x8000000 BAR2 0x8040000 BAR4 0x9000000`
  - `fan: probing unit 1 BAR at 0x08040000` succeeds, `us48_cmic_eth` pings on xe1.
- Warm (stock reboot, `CLK_CTRL 0x1`): expect `pcie: link already up (warm)`, no PERST.
- On `pcie: FAIL`: the printed registers and microseconds go into #76; adjust only from that measurement.

## Unit 1 CMIC wedge guard (#98, #91)
- Wedge = unit 1 CMIC AXI slave stuck (a2 read of `0x48023000`, or PERST# with a CMIC read in flight). Any BAR2 read then never completes: a1 stalls, SP805 bites, uboot-1 loops until a relay power cycle.
- No PAXB-local register shows it (BAR0 `0x2000-0x2fff` dump healthy vs wedged: only a2's own window regs differ). PCIe completion timeout (RC DevCtl2 range A) does not bound the stall either.
- Guard = probe marker: unit 1 PAXB `PCIE_SYS_EQ_PAGE_UPPER` (BAR0 `+0x22c0`, no function on the endpoint), `0x31554d43` = a BAR2 probe started and never finished.
  - `us48_unit1_cmic_arm()` (pcie.c, from `fan.c`): link up, BAR0 PAXB id (`+0x2400`) = `0x834614e4`, marker clear, then write marker. Anything else: one line (`unit 1: CMIC SKIPPED ...` / `... access skipped`), skip.
  - `us48_unit1_cmic_done()`: clear marker after the first BAR2 read returns; sets the `us48_pcie_up()` CMIC gate (fan, xe1 `start()`, `cmic unit 1`).
  - The marker survives a1 restarts, SP805 and warm PERST#-less boots; only a unit 1 power cycle clears it (it dies with the wedge).
  - Linux `us48_pci` probe reads it: set = no `cmic`/`pvtmon`/`gpio` aux devices (a2, clk, bustest stay). Fans: no frame (group 0's select line B is unit 1 GP0), LM75 and unit 0 PVTMON still read.
- Boot count with a wedge, Linux `reboot` (2026-11-16, 3 of 3): the first boot after Linux's PERST# stalls before the marker is armed (first BAR0 read after PERST# on a wedged unit 1), the second arms it and stalls on BAR2, the third skips and reaches ssh (ssh +38..62 s, 2 SP805 bites). Later reboots from the skip state: same, 38 s.
- Software unit 1 reset, tested 2026-11-16:
  - Secondary bus reset from the a1 root port (`pci_reset_bus` = Bridge Control bit 6, config save/restore, 106 ms): link stays up (`LINK_STATUS` 0xd) but every later memory access to unit 1 bus-errors and the next config read hangs a1. Same on a healthy unit 1, so the wedge cannot be shown cleared. PCIe hot reset = the same mechanism.
  - DMU `CRU_RESET` clear of `IPROC_RESET_N` on unit 1 through the BAR0 window: unit 1 loses its BARs (bus error), a1 then hung on `reboot -f`. `SW_RESET_N` does nothing on unit 0 (#63 E2b), not tried on unit 1.
  - No board-level unit 1 reset line (GPIO/CPLD) in hwdb or the docs.
- Healthy reboot: fixed by `mainline/0024` (PERST# stays released at shutdown, #67); no SP805 bite. The aux-client removal and a2 reboot notifier stay as the second line.
