#!/usr/bin/env python3
"""dev.py — canonical entry point for awto-unifi-us48 (awto dev.py convention).

AI agents: run `scripts/dev.py describe` for machine-readable JSON help.
Humans: run `scripts/dev.py --help`.

All output is timestamped in LOCAL time and mirrored to ./tmp/logs/dev.log.
Console: one visible tio window ("US-48 console", scripts/console.py); every
command here talks through its socket, so whatever dev.py does you see there.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
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

# $US48_IMAGE overrides, e.g. tmp/mainline/us48-mainline.bin (#15).
IMAGE = Path(os.environ.get("US48_IMAGE") or REPO / "tmp/tftp/us48-initramfs.bin").resolve()
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

# Serial/SSH `reboot` → U-Boot banner measured 20.8 s (2026-10-13); 120 s backstop.
REBOOT_TO_UBOOT_S = 120
# Waiting for Dan to power-cycle by hand (halted kernel, no reset handler): human walk + reach, 10 min.
POWER_CYCLE_WAIT_S = 600
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


@command("recover U-Boot stuck in an interrupted loady (CAN flood at FAST_BAUD, ESC at 115200), then boot stock",
         args="[--stay]", kind="action")
def cmd_abort_loady(extra: list[str]) -> int:
    if not console.restart(console.FAST_BAUD):
        return 1
    s = console.Session()
    # One 1K block + header ≈ 1030 B: a CAN run longer than that ends any partial block (worked 2026-10-13).
    s.write(bytes([console.CAN] * 1100))
    s, ok = _loady_close(s, console.FAST_BAUD)
    if not ok:
        log("no u-boot> after CAN + ESC: power-cycle", "FATAL")
        s.close()
        return 1
    if "--stay" not in extra:
        s.send_line("run bootcmd")
        log("u-boot> recovered: booting stock (kernel1)", "ALERT")
    s.close()
    return 0


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


# Reboot -> stock shell prompt: LZMA 13 MB uncompress + kernel + init; reboot->cfgmtd took >175 s (2026-10-14).
# 300 s = 1.25x a ~240 s worst case. Expiry = stock did not boot; see console capture.
STOCK_BOOT_S = 300
ANSI = re.compile(rb"\x1b\[[0-9;]*[A-Za-z]")
# Silent board: nothing at all in this long means no console output to wait for (dead / wrong baud).
SILENT_S = 15.0


def _classify_tail(raw: bytes) -> str | None:
    """State from the END of the output only (stale prompts earlier in the stream never match)."""
    t = ANSI.sub(b"", raw)[-400:]
    e = t.rstrip()
    if e.endswith((b"awto-uboot>", b"uboot-2>", b"us48-uboot>")):
        return "ourboot"
    if e.endswith(b"u-boot>"):
        return "uboot"
    if e.endswith(b"login:"):
        return "stock-login"
    if e.endswith(b"activate this console."):
        return "press-enter"
    if e.endswith((b"#", b"$")):
        return "openwrt" if b"OpenWrt" in t else "stock-shell"
    i = t.rfind(b"Hit any key")
    if i >= 0 and re.fullmatch(rb"Hit any key to stop autoboot:[\s\x08\d]*", t[i:]):
        return "countdown"
    return None


def _board_state(s: console.Session, budget_s: float = STOCK_BOOT_S) -> str:
    """Passive first: listen, never type into a booting board. Ctrl-C only after 2 s of silence.

    Returns uboot | countdown | stock-login | stock-shell | openwrt | press-enter | reset-loop | silent | unknown.
    Ctrl-C is harmless at u-boot>, a shell and a login prompt, and each answers with a prompt we can read.
    """
    t0, acc, ever = time.monotonic(), b"", False
    s.buf = b""
    while time.monotonic() - t0 < budget_s:
        data = s.read_idle(2.0, 6.0)
        if data:
            ever = True
            acc = (acc + data)[-4000:]
            if acc.count(b"U-Boot usw") >= 3:
                return "reset-loop"
            st = _classify_tail(acc)
            if st:
                s.buf = b""
                return st
            continue
        # Ctrl-C first (harmless everywhere); a quiet Linux console that has not been activated ignores it,
        # so then Enter. Enter is only sent second: a fresh boot has no U-Boot command to repeat.
        for probe in (b"\x03", b"\r"):
            s.write(probe)
            acc = (acc + s.read_idle(0.7, 3.0))[-4000:]
            st = _classify_tail(acc)
            if st:
                s.buf = b""
                return st
        ever = ever or bool(acc)
        if not ever and time.monotonic() - t0 > SILENT_S:
            return "silent"
    return "unknown"


def _abort_countdown(s: console.Session) -> bool:
    t1 = time.monotonic()
    while time.monotonic() - t1 < ABORT_BUDGET_S:
        s.write(b" ")
        s._pump(ABORT_EVERY_S)
        if PROMPT in s.buf:
            s.read_idle(0.3, 1.0)
            return True
    return False


def _at_uboot(s: console.Session) -> bool:
    st = _board_state(s, 20.0)
    if st == "countdown":
        st = "uboot" if _abort_countdown(s) else st
    return st == "uboot"


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


# Remote power for the US-48: an ESPHome relay in the mains feed, switch entity "relay" (ON = board powered), the
# same REST API as awto-tl-st1008f/scripts/switch_power.py. Set US48_RELAY=<host[:port]> (house registry name
# us48-relay once installed). No relay installed yet: without the variable the commands say so and callers fall
# back to asking for a manual power-cycle.
# Off -> on gap 5 s: the 500 W PSU bulk capacitors and the chip rails must fully discharge for the stuck states
# this recovers (flash left in 4-byte mode, wedged PCIe); the ST1008F relay uses 1 s for a much smaller supply (I).
# Relay POST/GET is a LAN call answered in ms: 5 s.
RELAY_GAP_S = 5.0
RELAY_HTTP_S = 5
RELAY_PY = "/mnt/2tb/git/awto-terminal/.venv/bin/python3"  # has aioesphomeapi
RELAY_DEFAULT = "so-th-1.local"  # Sonoff TH outlet, US-48 mains (Dan 2026-11-13)


def _relay_post(host: str, action: str) -> str:
    r = subprocess.run([RELAY_PY, str(REPO / "scripts/relay_native.py"), host, action],
                       capture_output=True, text=True, timeout=RELAY_HTTP_S + 8)
    if r.returncode:
        raise OSError(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "relay_native failed")
    return r.stdout.strip()


def _relay_cycle() -> bool:
    host = os.environ.get("US48_RELAY", RELAY_DEFAULT)
    if not host:
        return False
    try:
        log(f"relay {host}: off -> {_relay_post(host, 'off')}", "WARN")
        threading.Event().wait(RELAY_GAP_S)
        log(f"relay {host}: on -> {_relay_post(host, 'on')}", "ALERT")
    except OSError as e:
        log(f"relay {host} unreachable: {e}; power is NOT cycled", "FATAL")
        return False
    return True


@command("power-cycle the board through its ESPHome relay (US48_RELAY=<host[:port]>); no relay: says what is needed",
         kind="action")
def cmd_power_cycle(_extra: list[str]) -> int:
    if not os.environ.get("US48_RELAY", RELAY_DEFAULT):
        log("no relay configured: install an ESPHome relay (switch 'relay') in the US-48 mains feed and set "
            "US48_RELAY=<host>; until then the board needs a manual power-cycle", "FATAL")
        return 1
    return 0 if _relay_cycle() else 1



# switchdrvr touches /var/run/fastpath/switchdrvr_ready when its chip init is done (measured 213 s after reset).
# Resetting into uboot-2 earlier leaves the BCM5334x half-configured and uboot-2 then loses most received frames
# (2026-11-13). Polled over ssh every 3 s (connect 3 s); 300 s = 1.4x the measured 213 s.
STOCK_READY_S = 300


def _wait_stock_ready() -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < STOCK_READY_S:
        try:
            r = subprocess.run(["sshpass", "-e", "ssh", "-T", "-o", "ControlMaster=no", "-o", "ControlPath=none",
                                "-o", "RequiredRSASize=1024", "-o", "HostKeyAlgorithms=+ssh-rsa",
                                "-o", f"UserKnownHostsFile={REPO / 'private/known_hosts'}",
                                "-o", "PubkeyAuthentication=no", "-o", "ConnectTimeout=3",
                                f"ubnt@{SWITCH_IP}", "test -e /var/run/fastpath/switchdrvr_ready"],
                               env=dict(os.environ, SSHPASS=os.environ.get("SSHPASS", "ubnt")),
                               capture_output=True, timeout=10)
            if r.returncode == 0:
                log(f"stock switchdrvr ready after {time.monotonic() - t0:.0f} s (chip initialised)", "ALERT")
                return True
        except subprocess.TimeoutExpired:
            pass
        threading.Event().wait(3.0)
    log(f"stock switchdrvr not ready after {STOCK_READY_S} s: continuing, the chip may be half-configured", "WARN")
    return False


def _to_uboot(s: console.Session) -> bool:
    st = _board_state(s)
    log(f"board state: {st}")
    if st == "countdown" and _abort_countdown(s):
        log("at u-boot> prompt", "ALERT")
        return True
    if st == "uboot":
        log("already at u-boot> prompt")
        return True
    if st == "reset-loop":
        if not _relay_cycle():
            log("U-Boot banner repeating (reset loop): POWER-CYCLE the switch, then rerun", "FATAL")
            return False
        st = "relay-cycled"
    wait_s = REBOOT_TO_UBOOT_S
    if st in ("stock-login", "stock-shell", "press-enter"):
        _wait_stock_ready()
    if st == "ourboot":
        log("in our second-stage U-Boot: reset back to stock U-Boot")
        s.send_line("reset")
    elif st in ("stock-login", "stock-shell", "openwrt", "press-enter") and _serial_reboot(s):
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
    elif st == "relay-cycled":
        wait_s = REBOOT_TO_UBOOT_S
    elif _relay_cycle():
        wait_s = REBOOT_TO_UBOOT_S
    else:
        log(f"switch not answering ping and not at u-boot> — POWER-CYCLE IT NOW "
            f"(waiting {POWER_CYCLE_WAIT_S} s for U-Boot)", "WARN")
        wait_s = POWER_CYCLE_WAIT_S
    t0 = time.monotonic()
    try:
        s.expect([b"U-Boot usw"], wait_s, "U-Boot banner")
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
# kernel0 holds two images: our U-Boot (< 1 MiB, uImage) at the slot start and OpenWrt (< 14 MiB) after it.
# Stock U-Boot loads either with sf read + bootm; u-boot, bootsel (0x1ee0000), kernel1, EEPROM stay untouched.
SLOTS = {"uboot": (0x0, 0x100000, REPO / "tmp/uboot/us48-uboot-chain.bin"),
         "openwrt": (0x100000, 0xE00000, None)}


def _slot(extra: list[str]) -> tuple[str, int, int, Path]:
    name = extra[extra.index("--slot") + 1] if "--slot" in extra else "openwrt"
    if name not in SLOTS:
        raise SystemExit(f"--slot {name}: expected one of {', '.join(SLOTS)}")
    off, length, image = SLOTS[name]
    return name, off, length, (IMAGE if (image is None or os.environ.get("US48_IMAGE")) else image)
# sf erase/write of ≤ 6 MiB on MX25L256: erase ~64 ms/64 KiB → ~6 s, write ~10 s (I).
# 120 s backstop each; expiry = flash op hung, power-cycle (stock slot untouched).
SF_BUDGET_S = 120


def _load_image(s: console.Session, baud: int, image: Path | None = None) -> tuple[console.Session, int | None]:
    """loady IMAGE to LOAD_ADDR at baud, back at 115200 after. Returns (session, size) or (session, None)."""
    image = image or IMAGE
    size = image.stat().st_size
    s, ok = _loady_open(s, baud)
    if not ok:
        log(f"loady at {baud} failed — power-cycle if the console is silent, retry slower", "FATAL")
        return s, None
    log(f"ymodem: {image.name} {size} B at {baud} baud")

    def progress(i: int, n: int, dt: float) -> None:
        rate = i * 1024 / dt if dt else 0
        log(f"ymodem: {i}/{n} blocks, {rate / 1024:.1f} KiB/s, ETA {(n - i) * 1024 / rate:.0f} s" if rate
            else f"ymodem: {i}/{n} blocks")
    try:
        console.ymodem_send(s, image, progress)
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


# SP805 @0x18039000 (bcm-hr2.dtsi). Armed from U-Boot so a kernel that panics/hangs before sp805-wdt probes still
# resets the chip (no power-cycle). 50 MHz wdog clock: LOAD s to IRQ, reset at the 2nd expiry (2 x LOAD).
# 46 s = 1.25 x worst case ~37 s: arm -> handover 8 s (uboot-2 flash read, #20) + handover -> userspace ~29 s
# (14 MB mainline image, #15). Linux sp805-wdt sees it running and keeps feeding it.
SP805 = 0x18039000
SP805_LOAD = 50_000_000 * 23


def _arm_sp805(s: console.Session, prompt: bytes | list[bytes] = PROMPT) -> bool:
    for cmd in (f"mw.l {SP805 + 0xC00:x} 1acce551",   # WDTLOCK unlock
                f"mw.l {SP805:x} {SP805_LOAD:x}",       # WDTLOAD
                f"mw.l {SP805 + 0x8:x} 3",              # WDTCONTROL = INTEN | RESEN
                f"md.l {SP805 + 0x8:x} 1"):             # read back CONTROL
        if _uboot_cmd(s, cmd, prompt, BAUD_REPLY_S, cmd) is None:
            return False
    log(f"SP805 armed from U-Boot: reset ~{2 * SP805_LOAD / 50e6:.0f} s unless Linux feeds it", "ALERT")
    return True


def _bootm_watch(s: console.Session, slot: str = "openwrt", prompt: bytes | list[bytes] = PROMPT) -> int:
    if not _arm_sp805(s, prompt):
        log("could not arm SP805 — not booting without a hardware watchdog", "FATAL")
        return 1
    s.send_line(f"bootm {LOAD_ADDR}")
    try:
        hit = s.expect([b"Starting kernel", b"Bad Data CRC", b"Bad Header", b"Bad Magic"], BOOTM_BUDGET_S,
                       "bootm 'Starting kernel'")
        if hit != b"Starting kernel":
            log(f"bootm rejected the image ({hit.decode()}) — see {CAPTURE_HINT}", "FATAL")
            return 1
        log("U-Boot handed over: Starting kernel", "ALERT")
        t0 = time.monotonic()
        if slot == "uboot":
            # Our second stage: DDR is up, banner + prompt in ~2 s; 20 s = 10x. It feeds the SP805 itself.
            s.expect([b"awto-uboot>", b"uboot-2>", b"us48-uboot>"], 20.0, "second-stage prompt")
            log(f"second-stage U-Boot prompt {time.monotonic() - t0:.1f} s after handover", "ALERT")
            return 0
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


def _uboot_cmd(s: console.Session, cmd: str, ok_pat: bytes | list[bytes], budget_s: float, what: str) -> str | None:
    s.send_line(cmd)
    try:
        s.expect(ok_pat if isinstance(ok_pat, list) else [ok_pat], budget_s, what)
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


# uboot-2 prompt and the board's own address (ip-registry: us48-boot). The TFTP server is started per transfer.
# awto-uboot (uboot-2). The old prompt is still accepted by the expect lists until the flash slot holds the new image.
U2_PROMPT = b"awto-uboot> "
U2_PROMPTS = [b"awto-uboot> ", b"uboot-2> "]
# ge1: GPHY AN <= 4 s in start() + sw1 STP forwarding ~2 s + ARP; 12 s = 1.4x that, expiry = warn and try the transfer anyway.
U2_PING_S = 12.0
U2_IP = "192.168.25.207"
TFTP_SERVE = REPO / "scripts/tftp_serve.py"
TFTP_DIR = REPO / "tmp/tftp"
# tftp_serve.py start (python + bind) ~0.2 s; polled every 0.1 s, 3 s = 15x. Expiry = RuntimeError, server killed.
TFTP_START_S = 3.0
# uboot-2 TFTP restarts a failed transfer by itself; sw1 holds a new port ~30 s (STP) and 5 ARP tries x 5 s are
# silent for 25 s per round. Stall = 70 s without a console byte (two rounds + margin); cap 540 s = 3x the
# slowest measured 8.5 MB run (~180 s). Expiry = log the last output and give up, board stays at the prompt.
U2_TFTP_STALL_S = 70.0
U2_TFTP_CAP_S = 540.0
U2_IMAGE = "us48-initramfs.bin"


def _host_ip_for(board_ip: str) -> str | None:
    """The host address the kernel would use to reach the board (the 10G-facing interface)."""
    r = subprocess.run(["ip", "-4", "-o", "route", "get", board_ip], capture_output=True, text=True)
    m = re.search(r"\bsrc (\d+\.\d+\.\d+\.\d+)", r.stdout)
    return m.group(1) if m else None


def _udp_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as t:
        try:
            t.bind((host, port))
        except OSError:
            return True
    return False


@contextlib.contextmanager
def _tftp_server(board_ip: str = U2_IP):
    """Start scripts/tftp_serve.py on the host IP that reaches the board and a free UDP port; stop it on exit.

    Yields (host_ip, port). Use for every TFTP transfer: no fixed port, no server left running.
    """
    host = _host_ip_for(board_ip)
    if not host:
        raise RuntimeError(f"no route to {board_ip}: cannot pick a host address for TFTP")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as t:
        t.bind((host, 0))
        port = t.getsockname()[1]
    p = subprocess.Popen([sys.executable, str(TFTP_SERVE), "--bind", host, "--port", str(port), "--dir", str(TFTP_DIR)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        t0 = time.monotonic()
        while not _udp_in_use(host, port):
            if time.monotonic() - t0 > TFTP_START_S:
                raise RuntimeError(f"tftp_serve did not bind {host}:{port} within {TFTP_START_S:.0f} s")
            try:
                p.wait(0.1)
                raise RuntimeError(f"tftp_serve exited early (rc {p.returncode})")
            except subprocess.TimeoutExpired:
                pass
        log(f"tftp server up on {host}:{port} serving {TFTP_DIR.relative_to(REPO)}")
        yield host, port
    finally:
        p.terminate()
        try:
            p.wait(2.0)  # exits on SIGTERM within ms
        except subprocess.TimeoutExpired:
            p.kill()
        log("tftp server stopped")


def _u2_tftp(s: console.Session, name: str) -> bool:
    try:
        with _tftp_server() as (host, port):
            # Warm the path first: link wait + sw1 STP forwarding delay + ARP make the first RRQ get lost, and the
            # restarted RRQ then races the server's first transfer thread ("First block is not block 1").
            s.send_line(f"setenv ipaddr {U2_IP}; setenv serverip {host}; ping {host}")
            try:
                s.expect([b"is alive"], U2_PING_S, "uboot-2 ping before tftp")
            except console.Timeout as e:
                log(str(e), "WARN")
            s.send_line("")
            s.expect(U2_PROMPTS, BAUD_REPLY_S, "prompt after ping")
            s.send_line(f"setenv tftpdstp {port}; tftpboot {LOAD_ADDR} {name}")
            t0 = last = time.monotonic()
            seen = b""
            while True:
                d = s.read_idle(1.0, 5.0)
                now = time.monotonic()
                if d:
                    seen += d
                    last = now
                if b"Bytes transferred" in seen:
                    log(f"tftp {name} done in {now - t0:.0f} s", "ALERT")
                    return True
                if now - last > U2_TFTP_STALL_S:
                    log(f"tftp stalled: no console output for {U2_TFTP_STALL_S:.0f} s, last: {seen[-120:]!r}", "FATAL")
                    return False
                if now - t0 > U2_TFTP_CAP_S:
                    log(f"tftp backstop {U2_TFTP_CAP_S:.0f} s exceeded, last: {seen[-120:]!r}", "FATAL")
                    return False
    except RuntimeError as e:
        log(str(e), "FATAL")
        return False


@command("start the TFTP server on the host IP that reaches the board and a free UDP port; prints the uboot-2 "
         "setenv line; runs until killed", kind="action")
def cmd_tftp_serve(_extra: list[str]) -> int:
    signal.signal(signal.SIGTERM, lambda *_a: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        with _tftp_server() as (host, port):
            sys.stdout.write(f"setenv ipaddr {U2_IP}; setenv serverip {host}; setenv tftpdstp {port}\n")
            sys.stdout.flush()
            threading.Event().wait()
    except KeyboardInterrupt:
        return 0
    except RuntimeError as e:
        log(str(e), "FATAL")
        return 1
    return 0


def _u2_image(extra: list[str]) -> str:
    """Name of the image in TFTP_DIR: --image FILE (copied there if elsewhere), else U2_IMAGE."""
    if "--image" not in extra:
        return U2_IMAGE
    f = Path(extra[extra.index("--image") + 1]).resolve()
    if f.parent != TFTP_DIR.resolve():
        shutil.copy2(f, TFTP_DIR / f.name)
    return f.name


@command("uboot-2 (at its prompt): load the OpenWrt image into RAM (flash slot or TFTP) and bootm it", args="[--from flash|tftp] [--image FILE]",
         kind="action")
def cmd_u2_boot(extra: list[str]) -> int:
    src = extra[extra.index("--from") + 1] if "--from" in extra else "flash"
    if src not in ("flash", "tftp") or not _ensure_console():
        log("usage: u2-boot [--from flash|tftp] [--image FILE]", "ERROR")
        return 2
    img = _u2_image(extra)
    name, off, length, _img = _slot(["--slot", "openwrt"])
    s = console.Session()
    try:
        s.write(b"\x03\r\n")  # abort a running TFTP retry loop or a half-typed line
        s.expect(U2_PROMPTS, BAUD_REPLY_S, "uboot-2 prompt (is the board at uboot-2?)")
        if src == "tftp":
            if not _u2_tftp(s, img):
                return 1
            s.expect(U2_PROMPTS, BAUD_REPLY_S, "prompt after tftp")
        else:
            if _uboot_cmd(s, "sf probe 0", U2_PROMPTS, SF_BUDGET_S, "sf probe") is None:
                return 1
            if _uboot_cmd(s, f"sf read {LOAD_ADDR} {KERNEL0_OFF + off:x} {length:x}", U2_PROMPTS, SF_BUDGET_S,
                          f"sf read kernel0 slot {name}") is None:
                return 1
        return _bootm_watch(s, name, U2_PROMPTS)
    except console.Timeout as e:
        log(str(e), "FATAL")
        return 1
    finally:
        s.close()


@command("reboot to stock Linux and wait until switchdrvr has initialised the switch chip (run before a uboot-2 "
         "session that needs a clean chip)", kind="action")
def cmd_stock_fresh(_extra: list[str]) -> int:
    if not _ensure_console():
        return 1
    with console.Session() as s:
        st = _board_state(s)
        log(f"board state: {st}")
        if st == "ourboot":
            s.send_line("reset")  # uboot-1 autoboots stock Linux (we do not abort its countdown)
        elif st not in ("stock-login", "stock-shell", "press-enter", "openwrt"):
            log(f"cannot start from state {st}", "FATAL")
            return 1
        elif st == "openwrt" and not _serial_reboot(s):
            return 1
    return 0 if _wait_stock_ready() else 1


@command("reboot to the u-boot> prompt and stay there (nothing boots; for mw/md experiments)", kind="action")
def cmd_uboot(extra: list[str]) -> int:
    s, _baud = _session_at_uboot(extra)
    if s is None:
        return 1
    s.close()
    log("at u-boot>: staying (run `abort-loady` or `boot-flash` to leave)", "ALERT")
    return 0


@command("RAM-boot the OpenWrt initramfs: reboot -> U-Boot -> loady -> bootm (no flash writes)",
         args="--serial [--baud N] [--no-boot]", kind="action")
def cmd_boot_ram(extra: list[str]) -> int:
    if "--serial" not in extra:
        log("serial image loading is off (Dan): use `u2-boot --from tftp` at uboot-2; --serial overrides", "FATAL")
        return 1
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


@command("write an image to a kernel0 slot: uboot-2 TFTP -> RAM -> sf erase/write + read-back CRC, then boot it "
         "(--serial: old loady path, off by default)",
         args="[--slot openwrt] [--image FILE] [--no-boot] [--serial [--baud N]]", kind="action")
def cmd_flash_kernel0(extra: list[str]) -> int:
    if "--serial" not in extra:
        return _flash_kernel0_u2(extra)
    return _flash_kernel0_serial(extra)


def _flash_kernel0_u2(extra: list[str]) -> int:
    """Board must be at the uboot-2 prompt (boot-flash --slot uboot). Only the openwrt slot is written."""
    slot, slot_off, slot_len, _img = _slot(extra)
    if slot != "openwrt":
        log("flash-kernel0 over TFTP writes the openwrt slot only", "FATAL")
        return 1
    if not _ensure_console():
        return 1
    name = _u2_image(extra if "--image" in extra else ["--image", str(IMAGE)])
    size = (TFTP_DIR / name).stat().st_size
    if size > slot_len:
        log(f"image {size} B > slot {slot} 0x{slot_len:x}", "FATAL")
        return 1
    flash_off = KERNEL0_OFF + slot_off
    erase_len = (size + 0xFFFF) & ~0xFFFF
    s = console.Session()
    try:
        s.write(b"\x03\r\n")
        s.expect(U2_PROMPTS, BAUD_REPLY_S, "uboot-2 prompt (is the board at uboot-2?)")
        if not _u2_tftp(s, name):
            return 1
        s.expect(U2_PROMPTS, BAUD_REPLY_S, "prompt after tftp")
        ram_crc = _crc32(s, int(LOAD_ADDR, 16), size, "crc32 of RAM image")
        if ram_crc is None:
            return 1
        log(f"writing kernel0 @0x{flash_off:x}: erase 0x{erase_len:x}, write 0x{size:x}, RAM crc {ram_crc}", "WARN")
        for cmd, what in (("sf probe 0", "sf probe"),
                          (f"sf erase {flash_off:x} {erase_len:x}", "sf erase kernel0"),
                          (f"sf write {LOAD_ADDR} {flash_off:x} {size:x}", "sf write kernel0"),
                          (f"sf read 0x02000000 {flash_off:x} {size:x}", "sf read-back")):
            if _uboot_cmd(s, cmd, U2_PROMPTS, SF_BUDGET_S, what) is None:
                return 1
        back = _crc32(s, 0x02000000, size, "crc32 of read-back")
        if back != ram_crc:
            log(f"read-back crc {back} != {ram_crc} - kernel0 NOT good (stock kernel1 untouched)", "FATAL")
            return 1
        log(f"kernel0 written and verified (crc {ram_crc}); stock kernel1 + bootsel untouched", "ALERT")
        if "--no-boot" in extra:
            return 0
        return _bootm_watch(s, slot, U2_PROMPTS)
    except console.Timeout as e:
        log(str(e), "FATAL")
        return 1
    finally:
        s.close()


def _flash_kernel0_serial(extra: list[str]) -> int:
    slot, slot_off, slot_len, image = _slot(extra)
    if not image.exists():
        log(f"{image.relative_to(REPO)} missing — run: scripts/dev.py build", "FATAL")
        return 1
    if image.stat().st_size > slot_len:
        log(f"image {image.stat().st_size} B > slot {slot} 0x{slot_len:x}", "FATAL")
        return 1
    flash_off = KERNEL0_OFF + slot_off
    s, baud = _session_at_uboot(extra)
    if s is None:
        return 1
    try:
        s, size = _load_image(s, baud, image)
        if size is None:
            return 1
        ram_crc = _crc32(s, int(LOAD_ADDR, 16), size, "crc32 of RAM image")
        if ram_crc is None:
            return 1
        erase_len = (size + 0xFFFF) & ~0xFFFF
        log(f"writing kernel0 @0x{flash_off:x}: erase 0x{erase_len:x}, write 0x{size:x}, RAM crc {ram_crc}", "WARN")
        for cmd, what in ((f"sf probe 0", "sf probe"),
                          (f"sf erase {flash_off:x} {erase_len:x}", "sf erase kernel0"),
                          (f"sf write {LOAD_ADDR} {flash_off:x} {size:x}", "sf write kernel0"),
                          # Read back to a different RAM address so the check cannot pass on the loady copy.
                          (f"sf read 0x02000000 {flash_off:x} {size:x}", "sf read-back")):
            if _uboot_cmd(s, cmd, PROMPT, SF_BUDGET_S, what) is None:
                return 1
        back = _crc32(s, 0x02000000, size, "crc32 of read-back")
        if back != ram_crc:
            log(f"read-back crc {back} != {ram_crc} — kernel0 NOT good (stock kernel1 untouched)", "FATAL")
            return 1
        log(f"kernel0 written and verified (crc {ram_crc}); stock kernel1 + bootsel untouched", "ALERT")
        if "--no-boot" in extra:
            return 0
        return _bootm_watch(s, slot)
    finally:
        s.close()


@command("boot a kernel0 slot: reboot -> U-Boot -> sf read -> bootm (seconds, no serial transfer)",
         args="[--slot uboot|openwrt]", kind="action")
def cmd_boot_flash(extra: list[str]) -> int:
    name, off, length, _img = _slot(extra)
    s, _baud = _session_at_uboot(extra)
    if s is None:
        return 1
    try:
        if _uboot_cmd(s, "sf probe 0", PROMPT, SF_BUDGET_S, "sf probe") is None:
            return 1
        # Slot read: the uImage header carries its own length + CRCs; bootm verifies both.
        if _uboot_cmd(s, f"sf read {LOAD_ADDR} {KERNEL0_OFF + off:x} {length:x}", PROMPT, SF_BUDGET_S,
                      f"sf read kernel0 slot {name}") is None:
            return 1
        return _bootm_watch(s, name)
    finally:
        s.close()


# Stock userspace → eth0 has an address: switchdrvr brings the tun up ~60-90 s after the shell (I). 240 s backstop.
STOCK_NET_S = 240
# scp 5.6 MB over 1G LAN to the switch CPU: a few s; dd to mtdblock (erase+write 5.6 MB): ~30 s (I). 120 s each.
NET_XFER_S = 120


def _stock_cmd(s: console.Session, cmd: str, budget_s: float) -> tuple[int, str] | None:
    """Run a command in the stock shell over serial; return (rc, output)."""
    s.buf = b""
    # `__r""c=` in the command line, `__rc=` in the output: the tty echo cannot match the marker.
    s.send_line(f'{cmd}; echo __r""c=$?__')
    t0 = time.monotonic()
    while not re.search(rb"__rc=(\d+)__", s.buf):
        if time.monotonic() - t0 > budget_s:
            log(f"stock: {cmd[:40]}: no exit marker in {budget_s} s (tail {s.buf[-200:]!r})", "ERROR")
            return None
        s._pump(0.2)
    out = s.buf.decode(errors="replace")
    s.buf = b""
    m = re.search(r"__rc=(\d+)__", out)
    body = out[:m.start()]
    # Drop the tty echo of our own command line (ends with the marker text): saves tokens, keeps real output.
    k = body.rfind('__r""c=$?__')
    if k >= 0:
        body = body[k + len('__r""c=$?__'):].lstrip("\r\n")
    return int(m.group(1)), body


def _stock_shell(s: console.Session) -> bool:
    """Get to a stock root shell on serial; boots stock from U-Boot if needed."""
    st = _board_state(s)
    log(f"board state: {st}")
    if st == "reset-loop":
        log("U-Boot banner repeating (reset loop): POWER-CYCLE the switch, then rerun", "FATAL")
        return False
    if st == "ourboot":
        log("in our second-stage U-Boot: reset, then stock boots by itself")
        s.send_line("reset")
        st = "countdown"
    if st in ("uboot", "countdown"):
        if st == "uboot":
            log("at u-boot>: booting stock (kernel1)")
            s.send_line("run bootcmd")
        try:
            s.expect([b"Please press Enter", b"login:"], STOCK_BOOT_S, "stock userspace after boot")
        except console.Timeout as e:
            log(str(e), "FATAL")
            return False
    s.send_line("")
    if st == "openwrt":
        # Our image is up: reboot (SP805) -> U-Boot autoboots stock kernel1 by itself.
        log("OpenWrt running: rebooting into stock (kernel1)")
        s.send_line("reboot")
        try:
            s.expect([b"Please press Enter", b"login:"], STOCK_BOOT_S, "stock userspace after OpenWrt reboot")
        except console.Timeout as e:
            log(str(e), "FATAL")
            return False
        s.send_line("")
    # Stock prints its console prompt only after init scripts finish (minutes after a warm reset):
    # nudge with Enter every 10 s until a prompt shows, within STOCK_BOOT_S.
    hit, t0 = None, time.monotonic()
    while hit is None and time.monotonic() - t0 < STOCK_BOOT_S:
        try:
            hit = s.expect([b"login:", b"# ", b"$ "], 10.0, "stock login prompt or shell")
        except console.Timeout:
            s.send_line("")
    if hit is None:
        log(f"no stock shell on serial after {STOCK_BOOT_S} s", "FATAL")
        return False
    try:
        if hit == b"login:":
            s.send_line("ubnt")
            s.expect([b"Password:"], BAUD_REPLY_S, "stock 'Password:'")
            s.send_line(os.environ.get("SSHPASS", "ubnt"))
            s.expect([b"# ", b"$ "], 10.0, "stock shell prompt after login")
    except console.Timeout as e:
        log(f"no stock shell on serial ({e})", "FATAL")
        return False
    return True


@command("run a command in whatever shell is on the console (no login) and wait for its exit marker",
         args='"cmd" [budget_s]', kind="action")
def cmd_sh(extra: list[str]) -> int:
    if not extra:
        log('usage: sh "cmd" [budget_s]', "ERROR")
        return 2
    # Default 20 s: shell commands answer in ms; long loops pass their own budget.
    budget = float(extra[1]) if len(extra) > 1 else 20.0
    if not _ensure_console():
        return 1
    with console.Session() as s:
        r = _stock_cmd(s, extra[0], budget)
    if r is None:
        return 1
    sys.stdout.write(r[1] + "\n")
    return r[0]


@command("run a command in the stock shell over serial (logs in if needed) and print its output",
         args='"cmd" [budget_s]', kind="action")
def cmd_stock_sh(extra: list[str]) -> int:
    if not extra:
        log('usage: stock-sh "cmd" [budget_s]', "ERROR")
        return 2
    # Default 20 s: shell commands answer in ms; dmesg / mem dumps over 115200 take a few s.
    budget = float(extra[1]) if len(extra) > 1 else 20.0
    if not _ensure_console():
        return 1
    with console.Session() as s:
        if not _stock_shell(s):
            return 1
        r = _stock_cmd(s, extra[0], budget)
    if r is None:
        return 1
    sys.stdout.write(r[1] + "\n")
    return r[0]


# (name, op, blk, addr, words): everything the ingress/egress decision for unicast-to-CPU depends on (unit 1).
CHIPSTATE = [
    ("PORT_TAB[26]", 0x07, 10, 0x0400001A, 8), ("PORT_TAB[27]", 0x07, 10, 0x0400001B, 8),
    ("PORT_TAB[0]", 0x07, 10, 0x04000000, 8),
    ("VLAN_TAB[1]", 0x07, 10, 0x14000001, 4), ("VLAN_TAB[48]", 0x07, 10, 0x14000030, 4),
    ("VLAN_TAB[49]", 0x07, 10, 0x14000031, 4), ("VLAN_TAB[50]", 0x07, 10, 0x14000032, 4),
    ("EGR_VLAN[1]", 0x07, 11, 0x100C0001, 3), ("EGR_VLAN[48]", 0x07, 11, 0x100C0030, 3),
    ("EGR_VLAN[49]", 0x07, 11, 0x100C0031, 3), ("EGR_VLAN[50]", 0x07, 11, 0x100C0032, 3),
    ("STG_TAB[1]", 0x07, 10, 0x14040001, 2), ("EGR_VLAN_STG[1]", 0x07, 11, 0x10100001, 2),
    ("VLAN_PROFILE[0]", 0x07, 10, 0x14080000, 1), ("VLAN_PROFILE[1]", 0x07, 10, 0x14080001, 1),
    ("EGR_ENABLE[0]", 0x07, 11, 0x345C0000, 1), ("EGR_ENABLE[26]", 0x07, 11, 0x345C001A, 1),
    ("EGR_ENABLE[27]", 0x07, 11, 0x345C001B, 1),
    ("EPC_LINK_BMAP", 0x0B, 10, 0x46002700, 2), ("ING_CONFIG_64", 0x0B, 16, 0x06000000, 2),
    ("CPU_CONTROL_0", 0x0B, 10, 0x1E000600, 1), ("CPU_CONTROL_1", 0x0B, 10, 0x3A000300, 1),
    ("UUC_BLOCK[27]", 0x0B, 10, 0x4400001B, 2), ("UMC_BLOCK[27]", 0x0B, 10, 0x4400021B, 2),
    ("BC_BLOCK[27]", 0x0B, 10, 0x4400041B, 2), ("MMUPORTENABLE", 0x0B, 12, 0x02025E00, 1),
    ("XLMAC_CTRL[0]", 0x0B, 5, 0x00060000, 2), ("XLMAC_CTRL[2]", 0x0B, 5, 0x00060002, 2),
    ("XLMAC_TXCTRL[2]", 0x0B, 5, 0x00060402, 2), ("XLMAC_RXCTRL[2]", 0x0B, 5, 0x00060602, 2),
]


@command("dump the unit-1 tables that decide unicast-to-CPU (one line each), for diffing a working vs a failing state; "
         "--egr = EGR_ENABLE of all 30 logical ports instead (which ports stock left enabled)", args="[--egr]", kind="action")
def cmd_chipstate(extra: list[str]) -> int:
    b = 0x0A000000 + 0x31000
    fn = (f"sc(){{ devmem {b:#x} 32 0; devmem {b + 12:#x} 32 $1; devmem {b + 16:#x} 32 $2; devmem {b:#x} 32 1; n=0; "
          f"while [ $(( $(devmem {b:#x} 32) & 2 )) = 0 ] && [ $n -lt 50 ]; do n=$((n+1)); done; "
          f"echo $3 c=$(devmem {b:#x} 32) $(for i in 1 2 3 4 5 6 7 8; do [ $i -le $4 ] && devmem $(({b + 12}+4*i)) 32; done); "
          f"devmem {b:#x} 32 0; }}")
    if not _ensure_console():
        return 1
    out = []
    with console.Session() as s:
        if _stock_cmd(s, fn, 10.0) is None:
            return 1
        entries = ([(f"EGR_ENABLE[{i}]", 0x07, 11, 0x345C0000 + i, 1) for i in range(30)]
                   if "--egr" in extra else CHIPSTATE)
        for name, op, blk, addr, words in entries:
            hdr = op << 26 | blk << 20 | (4 * words) << 7
            r = _stock_cmd(s, f"sc {hdr:#x} {addr:#x} '{name}' {words}", 10.0)
            line = [ln for ln in (r[1] if r else "").splitlines() if ln.startswith(name)]
            out.append(line[0].replace("0x0000", "0x").strip() if line else f"{name} <no reply>")
    sys.stdout.write("\n".join(out) + "\n")
    return 0


@command("S-channel op on the running board (needs a shell): unit 0|1, r=reg m=mem w=32-bit reg write, block, addr, [words|value]",
         args="<0|1> <r|m|w|x> <blk> <addr> [words|value|w0 w1 ...]",
         kind="action")
def cmd_schan(extra: list[str]) -> int:
    if len(extra) < 4:
        log("usage: schan <0|1> <r|m> <blk> <addr hex> [words]", "ERROR")
        return 2
    unit, kind, blk, addr = extra[0], extra[1], int(extra[2], 0), int(extra[3], 0)
    words = int(extra[4]) if len(extra) > 4 and kind in "rm" else 1
    wval = int(extra[4], 0) if len(extra) > 4 and kind == "w" else 0
    if kind == "x":  # table-entry write (op 0x09): schan <u> x <blk> <addr> <w0> [w1 ...]
        vals = [int(x, 0) for x in extra[4:]]
        words = len(vals)
    base = {"0": 0x48000000, "1": 0x0A000000}[unit]
    hdr = {"r": 0x0B, "m": 0x07, "w": 0x0D, "x": 0x09}[kind] << 26 | blk << 20 | (4 * words) << 7
    b = base + 0x31000
    # Poll for DONE (b1) in the shell loop: an S-channel op answers in us, 50 polls = far above that.
    cmd = (f"devmem {b:#x} 32 0; devmem {b + 12:#x} 32 {hdr:#x}; devmem {b + 16:#x} 32 {addr:#x}; "
           + (f"devmem {b + 20:#x} 32 {wval:#x}; " if kind == "w" else "")
           + ("".join(f"devmem {b + 20 + 4 * i:#x} 32 {v:#x}; " for i, v in enumerate(vals)) if kind == "x" else "") +
           f"devmem {b:#x} 32 1; n=0; while [ $(( $(devmem {b:#x} 32) & 2 )) = 0 ] && [ $n -lt 50 ]; do n=$((n+1)); done; "
           f"echo ctrl=$(devmem {b:#x} 32); for i in $(seq 0 {words}); do devmem $(({b + 12}+4*i)) 32; done; "
           f"devmem {b:#x} 32 0")
    if not _ensure_console():
        return 1
    with console.Session() as s:
        res = _stock_cmd(s, cmd, 20.0)
    if res is None:
        return 1
    sys.stdout.write(res[1] + "\n")
    return 0


@command("push a kernel module over serial (md5, 3 tries), rmmod/insmod it, give xe0 an IP, ping a host",
         args="[module.ko] [--ip A.B.C.D] [--ping A.B.C.D] [-- insmod args...]", kind="action")
def cmd_cmic_load(extra: list[str]) -> int:
    ko = REPO / "kernel/us48-cmic/us48_cmic.ko"
    ip, ping, mod_args = os.environ.get("US48_IP", "192.168.25.207"), "192.168.25.11", ""
    rest = list(extra)
    if "--" in rest:
        i = rest.index("--")
        mod_args, rest = " ".join(rest[i + 1:]), rest[:i]
    while rest:
        a = rest.pop(0)
        if a == "--ip":
            ip = rest.pop(0)
        elif a == "--ping":
            ping = rest.pop(0)
        else:
            ko = Path(a)
    if not ko.exists():
        log(f"{ko} missing: python3 scripts/build_us48_cmic.py", "FATAL")
        return 1
    if not _ensure_console():
        return 1
    r = subprocess.run([sys.executable, str(REPO / "scripts/push_file.py"), str(ko), f"/tmp/{ko.name}"],
                       capture_output=True, text=True, timeout=120)  # 80 KB at ~7 KB/s = 12 s, x3 tries
    if r.returncode != 0:
        log("push failed:\n" + r.stdout[-400:], "FATAL")
        return 1
    mod = ko.stem
    # ping budget: 4 pings x 1 s + ARP; the tail of dmesg shows the driver's own verdict.
    cmd = (f"rmmod {mod} 2>/dev/null; insmod /tmp/{ko.name} {mod_args}; "
           f"for i in xe49 xe50; do echo 1 > /proc/sys/net/ipv6/conf/$i/disable_ipv6; ip link set $i up; done; "
           f"ip addr flush dev xe50; ip addr add {ip}/24 dev xe50; "
           f"dmesg | grep -E 'us48_cmic|xe49|xe50' | tail -8; ping -c 4 -W 1 {ping} | tail -2; "
           f"ip -br link show xe49; ip -br link show xe50; ip -s link show xe50 | grep -A1 -E 'RX|TX'")
    with console.Session() as s:
        res = _stock_cmd(s, cmd, 40.0)
    if res is None:
        return 1
    sys.stdout.write(res[1] + "\n")
    return 0


# dd of a 14.4 MB image to mtdblock3 (NOR, erase+write) did not finish in 120 s; 300 s = backstop until the rate is measured.
FLASH_DD_S = 300


@command("write an image to a kernel0 slot FROM STOCK over the LAN (scp + dd mtdblock3 + sha256), then boot it",
         args="[--slot uboot|openwrt] [--no-boot]", kind="action")
def cmd_flash_net(extra: list[str]) -> int:
    import hashlib
    slot, slot_off, slot_len, image = _slot(extra)
    if not image.exists():
        log(f"{image.relative_to(REPO)} missing — run: scripts/dev.py build", "FATAL")
        return 1
    size = image.stat().st_size
    if size > slot_len:
        log(f"{image.name} is {size} B, slot {slot} holds {slot_len} B", "FATAL")
        return 1
    want = hashlib.sha256(image.read_bytes()).hexdigest()
    if not console.restart(console.BAUD):
        return 1
    s = console.Session()
    try:
        if not _stock_shell(s):
            return 1
        ip, t0 = None, time.monotonic()
        while time.monotonic() - t0 < STOCK_NET_S:
            r = _stock_cmd(s, "ip -4 -o addr show eth0", 5.0)
            m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/", r[1] if r else "")
            # 192.168.1.20 = stock's pre-DHCP fallback; only accept an address this host can reach
            if m and subprocess.run(["ping", "-c", "1", "-W", "1", m.group(1)],
                                    capture_output=True).returncode == 0:
                ip = m.group(1)
                break
            time.sleep(2.0)  # poll interval while switchdrvr brings eth0 up
        if not ip:
            log(f"stock eth0 has no IPv4 after {STOCK_NET_S} s", "FATAL")
            return 1
        log(f"stock is at {ip}; scp {image.name} ({size} B) for slot {slot}")
        ssh_opts = ["-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "RequiredRSASize=1024",
                    "-o", "HostKeyAlgorithms=+ssh-rsa", "-o", "StrictHostKeyChecking=no",
                    "-o", "UserKnownHostsFile=/dev/null", "-o", "PubkeyAuthentication=no", "-o", "ConnectTimeout=5"]
        env = dict(os.environ, SSHPASS=os.environ.get("SSHPASS", "ubnt"))
        t1 = time.monotonic()
        # 2 tries: stock restarts dropbear during startup and resets the first copy (seen 2026-11-12).
        for attempt in (1, 2):
            try:
                r = subprocess.run(["sshpass", "-e", "scp", "-O"] + ssh_opts + [str(image), f"ubnt@{ip}:/tmp/us48.bin"],
                                   env=env, capture_output=True, text=True, timeout=NET_XFER_S)
            except subprocess.TimeoutExpired:
                log(f"scp did not finish in {NET_XFER_S} s", "FATAL")
                return 1
            if not r.returncode:
                break
            log(f"scp try {attempt} rc={r.returncode}: {r.stderr.strip()}", "WARN" if attempt == 1 else "FATAL")
        if r.returncode:
            return 1
        log(f"scp done in {time.monotonic() - t1:.1f} s; writing kernel0 slot {slot} (+{slot_off:#x}) via /dev/mtdblock3", "WARN")
        r = _stock_cmd(s, f"dd if=/tmp/us48.bin of=/dev/mtdblock3 bs=65536 seek={slot_off // 65536} && sync", FLASH_DD_S)
        if not r or r[0] != 0:
            log(f"dd to mtdblock3 failed: {r}", "FATAL")
            return 1
        r = _stock_cmd(s, f"dd if=/dev/mtd3ro bs=65536 skip={slot_off // 65536} 2>/dev/null | head -c {size} | sha256sum", 30.0)
        got = re.search(r"([0-9a-f]{64})", r[1] if r else "")
        if not got or got.group(1) != want:
            log(f"kernel0 sha256 {got.group(1) if got else '?'} != {want}", "FATAL")
            return 1
        log(f"kernel0 slot {slot} written over LAN and verified (sha256 {want[:16]}…) in {time.monotonic() - t1:.0f} s", "ALERT")
    finally:
        s.close()
    if "--no-boot" in extra:
        return 0
    return cmd_boot_flash(["--slot", slot])


@command("watch serial + LAN continuously; state changes → tmp/logs/us48-watch.log + tmp/us48-status.json",
         kind="action")
def cmd_watch(_extra: list[str]) -> int:
    import watch
    if not console.start():
        return 1
    return watch.run()


@command("print current board state (from the watcher)", kind="meta")
def cmd_status(_extra: list[str]) -> int:
    import watch
    st = watch.read_status()
    if not st:
        log("no status yet — run: scripts/dev.py watch", "WARN")
        return 1
    age = time.time() - st.get("t", 0)
    log(f"state={st.get('state')} ip={st.get('ip')} ping={st.get('ping')} since={st.get('since')} "
        f"({age:.0f} s ago) line={st.get('line', '')!r}")
    return 0


@command("stock firmware: set fan preset 0..3 (50/70/90/100 %) or -1 auto; RAM-only (docs/fan.md)",
         args="<level>", kind="action")
def cmd_fan_level(extra: list[str]) -> int:
    if not extra or extra[0] not in ("-1", "0", "1", "2", "3"):
        log("usage: fan-level <-1|0|1|2|3>", "ERROR")
        return 2
    if not console.restart(console.BAUD):
        return 1
    s = console.Session()
    try:
        if not _stock_shell(s):
            return 1
        # MAC gate: swctrl's setter needs the lowercase base MAC in this marker (docs/fan.md "Authorization gate").
        marker = "/var/run/.swctrl.fan.force"
        r = _stock_cmd(s, f"if [ -e {marker} ] || [ -L {marker} ]; then echo MARKER_EXISTS; false; else "
                          f"mac=$(cat /sys/class/net/eth0/address); (umask 077; set -C; echo $mac > {marker}) "
                          f"&& swctrl fan set level {extra[0]}; rc=$?; rm -f {marker}; [ $rc = 0 ]; fi", 20.0)
        if not r or r[0] != 0:  # existing marker → `false` → rc 1 (the echoed command line also contains the word)
            log(f"fan set failed: {r}", "FATAL")
            return 1
        r = _stock_cmd(s, "swctrl env show", 20.0)
        log("swctrl env show:\n" + (r[1].strip() if r else "?"))
        log(f"fan level {extra[0]} set (stock, RAM-only; reverts on reboot)", "ALERT")
        return 0
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
    ok = console.tty_present()
    where = f"{console.HOST}:" if console.HOST else ""
    log(f"console tty: {where}{console.TTY} {'ok' if ok else 'MISSING'}", "INFO" if ok else "ERROR")
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
