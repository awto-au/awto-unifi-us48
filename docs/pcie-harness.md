# PCIe bus harness: unit 0 (a1) <-> unit 1 (a2) (#80)

Performance + integrity of the bus in both directions, plus link health. Never run on the board when written (offline build): numbers and a2 firmware behaviour are unmeasured.

## Parts
| Part | Path | Role |
|---|---|---|
| a1 module | `kernel/us48-bustest/us48_bustest.c` | aux driver of `us48_pci.bustest` (new aux device in `us48_pci`); debugfs `/sys/kernel/debug/us48_bustest/` |
| `us48_pci` additions | `kernel/us48-pci` | aux device `bustest`; `us48_pci_win_get/move/put`: window lock + page select + window VA (raw access, no table check) |
| a2 firmware | `a2fw/bustest.c`, `a2fw/bustest.h` | `us48-a2-bustest.elf` (MMU + caches), `us48-a2-bustest-nommu.elf` (strongly-ordered only; manual fallback) via `scripts/build_a2fw.py` |
| host driver | `scripts/pcie_harness.py` | `smoke`, `soak`; ssh to `$US48_SSH` (default `root@192.168.25.207`), BatchMode |

## debugfs (a1)
- `cmd` (write `<test> [count] [limit_ms]`), `result` (last result line, `status=ok|mismatch|link|timeout|fail`, `dur_ms`), `stats`, `a2`, `mismatch`.
- `limit_ms`: kernel-side abort of a test, checked per batch of 1000 accesses or per page.

## Tests a1 -> unit 1 (SRAM `0x1b000000`, 16 KiB, hwdb `a2_sram`)
SRAM content is saved before and restored after each test. The a2 test firmware does not use it (code, stack, buffers are in the carveout); the old a2 stub does (mailbox +0x100, payload +0x200, stack +0x3ff0): do not run it with the harness.

| Test | Does | Reports |
|---|---|---|
| `lat N` | N x 32-bit read, N x write through the IMAP window (batches of 1000, IRQs off), write batch ends with a read | `rd_ns`, `wr_ns` (+ min/max batch average) |
| `burst N` | N x 4 KiB `__ioread32_copy` / `__iowrite32_copy` through the window | `rd_MBps`, `wr_MBps` |
| `schan N` | N S-channel register reads (PVTMON result, block 16) on unit 1 | `ops_per_s`, `us_per_op` |
| `pat_walk1/walk0/addr/prbs31 R` | R rounds: write the 16 KiB with the pattern, then read all back and compare; mismatches logged (address, expected, got, time) | `mismatches`, `words` |
| `a2push` | a1 writes a seeded pattern into `YBUF` (carveout), bumps `y_seq` (only when a2 acked the last one) | `seq`, `seed`, `y_mismatch` (a2's count) |
| `a2xverify` | a1 reads a2's `XBUF` and compares with `bt_xpat(i)` | `mismatches` |
| `a2clear` | zero the a2 results struct (before starting a2) | |

- Patterns: walk1 `1<<(i&31)`, walk0 inverse, addr = bus address of the word, PRBS31 `x^31+x^28+1`.
- Throughput numbers are for a 4 KiB window and word accesses; the kernel's `memcpy_fromio` is byte-wise on ARM and is not used.

## Link health (`stats`, polled every 100 ms by a delayed work)
- `ep_*` / `rc_*`: LNKSTA width and speed of unit 1 (endpoint) and the root port; `ep_link_chg` / `rc_link_chg` = width or speed changed between polls (retrain), `lt_seen` = LNKSTA.LT seen.
- `link_down`: polls where `us48_pci_link_ok()` failed; `dl_inactive`: PAXB `PCIE_LINK_STATUS` DL_ACTIVE or PHYLINKUP clear (unit 1 PAXB through the regmap).
- `paxb_link`, `paxb_reset`: raw PAXB `PCIE_LINK_STATUS`, `RESET_STATUS`.
- No PAXB error/abort register is in hwdb. Stand-in: PCI_STATUS and bridge SEC_STATUS error bits (master/target abort, parity, SERR), read and cleared each poll, summed as `pci_status_err`.
- AER: counted (read-and-clear) on endpoint and root port when the capability exists: `aer_ep_cor/unc`, `aer_rc_cor/unc`. Whether this chip exposes AER is unknown (I); the counters stay 0 otherwise.
- `t0_mc`, `t1_mc`: PVTMON die temperature of unit 0 / unit 1 in mC (-1 unreadable), S-channel block 16 as in `us48-fand`. Unit 0 is read through CMC1, which `us48-fand` also uses: the reads are retried 3x and plausibility-checked (-20..150 C). Constants are local in the module (`docs/clock-audit.md` 3): not in hwdb yet.

## a2 firmware (`a2fw/bustest.c`)
- Results struct `struct bt_results` (`a2fw/bustest.h`, `BT_VERSION` 1) at carveout + `BT_SHARED_OFF + 0x2000` (hwdb `a2_shared`); trace0 at `a2_shared + 0x1000`. a2 writes it through a non-cacheable alias, a1 reads it with a WC mapping (`debugfs a2`, `key=value`, all fields, ticks of a 200 MHz timer).
- Loop per pass: cached 1 MiB read / write / 256 KiB copy; non-cacheable word read / write / copy of 64 KiB; 1024 dependent non-cacheable reads (latency sum/min/max) and 1024 cold cached line fills; cold and warm I-fetch of 8 KiB; fill `XBUF`, verify `YBUF` (non-cacheable and cached views); integrity sweep (rotates walk1, walk0, addr, prbs31, each non-cacheable then cached with clean + invalidate).
- MMU: first-level table in a2_shared; carveout identity = write-back, alias `0x20000000` = non-cacheable, `0x18000000-0x1bffffff` = device. Not run on the board: if `a2_running` fails in smoke, try `--a2-fw us48-a2-bustest-nommu.elf` (cached columns stay 0) and report.
- Heartbeat `hb` increments between phases (< 0.5 s apart).
- Unit conversions are in `scripts/pcie_harness.py` (`a2_metrics`), sizes come from `a2fw/bustest.h`.
- Not built: unit 1 CMIC packet DMA -> host throughput (no internal loopback in `us48_cmic_xe`; `driver-internals.md` mentions loopback only for the PHY). Skipped.

## Run
Image: `python3 scripts/build_mainline.py` stages `us48_bustest.ko`, `us48_a2.ko`, `us48_pci.ko` into `/lib/us48/` and `us48-a2-bustest*.elf` into `/lib/firmware/` of the initramfs. `us48-net` loads `us48_pci`; the harness `insmod`s `us48_a2`, `us48_bustest`, stops/clears/starts the remoteproc.

    python3 scripts/pcie_harness.py smoke                  # ~10 s, pass/fail table, writes tmp/logs/pcie_smoke.json
    python3 scripts/pcie_harness.py soak --hours 8         # runs smoke first, aborts if it fails

Options: `--a2-fw NAME`, `--no-a2` (a1 side only). Stop a soak early with Ctrl-C / SIGTERM: the report is still written.

## Timeouts
| Bound | Value | Reason |
|---|---|---|
| ssh stall | 60 s without output | host-process rule: hung, kill, log last output |
| ssh per call (soak) | 3 x longest smoke cycle wall time | backstop on measured value |
| ssh first calls (smoke) | 30 s | no measurement yet |
| kernel per test (soak) | 1.25 x smoke `dur_ms`, floor 50 ms | IRQ/scheduler jitter on the A9 is of that order |
| kernel per test (smoke) | 2000 ms | no measurement yet |
| a2 first pass | 5 s after `start` | arithmetic ~0.5 s, no measurement: adjust after the first board run (`A2_READY_S`) |

## Soak: logging
- `tmp/logs/pcie_soak.log`: one line per minute (means of cycles in that minute: latency, throughput, a2 metrics, link width/speed, a2 `hb`, temperatures, counted events), `HOURLY` line with min/mean/max per metric.
- `tmp/logs/pcie_harness.log`: everything. `tmp/logs/pcie_soak_report.md`: totals, counted events, min/mean/max per metric and temperature, first failure.
- Every cycle runs all tests once, back to back (one ssh call), and reads stats, a2 struct and the 4 LM75s (`hwmon*/temp1_input`).

## Stop rules
Stop = snapshot to `tmp/logs/pcie_freeze-<ts>.txt` (mismatch log, a2 struct, stats, trace0, dmesg, console tail), report, exit 2. Nothing is reloaded; a2 keeps running.
- Data mismatch: any test `status=mismatch`, a2 `mismatch` / `y_mismatch` > 0, kernel mismatch log growth.
- Link loss: `status=link`, `link_down` or `dl_inactive` increased, ssh unreachable twice (3 s apart) or stalled.
- Die or LM75 >= 100 C (`us48-fand` already goes to 100 % fan on a die >= 100 C).
- No temperature sensor readable for 3 cycles.
- AER uncorrectable error.
- a2 heartbeat not advancing for 3 cycles, or a2 struct gone.
- 3 consecutive kernel-side test timeouts, or a test `fail`.

Counted, soak continues: single test timeout (`slow:<test>`), retrain (`ep_link_chg`, `rc_link_chg`, `lt_seen`), AER correctable, `pci_status_err`.

## Open
- All of it unmeasured; first board run sets `A2_READY_S`, the 50 ms floor and the a2 numbers.
- MMU bring-up of the firmware, IRQ-off timing in `lat`, whether unit 0 PVTMON through CMC1 collides with `us48-fand` (reads are retried; a collision at worst reads 'unreadable' once).
- PVTMON constants and PAXB error registers belong in hwdb.
