#!/usr/bin/python3
"""miim — read clause-22 PHY registers through the unit-0 CMIC MIIM controller, from the OpenWrt shell (devmem).

Usage: scripts/miim.py [--phy 0x09] [--bus 0] [--internal] [--regs 0,1,2,3]
- Unit 0 CMIC at AXI 0x48000000; offsets from regdb (CMIC_CMC0_MIIM_*). Run under OpenWrt: stock's link-scan
  drives the same controller. Read-only on the PHY. Log: tmp/logs/miim.log
"""
import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import console  # noqa: E402

LOG = Path(__file__).resolve().parent.parent / "tmp/logs/miim.log"
CMIC = 0x48000000
PARAM, RDATA, ADDR, CTRL, STAT = (CMIC + o for o in (0x31080, 0x31084, 0x31088, 0x3108C, 0x31090))
RD_START, OPN_DONE = 1 << 1, 1 << 0
# MDC ~2.5 MHz: one C22 frame = 64 bits ≈ 26 µs. Each devmem poll is a ~10 ms console round trip, so 10 polls
# (~100 ms, ~4000x the frame) is the cap. Expiry = controller never set OPN_DONE: logged, read aborted.
DONE_POLLS = 10


def run(s, cmd: str) -> str:
    s.buf = b""
    s.send_line(cmd)
    return s.read_idle(0.3, 5.0).decode(errors="replace")


def rd(s, addr: int) -> int:
    out = run(s, f"echo V=$(devmem 0x{addr:x} 32)")
    m = re.search(r"^V=(0x[0-9a-fA-F]+)\s*$", out.replace("\r", ""), re.M)
    if not m:
        raise RuntimeError(f"no value for 0x{addr:x}: {out!r}")
    return int(m.group(1), 16)


def wr(s, addr: int, val: int) -> None:
    run(s, f"devmem 0x{addr:x} 32 0x{val:x}")


def miim_read(s, phy: int, bus: int, internal: bool, reg: int) -> int | None:
    wr(s, CTRL, 0)
    wr(s, PARAM, (int(internal) << 25) | (bus << 22) | (phy << 16))
    wr(s, ADDR, reg & 0x1F)
    wr(s, CTRL, RD_START)
    for _ in range(DONE_POLLS):
        if rd(s, STAT) & OPN_DONE:
            val = rd(s, RDATA) & 0xFFFF
            wr(s, CTRL, 0)
            return val
    wr(s, CTRL, 0)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--phy", type=lambda x: int(x, 0), default=0x09)
    ap.add_argument("--bus", type=lambda x: int(x, 0), default=0)
    ap.add_argument("--internal", action="store_true")
    ap.add_argument("--regs", default="0,1,2,3")
    a = ap.parse_args()
    lines, rc = [], 0
    with console.Session() as s:
        for reg in (int(r, 0) for r in a.regs.split(",")):
            v = miim_read(s, a.phy, a.bus, a.internal, reg)
            if v is None:
                lines.append(f"phy 0x{a.phy:02x} bus {a.bus} reg {reg}: no OPN_DONE after {DONE_POLLS} polls")
                rc = 1
            else:
                lines.append(f"phy 0x{a.phy:02x} bus {a.bus} reg {reg}: 0x{v:04x}")
    text = "\n".join(lines)
    print(text)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%S%z ") + text.replace("\n", "; ") + "\n")
    return rc


if __name__ == "__main__":
    sys.exit(main())
