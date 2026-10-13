#!/usr/bin/env python3
"""ghidra_export — headless Ghidra import + auto-analysis + full decompile export per stock binary.

- One Ghidra project per binary under tmp/ghidra/proj/<name> (kept, so agents can re-open it).
- Export (scripts/ghidra/ExportAll.java) -> tmp/ghidra/out/<name>/: <prog>.c (all functions),
  .funcs.tsv (addr, name, size), .calls.tsv (caller -> callee), .strings.tsv (addr, string, xrefs).
- Targets: TARGETS below; raw blobs get processor + base address. Inputs come from private/ (gitignored).
- Jobs run in parallel (--jobs); each JVM gets --heap. Log: tmp/logs/ghidra_export.log,
  per-target Ghidra output tmp/logs/ghidra_<name>.log.

Usage: ghidra_export.py [--only a,b] [--jobs N] [--heap 8G] [--threads 8] [--list]
"""

import argparse
import concurrent.futures as cf
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GHIDRA = Path.home() / ".local/bin/analyzeHeadless"
LOG = ROOT / "tmp/logs/ghidra_export.log"
PROJ = ROOT / "tmp/ghidra/proj"
OUT = ROOT / "tmp/ghidra/out"
SCRIPTS = ROOT / "scripts/ghidra"
FW = ROOT / "private/firmware"
MODS = FW / "rootfs-selected/lib/modules/3.6.5"

# name -> (path, extra import args). Raw blobs need processor + base.
TARGETS = {
    "ubnthal": (MODS / "ubnthal.ko", []),
    "gpiodev": (MODS / "gpiodev.ko", []),
    "ubnt_common": (MODS / "ubnt_common.ko", []),
    "vmlinux": (ROOT / "tmp/ghidra/in/vmlinux-us48-7.5.15.elf", []),  # vmlinux-to-elf, kallsyms, base 0xc0018000
    "libubnt": (FW / "rootfs-selected/lib/libubnt.so.1.0.0", []),
    "switchdrvr": (FW / "rootfs-selected/bin/switchdrvr", []),
    "eswh_switchdrvr": (ROOT / "tmp/eswh/swd/switchdrvr", []),
    "uboot": (FW / "unpacked/00-u-boot.bin",
              ["-loader", "BinaryLoader", "-processor", "ARM:LE:32:v7", "-loader-baseAddr", "0x1c000000"]),
}


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def run(name: str, heap: str, threads: int) -> int:
    src, extra = TARGETS[name]
    if not src.exists():
        log(f"{name}: missing input {src.relative_to(ROOT)}")
        return 1
    proj = PROJ / name
    if proj.exists():
        shutil.rmtree(proj)
    proj.mkdir(parents=True)
    out = OUT / name
    cmd = [str(GHIDRA), str(proj), name, "-import", str(src), *extra,
           "-scriptPath", str(SCRIPTS), "-postScript", "ExportAll.java", str(out), str(threads), "60",
           "-analysisTimeoutPerFile", "14400"]  # 4 h cap: switchdrvr (35 MB) is the long pole
    env = dict(os.environ, GHIDRA_HEADLESS_MAXMEM=heap)
    t0 = time.monotonic()
    log(f"{name}: start ({src.name}, {src.stat().st_size} B)")
    with (ROOT / f"tmp/logs/ghidra_{name}.log").open("w") as f:
        rc = subprocess.run(["nice", "-n", "10", *cmd], stdout=f, stderr=subprocess.STDOUT, env=env).returncode
    ok = rc == 0 and (out / f"{src.name}.c").exists()
    log(f"{name}: {'ok' if ok else f'FAILED rc={rc}'} in {time.monotonic() - t0:.0f} s -> {out.relative_to(ROOT)}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", default="")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--heap", default="8G")
    ap.add_argument("--threads", type=int, default=6, help="decompiler threads per job")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list:
        for k, (p, _) in TARGETS.items():
            print(f"{k}\t{p.relative_to(ROOT)}\t{'ok' if p.exists() else 'MISSING'}")
        return 0
    LOG.parent.mkdir(parents=True, exist_ok=True)
    names = [n for n in a.only.split(",") if n] or list(TARGETS)
    # largest first so the long pole starts immediately
    names.sort(key=lambda n: -(TARGETS[n][0].stat().st_size if TARGETS[n][0].exists() else 0))
    with cf.ThreadPoolExecutor(a.jobs) as ex:
        rcs = list(ex.map(lambda n: run(n, a.heap, a.threads), names))
    log(f"done: {sum(r == 0 for r in rcs)}/{len(rcs)} ok")
    return 0 if not any(rcs) else 1


if __name__ == "__main__":
    sys.exit(main())
