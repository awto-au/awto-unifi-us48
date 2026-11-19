#!/usr/bin/env python3
"""led_test: board checks for the reset button, system LED and LEDUP port LEDs (docs/leds.md, #60, #14).

Usage: led_test.py [STEP ...] [--apply] [--interactive] [--ports 1,8,17] [--host root@IP]
- Dry-run by default: prints every board command and what Dan should see, runs nothing. --apply = ssh (scripts/_board.py).
- --interactive: after each visual step wait for Enter (answer y/n/text, logged). Without it no step waits: visual
  steps set the state, read it back from sysfs, print the expected look and move on.
- Steps (default all, in this order): preflight button sysled ledup prog regs patterns ports cages chain links reload
  latch final
  - preflight: kernel objects exist (us48-leds, gpio-a, input device, LED class devices); LEDUP running per unit.
  - button: reset-button line reads high = not pressed (debugfs gpio); --interactive: press it once, 2 input events read.
  - sysled: white on, blue on (+ CCA DIN/DOUT/OUTEN readback, bit 7 = pad level), both off, white blink.
  - ledup: TICK advances; refresh rate from two `chain` reads (22.4 Hz measured 2026-11-19).
  - prog: LEDUP0 program RAM of both units read back (devmem, read only) = kernel/us48-cmic/us48_ledup_prog.h.
  - regs: read only: LEDUP CTRL delays 6/5 and PORT_ORDER_REMAP 0-7 = SDK/stock values (docs/leds.md).
  - patterns: on, off, booting, error, fallback, panic (sysfs pattern only, never a real panic), hw.
  - ports: each column (default every port, or --ports) forced on alone, bit 0 then bit 1; readback via `ports`.
  - cages: SFP+ 1/2, SFP 1/2: symbols 10, 01, 11 one cage at a time (cage colour map, docs/leds.md).
  - reload: rmmod/insmod us48_cmic: state must come back `hw`, not `booting (manual)`. Drops every port link
    briefly; only with --interactive (ge1 = management: the ssh session reconnects).
  - chain: only with --interactive: first the stock-derived format (2 bits, front order, 28 slots, LSB first); if Dan says no, tries nbits 1/2/4 x order front/rfront/phys/rphys x pol 0/full with all cells on,
    Dan says which combination lights the panel correctly; writes nothing permanent (module reload resets).
  - links: loop cables 8<->17 (unit 0) and 32<->41 (unit 1): admin up, carrier within 5 s, `ports` link_mbps and CTL
    value; IPv6 multicast ping for activity; admin state restored as found. ge1 (management) is never touched.
  - latch: error latch with reason, pattern write refused (EBUSY), clear releases.
  - final: clear -> state hw.
- Time limits: ssh call 15 s (_board.CALL_S); carrier 5 s = 1000BASE-T AN ~3 s + 1 s link poll, 1.25x (copper_check.py);
  button press 20 s (a human); expiry = step FAIL with the limit and elapsed time.
- Exit: 0 all checks passed, 1 a check failed, 2 usage/board problem. Log: tmp/logs/led_test.log
"""
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402

help_guard(__doc__, strict=True, known=("--apply", "--interactive", "--ports", "--host"))

import _board  # noqa: E402
import hwdb  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/led_test.log"
L = "/sys/devices/platform/us48-leds"
WHITE, BLUE = "/sys/class/leds/white:status", "/sys/class/leds/blue:status"
STEPS = ["preflight", "button", "sysled", "ledup", "prog", "regs", "patterns", "ports", "cages", "chain", "links",
         "reload", "latch", "final"]
_DB = hwdb.lite()
LOOPS = [(_DB.netdev(g), _DB.netdev(o)) for _, g, o in _DB.loop_pairs()]  # loop cables, hwdb/board/ports.toml
LINK_S = 5.0
PRESS_S = 20.0


class Run:
    def __init__(self, apply: bool, interactive: bool, ports: list[int] | None):
        self.apply, self.interactive, self.ports = apply, interactive, ports
        self.failed = 0

    def log(self, msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
        print(line, flush=True)
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(line + "\n")

    def sh(self, cmd: str, call_s: float = _board.CALL_S) -> str:
        self.log(f"$ {cmd}")
        if not self.apply:
            return ""
        t = time.monotonic()
        rc, out = _board.ssh(cmd, call_s)
        if rc == 255:
            self.log(f"ssh failed after {time.monotonic() - t:.1f} s (limit {call_s} s): {out.strip()}")
            raise SystemExit(2)
        for ln in out.rstrip().splitlines():
            self.log(f"  {ln}")
        return out

    def check(self, ok: bool, what: str) -> None:
        if not self.apply:
            return
        self.log(f"{'PASS' if ok else 'FAIL'} {what}")
        self.failed += not ok

    def see(self, what: str) -> None:
        self.log(f"LOOK: {what}")
        if self.interactive and self.apply:
            ans = input("  seen as described? [y/n/text] ").strip()
            self.log(f"  Dan: {ans or 'y'}")
            self.failed += ans.lower().startswith("n")


def preflight(r: Run) -> None:
    out = r.sh(f"cat {L}/state {L}/chain; ls /sys/class/leds | head -5; ls /sys/class/leds | wc -l; "
               "grep -A4 -i 'gpio-keys' /proc/bus/input/devices; ls /sys/bus/platform/drivers/nsp-gpio-a")
    for ln in out.splitlines():
        if ln.startswith("unit") and " STATUS " in ln:
            st = int(ln.split(" STATUS ")[1].split()[0], 16)  # RUNNING (bit 8) reads 0 between refreshes (#14)
            r.log(f"{ln.split()[0]} LEDUP STATUS {st:08x}: RUNNING {st >> 8 & 1}, PC {st & 0xff:#x}; running = TICK step")
    r.check("Handlers" in out, "gpio-keys-polled input device registered")
    r.check("white:status" in out or "blue:status" in out, "system LED class devices")


def button(r: Run) -> None:
    out = r.sh("grep -E 'reset-button|led-white|led-blue|pse-reset' /sys/kernel/debug/gpio")
    r.check("reset-button" in out and " in " in out and "hi" in out.split("reset-button")[1].split("\n")[0], "reset button idle high (not pressed)")
    r.check("pse-reset" in out and "out" not in out.split("pse-reset")[1].split("\n")[0], "pse-reset (GPIO 9) untouched: not an output")
    if not r.interactive:
        r.log("button press: skipped (needs --interactive)")
        return
    r.log("PRESS the recessed reset button once, briefly (< 1 s), within 20 s")
    ev = r.sh("E=$(grep -l gpio-keys /sys/class/input/event*/device/name | head -1 | cut -d/ -f5); "
              "dd if=/dev/input/$E bs=16 count=4 2>/dev/null | hexdump -C | head -4", PRESS_S)
    r.check(bool(ev.strip()), "input events read (KEY_RESTART press + release)")


def sysled(r: Run) -> None:
    r.sh(f"cat {WHITE}/trigger {BLUE}/trigger | tr ' ' '\\n' | grep '\\['")
    r.sh(f"echo none > {WHITE}/trigger; echo 1 > {WHITE}/brightness; echo 0 > {BLUE}/brightness")
    r.see("system LED steady WHITE (GPIO 6 high)")
    r.sh(f"echo 0 > {WHITE}/brightness; echo 1 > {BLUE}/brightness")
    out = r.sh("devmem 0x18000060 32; devmem 0x18000064 32; devmem 0x18000068 32")  # CCA DIN, DOUT, OUTEN (read only)
    w = [int(x, 16) for x in out.split()] if r.apply else []
    if len(w) == 3:
        r.log(f"CCA bit 7: DIN {w[0] >> 7 & 1} DOUT {w[1] >> 7 & 1} OUTEN {w[2] >> 7 & 1}; bit 6: DIN {w[0] >> 6 & 1}")
        r.check(w[1] >> 7 & 1 and w[2] >> 7 & 1, "CCA 7 driven high (DOUT + OUTEN), = stock 'ready' led_pattern 1")
        r.check(w[0] >> 7 & 1, "CCA 7 pad reads high (DIN): pin not held low / not muxed away")
    r.see("system LED steady BLUE (CCA 7 high, CCA 6 low = stock 'ready' = syswrapper 'Solid blue')")
    r.sh(f"echo 0 > {BLUE}/brightness")
    r.see("system LED OFF (both pins low)")
    r.sh(f"echo timer > {WHITE}/trigger")
    r.see("system LED blinking white, 0.5 s on / 0.5 s off (= stock boot pattern '20')")
    r.sh(f"echo none > {WHITE}/trigger; echo 1 > {WHITE}/brightness")


def ledup(r: Run) -> None:
    a = r.sh(f"cat {L}/chain")
    b = r.sh(f"cat {L}/chain")
    for ln in b.splitlines():
        if ln.startswith("unit") and "tick" in ln:
            n = int(ln.split("(")[1].split()[0])
            ms = int(ln.split(" in ")[1].split()[0])
            hz = n * 1000 / ms if ms else 0
            r.log(f"{ln.split()[0]}: {n} refreshes in {ms} ms = {hz:.1f} Hz")
            r.check(n > 0, f"{ln.split()[0]} LEDUP program running (TICK advances)")
    r.check("unit0" in a, "unit 0 LEDUP loaded")


PROG_RAM = 0x20800  # CMIC_LEDUP0_PROGRAM_RAM (hwdb cmic0.LEDUP_PROGRAM_RAM), one byte per 32-bit word
PROG_H = ROOT / "kernel/us48-cmic/us48_ledup_prog.h"


def prog(r: Run) -> None:
    """Read-only: LEDUP0 program RAM of both units (devmem) = the generated header, rest zero-padded."""
    want = [int(b, 16) for b in PROG_H.read_text().split("{", 1)[1].split("}", 1)[0].replace(",", " ").split()]
    want += [0] * (256 - len(want))
    for unit in (0, 1):
        base = hwdb.cmic_base(unit) + PROG_RAM
        out = r.sh(f"i=0; while [ $i -lt 256 ]; do devmem $((0x{base:x} + 4 * i)) 32; i=$((i + 1)); done | tr '\\n' ' '",
                   60.0)  # 256 devmem forks; the debug image forks slowly (#14): 60 s backstop
        if not r.apply:
            continue
        got = [int(w, 16) & 0xff for w in out.split()]
        bad = [i for i in range(256) if i >= len(got) or got[i] != want[i]]
        r.check(not bad, f"unit{unit} PROGRAM_RAM = us48_ledup_prog.h ({len(got)} words read, {len(bad)} differ"
                + (f", first at {bad[0]:#x}" if bad else "") + ")")


LEDUP_CTRL, LEDUP_REMAP = 0x20000, 0x20010  # hwdb cmic0.LEDUP_CTRL / LEDUP_PORT_ORDER_REMAP


def remap_want(i: int) -> int:
    """SDK _hurricane2_ledup_init: ports 4i+2..4i+5, blocks starting 26/30 reversed (= stock u0 trace)."""
    p0 = 4 * i + 2
    ps = [p0 + 3 - k for k in range(4)] if p0 in (26, 30) else [p0 + k for k in range(4)]
    return sum(p << (6 * k) for k, p in enumerate(ps))


def regs(r: Run) -> None:
    """Read only: scan delays + port remap the driver writes at probe (activity root cause, #14)."""
    for unit in (0, 1):
        base = hwdb.cmic_base(unit)
        out = r.sh(" ; ".join(f"devmem 0x{base + a:x} 32" for a in [LEDUP_CTRL] + [LEDUP_REMAP + 4 * i for i in range(8)]))
        if not r.apply:
            continue
        w = [int(x, 16) for x in out.split()]
        if len(w) != 9:
            r.check(False, f"unit{unit} LEDUP regs read ({len(w)} of 9 words)")
            continue
        r.check((w[0] >> 1 & 7, w[0] >> 4 & 0x3f) == (5, 6), f"unit{unit} CTRL {w[0]:#x}: intra delay 5, start delay 6")
        bad = [i for i in range(8) if w[1 + i] != remap_want(i)]
        r.check(not bad, f"unit{unit} PORT_ORDER_REMAP = SDK" + (f" (differ: {bad})" if bad else ""))


def patterns(r: Run) -> None:
    looks = {"on": "every port LED green (one colour: '11' is dark on copper)", "off": "every port LED off",
             "booting": "every port green, slow blink (~0.7 Hz)",
             "error": "every copper port and SFP+ amber (bit 1), SFP 1/2 green, fast blink (~2.8 Hz)",
             "fallback": "same colours as error, mid blink (~1.4 Hz)",
             "panic": "fast blink, alternating green / amber between neighbouring ports (SFP 1/2 green)",
             "hw": "ports with link lit (green 1G copper / 10G SFP+, amber 10/100), others off; idle = steady"}
    for p, look in looks.items():
        r.sh(f"echo {p} > {L}/pattern; cat {L}/state")
        r.see(f"pattern {p}: {look}")


def columns(r: Run) -> list[tuple[int, int, str]]:
    out = r.sh(f"cat {L}/ports")
    cols = []
    for ln in out.splitlines()[1:]:
        f = ln.split()
        if len(f) >= 3 and f[0].isdigit():
            cols.append((int(f[0]), int(f[1]), f[2]))
    if not r.apply:
        cols = [(i, i + 1, f"ge{i + 1}") for i in range(48)]
    return cols


def ports(r: Run) -> None:
    cols = columns(r)
    n = len(cols)
    for col, front, name in cols:
        if r.ports and front not in r.ports:
            continue
        for bit in (0, 1):
            cells = ["0"] * (2 * n)
            cells[bit * n + col] = "7"
            r.sh(f"echo {''.join(cells)} > {L}/pattern; grep '^{col} ' {L}/ports")
            r.see(f"only front {front} ({name}) lit, bit {bit} colour")
    r.sh(f"echo hw > {L}/pattern")


CAGES = {49: "SFP+ 1", 50: "SFP+ 2", 51: "SFP 1", 52: "SFP 2"}  # panel labels (docs/port-map.md)


def cages(r: Run) -> None:
    """Cage colour map: each cage alone with bit 0, bit 1, both. Expected per docs/leds.md (I where marked)."""
    cols = {front: col for col, front, _ in columns(r)}
    n = len(columns(r)) if r.apply else 52
    expect = {49: ("green", "second colour (I)", "purple (seen in panic)"), 50: ("green", "second colour (I)", "purple"),
              51: ("dark", "green", "green"), 52: ("dark", "green", "green")}
    for front, label in CAGES.items():
        col = cols.get(front, front - 1)
        for sym, (b0, b1) in (("10", (1, 0)), ("01", (0, 1)), ("11", (1, 1))):
            cells = ["0"] * (2 * n)
            cells[col], cells[n + col] = "7" if b0 else "0", "7" if b1 else "0"
            r.sh(f"echo {''.join(cells)} > {L}/pattern")
            r.see(f"only {label} (front {front}) lit, symbol bit0={b0} bit1={b1}: expect {expect[front][('10', '01', '11').index(sym)]}; say the colour")
    r.sh(f"echo hw > {L}/pattern")


STOCK ="nbits=2 pol=0 order=front extra=0 slots=28"  # stock program format (docs/ledup-stock.md)


def chain(r: Run) -> None:
    if not r.interactive:
        r.log("chain scan: skipped (needs --interactive)")
        return
    found = []
    r.sh(f"echo '{STOCK}' > {L}/chain; echo booting > {L}/pattern")
    r.log("chain = stock format: front 1..48 blink in order on bit 0 (1G colour), no mirrored or shifted panel")
    if input("  stock format correct? [y/n] ").strip().lower().startswith("y"):
        r.log("chain: stock format confirmed, scan skipped")
        r.sh(f"echo '{STOCK}' > {L}/chain")
        return
    for nbits in (2, 1, 4):
        for order in ("front", "rfront", "phys", "rphys"):
            for pol in (0, (1 << nbits) - 1):
                r.sh(f"echo 'nbits={nbits} pol={pol:x} order={order}' > {L}/chain; echo booting > {L}/pattern")
                r.log(f"chain nbits={nbits} pol={pol:x} order={order}: front 1 should blink slowly first, then 2, ... (bit 0)")
                ans = input("  correct panel order and colour? [y/n] ").strip().lower()
                r.log(f"  Dan: {ans}")
                if ans.startswith("y"):
                    found.append((nbits, pol, order))
    r.log(f"chain combinations Dan accepted: {found}")
    r.sh(f"echo '{STOCK}' > {L}/chain")


def links(r: Run) -> None:
    r.sh(f"echo clear > {L}/clear")
    for a, b in LOOPS:
        snap = r.sh(f"for p in {a} {b}; do echo $p $(cat /sys/class/net/$p/operstate) $(ip -o link show $p | grep -c '[<,]UP[,>]'); done")
        r.sh(f"ip link set {a} up; ip link set {b} up")
        t0 = time.monotonic()
        up = not r.apply
        while r.apply and time.monotonic() - t0 < LINK_S:
            out = r.sh(f"cat /sys/class/net/{a}/carrier /sys/class/net/{b}/carrier")
            if out.split() == ["1", "1"]:
                up = True
                break
        r.check(up, f"{a}<->{b} carrier within {LINK_S} s (elapsed {time.monotonic() - t0:.1f} s)")
        out = r.sh(f"grep -wE '{a}|{b}' {L}/ports; cat /sys/class/leds/{a}:green:lan/trigger | grep -o '\\[netdev\\]'; "
                   f"cat /sys/class/leds/{a}:green:lan/offloaded")
        r.check("1000" in out, f"{a}/{b} link_mbps 1000 in ports")
        r.check(out.strip().endswith("1"), f"{a} green LED offloaded to LEDUP (netdev trigger)")
        r.see(f"{a} and {b}: green STEADY (1G link, idle: no flicker)")
        r.sh(f"ping -6 -c 100 -i 0.02 -I {a} ff02::1 > /dev/null 2>&1; echo done")
        r.see(f"{a} and {b} blinked (~4.5 Hz, off 2 of 5 refreshes) only during the 2 s multicast ping, steady after")
        for ln in snap.splitlines():
            f = ln.split()
            if len(f) == 3 and f[2] == "0":
                r.sh(f"ip link set {f[0]} down")


def reload(r: Run) -> None:
    if not r.interactive:
        r.log("reload: skipped (needs --interactive: drops every port link incl. ge1 management)")
        return
    cmd = [sys.executable, str(ROOT / "scripts/mod_reload.py"), "us48_cmic"]  # handles ge1/ssh loss; own limits
    r.log("$ " + " ".join(cmd))
    if r.apply:
        rc = subprocess.run(cmd).returncode
        r.check(rc == 0, f"mod_reload.py us48_cmic rc {rc}")
    out = r.sh(f"cat {L}/state; dmesg | grep 'ledup0:' | tail -2")
    r.check(out.strip().startswith("hw") or "\nhw" in out, "state hw after module reload (not booting)")
    r.check("reload: was running" in out, "driver saw its program running (reload detection)")
    r.see("port LEDs show link state (hw), no slow booting blink")


def latch(r: Run) -> None:
    r.sh(f"echo led-test > {L}/error; cat {L}/state {L}/error")
    r.see("copper + SFP+ amber, SFP 1/2 green, all fast blink (error latched)")
    out = r.sh(f"echo on > {L}/pattern 2>&1; echo rc=$?")
    r.check("rc=1" in out or "busy" in out.lower(), "pattern refused while latched")
    r.sh(f"echo 1 > {L}/clear; cat {L}/state {L}/error")


def final(r: Run) -> None:
    out = r.sh(f"echo 1 > {L}/clear; cat {L}/state")
    r.check(out.startswith("hw"), "state hw after clear")


def main() -> int:
    args = sys.argv[1:]
    if "--host" in args:
        _board.HOST = args[args.index("--host") + 1]
    ports_sel = None
    if "--ports" in args:
        ports_sel = [int(x) for x in args[args.index("--ports") + 1].split(",")]
    steps = [a for a in args if a in STEPS] or STEPS
    bad = [a for a in args if not a.startswith("-") and a not in STEPS and (args.index(a) == 0 or args[args.index(a) - 1] not in ("--ports", "--host"))]
    if bad:
        print(f"unknown step(s) {bad}; steps: {' '.join(STEPS)}", file=sys.stderr)
        return 2
    r = Run("--apply" in args, "--interactive" in args, ports_sel)
    r.log(f"led_test {'APPLY' if r.apply else 'DRY-RUN'} host {_board.HOST} steps {' '.join(steps)} pid {os.getpid()}")
    for s in steps:
        r.log(f"== {s}")
        globals()[s](r)
    r.log(f"done: {r.failed} failed check(s)")
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(main())
