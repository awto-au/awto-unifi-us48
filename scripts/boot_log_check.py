#!/usr/bin/env python3
"""boot_log_check — fail on a warning/error line in a saved Linux boot log that is not in the allowlist.

Usage: boot_log_check.py [--log FILE] [--allow FILE] [--boot N] [--all] [--show-ok]
- Offline: reads a console/dmesg log saved by scripts/console.py (default: newest tmp/logs/console/us48-*.log with a 7.x boot).
- A boot starts at a `[    0.000000] Linux version` line. Default = the last boot; --boot N = Nth (python index); --all = every boot.
- Checked: kernel-timestamped lines, plus untimestamped lines before the first shell prompt (rc.sh output).
- A line matching DENY (warning, error, fail, timeout, taint, ...) must match an allowlist row, else ERROR.
- Allowlist rows (scripts/boot_log_allowlist.txt): `regex  # #NN reason`, regex fullmatch on the line without timestamp; every row names an issue.
- Allowlist rows that matched nothing are listed as UNUSED (info only).
- Exit 0 = clean, 2 = unlisted warning/error. Log: tmp/logs/boot_log_check.log
"""
import argparse
import re
import sys
import time
from collections import Counter
from pathlib import Path

from _cli import help_guard

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/boot_log_check.log"
CONSOLE_DIR = ROOT / "tmp/logs/console"
DEFAULT_ALLOW = ROOT / "scripts/boot_log_allowlist.txt"

DENY = re.compile(
    r"panic|\bOops\b|\bBUG\b|WARNING|\bwarn(ing)?\b|Call Trace|cut here|Unable to handle|segfault|"
    r"\bfail(ed|ure)?\b|\berror\b|timed out|timeout|invalid|unknown|not found|(?<!\w)-E[A-Z]{2,}\b|"
    r"\bhung\b|\bstall(ed)?\b|corrupt|\bbad\b|cannot|can't|not (yet )?implemented|deprecated|taints kernel|"
    r"\bNAK\b|\bRCU\b|lockdep|\bleak",
    re.IGNORECASE)
BOOT_START = re.compile(r"\[\s*0\.000000\] Linux version ")
KTS = re.compile(r"\[\s*\d+\.\d{6}\] ?")
PROMPT = re.compile(r"root@[\w-]+:[^#]*# ?")
ROW = re.compile(r"^(.*?)\s+# (#\d+)\b(.*)$")


def log(msg: str) -> None:
    print(msg)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(msg + "\n")


def load_allow(path: Path) -> list[tuple[re.Pattern, str, str]]:
    rows = []
    for n, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        m = ROW.match(line)
        if not m:
            raise SystemExit(f"{path}:{n}: row needs '  # #NN reason' (an issue number)")
        rows.append((re.compile(m.group(1).strip()), m.group(2), m.group(1).strip()))
    return rows


def newest_log() -> Path:
    for f in sorted(CONSOLE_DIR.glob("us48-*.log"), reverse=True):
        if re.search(rb"Linux version [7-9]\.", f.read_bytes()):
            return f
    raise SystemExit(f"no us48 log with a 7.x boot in {CONSOLE_DIR}")


def split_boots(lines: list[str]) -> list[list[str]]:
    boots: list[list[str]] = []
    for line in lines:
        if BOOT_START.search(line):
            boots.append([])
        if boots:
            boots[-1].append(line)
    return boots


def candidates(boot: list[str]):
    """Yield (stamped, text) for the lines a boot gate covers; text has the kernel timestamp stripped."""
    interactive = False
    for raw in boot:
        line = raw.rstrip()
        m = KTS.search(line)
        if m:  # a kernel line may follow console text on the same line
            text = line[m.end():]
            stamped = True
        else:
            text, stamped = line, False
        if PROMPT.search(line):
            interactive = True
        if interactive and not stamped:
            continue
        if text.strip():
            yield stamped, text.strip()


def main() -> int:
    help_guard(__doc__)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--log", type=Path)
    ap.add_argument("--allow", type=Path, default=DEFAULT_ALLOW)
    ap.add_argument("--boot", type=int, default=-1)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--show-ok", action="store_true")
    a = ap.parse_args()

    rows = load_allow(a.allow)
    path = a.log or newest_log()
    boots = split_boots(path.read_text(errors="replace").splitlines())
    if not boots:
        log(f"{path}: no `Linux version` boot line found")
        return 2
    pick = boots if a.all else [boots[a.boot]]
    log(f"=== {time.strftime('%Y-%m-%dT%H:%M:%S%z')} {path.name}: {len(boots)} boots, checking {len(pick)}")
    used: Counter = Counter()
    bad: Counter = Counter()
    for b in pick:
        for _, text in candidates(b):
            if not DENY.search(text):
                continue
            hit = next((r for r in rows if r[0].fullmatch(text)), None)
            if hit:
                used[hit[2]] += 1
                if a.show_ok:
                    log(f"  ok    {hit[1]} {text[:150]}")
            else:
                bad[text] += 1
    for t, c in sorted(bad.items()):
        log(f"  ERROR x{c:<3} {t[:200]}")
    for r in rows:
        if r[2] not in used:
            log(f"  UNUSED {r[1]} {r[2][:80]}")
    log(f"=== result: {'ERRORS (unlisted warning/error lines)' if bad else 'CLEAN'}; allowlisted hits {sum(used.values())}")
    return 2 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
