#!/usr/bin/env python3
"""Check relative links and anchors in docs/*.md, and the docs/README.md index.

- Every relative link target exists; every `#anchor` matches a heading (GitHub slug rules) or `<a id/name>`.
- docs/README.md links every docs/*.md exactly once (index completeness).
- External links (http, mailto) are not fetched. Code spans and fences are skipped.
Also checked: README.md and CLAUDE.md; positional args add more files (repo-relative).
Log: tmp/logs/doc_link_check.log. Exit 1 on any problem.
"""
import argparse
import re
import sys
from pathlib import Path
from urllib.parse import unquote

from _cli import help_guard

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
LOG = ROOT / "tmp/logs/doc_link_check.log"

LINK = re.compile(r"!?\[(?:[^\[\]]|\[[^\]]*\])*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
HEAD = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
HTML_ID = re.compile(r"<a\s+[^>]*(?:id|name)=\"([^\"]+)\"", re.I)
FENCE = re.compile(r"^\s*(```|~~~)")
CODE_SPAN = re.compile(r"(`+)(?:(?!\1).)+?\1")


def prose_lines(text: str):
    """(lineno, line) outside fenced code blocks."""
    fence = None
    for n, line in enumerate(text.splitlines(), 1):
        m = FENCE.match(line)
        if m:
            fence = None if fence == m.group(1) else (fence or m.group(1))
            continue
        if fence is None:
            yield n, line


def slug(heading: str) -> str:
    """GitHub heading anchor: strip markup, lowercase, drop punctuation, spaces -> '-'."""
    h = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # [text](url) -> text
    h = h.replace("`", "").replace("*", "").strip().lower()
    h = re.sub(r"[^\w\- ]", "", h)
    return h.replace(" ", "-")


_anchor_cache: dict[Path, set[str]] = {}


def anchors(path: Path) -> set[str]:
    if path not in _anchor_cache:
        seen: dict[str, int] = {}
        out: set[str] = set()
        for _, line in prose_lines(path.read_text(encoding="utf-8", errors="replace")):
            out.update(HTML_ID.findall(line))
            m = HEAD.match(line)
            if not m:
                continue
            s = slug(m.group(2))
            k = seen.get(s, 0)
            seen[s] = k + 1
            out.add(s if k == 0 else f"{s}-{k}")
        _anchor_cache[path] = out
    return _anchor_cache[path]


def links(path: Path):
    for n, line in prose_lines(path.read_text(encoding="utf-8", errors="replace")):
        for m in LINK.finditer(CODE_SPAN.sub("", line)):
            yield n, m.group(1)


def check_file(path: Path, log) -> list[str]:
    bad = []
    rel = path.relative_to(ROOT)
    for n, url in links(path):
        if re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I):  # http:, https:, mailto:
            continue
        target, _, frag = url.partition("#")
        tpath = (path.parent / unquote(target)).resolve() if target else path
        if not tpath.exists():
            bad.append(f"{rel}:{n}: missing target {url}")
            continue
        if frag and tpath.suffix == ".md" and unquote(frag) not in anchors(tpath):
            bad.append(f"{rel}:{n}: missing anchor {url}")
        log.write(f"checked {rel}:{n} {url}\n")
    return bad


def check_index() -> list[str]:
    index = DOCS / "README.md"
    counts: dict[str, int] = {}
    for _, url in links(index):
        target = url.partition("#")[0]
        if target and "/" not in target and target.endswith(".md"):
            counts[target] = counts.get(target, 0) + 1
    bad = []
    for md in sorted(DOCS.glob("*.md")):
        if md.name == "README.md":
            continue
        c = counts.get(md.name, 0)
        if c != 1:
            bad.append(f"docs/README.md: {md.name} listed {c} times (want 1)")
    return bad


def main() -> int:
    help_guard(__doc__)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("extra", nargs="*", help="more Markdown files to check (repo-relative)")
    a = ap.parse_args()
    files = sorted(DOCS.glob("*.md")) + [ROOT / p for p in ("README.md", "CLAUDE.md", *a.extra)]
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("w", encoding="utf-8") as log:
        bad = []
        for f in files:
            bad += check_file(f, log)
        bad += check_index()
        for b in bad:
            log.write(f"BAD {b}\n")
            print(b)
        summary = f"doc_link_check: {len(files)} files, {len(bad)} problems"
        log.write(summary + "\n")
        print(summary, file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
