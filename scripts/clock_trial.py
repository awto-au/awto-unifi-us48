#!/usr/bin/env python3
"""clock_trial — RAM-only clock experiments from docs/clocks.md (#17). No flash writes, no saveenv.

  clock_trial.py dump            E0: read-only CCU/GENPLL/CRU dump at the uboot-1 prompt
  clock_trial.py cpu --mhz N     N = 400 control | 800 fid 7 | 1000/1200/1400 relock; E0 + gate, sf read, SP805, bootm
  clock_trial.py linux [--soak S] over ssh: cpuinfo, dd|md5sum benchmark, temps, soak (default 300 s)
  clock_trial.py e3              at the uboot-2 prompt: BSPI clock 50 -> 62.5 MHz, crc32 compare, revert
Log: tmp/logs/clock_trial.log. Recovery: US48_HOST=lizard python3 scripts/dev.py power-cycle, then boot-flash.
"""
from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import console  # noqa: E402
import dev  # noqa: E402

LOGF = dev.LOGS / "clock_trial.log"
BOARD_IP = dev.U2_IP
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
       "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=5", f"root@{BOARD_IP}"]
# dropbear key generation after a power-cycle takes minutes (caller's measurement); 600 s backstop, poll 5 s.
SSH_UP_S = 600
TEMP_ABORT_C = 70
SOAK_POLL_S = 15
CMD_S = 2.0  # uboot md/mw reply: ms; 2 s = dev.BAUD_REPLY_S
E0 = [("19000000", 4), ("19000c00", 3), ("19000c20", 2), ("19000e00", 1), ("19000ec0", 1),
      ("1803fc00", 4), ("1803fc18", 1), ("1803fc1c", 5), ("1803e000", 1), ("1803e010", 1)]


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    print(line, flush=True)
    LOGF.parent.mkdir(parents=True, exist_ok=True)
    with LOGF.open("a") as f:
        f.write(line + "\n")


def wait(sec: float) -> None:
    threading.Event().wait(sec)


def cmd(s, line: str, prompt: bytes = dev.PROMPT) -> str:
    s.buf = b""
    if line.startswith("sf "):  # silent for seconds: wait for the prompt, not for idle
        out = dev._uboot_cmd(s, line, prompt, dev.SF_BUDGET_S, line)
        if out is None:
            raise SystemExit(f"no prompt after {line!r}")
        log(f"> {line}\n{out.strip()}")
        return out
    s.send_line(line)
    # reply idle gap 0.4 s (U-Boot answers in ms), cap CMD_S; sf read of 14 MB needs dev.SF_BUDGET_S
    out = s.read_idle(0.4, dev.SF_BUDGET_S if line.startswith("sf ") else CMD_S).decode(errors="replace")
    if prompt.strip().decode() not in out:
        raise SystemExit(f"no prompt after {line!r}: {out!r}")
    log(f"> {line}\n{out.strip()}")
    return out


def words(out: str) -> list[int]:
    m = re.search(r"^[0-9a-f]{8}:((?: [0-9a-f]{8})+)", out, re.M)
    return [int(w, 16) for w in m.group(1).split()] if m else []


def rd(s, addr: str, n: int = 1, prompt: bytes = dev.PROMPT) -> list[int]:
    return words(cmd(s, f"md.l {addr} {n}", prompt))


def e0(s, prompt: bytes = dev.PROMPT) -> dict[str, list[int]]:
    log("E0 read-only dump")
    return {a: rd(s, a, n, prompt) for a, n in E0}


def cmd_dump(_a: list[str]) -> int:
    s, _ = dev._session_at_uboot([])
    if s is None:
        return 1
    with s:
        e0(s)
    return 0


NDIV = {1000: 80, 1200: 96, 1400: 112}  # [DL] armpll.c rows (1400 is extrapolated: VCO 2800, UNTESTED)


def ccu(s, c: str) -> None:
    cmd(s, c)


def cmd_cpu(a: list[str]) -> int:
    """--mhz 400 (control) | 800 (fid 7) | 1000/1200/1400 (relock, ndiv from NDIV, pdiv 1, ch1 mdiv 2)."""
    mhz = int(a[a.index("--mhz") + 1]) if "--mhz" in a else 800
    s, _ = dev._session_at_uboot([])
    if s is None:
        return 1
    with s:
        d = e0(s)
        mdiv1 = (d["19000c20"] or [0])[0] & 0xFF
        active = ((d["19000ec0"] or [0])[0] >> 12) & 7
        log(f"gate: ch1 mdiv={mdiv1} (need 2), active fid={active} (need 6)")
        if mhz != 400 and (mdiv1 != 2 or active != 6):
            log("GATE FAILED: not switching")
            return 1
        _, off, length, _img = dev._slot([])
        cmd(s, "sf probe 0")
        cmd(s, f"sf read {dev.LOAD_ADDR} {dev.KERNEL0_OFF + off:x} {length:x}")
        if not dev._arm_sp805(s):
            return 1
        if mhz != 400:
            ccu(s, "mw.l 19000000 a5a501")
            if mhz in NDIV:
                ccu(s, "mw.l 19000008 82020202")  # CPU on sys clk while the PLL relocks
                ccu(s, "mw.l 1900000c 3")
                if rd(s, "1900000c")[0] & 1:
                    log("GO stuck during fid 2 switch")
                    return 1
                ccu(s, "mw.l 19000c04 00000001")
                ccu(s, f"mw.l 19000c00 {0x10000000 | NDIV[mhz] << 8 | 1 << 24 | 1:x}")
                locked = 0
                for _ in range(3):
                    if rd(s, "19000c00")[0] & (1 << 28) and True:
                        locked = 1
                        break
                if not locked:
                    log("PLL LOCK NOT SEEN: aborting, SP805 resets in ~46 s")
                    return 1
                ccu(s, f"mw.l 19000c00 {0x10000000 | NDIV[mhz] << 8 | 1 << 24 | 3:x}")
            ccu(s, "mw.l 19000008 87070707")
            ccu(s, "mw.l 1900000c 3")
            go = rd(s, "1900000c")
            if go and go[0] & 1:
                go = rd(s, "1900000c")
            dbg = rd(s, "19000ec0")
            ccu(s, "mw.l 19000000 0")
            if (go or [1])[0] & 1 or (((dbg or [0])[0] >> 12) & 7) != 7:
                log(f"SWITCH NOT CONFIRMED go={go} dbg={dbg}: not booting; SP805 resets in ~46 s")
                return 1
            log(f"CPU on fid 7, {mhz} MHz expected; PLLARMA={rd(s, '19000c00')}")
        else:
            log(f"control run: CCU untouched (fid {active})")
        s.send_line(f"bootm {dev.LOAD_ADDR}")
        try:
            s.expect([b"Starting kernel"], dev.BOOTM_BUDGET_S, "bootm 'Starting kernel'")
            t0 = time.monotonic()
            hit = s.expect([b"Please press Enter", b"Kernel panic", b"BusyBox"], dev.USERSPACE_BUDGET_S, "userspace")
        except console.Timeout as e:
            log(f"FAIL: {e}")
            return 1
        log(f"{hit.decode()} {time.monotonic() - t0:.1f} s after handover")
    return 0 if hit != b"Kernel panic" else 1


def ssh(cmdline: str, cap: float = 60) -> str:
    r = subprocess.run(SSH + [cmdline], capture_output=True, text=True, timeout=cap)
    return r.stdout + r.stderr


def temps() -> list[int]:
    out = ssh("cat /sys/class/hwmon/hwmon*/temp*_input 2>/dev/null")
    return [int(x) // 1000 for x in out.split() if x.lstrip("-").isdigit()]


def cmd_linux(a: list[str]) -> int:
    soak = int(a[a.index("--soak") + 1]) if "--soak" in a else 300
    t0 = time.monotonic()
    while time.monotonic() - t0 < SSH_UP_S:
        try:
            if "ok" in ssh("echo ok", 15):
                break
        except subprocess.TimeoutExpired:
            pass
        wait(5)
    else:
        log(f"ssh not up after {SSH_UP_S} s")
        return 1
    log(f"ssh up after {time.monotonic() - t0:.0f} s")
    log("cpuinfo/clk:\n" + ssh("grep -E 'BogoMIPS|model name' /proc/cpuinfo; dmesg | grep -E 'sched_clock|Calibrating'"))
    bench = ("a=$(cut -d' ' -f1 /proc/uptime); dd if=/dev/zero bs=1M count=64 2>/dev/null | md5sum; "
             "b=$(cut -d' ' -f1 /proc/uptime); echo bench_s $a $b")
    for i in range(3):
        log(f"bench {i}: " + ssh(bench, 120).strip().replace("\n", " "))
    log(f"temps C: {temps()}")
    t1 = time.monotonic()
    ssh("(while :; do :; done) >/dev/null 2>&1 & echo $! >/tmp/ct_load.pid")  # one core only: single-A9
    ref_md5 = ""
    while time.monotonic() - t1 < soak:
        wait(SOAK_POLL_S)
        try:
            t = temps()
            m = ssh("dd if=/dev/zero bs=1M count=8 2>/dev/null | md5sum", 60).split()[0]
        except (subprocess.TimeoutExpired, IndexError):
            log("SOAK: ssh lost/timeout")
            return 1
        ref_md5 = ref_md5 or m
        if m != ref_md5:
            log(f"MD5 MISMATCH {m} != {ref_md5}: silent corruption")
            ssh("kill $(cat /tmp/ct_load.pid)")
            return 1
        log(f"soak {time.monotonic() - t1:.0f}s temps={t} md5(8M zeros)={m}")
        if t and max(t) >= TEMP_ABORT_C:
            log(f"ABORT: {max(t)} C >= {TEMP_ABORT_C}")
            ssh("kill $(cat /tmp/ct_load.pid); reboot")
            return 1
    ssh("kill $(cat /tmp/ct_load.pid)")
    log("soak complete")
    return 0


def cmd_e3(_a: list[str]) -> int:
    """At the uboot-2 prompt. BSPI clock select from RAM-resident uboot-2; revert on any mismatch."""
    s = console.Session()
    s.send_line("")
    s.read_idle(0.3, 1.0)
    p = dev.U2_PROMPT
    with s:
        v = rd(s, "1803e000", 1, p)[0]
        rd(s, "1803e010", 1, p)
        ref = [cmd(s, "crc32 1c0e0000 100000", p) for _ in range(3)]
        log(f"V={v:#x} ref crc {ref}")
        cmd(s, f"mw.l 1803e000 {(v & ~6) | 6:x}", p)
        ready = rd(s, "1803e010", 1, p)
        ok = bool(ready) and ready[0] & 1
        got = [cmd(s, "crc32 1c0e0000 100000", p) for _ in range(10)] if ok else []
        good = ok and all(re.search(r"==> *([0-9a-f]{8})", g) and
                          re.search(r"==> *([0-9a-f]{8})", g).group(1) ==
                          re.search(r"==> *([0-9a-f]{8})", ref[0]).group(1) for g in got)
        cmd(s, f"mw.l 1803e000 {v:x}", p)
        log(f"E3 62.5 MHz: {'PASS 10/10 crc match' if good else 'FAIL (reverted)'}")
    return 0 if good else 1


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("dump", "cpu", "linux", "e3"):
        print(__doc__)
        return 2
    return {"dump": cmd_dump, "cpu": cmd_cpu, "linux": cmd_linux, "e3": cmd_e3}[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main())
