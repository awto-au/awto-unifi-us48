# CLAUDE.md — awto-unifi-us48

OpenWrt port + RE for the UniFi US-48-500W. **Public repo** — no secret values (filesystem paths + device IDs are fine), no backups/firmware images. SDK source + derived register tables ARE fine to commit (Dan 2026-10-14).

- Entry point `scripts/dev.py` (`describe` lists every command; `doctor`, `console`, `send`, `boot-ram`, `flash-net`, `boot-flash`). Console = one visible tio window; scripts use its socket, never the tty. Adapter is on `lizard`: `US48_HOST=lizard`.
- Build: `python3 scripts/build_openwrt.py` (OpenWrt tree in `tmp/openwrt-build`, overlay = `openwrt/*.patch` + `openwrt/files/`). Edit the tree, then `--save-patch` to write the diff back.
- Source trees from machine-shared stores, never fresh clones (#45): `tmp/linux-mainline` = worktree of the Linux store (`tmp/linux-store`); OpenWrt downloads + ccache shared (`tmp/openwrt-dl`, `tmp/ccache-openwrt`). All gitignored symlinks into `/mnt/1tnvme/git-mirrors`; never `git gc` in a store.
- Facts in `docs/`; findings + open questions in GitHub issues.
- Device: dev/test unit (it powers 5 PoE cameras + uplinks the house LAN on port 50, but Dan says still test), recovery via serial + JTAG, full-flash backups. Test images go in backup slot `kernel0` (`scripts/dev.py flash-kernel0` / `boot-flash`). Never write `kernel1` (stock), the bootsel record (`0x1ee0000`), `u-boot`, `EEPROM`; never `saveenv`; don't guess GPIO/I2C/SPI writes.
- Fans: presets only (50/70/90/100 %), no tach, no off. OpenWrt runs `us48-fand` (50 %, full above 80 C) — watch temperature on every boot. PoE is off at boot (`us48-poe-off`).
- Backups + stock firmware images stay out of git under `tmp/` or `private/`.
