#!/usr/bin/env python3
"""a2_build_ko — build us48_a2.ko of a past commit against the normal-profile kernel build (#103).

- `git archive` of kernel/us48-a2, kernel/us48-pci, a2fw, hwdb/gen into tmp/kosrc/<commit>; builds there, copies to tmp/kosrc/<commit>.ko.
- Needs tmp/mainline/build-normal (scripts/build_mainline.py --profile normal).
- `WORK` as the commit = the working tree (uncommitted edits); `--profile debug` builds against tmp/mainline/build-debug (module vermagic of the debug a1 image).
Usage: a2_build_ko.py <commit|WORK> [--profile normal|debug]   Output: tmp/kosrc/<commit>[-debug].ko   Log: tmp/logs/a2_build_ko.log
"""
import os
import subprocess
import sys
from pathlib import Path
from _cli import help_guard  # noqa: E402

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
    global BUILD
    a = sys.argv[1:]
    prof = "normal"
    if len(a) == 3 and a[1] == "--profile" and a[2] in ("normal", "debug"):
        prof = a[2]
        a = a[:1]
    if len(a) != 1:
        print(__doc__)
        return 2
    BUILD = ROOT / f"tmp/mainline/build-{prof}"
    c = a[0]
    tag = c + ("" if prof == "normal" else "-" + prof)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    src = ROOT / "tmp/kosrc" / tag
    src.mkdir(parents=True, exist_ok=True)
    if c == "WORK":
        arc = subprocess.run(["tar", "-c", "--exclude=*.o", "--exclude=*.ko", "--exclude=*.cmd", "kernel/us48-a2", "kernel/us48-pci", "a2fw", "hwdb/gen"], cwd=ROOT, capture_output=True)
    else:
        arc = subprocess.run(["git", "archive", c, "kernel/us48-a2", "kernel/us48-pci", "a2fw", "hwdb/gen"], cwd=ROOT, capture_output=True)
    if arc.returncode:
        sys.exit(arc.stderr.decode())
    subprocess.run(["tar", "-x", "-C", str(src)], input=arc.stdout, check=True)
    env = dict(os.environ, ARCH="arm", CROSS_COMPILE="arm-linux-gnu-",
               KBUILD_EXTRA_SYMBOLS=str(ROOT / "kernel/us48-pci/Module.symvers"))
    mod = src / "kernel/us48-a2"
    sh(["make", "-C", str(ROOT / "tmp/linux-mainline"), f"-j{os.cpu_count()}", f"O={BUILD}", f"M={mod}", "clean"], env=env)
    sh(["make", "-C", str(ROOT / "tmp/linux-mainline"), f"-j{os.cpu_count()}", f"O={BUILD}", f"M={mod}", "modules"], env=env)
    out = ROOT / f"tmp/kosrc/{tag}.ko"
    sh(["arm-linux-gnu-strip", "--strip-debug", "-o", str(out), str(mod / "us48_a2.ko")])
    print(out, out.stat().st_size)
    return 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
