#!/usr/bin/env python3
"""a2_bw — run `us48bench bw` on the running a2 Linux (#85): stage the binary host -> a1 -> a2 over vnet, print the output.

- Needs a2 up with vnet (scripts/vnet_test.py first). Binary: tmp/rootfs/tools-bin/us48bench (normal-profile a2 has no tools).
- a2 fetches from a1 with busybox nc on port 5003 (`< /dev/null`: both ncs wait for the other's EOF otherwise).
Usage: a2_bw.py <label>     Log: tmp/logs/a2_bw.log
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vnet_test as V  # noqa: E402
from _cli import help_guard  # noqa: E402

BENCH = V.ROOT / "tmp/rootfs/tools-bin/us48bench"
V.LOG = V.ROOT / "tmp/logs/a2_bw.log"


def main() -> int:
    label = sys.argv[1] if len(sys.argv) > 1 else "run"
    V.LOG.parent.mkdir(parents=True, exist_ok=True)
    V.put(BENCH, "/tmp/us48bench")
    V.lx("setsid sh -c 'nc -l -p 5003 < /tmp/us48bench' < /dev/null > /dev/null 2>&1 &")
    # transfer 46 KB over vnet: ~0.1 s; 3000 cs limit is a backstop
    V.lx('a2c "nc 10.48.0.1 5003 < /dev/null > /tmp/us48bench; chmod +x /tmp/us48bench; ls -l /tmp/us48bench" 500')
    out = V.lx('a2c "/tmp/us48bench bw" 2000')
    V.say(f"{label}: us48bench bw on a2:\n{out.strip()}")
    return 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
