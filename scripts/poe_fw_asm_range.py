#!/usr/bin/env python3
"""Print a range of tmp/poe-fw/<image>.asm (docs/poe-fw-seq).

Usage: poe_fw_asm_range.py START END [IMAGE]
  START / END hex CPU addresses; IMAGE = bcm59111-v21 (default) | bcm59111-meraki. Needs scripts/poe_fw_dis.py output.
"""
import re
import sys

s, e = int(sys.argv[1], 16), int(sys.argv[2], 16)
img = sys.argv[3] if len(sys.argv) > 3 else "bcm59111-v21"
for line in open(f"tmp/poe-fw/{img}.asm"):
    m = re.match(r"([0-9A-F]{4}):", line)
    if m and s <= int(m.group(1), 16) <= e:
        print(line.rstrip())
