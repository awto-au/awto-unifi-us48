#!/usr/bin/env python3
"""clock_sweep — CPU clock sweep in 50 MHz steps with DDR/SRAM/SPI/PCI/net/die-temp checks (#17, #21, #55).

  clock_sweep.py --plan [--start 1400 --stop 2000 --step 50]   print ndiv/VCO per step, no board access
  clock_sweep.py --build                                        cross-compile scripts/memtest_arm.c
  clock_sweep.py [--start 1400] [--stop 2000] [--step 50] [--soak 300] [--mib 64]

Per step: uboot-1 prompt -> reference reads at 400 MHz -> SP805 -> relock (clock_trial.switch_cpu) ->
U-Boot DDR/SRAM/SPI checks -> bootm -> Linux checks (sched_clock, timer, memtest_arm, SPI md5, PCI, ping,
die temp both units) -> soak. Stops at the first failed check or at a temperature limit.
RAM-only: no flash writes, no saveenv; uboot-1 reprograms 400 MHz on every reset.
Log tmp/logs/clock_sweep.log, table tmp/logs/clock_sweep_results.md. Facts: docs/clock-audit.md.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import select
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import clock_trial as ct  # noqa: E402
import console  # noqa: E402
import dev  # noqa: E402

LOGF = dev.LOGS / "clock_sweep.log"
ct.LOGF = LOGF  # one log for the whole sweep, clock_trial helpers included
RESULTS = dev.LOGS / "clock_sweep_results.md"
MEMTEST_SRC = dev.REPO / "scripts/memtest_arm.c"
MEMTEST_BIN = dev.TMP / "memtest/memtest_arm"
CC = dev.REPO / ("tmp/openwrt-build/staging_dir/toolchain-arm_cortex-a9_gcc-14.4.0_musl_eabi/bin/"
                 "arm-openwrt-linux-muslgnueabi-gcc")
BACKUP = dev.REPO / "private/backups/20261012T055324Z/full-flash.bin"  # verified full-flash backup (ddr.md)

# BCM56150 DS: TJ max 125 C, steady state 110 C (DS lines 5957-5959, 8471). Abort 90 C (Dan, 2026-11-14):
# PVTMON accuracy unknown, idle die reads ~76 C. Board hwmon (LM75) abort = clock_trial.TEMP_ABORT_C (70 C).
DIE_ABORT_C = 90
VCO_HIGH_HIGH_MHZ = 3100  # generic iProc PLL loop-filter band edge (clk-iproc-pll.c); logged, not a stop

# PVTMON (TOP block 16) via S-channel on CMC1: the Linux drivers own CMC0 (us48_cmic.h SCHAN_CTRL 0x31000).
CMC1 = 0x32000
UNIT_BASE = {0: 0x48000000, 1: 0x0A000000}  # unit 0 AXI CMIC; unit 1 PCIe BAR2 (boot logs)
TOP_BLK = 16
PEAK: dict[int, float] = {}  # unit -> hottest PEAK_TEMP_DATA reading (since PVTMON init) as C
PVT_CTRL0, PVT_CTRL1, PVT_RESULT = 0x02050000, 0x02050400, 0x02050800
PVT_CTRL0_STOCK = 0x001D2003  # stock switchdrvr write (tmp/schan_stock_writes.txt), = OpenMDK fields
RING_MAP_16_23 = 0x100A0      # nibble 0 = ring of block 16 (TOP); stock/OpenMDK value 0x00005004

# U-Boot test region: kernel image sits at LOAD_ADDR 0x01000000 (<= 14 MiB), uboot-1 relocates near the
# top of the 256 MiB bank. 0x02000000..0x05ffffff is free (I, uboot.md §6 memory map).
UB_TEST = 0x02000000
UB_TEST_LEN = 64 << 20
UB_CHUNK = 4 << 20  # keeps each command short: uboot-1 private WDT is 30 s x 400 / MHz (6 s at 2000)
SRAM = 0x1B000000
SRAM_LEN = 0x4000
OPENWRT_FLASH = dev.KERNEL0_OFF + dev.SLOTS["openwrt"][0]
SP805_PET = f"mw.l {dev.SP805:x} {dev.SP805_LOAD:x}"  # WDTLOAD write restarts the count (SP805 TRM)

# memtest_arm: stall = no output line for 60 s (awto-dan#53); first run sets the cap to 3x its duration.
MEMTEST_STALL_S = 60
MEMTEST_FIRST_CAP_S = 900


def log(msg: str) -> None:
    ct.log(msg)


def plan(start: int, stop: int, step: int) -> list[int]:
    steps = []
    for mhz in range(start, stop + 1, step):
        if 2 * mhz >= ct.VCO_MAX_MHZ:
            log(f"{mhz} MHz: VCO {2 * mhz} >= VCO_MAX {ct.VCO_MAX_MHZ}: sweep ends at {mhz - step}")
            break
        steps.append(mhz)
    return steps


def show_plan(steps: list[int]) -> None:
    print("| MHz | ndiv | pdiv | ch1 mdiv | VCO MHz | PLLARMA (resetb) | PERIPHCLK | note |")
    print("|---|---|---|---|---|---|---|---|")
    for mhz in steps:
        n = ct.relock_ndiv(mhz)
        note = ("in [DL] table" if mhz in (1000, 1100, 1200) else
                "run 2026-11-14 (#17)" if mhz == 1400 else
                "VCO >= 3100: generic iProc PLL HIGH_HIGH band" if 2 * mhz >= VCO_HIGH_HIGH_MHZ else "")
        print(f"| {mhz} | {n} | 1 | 2 | {2 * mhz} | `{0x10000000 | n << 8 | 1 << 24 | 1:#010x}` | {mhz // 2} | {note} |")


def build() -> int:
    MEMTEST_BIN.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([str(CC), "-static", "-O2", "-s", "-Wall", "-Wextra", "-o", str(MEMTEST_BIN),
                        str(MEMTEST_SRC)], capture_output=True, text=True, timeout=120,
                       env={"PATH": "/usr/bin:/bin", "STAGING_DIR": str(dev.TMP / "memtest")})
    log(f"build memtest_arm rc={r.returncode} {r.stdout}{r.stderr}".strip())
    if r.returncode == 0:
        log(f"memtest_arm {MEMTEST_BIN.stat().st_size} B")
    return r.returncode


# ---------------------------------------------------------------- S-channel (PVTMON) command builders

def schan_cmds(unit: int, op: str, addr: int, data: int | None = None) -> list[tuple[str, int, int | None]]:
    """(kind, abs address, value) list: kind 'w' = write, 'r' = read. Order = driver-internals.md §1."""
    c = UNIT_BASE[unit] + CMC1
    hdr = (0x0D if op == "w" else 0x0B) << 26 | TOP_BLK << 20 | 4 << 7
    seq = [("w", c + 0xC, hdr), ("w", c + 0x10, addr)]
    if op == "w":
        seq.append(("w", c + 0x14, data))
    return seq + [("w", c, 1), ("r", c, None), ("r", c + 0xC, None), ("r", c + 0x10, None), ("w", c, 0)]


def temp_c(result: int) -> float:
    """OpenBCM soc_hu2_temperature_monitor_get: (4180000 - 5556 x code) / 1000 in 0.1 C; NS DT <(-556) 418000>."""
    return (4180000 - 5556 * (result & 0x3FF)) / 10000


def linux_devmem(seq) -> list[int]:
    sh = "; ".join(f"devmem {a:#x} 32 {v:#x}" if k == "w" else f"devmem {a:#x}" for k, a, v in seq)
    return [int(x, 16) for x in re.findall(r"0x[0-9A-Fa-f]{8}", ct.ssh(sh, 20))]


def linux_schan(unit: int, op: str, addr: int, data: int | None = None) -> int | None:
    vals = linux_devmem(schan_cmds(unit, op, addr, data))
    if len(vals) != 3:
        log(f"schan u{unit} {op} {addr:#x}: bad reply {vals}")
        return None
    ctrl, ack, word = vals
    if not ctrl & 2 or ctrl & 0x700000:
        log(f"schan u{unit} {op} {addr:#x}: CTRL={ctrl:#x} (DONE b1 clear or NACK/TIMEOUT/SER b20-22)")
        return None
    return word if op == "r" else ack


def die_temps_linux() -> dict[int, float | None]:
    out: dict[int, float | None] = {}
    for unit in (0, 1):
        ring = linux_devmem([("r", UNIT_BASE[unit] + RING_MAP_16_23, None)])
        if not ring or ring[0] & 0xF != 4:
            log(f"u{unit} SBUS ring map 16_23 = {ring}: TOP not routed (stock 0x00005004), no die temp")
            out[unit] = None
            continue
        c1 = linux_schan(unit, "r", PVT_CTRL1)
        if c1 is not None and not c1 & 1 << 31:
            log(f"u{unit} PVTMON in reset (CTRL_1={c1:#x}): stock init CTRL_0={PVT_CTRL0_STOCK:#x}, CTRL_1 0 -> 1<<31")
            linux_schan(unit, "w", PVT_CTRL0, PVT_CTRL0_STOCK)
            linux_schan(unit, "w", PVT_CTRL1, 0)
            linux_schan(unit, "w", PVT_CTRL1, 1 << 31)
        r = linux_schan(unit, "r", PVT_RESULT)
        out[unit] = temp_c(r) if r is not None else None
        if r is not None:
            PEAK[unit] = temp_c(r >> 22)
        log(f"u{unit} PVTMON RESULT={r if r is None else hex(r)} -> {out[unit]} C (peak code {r and (r >> 22) & 0x3ff})")
    return out


# ---------------------------------------------------------------- U-Boot side

def ub(s, line: str, budget: float) -> str:
    """Send one command, return everything printed before the next prompt (dev._uboot_cmd drops it)."""
    s.buf = b""
    s.send_line(line)
    t0 = time.monotonic()
    while dev.PROMPT not in s.buf:
        if time.monotonic() - t0 > budget:
            raise RuntimeError(f"no prompt within {budget:.1f} s after {line!r}: tail {s.buf[-120:]!r}")
        s._pump(0.2)
    i = s.buf.index(dev.PROMPT)
    out, s.buf = s.buf[:i].decode(errors="replace"), s.buf[i + len(dev.PROMPT):]
    log(f"> {line}\n{out.strip()}")
    return out


def ub_crc(s, addr: int, length: int, budget: float) -> str:
    m = re.search(r"==>\s*([0-9a-fA-F]{8})", ub(s, f"crc32 {addr:x} {length:x}", budget))
    return m.group(1).lower() if m else ""


def host_crc(pattern: bytes, length: int) -> str:
    return f"{zlib.crc32(pattern * (length // len(pattern))) & 0xFFFFFFFF:08x}"


def uboot_refs(s, budget: float) -> dict[str, str]:
    """At 400 MHz before the switch: flash reads through MSPI (sf read) and the BSPI window."""
    ub(s, "sf probe 0", dev.SF_BUDGET_S)
    ub(s, f"sf read {UB_TEST + UB_TEST_LEN:x} {OPENWRT_FLASH:x} 100000", dev.SF_BUDGET_S)
    return {"sf": ub_crc(s, UB_TEST + UB_TEST_LEN, 0x100000, budget),
            "bspi": ub_crc(s, 0x1C000000 + OPENWRT_FLASH, 0x100000, budget)}


def uboot_checks(s, refs: dict[str, str], budget: float) -> dict[str, str]:
    res: dict[str, str] = {}
    bad = 0
    for pat in (0x00000000, 0xFFFFFFFF, 0x55555555, 0xAAAAAAAA):
        exp = host_crc(struct.pack("<I", pat), UB_CHUNK)
        for off in range(0, UB_TEST_LEN, UB_CHUNK):
            ub(s, SP805_PET, ct.CMD_S)
            ub(s, f"mw.l {UB_TEST + off:x} {pat:x} {UB_CHUNK // 4:x}", budget)
            got = ub_crc(s, UB_TEST + off, UB_CHUNK, budget)
            bad += got != exp
    res["ub_ddr_const"] = "ok" if not bad else f"FAIL {bad} chunks"
    blk = 1 << 20  # walking ones: 1 MiB of 1 << k for k = 0..31, one CRC per 4 MiB
    for k in range(32):
        if k % 8 == 0:
            ub(s, SP805_PET, ct.CMD_S)
        ub(s, f"mw.l {UB_TEST + k * blk:x} {1 << k:x} {blk // 4:x}", budget)
    wbad = 0
    ub(s, SP805_PET, ct.CMD_S)
    for c in range(0, 32 * blk, UB_CHUNK):
        data = b"".join(struct.pack("<I", 1 << k) * (blk // 4) for k in range(c // blk, (c + UB_CHUNK) // blk))
        wbad += ub_crc(s, UB_TEST + c, UB_CHUNK, budget) != f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"
    res["ub_ddr_walk1"] = "ok" if not wbad else f"FAIL {wbad} chunks"
    half = 16 << 20  # copy + compare: read and write paths together
    ub(s, SP805_PET, ct.CMD_S)
    ub(s, f"cp.l {UB_TEST:x} {UB_TEST + half:x} {UB_CHUNK // 4:x}", budget)
    out = ub(s, f"cmp.l {UB_TEST:x} {UB_TEST + half:x} {UB_CHUNK // 4:x}", budget)
    res["ub_ddr_cmp"] = "ok" if re.search(rf"Total of {UB_CHUNK // 4} word", out) else "FAIL"
    sbad = 0
    for pat in (0xA5A5A5A5, 0x5A5A5A5A):
        ub(s, f"mw.l {SRAM:x} {pat:x} {SRAM_LEN // 4:x}", ct.CMD_S)
        sbad += ub_crc(s, SRAM, SRAM_LEN, ct.CMD_S) != host_crc(struct.pack("<I", pat), SRAM_LEN)
    res["ub_sram"] = "ok" if not sbad else f"FAIL {sbad}"
    ub(s, SP805_PET, ct.CMD_S)
    ub(s, f"sf read {UB_TEST + UB_TEST_LEN:x} {OPENWRT_FLASH:x} 100000", dev.SF_BUDGET_S)
    res["ub_spi_sf"] = "ok" if ub_crc(s, UB_TEST + UB_TEST_LEN, 0x100000, budget) == refs["sf"] else "FAIL"
    res["ub_spi_bspi"] = "ok" if ub_crc(s, 0x1C000000 + OPENWRT_FLASH, 0x100000, budget) == refs["bspi"] else "FAIL"
    return res


# ---------------------------------------------------------------- Linux side

def ssh_stream(cmdline: str, stall_s: float, cap_s: float) -> tuple[int | None, str]:
    """Run over ssh; kill on stall (no line for stall_s) or cap. Returns (rc or None, output)."""
    p = subprocess.Popen(ct.SSH + [cmdline], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out, t0, last = [], time.monotonic(), time.monotonic()
    while True:
        r, _, _ = select.select([p.stdout], [], [], 1.0)
        if r:
            line = p.stdout.readline()
            if not line:
                break
            out.append(line)
            log("  " + line.rstrip())
            last = time.monotonic()
        now = time.monotonic()
        if now - last > stall_s or now - t0 > cap_s:
            p.kill()
            log(f"ssh {cmdline!r}: killed, {'stall' if now - last > stall_s else 'cap'} "
                f"(silent {now - last:.0f} s, elapsed {now - t0:.0f} s, limits {stall_s}/{cap_s:.0f} s), "
                f"last line {out[-1].strip() if out else None!r}")
            return None, "".join(out)
    return p.wait(), "".join(out)


def wait_ssh() -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < ct.SSH_UP_S:
        try:
            if "ok" in ct.ssh("echo ok", 15):
                log(f"ssh up after {time.monotonic() - t0:.0f} s")
                return True
        except subprocess.TimeoutExpired:
            pass
        ct.wait(5)
    log(f"ssh not up after {ct.SSH_UP_S} s")
    return False


def mtd_md5_ref() -> dict[str, str]:
    """u-boot and shmoo are never written (CLAUDE.md), so the backup bytes are the reference."""
    img = BACKUP.read_bytes() if BACKUP.exists() else b""
    return {"u-boot": hashlib.md5(img[0:0xC0000]).hexdigest(),
            "shmoo": hashlib.md5(img[0xD0000:0xE0000]).hexdigest()} if img else {}


CPU_PASSES = 20000  # ~5 s at 400 MHz, ~1.3 s at 1550; host-timed (board timers derive from the same PLL)
BASE400 = [None]    # cpu_bench_s at 400 MHz, set by the 400 step; else the recorded reference below
BASE400_REF = Path(__file__).resolve().parent.parent / "tmp/logs/cpu_bench_400.txt"


def cpu_bench(mhz: int) -> dict[str, str]:
    """Host-timed run of memtest_arm --cpu minus an empty-ssh baseline; speedup vs the 400 MHz time."""
    def timed(cmdline: str) -> float:
        t = time.monotonic()
        out = ct.ssh(cmdline, 120)
        return time.monotonic() - t, out
    base = min(timed("true")[0] for _ in range(3))
    runs = []
    for _ in range(3):
        dt, out = timed(f"/tmp/memtest_arm --cpu {CPU_PASSES}")
        runs.append((dt - base, out.strip()))
    best, out = min(runs)
    log(f"cpu_bench {mhz} MHz: runs {[round(r[0], 3) for r in runs]} s (ssh baseline {base:.3f} s) {out}")
    res = {"cpu_bench_s": f"{best:.3f}", "want_x": f"{mhz / 400:.3f}"}
    if mhz == 400:
        BASE400_REF.write_text(f"{best:.6f} {out}\n")
    try:
        ref = float(BASE400_REF.read_text().split()[0])
        res["speedup"] = f"{ref / best:.3f}"
    except (OSError, ValueError):
        res["speedup"] = "?"
    return res


def linux_checks(mhz: int, mib: int, cap: list[float]) -> dict[str, str]:
    res: dict[str, str] = {}
    dm = ct.ssh("dmesg | grep -E 'sched_clock|Calibrating'")
    m = re.search(r"sched_clock: \d+ bits at (\d+)MHz", dm)
    res["sched_clock"] = m.group(1) if m else "?"
    res["sched_ok"] = "ok" if m and int(m.group(1)) == mhz // 2 else f"FAIL want {mhz // 2}"
    a = float(ct.ssh("cut -d' ' -f1 /proc/uptime").split()[0])
    h0 = time.monotonic()
    ct.wait(10)  # 10 s window: 1 % = 100 ms, well above ssh jitter (tens of ms, I)
    b = float(ct.ssh("cut -d' ' -f1 /proc/uptime").split()[0])
    ratio = (b - a) / (time.monotonic() - h0)
    res["timer_ratio"] = f"{ratio:.3f}"
    res["timer_ok"] = "ok" if abs(ratio - 1) < 0.02 else "FAIL"
    with MEMTEST_BIN.open("rb") as f:
        subprocess.run(ct.SSH + ["cat > /tmp/memtest_arm && chmod +x /tmp/memtest_arm"], stdin=f, timeout=60)
    t0 = time.monotonic()
    rc, out = ssh_stream(f"/tmp/memtest_arm --mib {mib}", MEMTEST_STALL_S, cap[0])
    if rc is not None and cap[0] == MEMTEST_FIRST_CAP_S:
        cap[0] = 3 * (time.monotonic() - t0)  # backstop 3x measured (awto-dan#53)
    res.update(cpu_bench(mhz))
    res["memtest"] = "ok" if rc == 0 and "DONE fails 0" in out else f"FAIL rc={rc}"
    for key in ("ddr_read32_MBps", "ddr_write32_MBps", "ddr_memcpy_MBps", "sram_read32_MBps", "sram_write32_MBps"):
        mm = re.search(rf"BW {key} ([\d.]+)", out)
        res[key] = mm.group(1) if mm else "?"
    mm = re.search(r"LAT ddr_ns ([\d.]+)", out)
    res["lat_ddr_ns"] = mm.group(1) if mm else "?"
    ref = mtd_md5_ref()
    for name, want in ref.items():
        got = ct.ssh(f"n=$(grep '\"{name}\"' /proc/mtd | cut -d: -f1); cat /dev/$n | md5sum", 60).split()
        res[f"spi_{name}"] = "ok" if got and got[0] == want else f"FAIL {got[:1]}"
    vend = ct.ssh("cat /sys/bus/pci/devices/0000:01:00.0/vendor").strip()
    ids = [int(x, 16) for x in re.findall(r"0x[0-9A-Fa-f]{8}", ct.ssh("devmem 0x48010224; devmem 0x0a010224"))]
    res["pci"] = "ok" if vend == "0x14e4" and len(ids) == 2 and ids[1] != 0xFFFFFFFF else f"FAIL {vend} {ids}"
    res["chip_ids"] = " ".join(f"{i & 0xFFFF:#06x}" for i in ids)
    p = subprocess.run(["ping", "-c", "5", "-W", "1", ct.BOARD_IP], capture_output=True, text=True, timeout=30)
    res["ping"] = "ok" if " 0% packet loss" in p.stdout else "FAIL"
    return res


SOAK_MAX = [0.0]


def soak(seconds: int, mib: int) -> str:
    ref = hashlib.md5(bytes(8 << 20)).hexdigest()  # md5 of 8 MiB of zeros, checked every pass next to memtest
    ct.ssh("rm -f /tmp/soak.fail; (while :; do /tmp/memtest_arm --mib 16 --no-sram >/tmp/soak.log 2>&1 || echo FAIL memtest >>/tmp/soak.fail; "
           f"[ \"$(dd if=/dev/zero bs=1M count=8 2>/dev/null | md5sum | cut -d' ' -f1)\" = {ref} ] || echo FAIL md5 >>/tmp/soak.fail; done)"
           " >/dev/null 2>&1 & echo $! >/tmp/soak.pid")
    t0 = time.monotonic()
    verdict = "ok"
    SOAK_MAX[0] = 0.0
    while time.monotonic() - t0 < seconds:
        ct.wait(ct.SOAK_POLL_S)
        d = die_temps_linux()
        hw = ct.temps()
        fail = ct.ssh("cat /tmp/soak.fail 2>/dev/null").strip()
        SOAK_MAX[0] = max([SOAK_MAX[0]] + [t for t in d.values() if t is not None])
        log(f"soak {time.monotonic() - t0:.0f}s die={d} hwmon={hw} fail={fail!r}")
        if fail:
            verdict = f"FAIL during soak: {fail}"
            break
        if any(t is not None and t >= DIE_ABORT_C for t in d.values()) or (hw and max(hw) >= ct.TEMP_ABORT_C):
            verdict = f"ABORT temp die={d} hwmon={hw}"
            break
    ct.ssh("kill $(cat /tmp/soak.pid); killall memtest_arm")
    return verdict


# ---------------------------------------------------------------- sweep

COLS = ["time", "mhz", "cpu_bench_s", "speedup", "want_x", "ndiv", "vco", "switch", "ub_ddr_const", "ub_ddr_walk1", "ub_ddr_cmp", "ub_sram", "ub_spi_sf",
        "ub_spi_bspi", "boot", "bogomips", "sched_clock", "sched_ok", "timer_ratio", "timer_ok", "memtest", "ddr_read32_MBps",
        "ddr_write32_MBps", "ddr_memcpy_MBps", "lat_ddr_ns", "sram_read32_MBps", "sram_write32_MBps",
        "spi_u-boot", "spi_shmoo", "pci", "chip_ids", "ping", "die_idle_u0", "die_idle_u1", "die_u0", "die_u1", "die_peak_u0", "die_peak_u1", "hwmon_max", "soak_die_max", "soak", "verdict"]


def write_row(row: dict[str, str]) -> None:
    if not RESULTS.exists():
        RESULTS.write_text(f"# clock_sweep results ({time.strftime('%Y-%m-%dT%H:%M:%S%z')})\n\n| "
                           + " | ".join(COLS) + " |\n|" + "---|" * len(COLS) + "\n")
    with RESULTS.open("a") as f:
        f.write("| " + " | ".join(str(row.get(c, "")) for c in COLS) + " |\n")


def one_step(mhz: int, args, cap: list[float]) -> dict[str, str]:
    row = {"time": time.strftime("%H:%M:%S"), "mhz": str(mhz),
           "ndiv": str(ct.relock_ndiv(mhz)) if mhz >= 1000 else "-", "vco": str(2 * mhz)}
    if 2 * mhz >= VCO_HIGH_HIGH_MHZ:
        log(f"{mhz} MHz: VCO {2 * mhz} in the generic iProc HIGH_HIGH band; ARM PLL loop params unchanged (I)")
    s, _ = dev._session_at_uboot([])
    if s is None:
        row["verdict"] = "FAIL no uboot-1 prompt"
        return row
    with s:
        t0 = time.monotonic()
        ub(s, f"crc32 {UB_TEST:x} {UB_CHUNK:x}", 60)  # 4 MiB at 400 MHz, caches off: sets the budget
        budget = 1.25 * (time.monotonic() - t0) + ct.CMD_S
        log(f"U-Boot op budget {budget:.1f} s (1.25 x measured 4 MiB crc32 at 400 MHz + console)")
        refs = uboot_refs(s, budget)
        _, off, length, _img = dev._slot([])
        ub(s, f"sf read {dev.LOAD_ADDR} {dev.KERNEL0_OFF + off:x} {length:x}", dev.SF_BUDGET_S)
        if not dev._arm_sp805(s) or not ct.switch_cpu(s, mhz):
            row["switch"] = row["verdict"] = "FAIL"
            return row
        row["switch"] = "ok"
        try:
            row.update(uboot_checks(s, refs, budget))
        except RuntimeError as e:
            log(f"U-Boot check hung: {e}")
            row["verdict"] = "FAIL U-Boot hang"
            return row
        if not dev._arm_sp805(s):  # restart the 46 s window for bootm -> Linux sp805-wdt
            row["verdict"] = "FAIL SP805 re-arm"
            return row
        s.send_line(f"bootm {dev.LOAD_ADDR}")
        try:
            s.expect([b"Starting kernel"], dev.BOOTM_BUDGET_S, "bootm 'Starting kernel'")
            t1 = time.monotonic()
            hit = s.expect([b"Please press Enter", b"Kernel panic", b"BusyBox"], dev.USERSPACE_BUDGET_S, "userspace")
        except console.Timeout as e:
            log(f"FAIL boot: {e}")
            row["boot"] = row["verdict"] = "FAIL boot"
            return row
        row["boot"] = "panic" if hit == b"Kernel panic" else f"{time.monotonic() - t1:.0f}s"
        if hit == b"Kernel panic":
            row["verdict"] = "FAIL panic"
            return row
    if not wait_ssh():
        row["verdict"] = "FAIL ssh"
        return row
    bm = re.search(r"BogoMIPS\s*:\s*([\d.]+)", ct.ssh("grep BogoMIPS /proc/cpuinfo"))
    row["bogomips"] = bm.group(1) if bm else "?"
    d = die_temps_linux()
    row["die_idle_u0"], row["die_idle_u1"] = (f"{d[u]:.1f}" if d.get(u) is not None else "?" for u in (0, 1))
    row.update(linux_checks(mhz, args.mib, cap))
    d = die_temps_linux()
    row["die_u0"], row["die_u1"] = (f"{d[u]:.1f}" if d.get(u) is not None else "?" for u in (0, 1))
    row["die_peak_u0"], row["die_peak_u1"] = (f"{PEAK.get(u, float('nan')):.1f}" for u in (0, 1))
    hw = ct.temps()
    row["hwmon_max"] = str(max(hw)) if hw else "?"
    if any(t is not None and t >= DIE_ABORT_C for t in d.values()) or (hw and max(hw) >= ct.TEMP_ABORT_C):
        row["verdict"] = f"ABORT temp die={d} hwmon={hw}"
        return row
    fails = [k for k, v in row.items() if str(v).startswith("FAIL")]
    if fails:
        row["verdict"] = "FAIL " + ",".join(fails)
        return row
    row["soak"] = soak(args.soak, args.mib)
    row["soak_die_max"] = f"{SOAK_MAX[0]:.1f}"
    row["verdict"] = "PASS" if row["soak"] == "ok" else row["soak"]
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", type=int, default=1400)
    ap.add_argument("--stop", type=int, default=ct.VCO_MAX_MHZ // 2)
    ap.add_argument("--step", type=int, default=50)
    ap.add_argument("--soak", type=int, default=0, help="seconds per step; 0 = none (soaks only when Dan asks)")
    ap.add_argument("--mib", type=int, default=64)
    ap.add_argument("--no-cycle", action="store_true", help="leave the board as is at the end (no power-cycle)")
    ap.add_argument("--steps", help="comma list of MHz overriding start/stop/step (400 allowed)")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--build", action="store_true")
    args = ap.parse_args()
    steps = [int(x) for x in args.steps.split(",")] if args.steps else plan(args.start, args.stop, args.step)
    if args.plan:
        show_plan(steps)
        return 0
    if args.build:
        return build()
    if not MEMTEST_BIN.exists() and build():
        return 1
    log(f"sweep {steps} (die abort {DIE_ABORT_C} C, hwmon abort {ct.TEMP_ABORT_C} C)")
    cap = [float(MEMTEST_FIRST_CAP_S)]
    rc = 0
    for mhz in steps:
        log(f"===== {mhz} MHz =====")
        try:
            row = one_step(mhz, args, cap)
        except (RuntimeError, subprocess.TimeoutExpired, console.Timeout) as e:
            log(f"step {mhz} aborted: {e!r}")
            row = {"mhz": str(mhz), "verdict": f"FAIL exception {type(e).__name__}"}
        write_row(row)
        log(f"{mhz} MHz verdict: {row.get('verdict')}")
        if row.get("verdict") != "PASS":
            rc = 1
            break
    if args.no_cycle:
        return rc
    log("sweep done: power-cycle back to stock 400 MHz (uboot-1 reprograms the ARM PLL)")
    dev.cmd_power_cycle([])
    return rc


if __name__ == "__main__":
    sys.exit(main())
