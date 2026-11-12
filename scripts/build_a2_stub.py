#!/usr/bin/env python3
"""Assemble tools/a2_stub.S (and tools/a2_test.S) for unit 1's A9 -> tmp/a2/*.bin. Log: tmp/logs/build_a2_stub.log."""
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
T = REPO / "private/tools/arm-linux-musleabi-cross/bin/arm-linux-musleabi-"
OUT = REPO / "tmp/a2"
LOG = REPO / "tmp/logs/build_a2_stub.log"
OUT.mkdir(parents=True, exist_ok=True)
LOG.parent.mkdir(parents=True, exist_ok=True)
rc = 0
log = []
for name in ("a2_stub", "a2_test"):
    obj, elf, binf = OUT / f"{name}.o", OUT / f"{name}.elf", OUT / f"{name}.bin"
    for cmd in ([f"{T}as", "-march=armv7-a", "-o", str(obj), str(REPO / f"tools/{name}.S")],
                [f"{T}ld", "-Ttext=0x1b000000", "-o", str(elf), str(obj)],
                [f"{T}objcopy", "-O", "binary", str(elf), str(binf)]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        log.append(" ".join(cmd) + "\n" + r.stdout + r.stderr)
        rc = rc or r.returncode
    log.append(f"{name}.bin: {binf.stat().st_size if binf.exists() else 'missing'} bytes")
LOG.write_text("\n".join(log))
print(log[-1])
print(log[-4].splitlines()[-1] if False else "\n".join(l for l in log if l.endswith("bytes")))
sys.exit(rc)
