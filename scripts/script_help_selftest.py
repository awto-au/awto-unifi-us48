#!/usr/bin/env python3
"""Static check (ast only, nothing is imported or run): every board-touching entry script guards -h/--help (#107).

Usage: python3 scripts/script_help_selftest.py      exit 0 = ok, 1 = violations listed on stdout
Board-touching = source matches BOARD_RE (console, dev, lx, relay, sf, ssh, tftp, schan, setenv, ramboot, ...).
Accepted: argparse, or a help_guard(...) call that is the first action: a module-level statement preceded only by
imports / docstring / assignments / sys.path edits, or the first statement of the `if __name__` block.
STRICT scripts (flash, env, relay, power, console writes) must also pass strict=True.
Side effects: none (reads scripts/*.py). Log: tmp/logs/script_help_selftest.log.
"""
import ast
import re
import sys
from pathlib import Path

from _cli import help_guard

help_guard(__doc__)

HERE = Path(__file__).resolve().parent
LOG = HERE.parent / "tmp/logs/script_help_selftest.log"
BOARD_RE = re.compile(r"\b(dev|console|lx|relay|schan|ssh|tftp|setenv|saveenv|ramboot|run_dev|aioesphomeapi)\b|\bsf (probe|erase|write|read)\b|\.sock\b")
STRICT = {"u2_flash_env", "u2_flash_uboot", "relay_native", "stock_ramboot", "u2_ramboot", "u2_replay_bisect", "u2_bisect",
          "u2_cold", "u2_regsync", "u2_memsync", "u2_cps_try", "u2_rls_try", "u2_sf_repro", "u2_sfbench", "cpu_sweep",
          "clock_trial", "a2_up", "u2_net_probe", "u2_ge_test", "u2_run", "u2_warm_prompt", "hwdb_probe_u0", "console_push"}


def _is_main_if(n):
    return isinstance(n, ast.If) and "__main__" in ast.dump(n.test)


def _is_guard(n):
    c = n.value if isinstance(n, ast.Expr) else None
    return isinstance(c, ast.Call) and getattr(c.func, "id", getattr(c.func, "attr", "")) == "help_guard" and c


def _inert(n):
    if isinstance(n, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.FunctionDef, ast.ClassDef)):
        return True
    if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant):
        return True
    return isinstance(n, ast.Expr) and "sys.path" in ast.unparse(n)  # sys.path.insert/append


def check(path):
    src = path.read_text()
    t = ast.parse(src)
    name = path.stem
    mains = [n for n in t.body if _is_main_if(n)]
    entry = bool(mains) or any(not _inert(n) and not isinstance(n, (ast.If, ast.Try)) for n in t.body)
    if not entry or not BOARD_RE.search(src):
        return None
    if "argparse" in src or '"--help"' in src:
        return None
    call = None
    for n in t.body:
        if _is_guard(n):
            call = _is_guard(n)
            break
        if _is_main_if(n):
            if n.body and _is_guard(n.body[0]):
                call = _is_guard(n.body[0])
            break
        if not _inert(n):
            return "help_guard is not the first action (statement at line %d runs before it)" % n.lineno
    if call is None:
        return "no argparse and no help_guard before the first action"
    if name in STRICT and not any(k.arg == "strict" and getattr(k.value, "value", False) is True for k in call.keywords):
        return "destructive script needs help_guard(..., strict=True)"
    return ""


def main():
    bad, ok = [], 0
    for p in sorted(HERE.glob("*.py")):
        if p.name.startswith("_"):
            continue
        r = check(p)
        if r is None:
            continue
        if r:
            bad.append(f"{p.name}: {r}")
        else:
            ok += 1
    missing = [s for s in STRICT if not (HERE / f"{s}.py").exists()]
    bad += [f"{s}.py: in STRICT list but missing" for s in missing]
    out = [f"guarded {ok}, violations {len(bad)}"] + bad
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("\n".join(out) + "\n")
    print("\n".join(out))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
