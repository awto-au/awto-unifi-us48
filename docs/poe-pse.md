# BCM59111 pse-pd driver (`kernel/bcm59111`)

Issue #26 (bench #28). Register facts and stock sequence: [poe.md](poe.md). Build 2026-11-19 against v7.4-rc1: `W=1` clean, checkpatch `--strict` clean. Not run on a board.

## 1. Reference drivers (read, reimplemented, not copied)

| | UDM `bcm59111.c` (Ubiquiti, GPL) | quenode `driver/bcm59111.c` (GPL-2.0, from Meraki MX64/65 GPL, Cisco) |
|---|---|---|
| Interface | `ubnt-poe` ops, sysfs `firmware_enabled/post_init/report` | own `poe` class, raw `reg`/`reg_arg` sysfs; one global chip |
| FW format | per block raw `[0x70][0x80][off BE16][8 data]`, 2048 writes; stock = 32 B x 512 | `0x70<-0x80` once, then SMBus block writes `[0x70][count][off BE16][30 data]`, then `0x70<-0x40` |
| FW wait | poll `0x75 == 0xaa` every 50 ms, 1 s | busy loop up to 10 s (wall seconds) |
| FW skip | `0x41 == 21` (else reload) | none: reloads at every probe |
| Detect | dev id `0x43` exact `0xc0`/`0x60` | none |
| Post-load config | table: INT mask `06`, `12=00` (ubnt-poe) or `ff`, `13=0f`, `14=ff`, HP off, ICUT `50`, ILIM `00`; only after a fresh load | `13=0f` (DC), `14=ff`, `12=ff` = every port auto |
| Opmode | RMW `0x12`, also rewrites `0x14=ff`; AUTO or SHUT only | RMW `0x12` AUTO/SHUT |
| Class / detect | `>>4` / `&0xf` | 3-bit fields (as stock) |
| Power | V x I bytes LSB first, `122.07 uA x 5.835 mV` | `read_word`, integer steps 5835 / 122 (truncating) |
| Interrupts | none (stub `board_alert`) | none |
| Reset | `en-gpio` driven high, no pulse | none |
| Fault info | none | reg `0x0a` read (UVLO main/VCC, FET, temp flags), counters `0x60+` |
| Temperature | none | none |
| Bugs | `PWR_ENABLE`/`PWR_GOOD` macros swapped vs stock; `probe_status` logic inverted-looking | HP register stride 4 (stock 5), `POE_COUNT_REG` uses wrong identifier (both dead code); ICUT encoding differs from stock converter |

- Functional difference that matters: the two use different firmware download framing (0x80 per block vs once; count byte). Which the chip accepts for which image is not known; this driver uses the stock/UDM framing (stock `0x1087ad0`).
- quenode FW_REV not checked (Meraki tarball not downloaded); our blob is the stock one: `hwdb/firmware/README.md`.
- quenode `0x0a` supply-fault flags are not used here (reg map in poe.md has them as UDM names only).

## 2. Design

- One `pse_controller_dev` per chip (4 lines = channels 1..4), type C33. Ops: admin state (reg `0x12` field), power status (`0x10` good bit, detect nibble), class (`0x0c+c`, only with detect = good), voltage (`0x32+4c`), actual power (V x I), enable, disable. hwmon `temp1_input` kept (reg `0x2c`).
- Port to front port: PoE-NN chip, PI p = front port `4(NN-1)+p+1` (stock port map linear, board test which RJ45 powers). Mapping is in the DT comments; the bit order inside reg `0x12` is channel 1 = bits 1:0 (stock RMW + UDM), the OpenWrt `us48-poe` assumed port 0 in bits 7:6: board test with the bench PD decides.
- Enable = `0x14 |= detect+class bits`, `0x12` field = auto (`11`, stock default, poed modes 0/3/4). Disable = field `00`.
- Budget: fixed regulators `poe-total` (436 W) <- `poe-card0/1` (also 436 W: the card split is unknown) <- `vpwr-supply` of every PI. Enable reserves `brcm,port-reserve-milliwatt` (default 30 W) on the PI supply chain and fails with `-ERANGE` when exceeded; disable releases. Reservation is by admin enable, not by measured class.
- No priorities: the core's static budget needs classification events (IRQ) and a software power-on path (push-button regs `0x18..0x1a` are UDM names only, stock never writes them). Revisit with the IRQ work.
- No IRQ: `us48_cca_gpio` has no IRQ support. Event registers `0x00/0x03/0x07` are never read (clear-on-read); INT mask `0x01` is never written. When the IRQ lands: read `0x00`, then `0x03` (bits 7:4 power-good change) / `0x07` (bits 7:4 disconnect) as stock `0x1087680`.
- Control surface until the ports have a PHY: per-chip sysfs on the i2c device (`/sys/bus/i2c/drivers/bcm59111/<bus>-00<addr>/`): `portN_enable` (rw, root), `portN_status` (`<state> class C voltage_uV V power_mW P reserved_mW R`). Root-only write.
- `ethtool --set-pse geN c33-admin-control enable` needs `phydev->psec`, set from a `pses` phandle on the PHY node. The ge ports are `us48_cmic` netdevs without a `phy_device`: no `pses`, and `pse-pi` has no pairset phandles. Once `us48_cmic` registers PHYs from DT nodes, add `pses = <&pse_pi_N>` there; nothing in the PSE driver changes. The xe ports have no PoE.

## 3. Safety

- Probe: read `0x43` (must be `0xc0` class) and `0x75`. No answer, wrong id or firmware not running: hwmon only, no PSE, no write.
- Firmware running: `0x12 <- 0x00`, read back must be `0x00`, else no PSE. All 48 ports are off after load; nothing enables a port except a userspace admin enable. Loading the module cuts PDs that stock left powered (intended, #24).
- Chip config registers are untouched on a warm start (stock configuration persists, #24). After a firmware load by this driver only: `0x13=0f`, `0x14=ff`, `0x44=0f`, per channel `46=01`, ICUT `e2` (638 mA), ILIM `c0` (stock runtime defaults, poe.md 4 step 11; ICUT/ILIM still M2), ports remain shut.
- Firmware: only with module parameter `fw_load=1`, `request_firmware("bcm59111-v21.bin")`, size must be 16384. Raw I2C blocks of 36 bytes (needs `I2C_FUNC_I2C`; iproc has it). Wait for `0x75 == 0xaa` every 50 ms up to 2.5 s (stock sleeps 2 s then checks once; x1.25); expiry logs reg value + limit and fails. Load time not measured (M4).
- Reset line CCA GPIO 9 (active low, all 12 chips): `fw_reset` module param (write-only): `off` unbinds all chips and holds reset (firmware gone, PoE off), `on` releases, waits 1 s and rebinds (loads firmware per `fw_load`), `pulse` = off, 10 ms, on. Needed for firmware swaps without a mains cycle (`fw_override` only loads into a chip with no firmware).
- Passive 24 V (#149): `portN_mode` (`802.3` / `passive24`) per port, opt-in mask `passive_ports` (bit 0 = front port 1, default none). Frame state (fan level + per-port MSPI field) is owned by `us48_boardio` (`us48_boardio_poe_field`), not the fan driver.
  - `passive24` refused unless: port in `passive_ports` (EPERM), chip channel off (EBUSY), netdev `geN` exists with no carrier (ENODEV / EBUSY). `enable` is refused (EBUSY) while passive.
  - Order (stock `0x60620c`): field 0, 100 ms, field 1, 50 ms. Unbind returns passive ports to field 2 (24 V off). Kernel dead = field stays as last written (24 V stays on).
  - Passive power is not in the measured-power budget. Front port N to field index N-1 is an inference; verify with a meter on an unplugged port before trusting it.
- Module unload (driver unbind) shuts all ports of the chip (Dan 2026-11-21); a warm reboot keeps the chips as they are (#24).

## 4. Test plan (board; nothing run yet)

1. Image: `python3 scripts/build_mainline.py` (needs the PSE/regulator defconfig lines and patches 0027/0028; `bcm59111` loads in `S05us48-sfp`).
2. `scripts/poe_test.py status --apply`: dmesg lines, 12 chips PoE-01..12, temperatures, every port `disabled`, budgets (436000 mW total). Read-only.
3. Failure cases to look at first: chip without answer (dmesg warn, hwmon only), `-EPROBE_DEFER` (regulators), firmware rev line (`0x15`).
4. Only with Dan's go and the bench Pi (#28) on port N: `scripts/poe_test.py enable --apply --enable-port N --go`. One port, polls status 0.5 s x 5 s, logs class/voltage/power/reserved, always disables and reads back. Record time to power-good in #26 (M4/#28). If the wrong RJ45 powers or nothing does: the reg `0x12` bit order or MSPI field (M1) is the suspect.
5. `poe_test.py off --apply` writes 0 to all ports.
6. Budget accounting is checked as `reserved_mW` while enabled and 0 after. Over-budget refusal needs 15 enabled ports at 30 W (or a DT `brcm,port-reserve-milliwatt` large enough for two ports): not done under the one-port-per-run rule.
7. Measurements still wanted: M2 (read `0x01,0x13,0x14,0x44,0x46-0x48` of a stock-configured chip), M3 (cold boot chip state, GPIO 9), M4 (firmware load time), M5 (over-budget behaviour of the chip), cold-boot `fw_load=1` path (needs reset).

## Power budget policy: measured total, newest port blocked (Dan 2026-11-21)

- Admission does not use reservations as the limit: every port is enabled at boot (30 W class-4 reservation, DT budget 1440 W = 48 x 30 W, so the regulator accounting never refuses).
- The limit is the **measured** power. `bcm_watch` (one work item for all 12 chips) scans every `power_poll_ms` (default 1000 ms; one scan = 12 x `STAT_PWR` + `VM`/`CM` of each powered port, ~240 I2C byte reads at most).
  - Per port: power-good bit (`STAT_PWR` bits 7:4) -> `mW = VM x 5.835 mV x CM x 122.07 uA` (same maths as `power_mW` in the port status).
  - Sum over all chips -> `total_mw` (read-only parameter).
  - Sum > `power_limit_mw` (default 436000 = 500 W PSU - 64 W, poe.md section 7; writable at runtime; 0 = off): the port that was **powered most recently** is shut (OP_MOD = 0) and marked `blocked_over_budget`, the sum is reduced by its last reading, repeat until under the limit.
  - "Most recently" = the first scan that saw the port with power good (resolution = scan period; ties inside one scan: highest front port number).
  - A blocked port stays shut: no automatic retry. `portN_enable` = 1 clears the block and tries again (it is shut again if the total is still over).
  - Log: `over budget (X mW > Y mW): port N (PoE-NN) shut and blocked, was Z mW`.
- Parameters: `power_limit_mw`, `power_poll_ms` (0 = no watcher), `total_mw` (ro). Code: `kernel/bcm59111/bcm59111.c` `bcm_watch`.

### Risk: the limit is enforced only while the kernel runs
- The chip firmware has no budget logic (poe-fw.md 7): it protects each port (ICUT / ILIM, fault cool-down) but nothing limits the sum.
- Kernel down (panic, hang, watchdog reset, i2c hang): the watcher stops; ports keep their state (a warm reboot leaves the chips powered, #24; a clean module unload shuts them) and the total can go over 436 W up to what the PSU supplies. Over the PSU rating: sagging rail, PSU protection trip, all PD power lost, or damage.
- Even with the kernel up: reaction time is at least one scan (1 s) plus the I2C scan time; inrush and class-4 PDs can overshoot briefly. A PD that goes from idle to full load between scans is only seen at the next scan.
- All 48 ports are enabled at boot, the chips power ports one chip at a time while their firmware loads (~4 s each): the total rises in steps and the watcher runs from the first chip, but there is no soft-start staging inside a chip.
- Open: the chip host watchdog `WDT_TIME` (reg 0x42) might shut ports when the host stops talking: semantics unknown (M, #151). Until measured it is not a mitigation.
- Options to reduce the risk (not done): lower `power_limit_mw` to leave headroom for the scan latency; shorter `power_poll_ms` after raising the I2C speed (#158); enable ports in stages at boot; a hardware PSU / current limit outside the chips.

### Host watchdog in the awto firmware (Dan 2026-11-21): the fix for "kernel down"
- Only the rewrite firmware (`poe-fw/`, docs/poe-fw-rewrite.md) has it; stock v21 has none (driver logs `firmware has no host watchdog`, the risk above stays).
- Registers (awto image only): `0x20` HOST_WD_TIME (write = arm + poke, 10 ms ticks, 0 = off), `0x21` HOST_WD_STAT (read: b7 feature, b1 armed, b0 expired latched; write b7 = restart). Stock `0x42` WDT_TIME is the 8051's own watchdog period and is not used.
- Driver (`host_wd_ms`, default 200, 20..2550, 0 = off): arms at probe, before any port is enabled; ppokes every half period (100 ms) from a per-chip work item (12 writes per 100 ms).
- Not poked for `host_wd_ms`: the firmware drops all PoE on the chip, latches `expired`, asserts INT, disarms, and ignores power requests until restarted.
- On the next work run the driver sees `expired`, logs `firmware host watchdog expired`, writes the restart, re-arms and re-enables the ports it had enabled (`want[]`; ports blocked by the power watcher stay off). The same path runs after a kernel stall that recovers.
- Unbind (module unload) = PoE off (Dan 2026-11-21): the driver shuts all ports of the chip (`OP_MOD` = 0, read back), on stock firmware too, then disarms the watchdog. A reload therefore drops PoE until the driver enables the ports again. An unloaded driver = ports off, so the watchdog is not needed then.
- Limits: the poke shares the I2C bus with everything else (a firmware download of another chip, the 1 s power scan): each transfer is short and the bus lock is per transfer, but a stall of the work queue or the I2C controller for 200 ms drops the ports (intended). Worst-case I2C latency under load is not measured (M).

### Bench: measured-power watcher with real loads (2026-11-21, cameras on front 26 and 40)
- Both cameras: class 4, 52.3 V, `power_mW` 2192 / 2234 (stock showed 2.47 / 2.39 W), total 4382 mW.
- `power_limit_mw` set to 3000: next scan (<= 1 s) logged `over budget (4382 mW > 3000 mW): port 4 (PoE-10) shut and blocked, was 2190 mW`; port 40 (the later port of the same scan) went `blocked_over_budget`, port 26 kept delivering, total 2195 mW.
- Limit back to 436000 and `portN_enable` = 1 on port 40: block cleared, port delivers again.
- Needs `us48_boardio.poe_field` = 2 (default): with field 0 the PSE never detects a PD (docs/poe.md M1).

## Stock differences (stock does X, we do Y)

| Item | Stock | Ours | Reason |
|---|---|---|---|
| Firmware download | one download to broadcast `0x30` with mux `0x30` (both PoE cards): all 12 chips load in parallel, about 4 s | one download to broadcast `0x30`, mux `0x30` on the root adapter (root lock held), each chip's probe waits for its own CRC | none recorded; poe.md section 11 step 4 marked it inferred (I). Fixed 2026-11-23: `fw_bcast=1` (default) = broadcast download, 12 chips in about 5 s after a reset (1 s settle + about 4 s); `fw_bcast=0` = old per-chip path (#26) |
| I2C clock | 100 kHz | 100 kHz | `MODE_400` gives about 190 kHz and every device behind the mux NAKs (bench 2026-11-23, #158) |
| Reset line | CCA GPIO 9 pulse before the load | `fw_reset` off / on / pulse | #26 |

## Firmware guard (#175)

- `fw_check_ms` (default 20000 = stock's about 20 s, poe.md section 4 step 10; 0 = off): every period reg `0x75` of each chip with a running PSE is read; not 0xaa (or no answer) = firmware lost.
- Recovery = `fw_reset=pulse` for all 12 chips (reset, broadcast reload, rebind), up to 3 attempts (stock: 3 retries, poe.md section 4 step 7); log `firmware lost` / `attempt n of 3` / `firmware recovered`. No check runs while a recovery is in progress.
- Ports enabled only through sysfs are lost with the rebind; `enable_ports` applies again. Bench 2026-11-23: raw reset pulse on GPIO 9 -> detected on the next check, all 12 chips reloaded in about 5.4 s, cameras delivering.
- Not covered: a probe that finds only some chips without firmware still broadcasts without a reset first (#176).

## Boot enable stagger (#165, #150)

- `enable_stagger_ms` (default 200 = stock poed `stagger_ms`, poe.md section 2; 0 = all at once): `enable_ports` are not enabled in the probe loop but by one worker, one port every 200 ms in front-port order across all chips (48 ports = about 9.6 s).
- Before each port the measured total (`bcm_watch`, `power_limit_mw`, default 436 W) is checked: at or over the limit the port and all still pending ports are blocked (log `boot enable: measured ... >= budget`), enable them by hand to retry.
- Bench 2026-11-23: module reload with all 48 enabled: about 5 ports per second rise, 33 of 48 after 7 s; cameras delivering. The over-budget branch is not exercised (two cameras only, about 4 W).
