# CLAUDE.md

OpenWrt port + RE for the UniFi US-48-500W. **Public repo** — no absolute paths, no secret values, no backups/firmware/vendor source.

- Build: `python3 scripts/build_openwrt.py` (OpenWrt tree in `tmp/openwrt-build`, overlay = `openwrt/*.patch`).
  Edit the tree, then `--save-patch` to write the diff back.
- Facts live in `docs/`; findings and open questions go in GitHub issues.
- Device: RAM boot only. Never `saveenv`, write flash, kill `switchdrvr`, or guess GPIO/I2C/SPI writes.
- Fans: stock firmware only has presets; OpenWrt has no fan control — watch temperature on every boot.
- Private material (backups, stock images, OpenMDK/OpenBCM/D-Link copies) stays out of git under `tmp/` or `private/`.
