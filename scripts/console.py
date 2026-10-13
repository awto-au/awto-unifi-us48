"""console — US-48 front RJ45 console: one visible tio window + programmatic access via its socket.

- `start()` opens ONE titled window running console_mon.py, which runs `tio -S unix:tmp/tio.us48.sock`
  (awto-l8 pattern): the window is Dan's console; scripts talk through the socket, never the tty.
  tio needs a real tty for -S to carry bytes, hence a window, not a daemon.
- `restart(baud)` changes baud inside that window (console_mon respawns tio); never a second window.
- `Session` = socket client: send, read-until-pattern, read-until-idle.
- `ymodem_send()` = YMODEM-1K sender over the socket, for U-Boot `loady` (no NIC in U-Boot).
- Log: tmp/logs/tio-us48.log (tio, plain), tmp/logs/console.log (this module).
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOCK = ROOT / "tmp/tio.us48.sock"
TIO_LOG = ROOT / "tmp/logs/tio-us48.log"
LOG = ROOT / "tmp/logs/console.log"
TTY = os.environ.get("US48_TTY", "/dev/serial/by-id/usb-Prolific_Technology_Inc._USB-Serial_Controller_D-if00-port0")
BAUD = 115200
TITLE = "US-48 console"
# loady speed ladder: MAX3321 transceiver is a 250 kbps part on paper; tested past it on purpose
# (parts often run faster). Measured result → docs/uboot.md.
BAUD_LADDER = [230400, 460800, 921600, 1500000, 3000000]
FAST_BAUD = 460800  # fastest clean loady baud; 921600 fails (docs/uboot.md "Console speed")
TERMINALS = [
    ("ptyxis", lambda t: ["--new-window", "--title", t, "--"]),
    ("gnome-terminal", lambda t: [f"--title={t}", "--"]),
    ("konsole", lambda t: ["-p", f"tabtitle={t}", "-e"]),
]
# tio binds its socket within ~100 ms of the window opening; 8 s = wedged-launch backstop (awto-l8 ctl.py).
SOCKET_BUDGET_S = 8.0
POLL_S = 0.02


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}\n")


# ── window lifecycle ─────────────────────────────────────────────────────────

def _socket_alive() -> bool:
    if not SOCK.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(str(SOCK))
        return True
    except OSError:
        return False


def status() -> str:
    if _socket_alive():
        return "running"
    if SOCK.exists():
        return "stale-socket"
    return "stopped"


def _tio_pids() -> list[int]:
    pids = []
    for p in Path("/proc").iterdir():
        if p.name.isdigit():
            try:
                cmd = (p / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if cmd and cmd[0].endswith(b"tio") and str(SOCK).encode() in b" ".join(cmd):
                pids.append(int(p.name))
    return pids


BAUD_FILE = ROOT / "tmp/tio.us48.baud"   # baud console_mon starts tio at
RESPAWN = ROOT / "tmp/tio.us48.respawn"   # present = tio exit is a baud change, not a window close
MON = Path(__file__).resolve().parent / "console_mon.py"


def read_baud() -> int:
    try:
        return int(BAUD_FILE.read_text().strip())
    except (OSError, ValueError):
        return BAUD


def tio_argv(baud: int) -> list[str]:
    return ["tio", TTY, "-b", str(baud), "-L", "--log-file", str(TIO_LOG), "--log-append", "--log-strip",
            "-S", f"unix:{SOCK}"]


def _mon_pids() -> list[int]:
    pids = []
    for p in Path("/proc").iterdir():
        if p.name.isdigit():
            try:
                if str(MON).encode() in (p / "cmdline").read_bytes():
                    pids.append(int(p.name))
            except OSError:
                continue
    return pids


def _wait_socket() -> bool:
    deadline = time.monotonic() + SOCKET_BUDGET_S
    while time.monotonic() < deadline:
        if _socket_alive():
            return True
        time.sleep(POLL_S)
    log(f"socket {SOCK} not up after {SOCKET_BUDGET_S} s")
    return False


def restart(baud: int) -> bool:
    """Change baud IN the existing window: console_mon respawns tio. Opens the window if there is none."""
    if not _mon_pids():
        BAUD_FILE.parent.mkdir(parents=True, exist_ok=True)
        BAUD_FILE.write_text(str(baud))
        return start()
    if read_baud() == baud and _socket_alive():
        return True
    BAUD_FILE.write_text(str(baud))
    RESPAWN.touch()
    _end_tio(_tio_pids())
    log(f"baud -> {baud} (same window)")
    return _wait_socket()


def _end_tio(pids: list[int]) -> None:
    import signal
    for pid in pids:
        os.kill(pid, signal.SIGTERM)
    # tio releases the port within ms of SIGTERM; 3 s (~100x) bounds a wedged one, then SIGKILL those pids only.
    deadline = time.monotonic() + 3.0
    while any(Path(f"/proc/{p}").exists() for p in pids) and time.monotonic() < deadline:
        time.sleep(POLL_S)
    for pid in pids:
        if Path(f"/proc/{pid}").exists():
            log(f"tio {pid} still up 3 s after SIGTERM; SIGKILL")
            os.kill(pid, signal.SIGKILL)


def start() -> bool:
    """Open THE console window (console_mon) unless one is running. True once the socket answers."""
    if _mon_pids():
        return _socket_alive() or _wait_socket()  # window exists, tio (re)starting
    legacy = _tio_pids()
    if legacy:  # bare tio window from before console_mon: replace it, its baud is unknown
        log(f"ending legacy tio window(s) {legacy}")
        _end_tio(legacy)
    if SOCK.exists():
        SOCK.unlink()
    if not Path(TTY).exists():
        raise RuntimeError(f"console adapter missing: {TTY} (check dmesg for pl2303)")
    TIO_LOG.parent.mkdir(parents=True, exist_ok=True)
    RESPAWN.unlink(missing_ok=True)
    cmd = ["/usr/bin/python3", str(MON)]
    for term, args_for in TERMINALS:
        if shutil.which(term):
            subprocess.Popen([term] + args_for(TITLE) + cmd, cwd=str(ROOT), start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log(f"window: {term} {' '.join(cmd)}")
            break
    else:
        raise RuntimeError("no terminal emulator found (ptyxis/gnome-terminal/konsole)")
    return _wait_socket()


# ── socket session ───────────────────────────────────────────────────────────

class Timeout(Exception):
    pass


class Session:
    def __init__(self) -> None:
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.connect(str(SOCK))
        self.s.setblocking(False)
        self.buf = b""

    def close(self) -> None:
        self.s.close()

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *_a) -> None:
        self.close()

    def _reconnect(self) -> None:
        self.s.close()
        if not _wait_socket():
            raise ConnectionError(f"tio socket gone and not back in {SOCKET_BUDGET_S} s (console window closed?)")
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.connect(str(SOCK))
        self.s.setblocking(False)
        log("session reconnected to tio socket")

    def write(self, data: bytes) -> None:
        try:
            self.s.sendall(data)
        except (BrokenPipeError, ConnectionResetError):
            self._reconnect()  # tio respawned (baud change / adapter reconnect)
            self.s.sendall(data)

    def send_line(self, text: str) -> None:
        log(f">> {text!r}")
        self.write(text.encode() + b"\r")

    def _pump(self, wait_s: float) -> bytes:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                chunk = self.s.recv(65536)
                if not chunk:
                    self._reconnect()  # tio respawned; raises if the window is really gone
                    continue
                self.buf += chunk
                return chunk
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return b""
                time.sleep(POLL_S)

    def expect(self, patterns: list[bytes], budget_s: float, what: str) -> bytes:
        """Read until any pattern appears; return the matched pattern. Timeout names what was awaited."""
        t0 = time.monotonic()
        while True:
            for p in patterns:
                i = self.buf.find(p)
                if i >= 0:
                    self.buf = self.buf[i + len(p):]
                    return p
            left = budget_s - (time.monotonic() - t0)
            if left <= 0:
                tail = self.buf[-200:].decode(errors="replace")
                log(f"timeout {budget_s:.1f} s waiting for {what}; tail={tail!r}")
                raise Timeout(f"{what}: not seen in {budget_s:.1f} s (last output: {tail!r})")
            self._pump(min(left, 0.2))

    def read_idle(self, idle_s: float, cap_s: float) -> bytes:
        """Collect output (incl. bytes already buffered past an expect()) until idle for idle_s (or cap_s)."""
        start, t0, last = 0, time.monotonic(), time.monotonic()
        while time.monotonic() - t0 < cap_s:
            if self._pump(0.05):
                last = time.monotonic()
            elif time.monotonic() - last > idle_s:
                break
        out = self.buf[start:]
        self.buf = b""
        return out

    def get_byte(self, budget_s: float) -> int | None:
        if not self.buf:
            self._pump(budget_s)
        if not self.buf:
            return None
        b, self.buf = self.buf[0], self.buf[1:]
        return b


# ── YMODEM-1K sender (U-Boot `loady`) ────────────────────────────────────────

SOH, STX, EOT, ACK, NAK, CAN, CRC_REQ = 0x01, 0x02, 0x04, 0x06, 0x15, 0x18, ord("C")
# Receiver ACKs a 1 KiB block within one block time: 1029 B * 10 bit / 115200 = 89 ms, + U-Boot
# flash-free RAM copy (µs). 1.25x rule → 0.12 s is too tight against tio/socket scheduling jitter;
# 1 s per block, then retry (YMODEM allows 10). Logged with the block number on expiry.
ACK_BUDGET_S = 1.0
# U-Boot `loady` 'C' interval is slow: 1 'C' seen in an 8 s window (2026-10-13). 30 s = several intervals.
START_BUDGET_S = 30.0
RETRIES = 10


def _crc16(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def _block(seq: int, payload: bytes, size: int) -> bytes:
    payload = payload.ljust(size, b"\x1a" if size == 1024 and payload else b"\x00")
    head = bytes([STX if size == 1024 else SOH, seq & 0xFF, 0xFF - (seq & 0xFF)])
    c = _crc16(payload)
    return head + payload + bytes([c >> 8, c & 0xFF])


def _await(sess: Session, want: set[int], budget_s: float) -> int | None:
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        b = sess.get_byte(min(0.2, deadline - time.monotonic()))
        if b is None:
            continue
        if b in want or b == CAN:
            return b
    return None


def _send_block(sess: Session, blk: bytes, seq: int) -> None:
    for attempt in range(RETRIES):
        sess.write(blk)
        r = _await(sess, {ACK, NAK}, ACK_BUDGET_S)
        if r == ACK:
            return
        if r == CAN:
            raise RuntimeError(f"ymodem: receiver cancelled at block {seq}")
        log(f"ymodem: block {seq} {'NAK' if r == NAK else f'no ACK in {ACK_BUDGET_S} s'} (try {attempt + 1})")
    raise RuntimeError(f"ymodem: block {seq} failed {RETRIES} times")


def ymodem_send(sess: Session, path: Path, progress=None) -> None:
    data = path.read_bytes()
    # Drop text before the receiver's first 'C' (prompt echo), keep the 'C' itself if already buffered.
    i = sess.buf.rfind(bytes([CRC_REQ]))
    sess.buf = sess.buf[i:] if i >= 0 else b""
    if _await(sess, {CRC_REQ}, START_BUDGET_S) != CRC_REQ:
        raise RuntimeError(f"ymodem: no 'C' from receiver in {START_BUDGET_S} s (is `loady` running?)")
    hdr = path.name.encode() + b"\x00" + str(len(data)).encode() + b"\x00"
    _send_block(sess, _block(0, hdr, 128), 0)
    if _await(sess, {CRC_REQ}, START_BUDGET_S) != CRC_REQ:
        raise RuntimeError("ymodem: no 'C' after header block")
    nblk = (len(data) + 1023) // 1024
    t0 = time.monotonic()
    for i in range(nblk):
        _send_block(sess, _block(i + 1, data[i * 1024:(i + 1) * 1024], 1024), i + 1)
        if progress and (i % 256 == 0 or i == nblk - 1):
            progress(i + 1, nblk, time.monotonic() - t0)
    for _ in range(RETRIES):
        sess.write(bytes([EOT]))
        if _await(sess, {ACK, NAK}, ACK_BUDGET_S) == ACK:
            break
    else:
        raise RuntimeError("ymodem: EOT not acknowledged")
    if _await(sess, {CRC_REQ}, START_BUDGET_S) == CRC_REQ:
        _send_block(sess, _block(0, b"", 128), 0)  # end-of-batch
    log(f"ymodem: sent {path.name} {len(data)} B in {time.monotonic() - t0:.0f} s")
