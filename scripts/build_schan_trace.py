#!/usr/bin/env python3
"""Build tools/schan_trace.c as a static ARM binary -> tmp/schan_trace. Log: tmp/logs/build_schan_trace.log."""
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CC = REPO / "private/tools/arm-linux-musleabi-cross/bin/arm-linux-musleabi-gcc"
LOG = REPO / "tmp/logs/build_schan_trace.log"
LOG.parent.mkdir(parents=True, exist_ok=True)
r = subprocess.run([str(CC), "-static", "-O2", "-Wall", "-o", str(REPO / "tmp/schan_trace"),
                    str(REPO / "tools/schan_trace.c")], capture_output=True, text=True)
LOG.write_text(r.stdout + r.stderr)
print(r.stdout + r.stderr, end="")
sys.exit(r.returncode)
