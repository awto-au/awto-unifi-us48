#!/usr/bin/env python3
"""file_issues — create GitHub issues from tmp/issue-drafts/*.md ('# Title' on line 1, body after).

Usage: file_issues.py [--repo awto-au/awto-unifi-us48] [--dry-run] [DRAFT ...]
- Labels: drafts whose title starts with "Decide:" get `needs-dan`.
- Drafts whose title ends with "— DONE <date>" are created then closed.
- Map draft → issue number: tmp/logs/file_issues.log (append). Skips drafts already in the log.
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DRAFTS = ROOT / "tmp/issue-drafts"
LOG = ROOT / "tmp/logs/file_issues.log"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default="awto-au/awto-unifi-us48")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("drafts", nargs="*")
    a = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    done = set(re.findall(r"^(\S+\.md) ", LOG.read_text(), re.M)) if LOG.exists() else set()
    files = [Path(d) for d in a.drafts] or sorted(DRAFTS.glob("*.md"))
    rc = 0
    for f in files:
        if f.name in done:
            print(f"skip {f.name} (already filed)")
            continue
        lines = f.read_text().splitlines()
        title = lines[0].lstrip("# ").strip()
        body = "\n".join(lines[1:]).strip() + "\n"
        cmd = ["gh", "issue", "create", "--repo", a.repo, "--title", title, "--body", body]
        if title.startswith("Decide:"):
            cmd += ["--label", "needs-dan"]
        if a.dry_run:
            print("DRY", f.name, "|", title)
            continue
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode:
            print(f"FAIL {f.name}: {r.stderr.strip()}")
            rc = 1
            continue
        url = r.stdout.strip().splitlines()[-1]
        num = url.rsplit("/", 1)[-1]
        if re.search(r"— DONE \d{4}-\d{2}-\d{2}$", title):
            subprocess.run(["gh", "issue", "close", num, "--repo", a.repo, "--reason", "completed"],
                           capture_output=True, text=True)
        with LOG.open("a") as fh:
            fh.write(f"{f.name} #{num} {url}\n")
        print(f"{f.name} -> #{num}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
