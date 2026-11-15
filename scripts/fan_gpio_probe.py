#!/usr/bin/python3
"""fan_gpio_probe — drive candidate GPIO pins for 'fan power' from the OpenWrt shell, one announced step at a time.

Usage: scripts/fan_gpio_probe.py [--hold S]
- Needs OpenWrt on the console (busybox devmem). Sets us48_fan pwm1_enable=1 (manual) first, restores the old value at the end.
  Manual pins the level: the driver writes a frame (and CMIC GP pins) only on a level change or a fail-level event.
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
import hwdb  # noqa: E402

LOG = Path(__file__).resolve().parent.parent / "tmp/logs/fan_gpio_probe.log"
# CMIC GP block: unit 0 (AXI), unit 1 (PCIe BAR of BCM53346); addresses from hwdb/soc (cmic.toml, periph.toml)
GP0, GP1 = hwdb.addr("cmic0.GP_DATA_IN"), hwdb.addr("cmic1.GP_DATA_IN")
CCA_DIN, CCA_DOUT, CCA_OUTEN = (hwdb.addr("cca.GPIO_DIN"), hwdb.addr("cca.GPIO_DOUT"), hwdb.addr("cca.GPIO_OUTEN"))
# us48_fan hwmon pwm1_enable: 1 = manual, 2 = thermal (kernel/us48-fan)
PWM_ENABLE = "$(dirname $(grep -l '^us48_fan$' /sys/class/hwmon/hwmon*/name))/pwm1_enable"


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


def pwm_enable(s) -> int:
    out = run(s, f"echo V=$(cat {PWM_ENABLE})")
    m = re.search(r"^V=(\d+)\s*$", out.replace("\r", ""), re.M)
    if not m:
        raise RuntimeError(f"no pwm1_enable value: {out!r}")
    return int(m.group(1))


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
        pwm_prev = pwm_enable(s)
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
        run(s, f"echo 1 > {PWM_ENABLE}")
        try:
            for i, (name, writes) in enumerate(steps, 1):
                say(f"STEP {i}/{len(steps)} {name}")
                for addr, val in writes:
                    wr(s, addr, val)
                time.sleep(a.hold)
                restore()
        finally:
            restore()
            run(s, f"echo {pwm_prev} > {PWM_ENABLE}")
    say("PROBE DONE (registers restored, pwm1_enable restored)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
