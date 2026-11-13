# SRAM-resident clock switch with rollback (#17)

**UNTESTED on hardware.** Build only: `CONFIG_US48_SRAM_CLK` off, on at 800, on at 1000 (+ GENPLL on); logs `tmp/logs/build_uboot_sram_{off,on800,on1000}.out`.
Related: [sram.md](sram.md) (16 KiB at `0x1b000000`), [clocks.md](clocks.md) E1/E2/E5, [uboot-cpu-clock.md](uboot-cpu-clock.md), [ddr.md](ddr.md) experiments 1-2.

## Files (`uboot-port/board/ubiquiti/us48/`)
- `sram_clk.c`: `us48_sram_exec()` (runs from SRAM), `us48_sram_end()` (end marker), DDR-side loader `us48_sram_clk_run()` and command builders.
- `us48_sram_clk.h`: command struct, op codes, status values, register defines (moved out of `board.c`), entry points.
- `board.c`: `us48_cpu_switch()` keeps the E0 gate and SP805 arm, then calls `us48_sram_clk_cpu()` when `CONFIG_US48_SRAM_CLK=y`; old DDR-resident code otherwise.

## Design
- Loader copies `[us48_sram_exec, us48_sram_end)` to `0x1b000000` (word stores, SRAM is device memory), copies the command into the area at `0x1b000800`, flushes D-cache, invalidates I-cache, saves CPSR, `cpsid if`, calls SRAM code, restores CPSR.
- SRAM code is position independent: no calls, no globals, no jump tables, no relocations (checked: `objdump -dr` shows 0 `R_ARM`; flags in `Makefile`). It only touches registers and its area pointer.
- Nothing is logged from SRAM. Result goes to the status word; the caller maps it to an errno and `board_init()` prints it.
- Stack stays in DDR/L2: fine for CPU/AXI changes (DDR clock unchanged). A DDR PLL change needs a stack in SRAM (not done).

## Command struct
- `struct us48_clk_cmd { magic, nfwd, nrb, pad, ops[24 + 16] }`, `ops` = forward ops then rollback ops. Op = `{op, addr, a, b}`.
- Ops: `WR` (*addr = a), `RMW` (clear a, set b), `POLL_SET` / `POLL_CLR` (until (v & a) == a / == 0; b = max reads, 1..100000), `CHECK` (one read, (v & a) == b).
- Every poll is a read count, never unbounded. Count limit 100000 x ~1 us = 0.1 s, below the 10 s SP805; a count of 0 or above the limit rejects the command. Per-poll counts: GO 4096 (expected ~10 us, I), ARM PLL lock 2000 (expected tens of us, I), GENPLL lock 4000 (expected <= ~100 us, I); all unmeasured, expiry = rollback.

## Status area (`0x1b000800`)
- `status`: `0` ok, `1` poll expired + rolled back, `4` CHECK mismatch + rolled back, `2` failure and a rollback op failed too, `3` command rejected (no write), `0xdead0000/1` pending/running (running = never finished).
- `fail_idx` = 1-based failing forward op, `fail_val` = last value read. `us48_sram_clk_fail_info()` reads both.
- Returns: 0, `-ETIMEDOUT`, `-EPROTO`, `-EIO` (clock state undefined, SP805 reset is the way out), `-EINVAL`.

## Rollback semantics
- Runs only when a forward op fails; the caller supplies it as ordinary ops (saved values captured before the change).
- All rollback ops run even if one fails (status 2 at the end). Sequence for the lock-bearing cases ends with a verifying `CHECK`.
- CPU E1: unlock, GO fid 6 (`0x86060606`), CHECK fid 6, lock.
- CPU E2: unlock, GO fid 2, `PLLARMB` saved, `PLLARMA` = `0x11004003` (400 row), poll lock, GO fid 6, CHECK fid 6, lock. A rollback lock timeout still switches to fid 6 (status 2).
- GENPLL: saved `CTRL0/1/2` rewritten, load strobe again, poll lock.
- The SP805 (armed by the caller, 10 s) remains the backstop for a hang inside SRAM.

## Entry points
- `us48_sram_clk_cpu(800|1000)`: E1 / E2 sequences from clocks.md, same policy words and polls as the DDR-resident code. Caller does the E0 gate.
- `us48_sram_clk_axi(ch3_mdiv, ch4_mdiv)`: GENPLL E5 sequence (FAST_LOCK, MDIV write, `SW_TO_GEN_PLL_LOAD` bit 28, load-enable `0x08`, or `0x18` with ch4, poll `STATUS` bit 0, CHECK). **DISABLED**: compiled only with `CONFIG_US48_SRAM_CLK_GENPLL=y`, else `-EPERM`. ch3 mdiv limited to the E5 table (5, 7, 8, 10, 20). No caller in the tree. Changes AXI/APB-related clocks (console baud, SP805 period; see clocks.md table), rollback restores saved values only.
- `us48_sram_clk_ddrpll(mhz)`: stub, `-ENOSYS`. Needs the shmoo/retrain code (ddr.md experiments 1-2), not available.

## Size
- Code 0x230 = 560 B (ARM mode), budget 0x800 (loader returns `-EINVAL` above it). Area at `0x800..0xa90` (16 B header + 656 B command). Total < 3 KiB; SRAM above `0xb00` stays free for DDR-retrain code. Measured on the `CONFIG_US48_SRAM_CLK=y` object.

## Enable
- `uboot-port/configs/us48_defconfig`: `CONFIG_US48_SRAM_CLK=y` and `CONFIG_US48_CPU_MHZ=800` (then 1000); `python3 scripts/build_uboot.py`. Console line as in uboot-cpu-clock.md, `status=` carries the errno.
- GENPLL: additionally `CONFIG_US48_SRAM_CLK_GENPLL=y` and a caller; nothing calls it yet.

## To test
- 800 with SRAM path first, compare the console line with the DDR-resident result; then a forced failure (poll count 1 in a temporary build) to see rollback to 400 with `status=-110`.
