# hwdb/firmware

## bcm59111-v21.bin

- BCM59111 PoE PSE firmware v21, 16384 B, sha256 `31496a90bead4c7feb87994ad30d626b70d07e112d79d60b587bf6b35ebdd889`.
- Primary source (GPLv2): Ubiquiti UDM kernel `drivers/net/power/BCM59111_POETEC_VER_21_RELEASE.h` (86426 B C array, bin2c; `#define BCM59111_POETEC_VER 21`, `SZ_..._RELEASE 16384`). Mirror: https://github.com/fabianishere/udm-kernel/tree/master/drivers/net/power, last change to the file commit `977c11883bd3fb422f6c0f68f86d2df92e7bb37c`. Parsed array == this file byte for byte (checked 2026-11-19).
- Cross-check: stock UniFi 7.5.15 `switchdrvr` `.data`, VA `0x1e7a394`, length word (`0x4000`) at `0x1e7a390` ([poe.md](../../docs/poe.md) 4). Identical to the GPL array.
- Version: the 12 chips report FW_REV `0x15` (v21) with this image loaded by stock (#24); the image itself writes `0x15` to reg `0x41` ([poe-fw.md](../../docs/poe-fw.md)).
- Regenerate from stock: `python3 scripts/extract_poe_fw.py <switchdrvr>` (checks length + sha256).
- Use: staged as `/lib/firmware/bcm59111-v21.bin` by `scripts/build_mainline.py`; `kernel/bcm59111` loads it only with `fw_load=1` and a chip reporting no running firmware.
- Decision to commit it: Dan 2026-11-19 (#8).

## bcm59111-meraki.bin

- BCM59111 PoE PSE firmware, Meraki MX64/MX65 GPL release copy (writes FW_REV `0x12` = v18), 16384 B, sha256 `f6f08472ac522912304a563b9167128b8f88748b227714b3dbbaa996b3597d99`.
- Source: https://github.com/quenode/mx65w-poe-openwrt `driver/bcm59111.bin` (repo GPL-2.0; file last changed in commit `b2322f00d6c57ec4530a1b935b3679a3cdf2949c`, 2026-10-06; repo HEAD `8a140017ebbaf7563267cbb7c3d327543d5f1cd6` at fetch 2026-11-19). Fetched with `gh api repos/quenode/mx65w-poe-openwrt/contents/driver/bcm59111.bin`.
- Not for loading on the US-48: reference copy for analysis. Differs from v21 in 74 % of bytes (other build, same source structure): [poe-fw.md](../../docs/poe-fw.md) section 6.
- Kept next to v21 on purpose: Dan 2026-11-19.
- Framing: quenode loads it with one `0x80` header write then SMBus block writes (count byte, 30 B); UDM / stock write `0x80` + BE16 offset + 32 B per block ([poe-pse.md](../../docs/poe-pse.md)). The payload is the same 16 KiB image either way.
