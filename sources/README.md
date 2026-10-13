# sources/

Downloaded third-party documents. PDFs are gitignored (`sources/**/*.pdf`); this manifest is the tracked part. Fetched with playwrong (real browser).

| File | URL | Revision | Good copy |
|---|---|---|---|
| `bcm59111-pb.pdf` | https://docs.broadcom.com/doc/12358228 | Broadcom 59111-PB01-R, 2011-11-04 | 2 pages, 1000614 B, sha256 `9313f18c…2e82d93d`; contains "IEEE 802.3at-2009-compliant" and "Firmware download capability via BSC" |
| `ubnt-unifi-poe-switch-ds.pdf` | https://dl.ubnt.com/datasheets/unifi/UniFi_PoE_Switch.pdf | UniFi PoE Switch datasheet (US-8-150W … US-48-750W) | 12 pages, 3962155 B, sha256 `716a8f9e…96c72df36`; contains "24V Passive PoE Voltage Range" (6×) and a US-48-500W spec page |

- No full BCM59111 datasheet found (Broadcom NDA). Register names come from Ubiquiti's GPL UDM kernel driver (see `tmp/agent-reports/poe-report.md` / PoE issues).
