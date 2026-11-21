#!/usr/bin/env python3
"""Read the hook-entry counters of a bcm59111-awto-probe{A,B,C}.bin chip over ssh i2cget and derive tick period, f_cpu, hook-3 rate.

Usage: poe_fw_probe_read.py --chip BUS-00AA [--seconds 12] [--host IP] [--selftest]
  --chip   kernel i2c device of the chip under test, e.g. 9-0028 (bus-address as in /sys/bus/i2c/devices). Required: confirm it first.
  --seconds  sampling window, default 12 (>= 10 s asked for; first and last read include all counters).
  --selftest  unit test with synthetic samples (16-bit wrap, torn hi/lo reads, aliasing guard); no board access.
  Read only: i2cget -f -y (no writes). Counter registers (poe-fw/inc/common.h, hwdb regs.tsv): hook 3 0x22 / 0x23, hook 8 0x2D / 0x40,
  hook 2 0x45 / 0x4F, slots 4..7 0x54 / 0x59, 0x5A / 0x5B, 0x5C / 0x5D, 0x1C / 0x1D (lo / hi), status 0x16.
  Method: each pair is read hi, lo, hi on the board between two /proc/uptime stamps (board clock, 10 ms resolution, so ssh latency
  does not enter). A pair is torn when the two hi reads differ: lo < 0x80 means the carry happened before the lo read (use hi2).
  Per sample only hooks 8 and 3 are read (about 7 i2cget forks, well under one cadence); all counters at the first and last sample.
  Cadence 1 s: a 16-bit counter must not wrap more than once between samples; at 10 ms tick it wraps in 6.55 s (65536 ticks, not 2.5 s:
  the low byte alone wraps in 2.56 s), the hook-3 counter at 1.5 ms in 98 s. Deltas are masked to 16 bit; a delta above 0x8000 is
  reported as aliasing. T = window / delta(hook 8), f_cpu = 80016 / T (6668 counts x 12 clk, docs/poe-fw-seq/05-timing.md section 5).
  Log: tmp/logs/poe_fw_probe.log.
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
LOG = ROOT / "tmp/logs/poe_fw_probe.log"
PAIRS = {"h8": (0x40, 0x2D), "h3": (0x23, 0x22), "h2": (0x4F, 0x45), "s4": (0x59, 0x54), "s5": (0x5B, 0x5A), "s6": (0x5D, 0x5C),
         "s7": (0x1D, 0x1C)}                     # name: (hi reg, lo reg)
FAST = ("h8", "h3")
CLK_PER_TICK = 80016                             # 6668 timer-1 counts x 12 clk
SSH_STALL = 15.0   # s: one sample is ~7 i2cget forks (< 2 s expected); 15 s without an answer = board path gone, abort


def log(msg):
    print(msg)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(msg + "\n")


def remote_cmd(bus, addr, names, with_status):
    fn = "rd(){ echo $1 $(cut -d' ' -f1 /proc/uptime) $(i2cget -f -y %d 0x%02x $2) $(i2cget -f -y %d 0x%02x $3) " \
         "$(i2cget -f -y %d 0x%02x $2) $(cut -d' ' -f1 /proc/uptime); }" % (bus, addr, bus, addr, bus, addr)
    calls = [f"rd {n} {PAIRS[n][0]:#04x} {PAIRS[n][1]:#04x}" for n in names]
    if with_status:
        calls.append(f"echo st $(i2cget -f -y {bus} {addr:#04x} 0x16)")
    return fn + "; " + "; ".join(calls)


def parse(text):
    """Lines 'name u0 hi lo hi2 u1' (uptime seconds, 0xNN) and 'st 0xNN' -> {name: (t_mid, value, torn)}, status."""
    out, st = {}, None
    for ln in text.splitlines():
        p = ln.split()
        if len(p) == 2 and p[0] == "st" and p[1].startswith("0x"):
            st = int(p[1], 16)
        elif len(p) == 6 and p[0] in PAIRS and all(x.startswith("0x") for x in p[2:5]):
            u0, hi, lo, hi2, u1 = float(p[1]), int(p[2], 16), int(p[3], 16), int(p[4], 16), float(p[5])
            torn = hi != hi2
            v = ((hi2 if lo < 0x80 else hi) << 8 | lo) if torn else (hi << 8 | lo)
            out[p[0]] = ((u0 + u1) / 2, v, torn)
    return out, st


def unwrap(samples):
    """samples [(t, v)] -> (total count, total time, max single delta); deltas masked to 16 bit."""
    tot, mx = 0, 0
    for (t0, v0), (t1, v1) in zip(samples, samples[1:]):
        d = (v1 - v0) & 0xFFFF
        tot += d
        mx = max(mx, d)
    return tot, samples[-1][0] - samples[0][0], mx


def analyse(series):
    """series {name: [(t, v)]} -> list of report lines."""
    out = []
    for n, s in series.items():
        if len(s) < 2:
            continue
        cnt, dt, mx = unwrap(s)
        alias = " ALIASING (delta > 0x8000: sample faster)" if mx > 0x8000 else ""
        out.append(f"{n}: {cnt} counts in {dt:.2f} s ({len(s)} samples) = {cnt / dt:.3f} /s, max step {mx}{alias}" if dt > 0 else f"{n}: dt 0")
    h8, h3 = series.get("h8", []), series.get("h3", [])
    if len(h8) >= 2:
        cnt, dt, _ = unwrap(h8)
        if cnt:
            T = dt / cnt
            out.append(f"tick T = {T * 1000:.4f} ms  ->  f_cpu = {CLK_PER_TICK / T / 1e6:.4f} MHz (80016 clk per tick)")
    if len(h3) >= 2:
        cnt, dt, _ = unwrap(h3)
        out.append(f"hook 3: {cnt / dt:.3f} calls/s -> period {1000 * dt / cnt:.4f} ms" if cnt else "hook 3: 0 calls in the window (slot 3 did not fire)")
    if len(h8) >= 2 and len(h3) >= 2 and unwrap(h8)[0] and unwrap(h3)[0]:
        out.append(f"hook 3 calls per tick: {unwrap(h3)[0] / unwrap(h8)[0]:.4f}")
    return out


def selftest():
    def sim(rate, t, v0):
        return ((t, (v0 + int(rate * t)) & 0xFFFF))
    # 100 ticks/s from 0xFF00 (wraps in the first second), board clock offset, 1 s cadence, 12 s
    s = [sim(100.0, t + 5000.0, 0xFF00 - int(100.0 * 5000)) for t in range(13)]
    cnt, dt, _ = unwrap(s)
    assert cnt == 1200 and abs(dt - 12) < 1e-9, (cnt, dt)
    lines = analyse({"h8": s, "h3": [sim(500.0, t, 0xFFF0) for t in range(13)]})
    assert any("T = 10.0000 ms" in x and "8.0016 MHz" in x for x in lines), lines
    assert any("period 2.0000 ms" in x for x in lines), lines
    assert any("calls per tick: 5.0000" in x for x in lines), lines
    # aliasing: 40000 counts per second at 1 s cadence
    assert any("ALIASING" in x for x in analyse({"h8": [(t, (t * 40000) & 0xFFFF) for t in range(5)]}))
    # no hook 3
    assert any("did not fire" in x for x in analyse({"h8": s, "h3": [(t, 0) for t in range(5)]}))
    # torn read: hi 0x01 -> 0x02 during the pair; lo 0x03 (after the carry) -> 0x0203; lo 0xFE (before) -> 0x01FE
    t = parse("h8 100.00 0x01 0x03 0x02 100.02\nh3 100.00 0x01 0xfe 0x02 100.02\nst 0xb2\n")
    assert t[0]["h8"][1] == 0x0203 and t[0]["h8"][2] and t[0]["h3"][1] == 0x01FE and t[1] == 0xB2, t
    t = parse("h8 7.10 0x00 0x41 0x00 7.12\nnoise\n")
    assert abs(t[0]["h8"][0] - 7.11) < 1e-9 and t[0]["h8"][1:] == (0x41, False)
    # a torn pair that straddles a wrap of the whole 16 bit counter stays monotone through the mask
    assert unwrap([(0, 0xFFFF), (1, 0x0003)])[0] == 4
    log("selftest PASS")
    return 0


def main():
    help_guard(__doc__)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--chip")
    ap.add_argument("--seconds", type=float, default=12.0)
    ap.add_argument("--host")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if a.selftest:
        return selftest()
    if not a.chip or not re.fullmatch(r"\d+-00[0-9a-fA-F]{2}", a.chip):
        print("--chip BUS-00AA is required (e.g. 9-0028)", file=sys.stderr)
        return 2
    import _ipreg
    from lx import OPTS
    bus, addr = int(a.chip.split("-")[0]), int(a.chip.split("-")[1], 16)
    host = a.host or _ipreg.ip("us48-boot")

    def sample(names, status):
        r = subprocess.run(["ssh", *OPTS, f"root@{host}", remote_cmd(bus, addr, names, status)], capture_output=True, text=True,
                           timeout=SSH_STALL)
        d, st = parse(r.stdout)
        if len(d) != len(names):
            raise SystemExit(f"sample incomplete ({len(d)} of {len(names)} counters): {r.stdout!r} {r.stderr!r}")
        return d, st

    log(f"# {time.strftime('%Y-%m-%dT%H:%M:%S%z')} chip {a.chip} host {host} window {a.seconds} s")
    series = {n: [] for n in PAIRS}
    torn = 0
    t_start = time.monotonic()
    n = 0
    while True:
        last = time.monotonic() - t_start >= a.seconds
        first = n == 0
        d, st = sample(list(PAIRS) if (first or last) else FAST, first or last)
        for k, (t, v, tn) in d.items():
            series[k].append((t, v))
            torn += tn
        if first or last:
            log(f"status 0x16 = {st:#04x} (variant {st >> 4}, bit0 hook-3 entered {st & 1}, bit1 armed {(st >> 1) & 1}, bit2 re-arm done {(st >> 2) & 1})"
                if st is not None else "status unreadable")
        log("sample %d " % n + " ".join(f"{k}={v:#06x}@{t:.2f}" for k, (t, v, _) in d.items()))
        if last:
            break
        n += 1
        # cadence: next deadline on a monotonic grid of 1 s (reason: wrap of the 16-bit counters is >= 49 s, so any cadence below 24 s is unambiguous; 1 s gives >= 10 samples)
        nxt = t_start + n * 1.0
        rem = nxt - time.monotonic()
        if rem > 0:
            time.sleep(rem)
    log(f"torn hi/lo pairs resolved: {torn}")
    for ln in analyse(series):
        log(ln)
    return 0


if __name__ == "__main__":
    sys.exit(main())
