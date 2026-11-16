#!/usr/bin/env python3
"""ghidra_pyrun: run PyGhidra scripts (hwdb import/export, naming) in a saved headless project.

- analyzeHeadless has no Jython here, .py scripts need PyGhidra. Setup once:
  `python3 -m venv tmp/pgvenv && tmp/pgvenv/bin/pip install --no-index --find-links
  ~/.local/opt/ghidra_12.2_DEV/Ghidra/Features/PyGhidra/pypkg/dist pyghidra`
- Run with `tmp/pgvenv/bin/python -I scripts/ghidra_pyrun.py <name> <script.py> [args...] [-- <script.py> [args...]]`.
  (-I is required: scripts/ghidra/ would shadow the `ghidra` Java package.)
- Project tmp/ghidra/proj/<name> (program <name>.bin) comes from ghidra_export.py or analyzeHeadless -import.
- Log: tmp/logs/ghidra_pyrun_<name>.log (stdout+stderr via tee).
"""
import os
import sys
from pathlib import Path
from _cli import help_guard  # noqa: E402

ROOT = Path(os.environ.get("US48_REPO") or Path(__file__).resolve().parent.parent)
GHIDRA = Path(os.environ.get("GHIDRA_INSTALL_DIR", Path.home() / ".local/opt/ghidra_12.2_DEV"))


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    name = sys.argv[1]
    os.environ.setdefault("GHIDRA_INSTALL_DIR", str(GHIDRA))
    os.environ["US48_REPO"] = str(ROOT)
    jobs, cur = [], []
    for a in sys.argv[2:]:
        if a == "--":
            jobs.append(cur)
            cur = []
        else:
            cur.append(a)
    jobs.append(cur)
    import pyghidra
    pyghidra.start()
    proj = ROOT / "tmp/ghidra/proj" / name
    for j in jobs:
        script, args = j[0], j[1:]
        print("== %s %s" % (script, args), flush=True)
        pyghidra.run_script(None, script, project_location=proj, project_name=name,
                            script_args=args, analyze=False, program_name=name + ".bin",
                            nested_project_location=False, install_dir=GHIDRA)
    return 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
