#!/usr/bin/env python3
"""poe_test: BCM59111 pse-pd driver (kernel/bcm59111) on the board, read-only first (#26, bench #28).

Usage: poe_test.py [status|off|enable] [--apply] [--enable-port N --go] [--hold-s S]
- Dry-run by default: prints the board commands, runs none. --apply = ssh to the board (root@192.168.25.207, as sensors_test.py).
- status (default, read-only): dmesg lines of the driver, per chip (hwmon label PoE-01..12, die temperature) the 4 ports' admin state
  and status (detect/class/voltage/power/reserved). Checks: 12 chips with PSE files, every port disabled, no port delivering,
  temperatures 20..120 degC, regulator budgets 436000 mW (poe-total).
- off: writes 0 to every port_enable (only ever lowers power). Not part of any other mode.
- enable: ONE port, needs --apply --enable-port N (1..48) --go (Dan's go) and a PoE device on that port (#28 bench Pi). Runs status first and
  refuses unless everything is off, writes 1, polls the port status every 0.5 s for up to 5 s (IEEE 802.3 detect + class + power-on is about
  1 s; expiry = FAIL, value goes to #26), logs class/voltage/power, then ALWAYS writes 0 and reads it back. Never more than one port per run.
- Port N (front, 1..48): chip PoE-(N-1)//4+1, channel (N-1)%4+1 (stock port map, board test: which RJ45 powers).
- Exit: 0 all checks passed, 1 a check failed, 2 usage. Log: tmp/logs/poe_test.log
"""
import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/poe_test.log"
BOARD = "root@192.168.25.207"  # ip-registry us48-boot
SSH = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
       "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=8"]
CALL_S = 20.0  # one board call: ~100 sysfs reads, each an I2C transaction at 100 kHz (< 2 s expected); 20 s = board gone
POLL_S, WINDOW_S = 0.5, 5.0
BUDGET_MW = 436000
DRV = "/sys/bus/i2c/drivers/bcm59111"

STATUS = (f'dmesg | grep bcm59111; for d in {DRV}/*-00*; do echo "CHIP ${{d##*/}} $(cat $d/hwmon/hwmon*/name 2>&1) '
          f'[$(cat $d/hwmon/hwmon*/label 2>&1)] temp $(cat $d/hwmon/hwmon*/temp1_input 2>&1)"; '
          f'for p in 1 2 3 4; do echo "  port$p enable $(cat $d/port${{p}}_enable 2>&1) status $(cat $d/port${{p}}_status 2>&1)"; done; done; '
          'for r in /sys/class/regulator/regulator.*; do echo "REG $(cat $r/name 2>&1) budget_mW $(cat $r/power_budget_milliwatt 2>&1)"; done')


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def board(cmd: str, apply: bool) -> str:
    if not apply:
        log(f"dry-run: {cmd}")
        return ""
    t0 = time.monotonic()
    try:
        r = subprocess.run(SSH + [BOARD, cmd], capture_output=True, text=True, timeout=CALL_S)
    except subprocess.TimeoutExpired:
        raise SystemExit(f"board call over {CALL_S:.0f} s ({time.monotonic() - t0:.1f} s): {cmd[:120]}")
    out = r.stdout + r.stderr
    with LOG.open("a") as f:
        f.write(f"$ {cmd}\n{out}")
    return out


def check(ok: bool, what: str, fails: list) -> None:
    log(("PASS " if ok else "FAIL ") + what)
    if not ok:
        fails.append(what)


def parse(out: str) -> dict:
    """{label: {"dev": i2c device, "temp": mdegC or None, "ports": {1..4: (enable str, status str)}}}"""
    chips, cur = {}, None
    for line in out.splitlines():
        m = re.match(r"CHIP (\S+) \S+ \[(.*)\] temp (.*)$", line)
        if m:
            t = m.group(3).strip()
            cur = chips.setdefault(m.group(2), {"dev": m.group(1), "temp": int(t) if re.fullmatch(r"-?\d+", t) else None,
                                                "ports": {}})
            continue
        m = re.match(r"  port(\d) enable (\S+) status (.*)$", line)
        if m and cur is not None:
            cur["ports"][int(m.group(1))] = (m.group(2), m.group(3).strip())
    return chips


def status(apply: bool) -> tuple[int, dict]:
    fails: list = []
    out = board(STATUS, apply)
    if not apply:
        return 0, {}
    print(out)
    chips = parse(out)
    check(sorted(chips) == [f"PoE-{i:02d}" for i in range(1, 13)], f"12 bcm59111 chips PoE-01..12 (got {len(chips)})", fails)
    for lab, c in sorted(chips.items()):
        t = c["temp"]
        check(t is not None and 20000 <= t <= 120000, f"{lab} temperature {t} mdegC in 20..120 degC", fails)
        check(len(c["ports"]) == 4 and all(en in ("0", "1") for en, _ in c["ports"].values()),
              f"{lab} PSE registered: 4 port_enable files (else see dmesg: fw not running / fw_load=0)", fails)
        for p, (en, st) in sorted(c["ports"].items()):
            check(en == "0" and st.startswith("disabled"), f"{lab} port{p} off (enable {en}, {st})", fails)
    regs = dict(re.findall(r"^REG (\S+) budget_mW (\d+)$", out, re.M))
    check(regs.get("poe-total") == str(BUDGET_MW), f"regulator poe-total budget {BUDGET_MW} mW (got {regs.get('poe-total')})", fails)
    for r in ("poe-card0", "poe-card1"):
        check(r in regs, f"regulator {r} present", fails)
    log(f"status: {len(fails)} check(s) failed" + (": " + "; ".join(fails) if fails else ""))
    return (1 if fails else 0), chips


def port_dir(n: int) -> str:
    lab = f"PoE-{(n - 1) // 4 + 1:02d}"
    return (f'for d in {DRV}/*-00*; do [ "$(cat $d/hwmon/hwmon*/label 2>/dev/null)" = {lab} ] && echo $d; done')


def off(apply: bool) -> int:
    cmd = f'for d in {DRV}/*-00*; do for p in 1 2 3 4; do echo 0 > $d/port${{p}}_enable; done; done; echo off written'
    log("off: every port_enable <- 0")
    out = board(cmd, apply)
    if apply:
        print(out)
    return 0


def enable(apply: bool, n: int, go: bool, hold_s: float) -> int:
    chan = (n - 1) % 4 + 1
    log(f"enable plan: front port {n} = chip PoE-{(n - 1) // 4 + 1:02d} channel {chan}; PoE device must be on that port (#28)")
    log("steps: status (all off) -> port_enable 1 -> poll status 0.5 s x10 -> read class/voltage/power -> hold -> port_enable 0 -> read back")
    if not (apply and go):
        log("not enabling: needs --apply --go (Dan's go); this was the plan only")
        status(apply)
        return 0
    rc, chips = status(True)
    if rc:
        log("refusing to enable: status checks failed (something is not off, or a chip has no PSE)")
        return 1
    d = board(port_dir(n), True).strip().splitlines()
    if len(d) != 1:
        log(f"refusing to enable: chip directory for port {n} not unique: {d}")
        return 1
    d = d[0]
    fails: list = []
    seen = ""
    try:
        log(f"port {n}: {d}/port{chan}_enable <- 1")
        out = board(f"echo 1 > {d}/port{chan}_enable 2>&1; echo rc=$?", True)
        check("rc=0" in out, f"enable accepted ({out.strip()})", fails)
        t0 = time.monotonic()
        while out.find("rc=0") >= 0 and time.monotonic() - t0 < WINDOW_S:
            seen = board(f"cat {d}/port{chan}_status", True).strip()
            log(f"  +{time.monotonic() - t0:.1f} s {seen}")
            if seen.startswith("delivering"):
                break
            time.sleep(POLL_S)  # no event source yet (no IRQ): poll; 0.5 s = half the expected detect+class+power-on time
        check(seen.startswith("delivering"), f"port {n} delivering within {WINDOW_S:.0f} s (last: {seen})", fails)
        m = re.match(r"delivering class (\d+) voltage_uV (\d+) power_mW (\d+) reserved_mW (\d+)", seen)
        if m:
            cls, uv, mw, rsv = map(int, m.groups())
            log(f"class {cls}, {uv / 1e6:.2f} V, {mw / 1000:.2f} W, reserved {rsv} mW")
            check(1 <= cls <= 4, f"class 1..4 (got {cls})", fails)
            check(40e6 <= uv <= 60e6, f"voltage 40..60 V (got {uv / 1e6:.2f})", fails)
            check(rsv > 0, f"budget reserved while enabled ({rsv} mW)", fails)
        if hold_s > 0:
            time.sleep(hold_s)  # operator-requested hold with the PD powered (--hold-s), default 0
    finally:
        out = board(f"echo 0 > {d}/port{chan}_enable 2>&1; cat {d}/port{chan}_enable; cat {d}/port{chan}_status", True)
        log(f"disabled again: {out.strip()}")
        check(out.splitlines()[-2:-1] == ["0"] and "reserved_mW 0" in out, "port off and budget released after disable", fails)
    log(f"enable: {len(fails)} check(s) failed" + (": " + "; ".join(fails) if fails else ""))
    return 1 if fails else 0


def main() -> int:
    help_guard(__doc__, strict=True, known=("--apply", "--enable-port", "--go", "--hold-s"))
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mode", nargs="?", default="status", choices=("status", "off", "enable"))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--enable-port", type=int, metavar="N")
    ap.add_argument("--go", action="store_true")
    ap.add_argument("--hold-s", type=float, default=0.0)
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if a.mode == "enable":
        if a.enable_port is None or not 1 <= a.enable_port <= 48:
            ap.error("enable needs --enable-port N (1..48)")
        return enable(a.apply, a.enable_port, a.go, a.hold_s)
    if a.enable_port is not None or a.go:
        ap.error("--enable-port/--go belong to mode enable")
    return off(a.apply) if a.mode == "off" else status(a.apply)[0]


if __name__ == "__main__":
    sys.exit(main())
