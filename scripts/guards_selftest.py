#!/usr/bin/env python3
"""guards_selftest - run the storm_watch, flash_guard and board_guard self-tests; exit 1 if any fails (#139).

Usage: guards_selftest.py     (no board, no NIC, no relay; log tmp/logs/guards_selftest.log)
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402

HERE = Path(__file__).resolve().parent
LOG = HERE.parent / "tmp/logs/guards_selftest.log"
# pure python on fake input: < 1 s each; 30 s names a hang
LIMIT_S = 30


def main() -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    bad = 0
    for name in ("storm_watch", "flash_guard", "board_guard"):
        try:
            r = subprocess.run([sys.executable, str(HERE / f"{name}.py"), "--selftest"], capture_output=True, text=True,
                               timeout=LIMIT_S)
            rc, out = r.returncode, r.stdout + r.stderr
        except subprocess.TimeoutExpired:
            rc, out = 124, f"exceeded {LIMIT_S} s"
        bad += rc != 0
        with LOG.open("a") as fh:
            fh.write(f"== {name} rc={rc}\n{out}\n")
        print(f"{name}: {'ok' if rc == 0 else 'FAIL rc=%d' % rc}")
    return 1 if bad else 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
