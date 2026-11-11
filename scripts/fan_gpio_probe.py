#!/usr/bin/python3
"""fan_gpio_probe — drive candidate GPIO pins for 'fan power' from the OpenWrt shell, one announced step at a time.

Usage: scripts/fan_gpio_probe.py [--hold S]
- Needs OpenWrt on the console (busybox devmem). Stops us48-fand first (it rewrites CMIC_GP_DATA_OUT), restarts it at the end.
- Candidates: CMIC_GP1 of unit 0 / unit 1 / both -> 1; CCA GPIO 4, 5, 10 -> opposite of their current level.
- Each step restores the registers. GP2/GP3 (SFP TX-disable) are not touched. Log: tmp/logs/fan_gpio_probe.log
"""
import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import console  # noqa: E402

LOG = Path(__file__).resolve().parent.parent / "tmp/logs/fan_gpio_probe.log"
GP0, GP1 = 0x48002000, 0x0A002000  # CMIC GP block: unit 0 (AXI), unit 1 (PCIe BAR of BCM53346)
CCA_DIN, CCA_DOUT, CCA_OUTEN = 0x18000060, 0x18000064, 0x18000068


def run(s, cmd: str) -> str:
    s.buf = b""
    s.send_line(cmd)
    return s.read_idle(0.3, 5.0).decode(errors="replace")


def rd(s, addr: int) -> int:
    # The tty echoes the command line, which also contains 0x..: only the `V=0x` result line counts, and the
    # echoed `V=$(` has no hex after it.
    out = run(s, f"echo V=$(devmem 0x{addr:x} 32)")
    m = re.search(r"^V=(0x[0-9a-fA-F]+)\s*$", out.replace("\r", ""), re.M)
    if not m:
        raise RuntimeError(f"no value for 0x{addr:x}: {out!r}")
    return int(m.group(1), 16)


def wr(s, addr: int, val: int) -> None:
    run(s, f"devmem 0x{addr:x} 32 0x{val:x}")


def say(msg: str) -> None:
    print(msg, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%S%z ") + msg + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # 8 s: a fan stop is audible within ~2-3 s; leaves time to say "now" before the step is restored.
    ap.add_argument("--hold", type=float, default=8.0)
    a = ap.parse_args()
    with console.Session() as s:
        run(s, "/etc/init.d/us48-fand stop")
        base = {GP0 + 4: rd(s, GP0 + 4), GP0 + 8: rd(s, GP0 + 8), GP1 + 4: rd(s, GP1 + 4), GP1 + 8: rd(s, GP1 + 8),
                CCA_DOUT: rd(s, CCA_DOUT), CCA_OUTEN: rd(s, CCA_OUTEN)}
        din = rd(s, CCA_DIN)
        # Sanity: a wrong readback must never become a write (an address echoed as a value did on the first run).
        if base[GP0 + 8] > 0xF or base[GP1 + 8] > 0xF or base[CCA_OUTEN] > 0xFFF or din > 0xFFF:
            raise SystemExit(f"implausible baseline {base} din={din:#x}: aborting before any write")
        say("baseline " + " ".join(f"{k:#x}={v:#x}" for k, v in base.items()) + f" cca_din={din:#x}")

        def restore():
            for k, v in base.items():
                wr(s, k, v)

        steps = [
            ("CMIC_GP1 unit0 -> 1", [(GP0 + 8, base[GP0 + 8] | 2), (GP0 + 4, base[GP0 + 4] | 2)]),
            ("CMIC_GP1 unit1 -> 1", [(GP1 + 8, base[GP1 + 8] | 2), (GP1 + 4, base[GP1 + 4] | 2)]),
            ("CMIC_GP1 both -> 1", [(GP0 + 8, base[GP0 + 8] | 2), (GP0 + 4, base[GP0 + 4] | 2),
                                    (GP1 + 8, base[GP1 + 8] | 2), (GP1 + 4, base[GP1 + 4] | 2)]),
        ]
        for bit in (4, 5, 10):
            lvl = (din >> bit) & 1
            dout = (base[CCA_DOUT] & ~(1 << bit)) | ((1 - lvl) << bit)
            steps.append((f"CCA GPIO bit {bit} -> output {1 - lvl} (was in={lvl})",
                          [(CCA_DOUT, dout), (CCA_OUTEN, base[CCA_OUTEN] | (1 << bit))]))
        try:
            for i, (name, writes) in enumerate(steps, 1):
                say(f"STEP {i}/{len(steps)} {name}")
                for addr, val in writes:
                    wr(s, addr, val)
                time.sleep(a.hold)
                restore()
        finally:
            restore()
            run(s, "/etc/init.d/us48-fand start")
    say("PROBE DONE (registers restored, us48-fand restarted)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
