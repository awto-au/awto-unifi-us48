# Copilot session handover — US-48 copper bring-up

Status at handover: 2026-11-18. Uncommitted worktree, RAM-booted bench session. This file is a
one-time handover note, not part of the permanent doc set — fold anything worth keeping into
`docs/switch-model.md`, `docs/gaps.md`, `docs/driver-internals.md`, then delete this file.

## What this session did

Goal: bring up all 48 US-48 copper ports and prove ASIC forwarding on native mainline Linux
(no OpenWrt, no flash writes). Work was RAM-boot/TFTP only throughout; nothing was flashed and
no SW1/SW2/gateway port was bounced or reconfigured.

1. **Fixed the octal-PHY RDB MIIM accessor.** The shared accessor was reading/writing Broadcom
   RDB registers without the documented enter/access/exit handshake, so earlier RDB reads were
   invalid junk. Added the enter (`reg 0x17<-0x0f7e`, `reg 0x15<-0`) / access (`reg 0x1e<-addr`,
   `reg 0x1f` data) / exit (`reg 0x1e<-0x0087`, `reg 0x1f<-0x8000`) sequence to
   `common/bcm-cmic/bcm_cmic_miim.c`. Verified on hardware: distinct live values for
   `POWER_MII_CTRL`, `DSP_TAP10`, `TOP_MISC_GLOBAL_RESET`.
2. **Corrected a wrong peer identification.** SW2 cage 2 was earlier assumed to be the US-48
   front-17 test peer (a historical "camera" cabling note). Read-only investigation on SW2 (login
   authorized, read-only only) showed cage 2 is actually an FS SFP-10G-T (AQR113C copper SFP+,
   RJ45, BASE-T) linked to the house gateway at 10G — not a 1G peer for front 17, and not safe to
   touch. Filed and updated [awto-au/awto-tl-st1008f#169](https://github.com/awto-au/awto-tl-st1008f/issues/169)
   with the correction. `scripts/cabling.json` and `docs/switch-model.md` were updated to mark the
   old front-17 "camera" mapping as historical/unverified.
3. **Found a plausible (unconfirmed) missing-init suspect**, then disproved it as the actual
   blocker. Source comparison (`tmp/ghidra/out/switchdrvr/switchdrvr.c`, stock
   `phy_qsgmii65_init` at `0x10a58b4`) showed native Linux never touches the QSGMII65 SerDes core
   at external MDIO `0x1c`, while GPORT2's QSGMII2 clock-valid bits were zero and PHY `0x14` had no
   link partner. Documented this as a candidate in `docs/driver-internals.md` §12.6 — but marked
   it explicitly unverified, because at that point there was no known-good peer to test against.
4. **Ran representative link tests with user-supplied patch cables** (per-family, not per-port, by
   agreement with the user — see "Test philosophy" below):
   - **Unit 0**: front 17 (`ge17`, octal PHY `0x14`, GPORT2 lane 0) ↔ front 8 (`ge8`, integrated
     GPHY `0x01`). **Result: both link at 1G.** Both PHY BMSR show link; both GPORT `MAC_MODE`
     reads are `0x2a` (LINK_STATUS set, 1G); `TOP_L1_RCVD_CLK_VALID_STATUS_1` went from `0` to
     `0x000f0281` (QC2 nibble now `0xf`). **This disproves the QSGMII65-init theory as a blocker**
     for this lane — link comes up fine without ever touching address `0x1c`.
   - **Unit 1**: front 41 (`ge41`, octal PHY `0x14` on `us48_cmic_xe`) ↔ front 32 (`ge32`,
     integrated GPHY `0x01`). **Result: both link at 1G.** Same signature: MAC_MODE `0x2a` both
     sides, clock-valid `0x000f0045`.
   - Together these two tests cover both copper PHY families (integrated GPHY, octal/QSGMII) on
     both chips (unit 0 AXI CMIC, unit 1 PCIe CMIC). No further copper cable moves were planned
     beyond this per the "test philosophy" agreed with the user.
   - **SFP+ check (read-only, no cable move):** `xe49` (front 49) shows `PCS_LIVE=0x0003`
     (sync + link, no remote fault) on an LC/SR optic; its far end is still unidentified (a
     previously FDB-verified SW1 `lan7` mapping no longer holds — `lan7` was down while `xe49` was
     up at last check). `xe50` (front 50) is down (`PCS_LIVE=0`), DAC to SW1 `lan3`, consistent
     with pre-existing notes (`docs/driver-internals.md` §13.7, issue #78: TX credit re-arm fixes
     the RF/PCS_LIVE flip but is a known separate issue, not touched this session).
5. **Updated test-scope philosophy per explicit user instruction** (2026-11-17): do not cable-test
   every one of the 48 jacks. One known-peer link test per distinct copper PHY family per chip is
   sufficient; one confirmed SFP+/SFP link per module/media type is sufficient. Test individual
   ports only when the PHY-ID inventory scan or actual use exposes a port-specific anomaly. This
   is recorded as the acceptance criteria in `docs/switch-model.md`.
6. User also said (2026-11-17, verbatim-paraphrased): serial/lab-only security concerns are out of
   scope for this private-lab RE work — don't spend time on security hardening unless asked. This
   is stored as a repository memory.

## Current verified state (as of last check, build running in RAM, uncommitted)

- All 48 copper netdevs (`ge1`-`ge48`) + `xe49`/`xe50` exist and register in Linux.
- Octal-PHY RDB access is correct and verified on hardware.
- **Link proven** (read-only scan + ethtool-equivalent dmesg lines) on:
  - unit 0: `ge17` ↔ `ge8` (1G, both up)
  - unit 1: `ge41` ↔ `ge32` (1G, both up)
  - `xe49`: PCS sync/link up (optic), far end unknown
  - `xe50`: down, far end is SW1 `lan3` DAC, known separate TX-credit issue (#78)
- **Link NOT yet individually tested** on the remaining ~44 copper ports — by design, per the
  user's "representative test" instruction above, not because of a known problem.
- **ASIC forwarding is NOT yet proven.** Every port is still in its own private CPU-only VLAN
  (`docs/switch-model.md` §"Known topology"). The link tests above only prove PHY/MAC link, not
  that the switch ASIC will forward frames between ports.
- Nothing has been committed to git this session (`git status --short` still shows the same
  modified files as when the session started, plus this handover file and new
  `docs/switch-model.md`). Unrelated pre-existing uncommitted changes from other work
  (`scripts/memtest_arm.c`, `.claude/`, `hwdb/board/ports.toml`, `docs/port-map.md`, etc.) are
  still in the worktree — do not discard them.

## In progress / interrupted when this handover was requested

I was mid-way through asking the user to nominate two **independent traffic-generating
endpoints** (not just the loop-back patch cables used above, which only prove link) so the next
step — a same-chip fixed-VLAN ASIC forwarding test — could be run. That question was never
answered; **no forwarding test has been attempted or configured.** This is the actual next step.

## Next steps (in order)

1. **Get two real traffic endpoints** (laptops/hosts, not just patch cables) on two front ports of
   the *same* chip (e.g. unit 0: one integrated-GPHY port + one octal port, reusing the
   `ge17`/`ge8` pair if convenient) that can send/receive pings or iperf traffic.
2. **Configure a shared, explicit test VLAN** per `docs/switch-model.md` §3: read back ingress
   membership, egress membership/untagged state, port VLAN IDs, STP state, egress enable, and the
   link bitmap for both test ports before and after. Do not assume VLAN 1 is already correctly
   configured on either chip. Disable the Linux CPU bridge for this test so success can't be
   masked by CPU-path forwarding.
3. **Prove same-chip hardware forwarding** first (both test ports on unit 0 or both on unit 1),
   then repeat **cross-chip over HiGig** (one port on unit 0, one on unit 1) only after same-chip
   is proven. Use switch counters/MIBs to confirm frames went through the ASIC, not just that a
   ping succeeded (which could still be via the CPU path if the private-VLAN isolation is
   accidentally bypassed).
4. Keep the CPU management path (`ge1`) reachable and verified after every config step.
5. Once fixed-VLAN hardware forwarding is proven on both chips and across HiGig, move to the
   control-plane design choice (DSA vs switchdev) per `docs/switch-model.md` §4 — not started.
6. Lower-priority follow-ups noted but not actioned this session:
   - Identify `xe49`'s actual current far end (SW1 `lan7` mapping no longer matches observed
     state) — read-only investigation only, do not touch SW1/SW2 ports.
   - `xe50` TX-credit re-arm fix for RF/PCS_LIVE (issue #78) — known, not part of this session's
     scope.
   - If any of the ~44 untested copper ports show an anomaly in later real use, test individually
     then; no blanket per-port test is planned.

## Key files touched this session

- `common/bcm-cmic/bcm_cmic_miim.c` — RDB enter/access/exit fix (the only functional kernel code
  change this session).
- `docs/switch-model.md` — new/updated: current bring-up status, representative-test philosophy
  and results, SFP+ status, acceptance criteria.
- `docs/driver-internals.md` §12.6 — QSGMII65-init gap note, now annotated as disproven-as-blocker
  for the tested lanes.
- `docs/gaps.md` — blocker #3 (octal/QSGMII2X link) and #4 (SFP+/TSC) updated to reflect current
  evidence.
- `scripts/cabling.json` — front-17 entry corrected from "camera" to historical/unverified; SFP+ 1
  entry updated with current `xe49` link state and far-end uncertainty.
- GitHub: [awto-au/awto-tl-st1008f#169](https://github.com/awto-au/awto-tl-st1008f/issues/169)
  (SW2 cage-2 identity correction, comments added).

## Things the next session should NOT do

- Do not install U-Boot on SW2, bounce SW2 `lan1`/`lan2`, or touch SW2 cages 3/4/7/8 (looped) —
  explicitly out of scope per the user.
- Do not touch SW1 ports — it's the in-service house uplink; read-only only.
- Do not write flash on the US-48 unit; RAM/TFTP boot only.
- Do not spend time on security hardening/review of this lab bench work unless explicitly asked
  (user instruction, 2026-11-17; see repository memory "task focus").
- Do not cable-test every one of the 48 copper jacks individually — one known-peer test per PHY
  family per chip is the agreed bar; see "Test philosophy" above.
