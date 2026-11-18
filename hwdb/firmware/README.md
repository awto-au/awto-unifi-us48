# hwdb/firmware

## bcm59111-v21.bin

- BCM59111 PoE PSE firmware, 16384 B, sha256 `31496a90bead4c7feb87994ad30d626b70d07e112d79d60b587bf6b35ebdd889`.
- Source: stock UniFi 7.5.15 `switchdrvr` `.data`, VA `0x1e7a394`, length word (`0x4000`) at `0x1e7a390` ([poe.md](../../docs/poe.md) 4).
- Version: the 12 chips report FW_REV `0x15` (v21) with this image loaded by stock (#24). The UDM `BCM59111_POETEC_VER_21_RELEASE.h` array was not compared here (`scripts/poe_fw_compare.py` does).
- Regenerate: `python3 scripts/extract_poe_fw.py <switchdrvr>` (checks length + sha256).
- Use: staged as `/lib/firmware/bcm59111-v21.bin` by `scripts/build_mainline.py`; `kernel/bcm59111` loads it only with `fw_load=1` and a chip reporting no running firmware.
- Decision to commit it: Dan 2026-11-19 (#8).
