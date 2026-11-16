"""Shared CLI guard for scripts without argparse (#107): -h/--help prints the docstring and exits before any action.

Usage (first executable statement, before any board/relay/flash/env action):
    from _cli import help_guard
    help_guard(__doc__)                                   # -h/--help only
    help_guard(__doc__, strict=True, known=("--image",))  # unknown -flag exits 2
Pure stdlib, no side effects on import. Selftest: scripts/script_help_selftest.py.
"""
import re
import sys

_NEG_NUM = re.compile(r"^-\d")  # negative numeric value of a flag (--lo -1), not a flag


def help_guard(doc, *, strict=False, known=(), argv=None):
    args = sys.argv[1:] if argv is None else argv
    usage = (doc or f"{sys.argv[0]}: no usage text").strip()
    if "-h" in args or "--help" in args:
        print(usage)
        sys.exit(0)
    if not strict:
        return
    for a in args:
        if a == "--":
            break
        if not a.startswith("-") or _NEG_NUM.match(a):
            continue
        if a.split("=", 1)[0] not in known:
            print(f"unknown option {a}\n\n{usage}", file=sys.stderr)
            sys.exit(2)
