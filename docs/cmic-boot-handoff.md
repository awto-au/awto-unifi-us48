# CMIC boot handoff review (#114)

Reviewed 17 November 2026 against `main` at `91d37000d7e5c8c560561a7397476fc7273a706d`.
No board access, boot, reset, flash, module load/unload, or network reconfiguration was performed.

## Finding

**The cited log does not prove active DMA or memory corruption.** `CTRL=00000001` is TX direction,
with `DMA_EN` (bit 1) clear. `DESC=03101000` identifies awto-uboot ge1's TX descriptor region:
`GE_DMA_REGION (0x03100000) + TX_DCB_OFF (0x1000)`. It is not an activity indication.

The source supports a stale-pointer explanation: `bcm_hr2_tx_reap()` clears EN after DCB DONE,
clears completion status, and leaves DESC intact. Before `5dfe7d7`, the shared abort also left DESC
intact. The old Linux `stock left` message printed both channels unconditionally, not just active
ones. It ran in `us48_probe_common()` before that function's abort, **after** the platform caller's
conditional cold init / DMA init. Thus even that control value was not necessarily an untouched
bootloader value. The word `stock` did not identify the actual bootloader.

This makes stale register state the best source-supported explanation for the reported pair, but
does not prove ACTIVE was clear. The full original console log, ch1 state, DMA_STAT, exact running
image revisions and timestamps are unavailable here. No causal link to the a1 overclock failures
in #17 is established. Do not close #114 as hardware-verified, and do not add another shutdown
sequence based on this log alone.

## Evidence and chronology

| Evidence | What it establishes | What it does not establish |
|---|---|---|
| [#15 log review](https://github.com/awto-au/awto-unifi-us48/issues/15#issuecomment-6894473862), 15 November: kernel `7.4.0-rc1-dbg+ #23`, a1 1000 MHz, RAM boot | Reported `stock left ch0 CTRL 00000001 DESC 03101000` | No STAT or ch1 sample; the claim that channel 0 was enabled misdecodes bit 0 |
| [5dfe7d7](https://github.com/awto-au/awto-unifi-us48/commit/5dfe7d72d46546909e32939cb6f795948db8a42a), 15 November 23:50 AEDT | Added U-Boot `.remove`, ACTIVE_DMA flags on both drivers, shared DESC clear, Linux pre-reset abort | Commit said built, not board-tested; an older running image is possible |
| [#78 follow-up](https://github.com/awto-au/awto-unifi-us48/issues/78#issuecomment-6894576699) | Already explicitly corrects the earlier log: `CTRL=1` had EN clear | Its handoff test was a plan, not captured before/after evidence |
| [#104 results](https://github.com/awto-au/awto-unifi-us48/issues/104#issuecomment-6932460364), 17 November | Reports flashed U-Boot CRC `b0c0139c`, 850952 B; kernel CRC `a199abe2`, 3826457 B; successful cold boots and quiesce + bootm | Successful networking / no WARN is not a register-level handoff proof; those CRCs are historical, not verified live here |
| [#114](https://github.com/awto-au/awto-unifi-us48/issues/114), created 17 November | Repeats the older #15 interpretation | Not a fresh observation of the latest code |

The full `tmp/logs/tio-us48.log` cited by #15 is not in this checkout. The source history and issue
excerpts above are the available historical evidence; no log contents have been reconstructed.

## Current execution order

1. U-Boot probe sets up the private structure and uncached descriptor/buffer mappings. It does
   not initialize or arm CMIC DMA. ge1 uses `0x03100000`, xe uses `0x03000000`.
2. `us48_eth_start()` rejects xe when PCIe is down; does the required chip setup and
   `bcm_hr2_dma_init()` (abort RX then TX, retain endian bits, ch0 TX/ch1 RX). Bring-up sets `p->up`
   before RX can be armed. L2/link failures return without arming new RX DMA. A successful start
   restores quiesced VLAN membership, arms RX, and drains the existing backlog.
3. TX completion clears EN but retains DESC. The normal U-Boot network loop calls `eth_halt()`
   on ping/TFTP success, interrupt and relevant failure/retry paths. `eth_halt()` stops the
   current running device, and `us48_eth_stop()` aborts both channels for a started device.
   `p->up` remains true after stop, so removal can stop it again.
4. `us48boot` reads/validates the kernel slot and invokes ordinary `bootm 0x01000000`. A direct
   `bootm` and the RAM chain-load path use the same ARM handoff. In pinned upstream U-Boot
   **v2026.07**, `do_bootm_linux()` -> `boot_jump_linux()` -> `bootm_final()` calls
   `board_quiesce_devices()` then `dm_remove_devices_active()`, before cache cleanup and kernel
   entry. The `Starting kernel` line is printed **before** removal, not proof that removal finished.
5. DM removal walks activated devices in two passes (non-vital first, then remaining active).
   Both CMIC registrations have `DM_FLAG_ACTIVE_DMA` and `.remove = us48_eth_remove`.
   With `DM_DEVICE_REMOVE` enabled (upstream default y, not disabled by our defconfig), this
   reaches both activated devices, independent of the current `ethact`.
6. `us48_eth_remove()` restores any quiesced VLAN membership, then, if `p->up`, calls
   `us48_eth_dma_off()` -> shared abort for ch1 and ch0. The abort checks ACTIVE, requests
   EN|ABORT only if active, polls up to 10000 us, reports timeout, clears EN|ABORT, clears DESC,
   and clears completion status. Direction/endian bits remain. **A timeout does not stop boot
   or return an error. Zero CTRL/DESC alone must not be called a successful abort.**
7. Linux does not run this check at kernel entry. Initramfs `S04us48-net` loads `us48_cmic`
   (unit0), then the `us48_pci` parent, then `us48_cmic_xe` (unit1). Unit0 platform probe maps
   AXI and calls `us48_chip_cold_init()`; unit1's aux probe checks PCIe, requests TSC firmware,
   and calls the same function. The parent enables PCI bus mastering before aux probe. Its
   suspect-CMIC marker can suppress the CMIC aux device entirely; no unit1 probe log then
   means no unit1 takeover check, not proof of safety.
8. `us48_chip_cold_init()` checks/aborts both inherited channels **before** forced TOP/CPS resets,
   then reinitializes DMA. Only afterward does `us48_probe_common()` allocate its Linux rings
   and register interfaces. RX/TX can start on interface open. This protects takeover, but
   cannot undo corruption that might already have happened before probe.

Upstream files inspected at the build's pinned tag:
[ARM bootm](https://github.com/u-boot/u-boot/blob/v2026.07/arch/arm/lib/bootm.c),
[bootm final / load order](https://github.com/u-boot/u-boot/blob/v2026.07/boot/bootm.c),
[DM root](https://github.com/u-boot/u-boot/blob/v2026.07/drivers/core/root.c),
[device removal](https://github.com/u-boot/u-boot/blob/v2026.07/drivers/core/device-remove.c),
[removal configuration](https://github.com/u-boot/u-boot/blob/v2026.07/drivers/core/Kconfig),
[Ethernet halt](https://github.com/u-boot/u-boot/blob/v2026.07/net/eth-uclass.c),
[network loop](https://github.com/u-boot/u-boot/blob/v2026.07/net/net.c).
The generated configuration and binary on the bench have not been inspected.

### Paths requiring hardware evidence

- A never-started or failed-before-bring-up device has `p->up=false`; removal deliberately
  skips its MMIO. Its own RX has not been armed by this driver, but inherited state from an
  earlier stage or manual `cmic w` commands is not thereby proven idle. Unprobed devices
  do not get DM removal at all. Cold reset evidence in #46 is useful but is not a warm-path proof.
- Removal may be absent in an older binary, disabled configuration or a nonstandard `go`
  jump; `go` is not the supported Linux path. DM removal can also fail earlier in its traversal.
- DMA removal is late in `bootm`, after image loading/decompression. Ordinary completed
  network commands already halt their device, but last-stage removal alone cannot prove the
  earlier load interval safe if a nonstandard command left DMA running.
- A unit1 PCIe link failure makes speculative BAR reads hazardous. No new unconditional
  MMIO was added for unstarted devices. A channel abort timeout remains an unresolved hardware
  failure and must retain its error log; do not retry/reset experimentally without approval.
- This review covers CMC0 channels 0 and 1 used by these drivers, not other CMIC DMA engines,
  CMCs or manually armed channels 2/3.

## Minimum register evidence

Use each unit's mapped CMIC base, not a guessed physical address for unit1. Facts come from
`regdb/hr2_regs.py` (`CMIC_CMC0_CHn_DMA_CTRL`, `CMIC_CMC0_DMA_DESCn`, `CMIC_CMC0_DMA_STAT`).

| Register | Offset from CMIC base | Required interpretation |
|---|---|---|
| ch0 DMA_CTRL | `0x31140` | DIRECTION bit 0; EN bit 1 (`0x2`); ABORT bit 2 (`0x4`) |
| ch1 DMA_CTRL | `0x31144` | Same fields; normal RX direction bit 0 clear |
| ch0 DMA_DESC | `0x31158` | Programmed descriptor address, not proof it is being fetched |
| ch1 DMA_DESC | `0x3115c` | Same for RX |
| DMA_STAT, sampled before and after the above | `0x31150` | ch0 ACTIVE bit 8 (`0x100`), ch1 ACTIVE bit 9 (`0x200`); save full raw status including errors/completion |

| EN | ACTIVE | Interpretation at the sampled instant |
|---|---|---|
| 0 | 0 | No enabled/active channel observed. Nonzero DESC may be stale; does not satisfy the current cleanup's DESC=0 postcondition |
| 1 | 0 | Enabled but not active when sampled: possibly completed or poised to run; not a clean handoff |
| 0 | 1 | Still active/draining or an inconsistent sample; not a proven stop |
| 1 | 1 | DMA active; an old RAM pointer is an unsafe handoff candidate |

After successful current cleanup: EN=0, ABORT=0, both ACTIVE bits=0, both DESC=0, and no timeout.
CTRL need not be zero (TX direction and endian bits can remain). DONE is historical completion,
not ACTIVE. Samples are not atomic; changing before/after STAT invalidates a simple stable-state
interpretation. Even identical samples cannot exclude activity between observation points.

Capture unit identity, image SHA/CRC, generated U-Boot `.config` (`CONFIG_DM_DEVICE_REMOVE=y`),
kernel/module build IDs, command sequence and log ordering. Required observation points:
U-Boot removal before/after, earliest Linux observation before any abort/reset, and after abort.
Probe-after-abort alone cannot establish bootloader safety. Where early-boot corruption remains a
specific hypothesis, a separately reviewed earliest-kernel snapshot is needed; this patch does not
map unit1 from early ARM code or claim probe logging covers the decompressor interval.

## Change and offline validation

No DMA shutdown, reset, allocation, timeout or networking behavior was changed.

- `bcm_hr2_dma_read_state()` reads STAT, both CTRL/DESC pairs, then STAT. No writes, waits,
  completion clearing, descriptor-memory reads or active-state assumptions.
- U-Boot `us48_handoff_diag=1` (volatile env, default off) prints `remove-before` /
  `remove-after`, including device identity and `up`; `up=0` prints `MMIO skipped`.
- Linux `handoff_diag=1` on **us48_cmic** module load (default off, 0444) prints both channels
  at `probe-before-abort` / `probe-after-abort`, including unit identity. The same parameter
  covers unit1 via the shared cold-init function. All reads precede each print.
- Optional logging adds serial time and can change timing when enabled. Default-off does no
  additional MMIO. Readback is diagnostic evidence, not a new stop guarantee.
- `tests/test_cmic_handoff.py` checks register fields against the generated regdb, both DM
  registrations, start/abort/probe ordering and opt-in/read-only logging. It compiles actual
  extracted C functions with `cc -std=c11 -Wall -Wextra -Werror` and fake MMIO. Scenarios:
  idle stale pointer; enabled/inactive; active abort; stuck-active timeout; bracketed snapshot;
  TX completion retaining DESC; removal of both channels; diagnostic guards. Abort cases run
  for both channels. The timeout fixture explicitly checks ACTIVE remains set despite DESC=0.
- Wired into `python3 scripts/dev.py selftest`.

Passed in this review: host regression suite (6 unittest methods, 8 C scenarios), full dev.py
selftest (including script help guards), and `git diff --check`. Shared-core staging/check and
Python compilation are also part of the final offline validation. These checks do not emulate
PCIe ordering, hardware DMA transactions or the whole U-Boot DM traversal.

Full ARM U-Boot/Linux builds are still required on the build host: this environment has no
`arm-linux-gnu-gcc`, kernel/U-Boot source-store worktrees, or generated target `.config`.
Do not deploy until both builds and image-size/CRC checks have passed.

## Remaining bench verification (not executed, approval required)

First obtain the live state and a recoverable serial session; do not assume the last #104 state
is still current. Prepare instrumented builds locally and record SHA256, CRC32, byte size and
target `.config` before proposing a RAM load. Enable Linux diagnostics in the **temporary
initramfs** by changing only the unit0 load line to:

```sh
insmod $M/us48_cmic.ko unit0=1 handoff_diag=1
```

Do not run this against an already loaded live driver. A kernel command-line module option
alone is insufficient for this explicit-insmod initramfs. No startup script was changed here.

At an already confirmed awto-uboot prompt, these are the exact read-only register commands for
unit0; repeat STAT around the four-word samples rather than reading only CTRL:

```text
cmic unit 0
cmic r 0x31150
cmic r 0x31140 2
cmic r 0x31158 2
cmic r 0x31150
```

For unit1 use `cmic unit 1` first **only after confirming the link and safe CMIC access**. The
selector rejects a down link, but a link can fail later; stale MMIO access can hang a1 (#76/#98).
Always restore `cmic unit 0`. A prompt snapshot is not a post-removal snapshot.

The first proposed disruptive test should be RAM-only, at normal CPU frequency, preserving the
flashed U-Boot/kernel slots. Before any such test, present a separate approval request with:

1. Exact host build/load/console commands, actual artifact filenames, addresses, sizes and CRCs;
   the current board state and serial-console owner; whether it involves one or two `bootm`s.
2. Exact volatile setup in the instrumented U-Boot: `setenv us48_handoff_diag 1`; the chosen
   ge1 or xe ping/TFTP command followed by its actual RAM-load and `bootm` command. `us48boot`
   uses the flashed kernel, so it would not use a new temporary initramfs without a flash.
3. Expected `remove-after` and `probe-before-abort` evidence for both channels and units; stop
   on ACTIVE/EN/ABORT left set, nonzero post-remove DESC, abort timeout or missing phase logs.
4. Risks: loss of management/PoE services during boot, hang/reset, timing changes due to logs,
   PCIe-access hang, and a watchdog reset returning to the normal flash chain.
5. Recovery: leave flash/env untouched; use the serial autoboot window to stop at the existing
   awto-uboot prompt. A separately approved power cycle returns to the flashed chain. If it
   cannot reach a prompt, the documented fallback is JTAG/SPI recovery with verified backups.
   The front-panel reset button is not a reliable recovery path after autochain (#63).

Then ask for approval and wait. No boot/reset/flash/reload commands were executed or authorized
by this review. Actual RAM-load commands are intentionally not invented without built artifacts
and a confirmed live state. Repeat cold/no-net, ge1, unit1 (when safe), and both-used sequences
under separately approved operations; do not infer unit1 safety from ge1 success.
