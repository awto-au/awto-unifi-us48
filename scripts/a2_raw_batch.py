#!/usr/bin/env python3
"""a2_raw_batch — push a2fw ELFs to the board and run each N times via a2_raw.py; one summary line per ELF (#79).

Usage: a2_raw_batch.py RUNS CPU_MHZ ELF_NAME... [k=v ...] (k=v = us48_a2 params, e.g. dcc_poll=0 hdbgen_off=1; names without us48-a2- prefix/.elf, e.g. rawtest-A); log tmp/logs/a2_raw_batch.log
"""
import subprocess
import sys
from pathlib import Path
from _cli import help_guard  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/a2_raw_batch.log"


def sh(*a: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *a], capture_output=True, text=True, cwd=ROOT)


def main() -> int:
    runs, mhz = int(sys.argv[1]), sys.argv[2]
    names = [x for x in sys.argv[3:] if "=" not in x]
    params = [x for x in sys.argv[3:] if "=" in x]
    LOG.parent.mkdir(parents=True, exist_ok=True)
    for n in names:
        f = f"us48-a2-{n}.elf"
        sh("scripts/lx.py", "--put", f"tmp/a2fw/{f}", f"/lib/firmware/{f}")
        res = []
        for _ in range(runs):
            r = sh("scripts/a2_raw.py", f, mhz, "5", "200", *params)
            v = [x for x in r.stdout.splitlines() if x.startswith("verdict")]
            res.append("ok" if v and v[0].endswith("ok") else "FAIL")
            LOG.open("a").write(f"--- {n}\n{r.stdout}\n")
        line = f"{n} {' '.join(params)}: {res.count('ok')}/{runs} ok {' '.join(res)}"
        print(line, flush=True)
        LOG.open("a").write(line + "\n")
    return 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
