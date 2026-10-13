#!/usr/bin/env python3
"""dev.py — canonical entry point for awto-unifi-us48 (awto dev.py convention).

AI agents: run `scripts/dev.py describe` for machine-readable JSON help.
Humans: run `scripts/dev.py --help`.

All output is timestamped in LOCAL time and mirrored to ./tmp/logs/dev.log.
Console: one visible tio window ("US-48 console", scripts/console.py); every
command here talks through its socket, so whatever dev.py does you see there.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT = "awto-unifi-us48"


def _repo_root() -> Path:
    """Three-tier resolution (awto-dan#35): env override, git toplevel, file-relative."""
    env = os.environ.get(f"{PROJECT.upper().replace('-', '_')}_REPO")
    if env:
        p = Path(env).expanduser().resolve()
        if p.is_dir():
            return p
    here = Path(__file__).resolve().parent
    try:
        # local .git read, ~1 ms warm; 0.25 s covers a cold first exec. On expiry: file-relative.
        out = subprocess.run(["git", "-C", str(here), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=0.25)
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return here.parent if here.name == "scripts" else here


REPO = _repo_root()
TMP = REPO / "tmp"
LOGS = TMP / "logs"
LOG = LOGS / "dev.log"
sys.path.insert(0, str(REPO / "scripts"))

import console  # noqa: E402

IMAGE = REPO / "tmp/tftp/us48-initramfs.bin"
LOAD_ADDR = "0x01000000"  # U-Boot scratch RAM, docs/uboot.md §6

# --------------------------------------------------------------------------
# STEPS — command name -> (summary, argv, accepts_extra_args). [] = not configured (SKIP).
# --------------------------------------------------------------------------
STEPS: dict[str, tuple[str, list[str], bool]] = {
    "build":     ("build the OpenWrt initramfs (scripts/build_openwrt.py)",
                  [sys.executable, str(REPO / "scripts/build_openwrt.py")], True),
    "test":      ("run the test suite", [], True),
    "lint":      ("static analysis", [], True),
    "fmt-check": ("formatting check (no writes)", [], False),
    "fmt":       ("reformat in place", [], False),
}
GATE = ["fmt-check", "lint", "test", "build"]
CI = ["fmt-check", "lint", "test", "build"]

# --------------------------------------------------------------------------
# Logging — local time, stderr + ./tmp/logs/dev.log, colour on TTY only.
# --------------------------------------------------------------------------
_COLOR = {"FATAL": "\033[1;91m", "ERROR": "\033[31m", "WARN": "\033[33m",
          "INFO": "\033[97m", "DEBUG": "\033[94m", "ALERT": "\033[92m"}
_RESET = "\033[0m"
_USE_COLOR = sys.stderr.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("CLICOLOR") != "0"


def _stamp() -> str:
    return datetime.now().astimezone().strftime("%H:%M:%S.%f%z")


def log(msg: str, level: str = "INFO") -> None:
    line = f"{_stamp()}  {level:<5} [dev.py] {msg}"
    sys.stderr.write(f"{_COLOR.get(level, '')}{line}{_RESET}\n" if _USE_COLOR else line + "\n")
    sys.stderr.flush()
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


SKIPPED = 125


def run_step(name: str, extra: list[str] | None = None) -> int:
    summary, argv, takes_extra = STEPS[name]
    if not argv:
        log(f"{name}: not configured for this project - skipped", "WARN")
        return SKIPPED
    if not shutil.which(argv[0]) and argv[0] != sys.executable:
        log(f"{name}: {argv[0]} not on PATH - skipped", "WARN")
        return SKIPPED
    cmd = argv + (list(extra) if (extra and takes_extra) else [])
    log("run: " + " ".join(cmd))
    rc = subprocess.call(cmd, cwd=REPO)
    log(f"rc={rc}: " + " ".join(cmd), "INFO" if rc == 0 else "ERROR")
    return rc


COMMANDS: dict[str, dict] = {}


def command(summary: str, *, args: str = "", kind: str = "action"):
    def deco(fn):
        COMMANDS[fn.__name__.replace("cmd_", "").replace("_", "-")] = {
            "summary": summary, "args": args, "kind": kind, "fn": fn}
        return fn
    return deco


# --------------------------------------------------------------------------
# Console + RAM boot
# --------------------------------------------------------------------------

# Stock `reboot` → U-Boot banner: switchdrvr teardown + kernel shutdown, unmeasured.
# 120 s backstop; the first run's log gives the real figure for docs/uboot.md.
REBOOT_TO_UBOOT_S = 120
# Any char aborts the 3 s countdown (docs/uboot.md §3). Send one every 100 ms from "Hit any key";
# prompt shows within one char time, 2 s = margin. Expiry = autoboot not stopped (stock boots, harmless).
ABORT_EVERY_S = 0.1
ABORT_BUDGET_S = 2.0
# Banner → "Hit any key": DDR shmoo restore + memtest + PCIe, measured ~1 s (2026-10-13). 10 s = stall detector;
# seen once: hang at "Restoring Shmoo parameters" after a soft reboot → power-cycle.
BANNER_TO_COUNTDOWN_S = 10
SWITCH_IP = "192.168.25.206"
# LAN ssh login + `reboot` returns in ~2 s; 15 s backstop. Expiry = ask for power-cycle, keep waiting for U-Boot.
SSH_REBOOT_S = 15
PROMPT = b"u-boot> "
# bootm: header + data CRC over 5.6 MB uncached (~2 s, I) then decompress; "Starting kernel" within 15 s.
BOOTM_BUDGET_S = 15
# Kernel + initramfs to the BusyBox shell prompt: unmeasured on this board; 120 s backstop.
USERSPACE_BUDGET_S = 120


def _ensure_console() -> bool:
    if console.start():
        log(f"console window up ({console.SOCK.relative_to(REPO)}, tty log {console.TIO_LOG.relative_to(REPO)})")
        return True
    log("console window did not come up — see tmp/logs/console.log", "FATAL")
    return False


@command("open the visible console window (tio) if not already running", kind="action")
def cmd_console(_extra: list[str]) -> int:
    return 0 if _ensure_console() else 1


@command("console window state: running / stale-socket / stopped", kind="meta")
def cmd_console_status(_extra: list[str]) -> int:
    st = console.status()
    log(f"console: {st}")
    return 0 if st == "running" else 1


@command("type a line into the console and print the reply", args='"text" [cap_s]', kind="action")
def cmd_send(extra: list[str]) -> int:
    if not extra:
        log('usage: send "text" [cap_s]', "ERROR")
        return 2
    cap = float(extra[1]) if len(extra) > 1 else 10.0
    if not _ensure_console():
        return 1
    with console.Session() as s:
        s.send_line(extra[0])
        # Reply idle gap 0.5 s: shell/U-Boot answer in ms; slow commands (i2cdetect) pause between rows.
        out = s.read_idle(0.5, cap)
    sys.stdout.write(out.decode(errors="replace") + "\n")
    return 0 if out else 1


# `setenv baudrate` does nothing on this U-Boot (no "press ENTER", stays 115200, 2026-10-13);
# `loady [off] [baud]` is the only speed switch: ENTER at the new baud starts, ESC at 115200 ends.
# U-Boot answers in ms; 2 s = window-restart race + margin. Expiry = that baud failed.
BAUD_REPLY_S = 2.0


def _loady_open(s: console.Session, baud: int) -> tuple[console.Session, bool]:
    """`loady LOAD_ADDR baud`, reopen the window at baud, ENTER → 'Ready for binary'. Session at baud."""
    s.send_line(f"loady {LOAD_ADDR}" + (f" {baud}" if baud != console.BAUD else ""))
    if baud == console.BAUD:
        try:
            s.expect([b"Ready for binary"], BAUD_REPLY_S, "loady 'Ready for binary'")
            return s, True
        except console.Timeout as e:
            log(str(e), "ERROR")
            return s, False
    try:
        s.expect([b"press ENTER"], BAUD_REPLY_S, f"loady 'press ENTER' for {baud}")
    except console.Timeout as e:
        log(str(e), "ERROR")
        return s, False
    s.close()
    if not console.restart(baud):
        log(f"console window did not come back at {baud}", "FATAL")
        return console.Session(), False
    n = console.Session()
    n.write(b"\r")
    try:
        n.expect([b"Ready for binary"], BAUD_REPLY_S, f"'Ready for binary' at {baud}")
    except console.Timeout as e:
        log(str(e), "ERROR")
        return n, False
    return n, True


def _loady_close(s: console.Session, baud: int) -> tuple[console.Session, bool]:
    """After the transfer (or a CAN abort): U-Boot says 'press ESC' at baud, drops to 115200, waits ESC."""
    if baud == console.BAUD:
        return s, True
    try:
        s.expect([b"press ESC"], BAUD_REPLY_S, f"loady 'press ESC' at {baud}")
    except console.Timeout as e:
        log(str(e), "WARN")  # garbled at a marginal baud; still try ESC at 115200
    s.close()
    console.restart(console.BAUD)
    n = console.Session()
    n.write(b"\x1b")
    try:
        n.expect([PROMPT], BAUD_REPLY_S, "u-boot> back at 115200")
    except console.Timeout as e:
        log(str(e), "ERROR")
        return n, False
    return n, True


@command("find the fastest loady baud (handshake + cancelled transfer per step; RAM only)", kind="action")
def cmd_baud_sweep(_extra: list[str]) -> int:
    if not console.restart(console.BAUD):
        return 1
    s = console.Session()
    if not _to_uboot(s):
        return 1
    good = console.BAUD
    for baud in console.BAUD_LADDER:
        log(f"trying {baud}")
        s, ok = _loady_open(s, baud)
        if ok:
            s.write(bytes([console.CAN] * 5))  # abort the empty transfer
            s, ok = _loady_close(s, baud)
        if not ok:
            log(f"{baud} failed. Fastest clean: {good}. If the console stays silent, power-cycle "
                f"(U-Boot may be waiting at {baud}).", "FATAL")
            s.close()
            return 1
        log(f"baud {baud}: handshake both ways clean, back at 115200", "ALERT")
        good = baud
    log(f"every ladder step clean; fastest {good}", "ALERT")
    s.close()
    return 0


def _at_uboot(s: console.Session) -> bool:
    # Not an empty line: U-Boot repeats the last command on bare Enter (could be loady/bootm).
    s.send_line("echo uboot_probe")
    out = s.read_idle(0.3, 2.0)
    return b"\nuboot_probe" in out and PROMPT in out


def _serial_reboot(s: console.Session) -> bool:
    """Stock Linux on the console: log in (vendor default ubnt/ubnt) and `reboot`. No IP or SSH needed."""
    # The echo probe may have landed as a username: Enter at "Password:" → "Login incorrect" (~3 s busybox
    # delay) → "login:". 6 s = that delay + margin. Expiry = not stock Linux on the console.
    s.send_line("")
    try:
        hit = s.expect([b"login:", b"# ", b"$ "], 6.0, "stock login prompt or shell")
    except console.Timeout:
        return False
    try:
        if hit == b"login:":
            s.send_line("ubnt")
            s.expect([b"Password:"], BAUD_REPLY_S, "stock login 'Password:'")
            s.send_line(os.environ.get("SSHPASS", "ubnt"))
            # Busybox login + profile: ~1 s on stock; 10 s = slow-boot margin. Expiry = not logged in.
            s.expect([b"# ", b"$ "], 10.0, "stock shell prompt after login")
    except console.Timeout as e:
        log(str(e), "WARN")
        return False
    log("stock shell on serial: rebooting")
    s.send_line("reboot")
    return True


def _to_uboot(s: console.Session) -> bool:
    if _at_uboot(s):
        log("already at u-boot> prompt")
        return True
    if _serial_reboot(s):
        pass
    elif subprocess.run(["ping", "-c", "1", "-W", "1", SWITCH_IP], capture_output=True).returncode == 0:
        log("rebooting stock firmware over SSH")
        try:
            r = subprocess.run(["sshpass", "-e", "ssh", "-T", "-o", "ControlMaster=no", "-o", "ControlPath=none",
                                "-o", "RequiredRSASize=1024", "-o", "HostKeyAlgorithms=+ssh-rsa",
                                "-o", f"UserKnownHostsFile={REPO / 'private/known_hosts'}",
                                "-o", "PubkeyAuthentication=no", "-o", "ConnectTimeout=5",
                                f"ubnt@{SWITCH_IP}", "reboot"],
                               env=dict(os.environ, SSHPASS=os.environ.get("SSHPASS", "ubnt")),
                               capture_output=True, text=True, timeout=SSH_REBOOT_S)
            log(f"ssh reboot rc={r.returncode}")
        except subprocess.TimeoutExpired:
            log(f"ssh reboot did not return in {SSH_REBOOT_S} s — POWER-CYCLE the switch", "WARN")
    else:
        log(f"switch not answering ping and not at u-boot> — POWER-CYCLE IT NOW "
            f"(waiting {REBOOT_TO_UBOOT_S} s for U-Boot)", "WARN")
    t0 = time.monotonic()
    try:
        s.expect([b"U-Boot usw"], REBOOT_TO_UBOOT_S, "U-Boot banner")
        log(f"U-Boot banner after {time.monotonic() - t0:.1f} s")
        s.expect([b"Hit any key"], BANNER_TO_COUNTDOWN_S, "autoboot countdown (DDR/PCIe init stalled?)")
    except console.Timeout as e:
        log(str(e), "FATAL")
        return False
    t1 = time.monotonic()
    while time.monotonic() - t1 < ABORT_BUDGET_S:
        s.write(b" ")
        s._pump(ABORT_EVERY_S)
        if PROMPT in s.buf:
            s.read_idle(0.3, 1.0)
            log("at u-boot> prompt", "ALERT")
            return True
    log(f"no u-boot> prompt {ABORT_BUDGET_S} s after countdown — autoboot not stopped", "FATAL")
    return False


CAPTURE_HINT = "tmp/logs/console/ (newest us48-*.log)"
# Backup slot only. kernel1 (stock, active) and the bootsel record at 0x1ee0000 are never written.
KERNEL0_OFF = 0xE0000
KERNEL0_LEN = 0xF00000
# sf erase/write of ≤ 6 MiB on MX25L256: erase ~64 ms/64 KiB → ~6 s, write ~10 s (I).
# 120 s backstop each; expiry = flash op hung, power-cycle (stock slot untouched).
SF_BUDGET_S = 120


def _load_image(s: console.Session, baud: int) -> tuple[console.Session, int | None]:
    """loady IMAGE to LOAD_ADDR at baud, back at 115200 after. Returns (session, size) or (session, None)."""
    size = IMAGE.stat().st_size
    s, ok = _loady_open(s, baud)
    if not ok:
        log(f"loady at {baud} failed — power-cycle if the console is silent, retry slower", "FATAL")
        return s, None
    log(f"ymodem: {IMAGE.name} {size} B at {baud} baud")

    def progress(i: int, n: int, dt: float) -> None:
        rate = i * 1024 / dt if dt else 0
        log(f"ymodem: {i}/{n} blocks, {rate / 1024:.1f} KiB/s, ETA {(n - i) * 1024 / rate:.0f} s" if rate
            else f"ymodem: {i}/{n} blocks")
    try:
        console.ymodem_send(s, IMAGE, progress)
        s.expect([b"Total Size"], BAUD_REPLY_S, "loady 'Total Size'")
        out = s.read_idle(0.3, 2.0).decode(errors="replace")
    except (RuntimeError, ConnectionError, console.Timeout) as e:
        log(str(e), "FATAL")
        return s, None
    m = re.search(r"=\s*0x([0-9a-fA-F]+)", out)
    got = int(m.group(1), 16) if m else None
    # YMODEM pads the last 1 KiB block; U-Boot counts whole blocks → got may exceed size by < 1024.
    ok_size = got is not None and size <= got < size + 1024
    log(f"U-Boot reports {got} B (file {size} B)", "INFO" if ok_size else "ERROR")
    if not ok_size:
        log(f"size mismatch; U-Boot said: {out.strip()!r}", "FATAL")
        return s, None
    s, ok = _loady_close(s, baud)
    if not ok:
        log("could not get back to u-boot> at 115200 — power-cycle", "FATAL")
        return s, None
    return s, size


def _bootm_watch(s: console.Session) -> int:
    s.send_line(f"bootm {LOAD_ADDR}")
    try:
        hit = s.expect([b"Starting kernel", b"Bad Data CRC", b"Bad Header", b"Bad Magic"], BOOTM_BUDGET_S,
                       "bootm 'Starting kernel'")
        if hit != b"Starting kernel":
            log(f"bootm rejected the image ({hit.decode()}) — see {CAPTURE_HINT}", "FATAL")
            return 1
        log("U-Boot handed over: Starting kernel", "ALERT")
        t0 = time.monotonic()
        hit = s.expect([b"Please press Enter", b"Kernel panic", b"BusyBox"], USERSPACE_BUDGET_S, "OpenWrt userspace")
    except console.Timeout as e:
        log(str(e), "FATAL")
        log(f"full console log: {CAPTURE_HINT}", "FATAL")
        return 1
    if hit == b"Kernel panic":
        log(f"kernel panic {time.monotonic() - t0:.1f} s after handover — see {CAPTURE_HINT}", "FATAL")
        return 1
    log(f"OpenWrt userspace {time.monotonic() - t0:.1f} s after handover", "ALERT")
    s.send_line("")
    s.read_idle(0.5, 3.0)
    log("power-cycle (or `reboot`) returns to stock slot kernel1", "ALERT")
    return 0


def _uboot_cmd(s: console.Session, cmd: str, ok_pat: bytes, budget_s: float, what: str) -> str | None:
    s.send_line(cmd)
    try:
        s.expect([ok_pat], budget_s, what)
        return s.read_idle(0.3, 2.0).decode(errors="replace")
    except console.Timeout as e:
        log(str(e), "FATAL")
        return None


def _crc32(s: console.Session, addr: int, size: int, what: str) -> str | None:
    s.send_line(f"crc32 {addr:x} {size:x}")
    try:
        s.expect([b"==> "], BAUD_REPLY_S * 5, what)  # crc32 of 6 MiB uncached: ~1 s (I)
        out = s.read_idle(0.3, 2.0).decode(errors="replace")
    except console.Timeout as e:
        log(str(e), "FATAL")
        return None
    m = re.match(r"\s*([0-9a-fA-F]{8})", out)
    return m.group(1).lower() if m else None


def _session_at_uboot(extra: list[str]) -> tuple[console.Session | None, int]:
    baud = int(extra[extra.index("--baud") + 1]) if "--baud" in extra else console.FAST_BAUD
    # U-Boot always starts at 115200; a previous run may have left the window at another baud.
    if not console.restart(console.BAUD):
        return None, 0
    s = console.Session()
    if not _to_uboot(s):
        s.close()
        return None, 0
    return s, baud


@command("RAM-boot the OpenWrt initramfs: reboot -> U-Boot -> loady -> bootm (no flash writes)",
         args="[--baud N] [--no-boot]", kind="action")
def cmd_boot_ram(extra: list[str]) -> int:
    if not IMAGE.exists():
        log(f"{IMAGE.relative_to(REPO)} missing — run: scripts/dev.py build", "FATAL")
        return 1
    s, baud = _session_at_uboot(extra)
    if s is None:
        return 1
    try:
        s, size = _load_image(s, baud)
        if size is None:
            return 1
        if "--no-boot" in extra:
            log("--no-boot: image loaded, staying at u-boot>", "ALERT")
            return 0
        return _bootm_watch(s)
    finally:
        s.close()


@command("write the image to BACKUP slot kernel0 (loady + sf write + read-back CRC), then boot it",
         args="[--baud N] [--no-boot]", kind="action")
def cmd_flash_kernel0(extra: list[str]) -> int:
    if not IMAGE.exists():
        log(f"{IMAGE.relative_to(REPO)} missing — run: scripts/dev.py build", "FATAL")
        return 1
    if IMAGE.stat().st_size > KERNEL0_LEN:
        log(f"image {IMAGE.stat().st_size} B > kernel0 0x{KERNEL0_LEN:x}", "FATAL")
        return 1
    s, baud = _session_at_uboot(extra)
    if s is None:
        return 1
    try:
        s, size = _load_image(s, baud)
        if size is None:
            return 1
        ram_crc = _crc32(s, int(LOAD_ADDR, 16), size, "crc32 of RAM image")
        if ram_crc is None:
            return 1
        erase_len = (size + 0xFFFF) & ~0xFFFF
        log(f"writing kernel0 @0x{KERNEL0_OFF:x}: erase 0x{erase_len:x}, write 0x{size:x}, RAM crc {ram_crc}", "WARN")
        for cmd, what in ((f"sf probe 0", "sf probe"),
                          (f"sf erase {KERNEL0_OFF:x} {erase_len:x}", "sf erase kernel0"),
                          (f"sf write {LOAD_ADDR} {KERNEL0_OFF:x} {size:x}", "sf write kernel0"),
                          # Read back to a different RAM address so the check cannot pass on the loady copy.
                          (f"sf read 0x02000000 {KERNEL0_OFF:x} {size:x}", "sf read-back")):
            if _uboot_cmd(s, cmd, PROMPT, SF_BUDGET_S, what) is None:
                return 1
        back = _crc32(s, 0x02000000, size, "crc32 of read-back")
        if back != ram_crc:
            log(f"read-back crc {back} != {ram_crc} — kernel0 NOT good (stock kernel1 untouched)", "FATAL")
            return 1
        log(f"kernel0 written and verified (crc {ram_crc}); stock kernel1 + bootsel untouched", "ALERT")
        if "--no-boot" in extra:
            return 0
        return _bootm_watch(s)
    finally:
        s.close()


@command("boot the image in kernel0: reboot -> U-Boot -> sf read -> bootm (seconds, no serial transfer)",
         kind="action")
def cmd_boot_flash(extra: list[str]) -> int:
    s, _baud = _session_at_uboot(extra)
    if s is None:
        return 1
    try:
        if _uboot_cmd(s, "sf probe 0", PROMPT, SF_BUDGET_S, "sf probe") is None:
            return 1
        # Whole slot: the uImage header carries its own length + CRCs; bootm verifies both.
        if _uboot_cmd(s, f"sf read {LOAD_ADDR} {KERNEL0_OFF:x} {KERNEL0_LEN:x}", PROMPT, SF_BUDGET_S,
                      "sf read kernel0") is None:
            return 1
        return _bootm_watch(s)
    finally:
        s.close()


# --------------------------------------------------------------------------
# Generic commands (template)
# --------------------------------------------------------------------------

@command("machine-readable command list (JSON, for AI agents)", kind="meta")
def cmd_describe(_extra: list[str]) -> int:
    print(json.dumps({
        "project": PROJECT, "schema": 1, "entrypoint": "scripts/dev.py",
        "log": str(LOG.relative_to(REPO)),
        "exit_codes": {"0": "success", "2": "usage error", "125": "step skipped (tool absent)", "other": "failure"},
        "commands": {n: {k: v for k, v in m.items() if k != "fn"} for n, m in COMMANDS.items()},
    }, indent=2))
    return 0


@command("check the toolchain + bench are present; run this first when stuck", kind="meta")
def cmd_doctor(_extra: list[str]) -> int:
    bad = 0
    log(f"python     : {sys.version.split()[0]} ({sys.executable})")
    for tool in ("tio", "sshpass", "ssh", "ptyxis"):
        ok = shutil.which(tool) is not None
        log(f"{tool:10s} : {'ok' if ok else 'MISSING'}", "INFO" if ok else "ERROR")
        bad += not ok
    ok = Path(console.TTY).exists()
    log(f"console tty: {console.TTY} {'ok' if ok else 'MISSING'}", "INFO" if ok else "ERROR")
    bad += not ok
    log(f"console    : {console.status()}")
    ok = IMAGE.exists()
    log(f"image      : {IMAGE.relative_to(REPO)} {'ok' if ok else 'missing (scripts/dev.py build)'}",
        "INFO" if ok else "WARN")
    if bad:
        log(f"doctor: {bad} problem(s)", "ERROR")
        return 1
    log("doctor ok", "ALERT")
    return 0


@command("fail-fast pre-commit gate: " + " + ".join(GATE), kind="aggregate")
def cmd_gate(_extra: list[str]) -> int:
    for name in GATE:
        rc = run_step(name)
        if rc not in (0, SKIPPED):
            log(f"GATE FAILED at {name} (rc={rc})", "FATAL")
            return rc
    log("GATE PASSED", "ALERT")
    return 0


@command("run every leg, collect all results, one GO/NO-GO", kind="aggregate")
def cmd_ci(_extra: list[str]) -> int:
    results = {name: run_step(name) for name in CI}
    failed = [k for k, v in results.items() if v not in (0, SKIPPED)]
    for name, rc in results.items():
        log(f"  {name:12s} {'SKIP' if rc == SKIPPED else ('ok' if rc == 0 else f'FAIL rc={rc}')}")
    if failed:
        log(f"NO-GO - {len(failed)} failed: {', '.join(failed)}", "FATAL")
        return 1
    log("GO", "ALERT")
    return 0


@command("verify dev.py's own contract (help/describe/exit codes)", kind="meta")
def cmd_selftest(_extra: list[str]) -> int:
    failures = []

    def check(label: str, cond: bool) -> None:
        log(f"  {label}: {'ok' if cond else 'FAIL'}", "INFO" if cond else "ERROR")
        if not cond:
            failures.append(label)

    out = subprocess.run([sys.executable, __file__, "describe"], capture_output=True, text=True, cwd=REPO)
    check("describe exits 0", out.returncode == 0)
    try:
        doc = json.loads(out.stdout)
        check("describe has project+commands", {"project", "commands"} <= doc.keys())
    except json.JSONDecodeError:
        check("describe emits valid JSON", False)
    h = subprocess.run([sys.executable, __file__, "--help"], capture_output=True, text=True, cwd=REPO)
    check("--help exits 2", h.returncode == 2)
    check("--help lists commands", all(c in h.stdout for c in COMMANDS))
    u = subprocess.run([sys.executable, __file__, "no-such-command"], capture_output=True, text=True, cwd=REPO)
    check("unknown command exits 2", u.returncode == 2)
    if failures:
        log(f"selftest FAILED: {', '.join(failures)}", "FATAL")
        return 1
    log("selftest ok", "ALERT")
    return 0


def _register_steps() -> None:
    for name, (summary, _argv, takes_extra) in STEPS.items():
        if name not in COMMANDS:
            COMMANDS[name] = {"summary": summary, "args": "[extra args passed through]" if takes_extra else "",
                              "kind": "step", "fn": (lambda n: lambda extra: run_step(n, extra))(name)}


_register_steps()


def usage() -> int:
    print(__doc__.rstrip())
    print("\nUsage: scripts/dev.py <command> [args]\n")
    order = ["aggregate", "step", "action", "meta"]
    for kind in sorted({m["kind"] for m in COMMANDS.values()}, key=lambda k: order.index(k) if k in order else 9):
        print(f"  {kind}:")
        for name, meta in sorted((n, m) for n, m in COMMANDS.items() if m["kind"] == kind):
            print(f"    {name:<15} {meta['args'] + ' ' if meta['args'] else ''}{meta['summary']}")
        print()
    print(f"Log: {LOG.relative_to(REPO)}   JSON help: scripts/dev.py describe")
    return 2


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help", "help"):
        return usage()
    name, *extra = argv
    meta = COMMANDS.get(name)
    if meta is None:
        print(f"unknown command: {name!r} — try scripts/dev.py describe", file=sys.stderr)
        return 2
    rc = meta["fn"](extra)
    return 0 if rc == SKIPPED else rc


if __name__ == "__main__":
    sys.exit(main())
