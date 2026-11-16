#!/usr/bin/env python3
"""Build tools/bde_trace.c (or tools/mmio_trace.c with --mmio, tools/tsc_read.c with --tscread) as a static ARM binary -> tmp/bde_trace (tmp/mmio_trace). Log: tmp/logs/build_bde_trace.log."""
import pathlib
import subprocess
import sys

from _cli import help_guard

help_guard(__doc__)

REPO = pathlib.Path(__file__).resolve().parent.parent
CC = REPO / "private/tools/arm-linux-musleabi-cross/bin/arm-linux-musleabi-gcc"
LOG = REPO / "tmp/logs/build_bde_trace.log"
LOG.parent.mkdir(parents=True, exist_ok=True)
NAME = "mmio_trace" if "--mmio" in sys.argv else "tsc_read" if "--tscread" in sys.argv else "bde_trace"
r = subprocess.run([str(CC), "-static", "-O2", "-Wall", "-o", str(REPO / "tmp" / NAME),
                    str(REPO / "tools" / (NAME + ".c"))], capture_output=True, text=True)
LOG.write_text(r.stdout + r.stderr)
print(r.stdout + r.stderr, end="")
sys.exit(r.returncode)
