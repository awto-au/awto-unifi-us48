# Stock BCM59111 v21 firmware: cluster decode (poe-fw-seq)

Specs from the 2026-11-21 cluster decode of the stock image, evidence tags C (code bytes) / I / U, v21 and Meraki v18 compared.

| Doc | Cluster | Branch |
|---|---|---|
| 01-detect-sequencer.md | 0x316B legacy probe, detect state flow | wip/poe-seq-1 |
| 02-class-monitor.md | class mux 0x29EE, SAT_Pn composition, monitor 0x2C79 | wip/poe-seq-2 |
| 03-powergood-limits.md | power-good, limits, F1 power-on write, calc_limit | wip/poe-seq-3 |
| 04-trim-measure.md | 0x1B03 trim, 0x3651 detect scaling, 0x34D0 / 0x37D2 measurement | wip/poe-seq-4 |
| 05-timing.md | interrupt map, timer scale (probe results: tick 10.13 ms, hook 3 2.28 ms, f_cpu ~7.9 MHz) | wip/poe-seq-5 |
| 06-oracle.md | stock image in a Python MCS-51 core (`scripts/poe_fw_oracle.py`, `scripts/mcs51.py`) with a modelled analog front end | wip/poe-seq-6 |

- The C ports `poe-fw/src/seq_*.c` + `hal_analog_seq3.c` are per-cluster reference code (each compiled on its own branch behind `-DPOE_FW_SEQ`). They are not linked into any image and are not built together: their `hal_analog.c` accessors overlap. The shipped firmware (`poe-fw/src/chan.c`, `class.c`, `meas.c`, ...) was ported from them and passes the oracle diff (`scripts/poe_fw_oracle_diff.py`).
- Board result (2026-11-22, #26): the full image powered a real class-4 camera with the same registers as stock; see docs/poe-fw-rewrite.md.
