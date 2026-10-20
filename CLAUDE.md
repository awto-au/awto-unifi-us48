# CLAUDE.md — awto-unifi-us48

OpenWrt port + RE for the UniFi US-48-500W. **Public repo** — no absolute paths, no secret values, no backups/firmware images. SDK source + derived register tables ARE fine to commit (Dan 2026-10-14).

- Entry point `scripts/dev.py` (`describe`, `doctor`, `console`, `send`, `boot-ram`). Console = one visible tio window; scripts use its socket, never the tty.
- Build: `python3 scripts/build_openwrt.py` (OpenWrt tree in `tmp/openwrt-build`, overlay = `openwrt/*.patch`). Edit the tree, then `--save-patch` to write the diff back.
- Source trees from machine-shared stores, never fresh clones (#45): `tmp/linux-mainline` = worktree of the Linux store (`tmp/linux-store`); OpenWrt downloads + ccache shared (`tmp/openwrt-dl`, `tmp/ccache-openwrt`). All gitignored symlinks; never `git gc` in a store.
- Facts in `docs/`; findings + open questions in GitHub issues.
- Device: dev/test unit (no house traffic), recovery via serial + JTAG, full-flash backups. Test images go in backup slot `kernel0` (`scripts/dev.py flash-kernel0` / `boot-flash`). Never write `kernel1` (stock), the bootsel record (`0x1ee0000`), `u-boot`, `EEPROM`; never `saveenv`; don't guess GPIO/I2C/SPI writes.
- Fans: stock firmware has presets only; OpenWrt has no fan control — watch temperature on every boot.
- Backups + stock firmware images stay out of git under `tmp/` or `private/`.
