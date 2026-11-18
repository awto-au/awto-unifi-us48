#!/usr/bin/env python3
"""file_issues — create GitHub issues from tmp/issue-drafts/*.md ('# Title' on line 1, body after).

Usage: file_issues.py [--repo awto-au/awto-unifi-us48] [--branch main] [--dry-run] [DRAFT ...]
- Writes go through awto-dan scripts/gh_claudia.py (bot identity); bodies via --body-file.
- Relative links in a draft (relative to the draft file, e.g. ../../docs/x.md) become blob URLs on --repo/--branch (GitHub does not resolve them in issues).
- Labels: drafts whose title starts with "Decide:" get `needs-dan`.
- Drafts whose title ends with "— DONE <date>" are created then closed.
- --dry-run prints the commands and the rewritten body, creates nothing.
- Map draft -> issue number: tmp/logs/file_issues.log (append). Skips drafts already in the log.
"""
import argparse
import os
import posixpath
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from _cli import help_guard

ROOT = Path(__file__).resolve().parent.parent
DRAFTS = ROOT / "tmp/issue-drafts"
LOG = ROOT / "tmp/logs/file_issues.log"
GH = ["python3", "/mnt/2tb/git/awto-dan/scripts/gh_claudia.py", "gh"]
LINK = re.compile(r"\]\((?!https?://|#|mailto:)([^)\s]+)\)")


def absolutise(body: str, draft: Path, repo: str, branch: str) -> str:
    """Rewrite relative links, resolved against the draft's path relative to the repo root."""
    try:
        base = draft.resolve().parent.relative_to(ROOT).as_posix()
    except ValueError:
        base = "."

    def sub(m: re.Match) -> str:
        target, _, frag = m.group(1).partition("#")
        path = posixpath.normpath(posixpath.join(base, target))
        return f"](https://github.com/{repo}/blob/{branch}/{path}{'#' + frag if frag else ''})"

    return LINK.sub(sub, body)


def main() -> int:
    help_guard(__doc__)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default="awto-au/awto-unifi-us48")
    ap.add_argument("--branch", default="main")
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
        body = absolutise("\n".join(lines[1:]).strip() + "\n", f, a.repo, a.branch)
        with tempfile.NamedTemporaryFile("w", suffix=".md", dir=LOG.parent, delete=False) as bf:
            bf.write(body)
        cmd = GH + ["issue", "create", "--repo", a.repo, "--title", title, "--body-file", bf.name]
        if title.startswith("Decide:"):
            cmd += ["--label", "needs-dan"]
        closing = bool(re.search(r"— DONE \d{4}-\d{2}-\d{2}$", title))
        if a.dry_run:
            print("DRY", f.name, "|", " ".join(cmd))
            if closing:
                print("DRY   then:", " ".join(GH + ["issue", "close", "<N>", "--repo", a.repo, "--reason", "completed"]))
            print(body)
            Path(bf.name).unlink()
            continue
        r = subprocess.run(cmd, capture_output=True, text=True)
        Path(bf.name).unlink()
        if r.returncode:
            print(f"FAIL {f.name}: {r.stderr.strip()}")
            rc = 1
            continue
        url = r.stdout.strip().splitlines()[-1]
        num = url.rsplit("/", 1)[-1]
        if closing:
            subprocess.run(GH + ["issue", "close", num, "--repo", a.repo, "--reason", "completed"],
                           capture_output=True, text=True)
        with LOG.open("a") as fh:
            fh.write(f"{f.name} #{num} {url}\n")
        print(f"{f.name} -> #{num}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
