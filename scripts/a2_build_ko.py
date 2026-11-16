#!/usr/bin/env python3
"""a2_build_ko — build us48_a2.ko of a past commit against the normal-profile kernel build (#103).

- `git archive` of kernel/us48-a2, kernel/us48-pci, a2fw, hwdb/gen into tmp/kosrc/<commit>; builds there, copies to tmp/kosrc/<commit>.ko.
- Needs tmp/mainline/build-normal (scripts/build_mainline.py --profile normal).
Usage: a2_build_ko.py <commit>   Log: tmp/logs/a2_build_ko.log
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "tmp/mainline/build-normal"
LOG = ROOT / "tmp/logs/a2_build_ko.log"


def sh(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    with LOG.open("a") as f:
        f.write(f"$ {' '.join(map(str, cmd))}\n{r.stdout}{r.stderr}")
    if r.returncode:
        sys.exit(f"failed rc={r.returncode}: {cmd[0]} (see {LOG})")
    return r


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    c = sys.argv[1]
    LOG.parent.mkdir(parents=True, exist_ok=True)
    src = ROOT / "tmp/kosrc" / c
    src.mkdir(parents=True, exist_ok=True)
    arc = subprocess.run(["git", "archive", c, "kernel/us48-a2", "kernel/us48-pci", "a2fw", "hwdb/gen"], cwd=ROOT, capture_output=True)
    if arc.returncode:
        sys.exit(arc.stderr.decode())
    subprocess.run(["tar", "-x", "-C", str(src)], input=arc.stdout, check=True)
    env = dict(os.environ, ARCH="arm", CROSS_COMPILE="arm-linux-gnu-",
               KBUILD_EXTRA_SYMBOLS=str(ROOT / "kernel/us48-pci/Module.symvers"))
    mod = src / "kernel/us48-a2"
    sh(["make", "-C", str(ROOT / "tmp/linux-mainline"), f"-j{os.cpu_count()}", f"O={BUILD}", f"M={mod}", "clean"], env=env)
    sh(["make", "-C", str(ROOT / "tmp/linux-mainline"), f"-j{os.cpu_count()}", f"O={BUILD}", f"M={mod}", "modules"], env=env)
    out = ROOT / f"tmp/kosrc/{c}.ko"
    sh(["arm-linux-gnu-strip", "--strip-debug", "-o", str(out), str(mod / "us48_a2.ko")])
    print(out, out.stat().st_size)
    return 0


if __name__ == "__main__":
    sys.exit(main())
