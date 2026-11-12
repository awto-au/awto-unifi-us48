#!/usr/bin/env python3
"""Time a2 running the same loop from SRAM and from host RAM (via PAXB OARR_0), I-cache off then on.

Usage (board at uboot-2, stub running or startable): US48_HOST=lizard python3 scripts/a2_bench.py
Build payloads: tools/a2_bench.S (see top of file; tmp/a2/bench_noic.bin, bench_ic.bin). Log: tmp/logs/a2_loader.log.
Host RAM scratch: physical 0x02800000 (unused by uboot-2), seen by a2 at AXI 0x0a800000 via OARR_0 = 0x08000001.
"""
import pathlib
import struct
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import a2_loader as L  # noqa: E402
import console  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent
N = 20000                 # loop count: 2 instr per iteration, long enough to dwarf call overhead
HOST_PHYS, HOST_AXI = 0x02800000, 0x0A800000
SRAM_AT = 0x1B000400
TIMER = 0x19020200        # A9 global timer (periph clock), control at +8


def words(path):
    d = pathlib.Path(path).read_bytes()
    d += b"\0" * (-len(d) % 4)
    return struct.unpack("<%dI" % (len(d) // 4), d)


with console.Session() as s:
    s.write(b"\x03\r\n")
    s.read_idle(1.0, 3.0)
    a = L.A2(s)
    try:
        m = a.mbox()
        m2 = a.mbox()
        if m[7] != L.MAGIC or m[6] == m2[6]:
            L.log("stub not running: reset + start")
            a.page(0x1803E000)
            a.wr(0, 0x20)
            a.wr(0, 0x30)
            a.start()
        a.cmd("mw.l 0x08002d24 0", 0.15)
        a.cmd("mw.l 0x08002d40 0", 0.15)
        a.cmd("mw.l 0x08002d44 0", 0.15)
        a.cmd("mw.l 0x08002d20 0x08000001", 0.15)            # OARR_0 -> host RAM
        a.command(3, TIMER + 8, 1)                            # enable the global timer
        for variant in ("noic", "ic"):
            w = words(REPO / f"tmp/a2/bench_{variant}.bin")
            a.load(struct.pack("<%dI" % len(w), *w), SRAM_AT)
            for i, x in enumerate(w):
                a.cmd("mw.l 0x%x 0x%08x" % (HOST_PHYS + 4 * i, x), 0.12)
            for name, addr in (("SRAM", SRAM_AT), ("host RAM", HOST_AXI)):
                t = a.command(1, addr, N)
                L.log("%-4s payload from %-8s: %d ticks for %d iterations = %.2f ticks/iter" % (variant, name, t, N, t / N))
    finally:
        a.cmd("mw.l 0x08002d20 0", 0.2)
        a.cmd("mw.l 0x%x 0x18032001" % L.IM, 0.2)
