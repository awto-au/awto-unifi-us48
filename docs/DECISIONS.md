# Decisions

- One line per standing decision: what, date, issue. Facts and measurements live in the linked docs and issues.
- Newest first. A superseded entry is deleted; the issue keeps the history.

- Mainline Linux only; OpenWrt removed from the repo, tag `openwrt-parked` (Dan 2026-11-18). #74
- Switchdev in the CMIC driver, not DSA: the CPU port is a DMA ring, not a conduit MAC (2026-11-18). [switchdev.md](switchdev.md), #57
- Learn/age reporting from the HR2 L2 change queue (`L2_MOD_FIFO`), not the 3 s table poll (Dan 2026-11-18). #134, #135
- Stock/SDK defaults are not hardware reasons: use the chip's feature unless a measurement says otherwise (Dan 2026-11-18). #134
- Clock and limit trials probe past the rated value to failure; Dan picks the run value (2026-11-15). [clocks.md](clocks.md), #17
- Test images only in `kernel0` (backup slot); `kernel1` (stock), bootsel `0x1ee0000`, uboot-1, EEPROM are never written; `u-boot-env` only via the docs/autochain.md image (Dan 2026-11-15). [autochain.md](autochain.md), #73, #139
- awto-uboot (uboot-2) loads images by TFTP; no serial YMODEM image loads. #46
- Fans: presets only (50/70/90/100 %), no tach, no off; quiet rule is the default, 100 % switchable (Dan 2026-11-16). [fan.md](fan.md), #19, #115
- PoE off at boot, per-port on/off. [poe.md](poe.md), #24
- Mainline kernel base is the newest Linus tag, patch series in `mainline/` (v7.4-rc1 now). #15
- Shared Linux store, never a fresh clone; worktree from the store (2026-11). #45
- Upstream series (DT, bindings, pcie-iproc, US-48 device) held until Dan's go-ahead. #41
- The US-48 is a dev/test unit, not the house uplink; the sw1/sw2 benches belong to the awto-tl-st1008f session (Dan 2026-11-16). `hwdb/lab/protected.toml`, #139
