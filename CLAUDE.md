# CLAUDE.md

OpenWrt port + RE for the UniFi US-48-500W. **Public repo** — no absolute paths, no secret values, no backups/firmware/vendor source.

- Entry point: `scripts/dev.py` (`describe`, `doctor`, `console`, `send`, `boot-ram`). Console = one visible tio window; scripts use its socket, never the tty.
- Build: `python3 scripts/build_openwrt.py` (OpenWrt tree in `tmp/openwrt-build`, overlay = `openwrt/*.patch`).
  Edit the tree, then `--save-patch` to write the diff back.
- Facts live in `docs/`; findings and open questions go in GitHub issues.
- Device: dev/test unit (no house traffic), recovery via serial + JTAG, full-flash backups. Test images go in backup slot `kernel0` (`scripts/dev.py flash-kernel0` / `boot-flash`).
  Never write `kernel1` (stock), the bootsel record (`0x1ee0000`), `u-boot`, `EEPROM`; never `saveenv`; don't guess GPIO/I2C/SPI writes.
- Fans: stock firmware only has presets; OpenWrt has no fan control — watch temperature on every boot.
- Private material (backups, stock images, OpenMDK/OpenBCM/D-Link copies) stays out of git under `tmp/` or `private/`.
