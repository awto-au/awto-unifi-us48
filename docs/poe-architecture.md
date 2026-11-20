# PoE architecture: who drives the BCM59111 from Linux

Decision doc for #26 (driver), #23 (RE), #28 (bench). Offline: nothing here ran on a board. Evidence: [poe.md](poe.md), [poe-pse.md](poe-pse.md), [poe-fw.md](poe-fw.md) (branch `wip/poe-fw-dw8051-26`), `hwdb/firmware/`, `kernel/bcm59111`.

- **C** = confirmed by code/bytes. **I** = inferred. **M** = needs a board measurement.

## 1. Facts that bound the choice

- The 8051 firmware is always running: it is the I2C slave. Every host register `r` is XRAM `0x8000+r` of the firmware (C, poe-fw.md 2, 9). No option removes it; options differ in the host side only.
- Firmware owns per-port detect, classify, power-up, disconnect (MPS), fault cool-down, current/voltage/temperature measurement (C, poe-fw.md 5, 11, 14). Host sets mode (`0x12`), enables (`0x14`), limits (ICUT `0x47+5c`, ILIM `0x48+5c`).
- Image = Ubiquiti's GPL array, byte-identical to stock (sha256 `31496a90...bdd889`); ROM below 0x1A00 is unavailable (C). Replacing the firmware is out of scope.
- No power budget or priority logic in the firmware or in stock host code (I, poe.md 7, poe-fw.md 7). The stock 436 W is a number in libubnt only. Stock behaviour = firmware auto mode + poed staging ports 200 ms apart.
- No 12-byte UART protocol in the image: I2C register slave only (C, poe-fw.md 15).
- Clear-on-read registers `0x00 0x03 0x05 0x07 0x09 0x0B` (C, poe-fw.md 4): a status poll must not touch them; read-only paths use `0x0c+c`, `0x10`, `0x2c`, `0x30..0x3f`.
- PoE mode select (802.3 vs passive 24 V) and PoE LEDs are MSPI board-IO chains, not the BCM59111: owned by `kernel/us48-boardio` (main). Passive field `1` is the only value that can put 24 V on spare pairs (C code, I physical; poe.md 6).
- Chips sit on the CMIC/iproc I2C bus behind a pca9548 (`0x70`, ch4/5 = 12 chips `0x28..0x2d`) (#26). The firmware read-byte ISR is about 70 us at an estimated 8 MHz (I, poe-fw.md 12, 17) versus 22.5 us/byte at 400 kHz: the slave, not the host CPU, limits the bus. Doubling the CPU did not change that.
- Answer to "can Linux drive the PoE I2C directly": yes. The firmware speaks plain SMBus byte reads/writes with auto-increment (C); `kernel/bcm59111` already does it from the kernel. Bus speed is M (#152).

## 2. Options

| | (a) pse-pd kernel driver, direct I2C | (b) userspace daemon over i2c-dev | (c) firmware in the loop, host does the minimum |
|---|---|---|---|
| Who owns the 8051 | kernel: `request_firmware` + reset line, `fw_load=1` | daemon: raw `I2C_RDWR` blocks to `0x70`; no kernel driver may bind the address | firmware free-runs; host loads it once (or relies on warm state) |
| State machine | firmware | firmware | firmware |
| Budget / priority / which ports on | kernel: regulator budget + admission (`pi_enable` fails `-ERANGE`); priority needs events (IRQ) or userspace policy on top | daemon: all policy in one process | nowhere: no budget enforced (stock behaviour) |
| Telemetry (hwmon temp, V, I, power) | hwmon + `pi_get_*` | reimplemented in the daemon (and its own export) | none beyond raw reads |
| Daemon / userspace dies | ports unchanged (policy was set, firmware keeps running) | ports stay in the last state, unmanaged: budget no longer enforced, nothing reacts to overload | n/a (no daemon) |
| Kernel panics / host hangs | firmware keeps ports as they are; chip protects each port itself (ICUT/ILIM, fault cool-down, C); host watchdog `0x42` WDT_TIME semantics unknown (M, #151) | same | same |
| Reboot / module reload | ports unchanged unless the driver writes them (module unload leaves ports, poe-pse.md 3); warm reboot keeps chips (#24) | same | same |
| Mainline fit | `drivers/net/pse-pd/bcm59111.c`: c33 ops, `ethtool --set-pse/--show-pse`, netlink, `pse-pi` DT binding (0027/0028). Needs PHY `pses` for ethtool (#155) | none: not upstreamable, no ethtool/netlink integration | none |
| Testable off-board | register model on `i2c-stub` for the pure logic (admission, conversions, probe safety); firmware not emulated | best: Python + mocked SMBus, no kernel build | trivial |
| Hazards | in-kernel bug = oops; mitigated by default-off and read-back | two writers to one chip if the kernel driver also binds; hwmon lost or duplicated | 436 W unguarded, no admin control |

## 3. Recommendation

- **(a) is the product**: pse-pd driver (`kernel/bcm59111`). It is the only upstream path, it already exists, and Linux drives the I2C directly with no extra hardware access.
- **Firmware stays the state machine**: detect/classify/power/fault/measurement are not reimplemented anywhere; host never writes a port to "on" outside `0x12` auto (push-button regs `0x18..0x1a` unused by stock, I).
- **Policy split**: kernel = admission (budget), admin enable, telemetry, safe default (all ports shut at probe, `us48-poe-off`); userspace = boot policy and priority (which ports, in what order, staggered 200 ms as stock poed), through sysfs now, `ethtool --set-pse` later. Reason: priority needs classification events the driver does not get yet (no IRQ, #148), and policy belongs outside the kernel.
- **(b) only as a bring-up/test tool**, never bound to a chip while the kernel driver is loaded (`scripts/poe_test.py` already works through the driver's sysfs; a raw i2c-dev script is for chips without a driver).
- **(c) is what stock does; rejected**: no budget, no admin control, no telemetry. It is the fallback if the driver is not loaded (chips keep their last state).
- **Passive 24 V**: kernel side only, in `us48_boardio` as a per-port opt-in with the sequence in poe.md 11 (chip field `00`, MSPI `0`, 100 ms, `1`, 50 ms), never from userspace i2c-dev and never default. Whether to implement it at all is Dan's decision (#149).

## 4. Module parameters (`kernel/bcm59111`, set at load, all 0444)

| Parameter | Default | Meaning | Status |
|---|---|---|---|
| `fw_load` | 0 | download firmware if the chip reports none running (needs reset line) | backed (stock format) |
| `enable_ports` | 0 | u64 front-port mask (bit 0 = port 1) admin-enabled at probe; chip from the DT label `PoE-NN` | backed: same writes as `pi_enable`; default keeps `us48-poe-off` |
| `budget_mw` | 0 | module-wide cap on reserved mW over all chips, on top of the DT regulators | software only; stock 436000 |
| `reserve_mw` | 0 (DT, else 30000) | mW reserved per enabled port; 30 W = 802.3at class 4 PSE output | software only |
| `icut_raw` | -1 | raw ICUT byte written on enable | **unverified (M2)**: encoding not confirmed; -1 keeps the chip value |
| `ilim_raw` | -1 | raw ILIM byte written on enable | **unverified (M2)**, as above |
| `poll_ms` | 0 | log power-good changes every N ms; reads `0x10` only | backed; 300 ms suggested = firmware disconnect timer (30 ticks x 10 ms, poe-fw.md 11, 17; tick is I) |
| `passive_ports` | 0 | 24 V passive front ports | **unverified (M1/M6), not implemented**: non-zero logs a warning and writes nothing |

- Per-port 802.3af/at vs passive mode select is not a chip register: no parameter writes it from this module (`us48_boardio` owns the MSPI frames).
- Poll interval reasoning: nothing the host can act on changes faster than the firmware's 300 ms disconnect timer; a shorter poll only costs I2C time against the 70 us/byte slave ISR.

## 5. Follow-ups

Tracked as child issues of the parent decision issue (list in the parent). Board runs needed: M1 (MSPI field 2), M2 (ICUT/ILIM encoding), M5 (over-budget behaviour), `WDT_TIME` crash behaviour, I2C bus speed, `enable_ports` / `poll_ms` smoke test.
