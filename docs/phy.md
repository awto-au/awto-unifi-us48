# Copper PHYs: phylib drivers (#58)

Tags: C = from source/docs, B = board-proven before this change, U = unproven (board question).

## Layout
- Reconciled with main (phylink/sfp/led for xe, #133 PHY power): xe link = phylink; copper link = phylib only. `link_work`, `copper_poll`/`copper_link`, `phy_dbg` removed. `bcm_hr2_ge_phy_power/link` stay for u-boot only.
- `kernel/us48-cmic/us48_cmic_mdio.c`: one `mii_bus` per unit and CMIC MIIM bus 0 (`us48-u<unit>-int0` GPHYs, `us48-u<unit>-ext0` octal); clause 22 only. Ops = `bcm_cmic_miim_read/write` under `u->mdio`. `phy_mask` = addresses of the port table (hwdb-generated `us48_ports.h`).
- `kernel/us48-cmic/us48_cmic_phy.c`: two `phy_driver`s (linked into `us48_cmic.ko`, registered before any bus) + netdev glue (`adjust_link`, ethtool ops).
- No DT: the CMIC ports are not DT devices (unit 0 = plain platform device, unit 1 = PCI aux). PHY address comes from the generated port table, attach = `phy_connect_direct` at every `ndo_open`, `phy_stop` + `phy_disconnect` in `ndo_stop` (an attached PHY holds a module reference: rmmod needs all ports down). DT would only add a second copy of hwdb.
- Bus 1 (TSC, clause 45) stays with `us48_tsc_rd/wr`; not an MDIO bus.

## PHY IDs
| ID | Where | Driver name |
|---|---|---|
| `0x600d85b0` (mask `0xfffffff0`) | 16 integrated GPHYs per unit, internal bus 0 (docs/hardware.md, live scan 2026-11-12) | BCM56150 integrated GPHY |
| `0x600d845b` (matched as `0x600d8450`/`0xfffffff0`) | external octal, ext bus 0 addr 0x14-0x1b (docs/board.md, 2026-11-17) | BCM54282 octal GPHY |
- #48's "0x600d/0x85b0 = BCM54282" is the integrated GPHY; the external octal reads `0x845b`. Both drivers share one op table; only `config_init` differs.

## Who owns what
- Octal top reset (RDB 0x82b, once per octal, read-back): `bcm_hr2_ge_bringup`, first open of any lane of the GPORT block (`blk_ready`), as before. Kept there because it must follow the pwrsave octal power-up (`us48_pwr_octal_up`) and precede every lane's `config_init` (attach happens after bring-up).
- `ge->phy_managed` (set by `us48_ge_of`): bring-up skips the PHY steps; u-boot (`uboot-port`) leaves it 0 and keeps the old path.
- Lane init: `config_init` of the octal driver (the former `bcm54282_link_init`, same register order; GB_CTRL/BMCR now come from phylib).
- Integrated GPHY: no vendor init (B: only BMCR 0x1340 before).

## Power (#133 preserved)
- PHY `probe` (at bus registration): BMCR |= PDN (SDK `_bcm_port_probe` leaves ports disabled).
- Admin up: `phy_start` -> `resume`: PDN clear (`genphy_resume`) + AN restart if autoneg (= old 0x1340).
- Admin down: `phy_stop` -> link down (`adjust_link`: MAC off) -> `suspend`: BMCR |= PDN, RMW as SDK `phy_bcm542xx_enable_set` / `phy_54680_enable_set` (IEEE 802.3 22.2.4.1.5). Counter shadow saved first.
- Never-opened ports of a re-powered octal (`us48_pwr_octal_up`): `us48_copper_power(p, false)` = BMCR PDN on the unattached phydev.

## Link
- phylib polls once a second (`PHY_STATE_TIME`), as the former `LINK_POLL_MS`. `adjust_link` (phydev->lock, then `link_lock`): `bcm_hr2_ge_mac_up(speed)` / `mac_down`, log `geN: link up 1000M` / `link down` (scripts grep it).
- Lock order: phydev -> bus -> `u->mdio`, and phydev -> `link_lock`. `phy_start/stop/connect` run outside `link_lock`.
- Half-duplex modes are removed from `supported` (`bcm_hr2_ge_mac_up` sets no HD_ENA). Octal: `master_slave_set = MASTER_PREFERRED` = former GB_CTRL SWITCH_DEV (CTRL1000 b10).
- Never-opened ports: `ethtool geN` reads the unattached phydev (down, speed unknown); everything else needs the port opened once.

## Registers written (all C unless noted)
| What | Access | Source |
|---|---|---|
| top reset | RDB 0x82b = 0x8400 | driver-internals 12.4, OpenBCM phy542xx.c:2628 (in `bcm_hr2_ge_bringup`) |
| RDB enter/exit | exp 0x0f7e <- 0; RDB 0x87 <- 0x8000 | stock 0x10c9be4 = `BCM54XX_EXP_REG7E` / `BCM54XX_ACCESS_MODE_LEGACY_EN` in bcm-phy-lib |
| AUX_CTRL | reg 0x18 = 0x0c00, end 0x4c00 | OM step 2/5, OpenBCM phy542xx.c:2653 |
| DSP_TAP10 | RDB 0x125 = 0x011b | stock + OM |
| SUPER_ISOLATE | RDB 0x02a b5 = 0 | stock + OM |
| MODE_SEL | shadow 0x1f [2:1] = 0 | OM |
| SGMII_SLAVE / AUTO_DETECT_MEDIUM | shadow 0x15 b0, shadow 0x1e = b5 | OM step 5 |
| LED_TRAFFIC_EN | reg 0x10 b5 | OM step 5 |
| autoneg, forced speed, pause | genphy (regs 0, 4, 9) | IEEE 802.3 cl. 22/28/40 |
| PHY stats | regs 0x12-0x14 (bcm-phy-lib `bcm_phy_hw_stats`), MMD 7 0x803f LPI count | mainline |
- PHY LEDs untouched (LEDUP work, #14).

## Board-unproven (U)
- `ethtool --cable-test`: BCM54xx ECD = exp 0x0fc0..0x0fc5 (`bcm_phy_cable_test_*`, legacy-mode variant; our PHYs sit in legacy mode between RDB accesses). BCM54282 support not shown in any local source: gated by `us48_cmic.phy_ecd=1` (runtime, default 0 -> -EOPNOTSUPP). The test breaks the link (`BREAK_LINK`).
- EEE: phylib writes EEE_ADV (MMD 7.0x3c); the driver adds LPI feature b15/b14 of MMD 7 0x803d (OM `EEE_803D`; mainline `bcm_phy_set_eee` sets both). Gated by `us48_cmic.phy_eee=1` at insmod; default 0 = abilities hidden, state as before (OM turns EEE off). The GPORT MAC has no LPI handling, so an EEE link may not pass traffic; AutoGrEEEn (RDB 0x800 b0) untouched.
- Downshift tunable (`ethtool --set-phy-tunable downshift`): bcm-phy-lib shadow auxctl misc / SCR2, standard BCM54xx, not checked against the BCM54282.
- Wake-on-LAN: not implemented; mainline needs the PHY interrupt or a wake GPIO, none wired to anything we drive.
- phylib EEE/abilities reads use MMD indirect (regs 13/14) on C22: first read of `supported_eee` on both PHY types.
- `master_slave_set = PREFERRED` and half-duplex removal change the advertised abilities vs the old bring-up (octal: CTRL1000 0x0600 -> 0x0400 | 1000T-FD; reg 4 unchanged).

## Debug
- `us48_cmic.phy_dbg=1` (runtime, 0644): BMCR, BMSR x2, ANAR, LPA, CTRL1000, STAT1000, AUX19 + phylib state/link/aneg/speed/suspended/supported/advertising at connect, started, adjust_link, every 5th poll while down.
- state numbers: 0 DOWN, 1 READY, 2 HALTED, 3 ERROR, 4 UP, 5 RUNNING, 6 NOLINK, 7 CABLETEST.

## Test
- `scripts/phy_test.py` (dry run: `--dry-run`); needs `ethtool` on the board (static build in the rootfs of `wip/sensors-sfp-21`) and `CONFIG_ETHTOOL_NETLINK` for the cable test.
- Order on the board: insmod -> `phy_test.py --only show,bound,stats,aneg,forced,octal,check,admin` -> `--ecd` run -> `--eee` run (insmod `phy_eee=1`).
