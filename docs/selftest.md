# Boot self-test (`us48-selftest`)

- Issue #35. Read-only. Untested on the board when written.
- `/usr/sbin/us48-selftest`: busybox ash; one line per check `PASS|WARN|FAIL name detail`; exit code = FAIL count (summary line excluded).
- Output: stdout, `/tmp/us48-selftest.log` (rewritten each run), `logger -t us48-selftest`.
- Boot: `/etc/init.d/us48-selftest` (`START=90`, background, never blocks boot). Run by hand: `us48-selftest; echo $?`.

## Checks

| Name | Check | Verdict |
|---|---|---|
| `cpu` | 1 processor, implementer `0x41`, part `0xc09` (MIDR `0x414fc091`, [hardware.md](hardware.md)) | FAIL otherwise |
| `mem` | `MemTotal` | PASS >= 200000 kB; WARN >= 100000 kB (one 128 MiB bank); else FAIL |
| `thermal` | max of lm75 hwmon `temp1_input` (i2c-9 `0x48`..`0x4b`) | FAIL >= 80 C, WARN >= 70 C or < 4 sensors, FAIL if none |
| `fand` | `us48-fand` running ([fan.md](fan.md)) | FAIL if not |
| `i2c-tree` | `i2c-2` (CMIC master), `i2c-3..10` (PCA9548 `0x70` children) | FAIL if any missing |
| `sfp-i2c-3..6` | `i2cdetect -y -r BUS 0x50 0x51`, `0x50` answers | WARN if empty cage, FAIL if bus missing |
| `module` | `us48_cmic` in `/proc/modules` | FAIL if not |
| `port-*` | `ge1`, `xe49`, `xe50`, `ge40` in `/sys/class/net` ([port-map.md](port-map.md)) | FAIL if missing |
| `poe-off` | stub | always WARN |
| `mtd` | 7 partitions with the [flash map](hardware.md#flash-map) names | FAIL < 7, WARN on name mismatch |

## Thresholds and limits

- 80 C = `us48-fand` full-fan alert point; 70 C = early warning (10 C under it).
- No `i2cdetect` on `i2c-0`/`i2c-1`: hangs, pads unrouted ([board.md](board.md#i2c-topology)).
- No access to `0x28`..`0x30` on `i2c-7`/`i2c-8`: PoE chip state. Only read path is BCM59111 reg `0x12`, so `poe-off` stays a WARN stub; `us48-poe-off` logs its own result.
- SFP scan limited to `0x50`/`0x51` on `i2c-3..6`; `i2c-7..10` are not scanned.
- Not covered yet (#35): clocks, PCIe, EEPROM, DDR patterns, flash CRCs, JSON output, per-step time bounds.
