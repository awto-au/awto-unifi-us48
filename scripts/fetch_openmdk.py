#!/usr/bin/python3
"""fetch_openmdk — pull the HR2-relevant subset of Broadcom/OpenMDK into the machine vendor store (#47).

Usage: scripts/fetch_openmdk.py [--dest /vendors/broadcom/openmdk] [--ref SHA]
- GitHub API via `gh` (authenticated; no web scraping). Files land in <dest>/<ref[:12]>/<repo path>.
- Writes <dest>/README.md (manifest: URL, commit, per-file size + sha256) and appends, never deletes.
- Reference only: Broadcom licence, not GPL; the #4 driver copies no code. Log: tmp/logs/fetch_openmdk.log
"""
import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

LOG = Path(__file__).resolve().parent.parent / "tmp/logs/fetch_openmdk.log"
REPO = "Broadcom/OpenMDK"
REF = "db9c678696800d1ebb8d331cb462acde85c31ffb"  # 2.10.9, same pin as docs/sources.md
PREFIXES = (
    "Legal/", "cdk/PKG/chip/bcm56150/", "cdk/PKG/chip/bcm53344/", "cdk/PKG/chip/bcm53346/",
    "cdk/PKG/arch/xgs/", "cdk/PKG/arch/xgsd/", "cdk/PKG/arch/xgsm/", "bmd/PKG/chip/bcm56150_a0/",
    "phy/PKG/bus/bcm56150_miim_int/", "phy/PKG/chip/bcm56150/", "phy/PKG/chip/bcmi_qsgmii_serdes/",
    "phy/PKG/chip/bcmi_qsgmiie_serdes/", "phy/PKG/chip/bcmi_tsc_xgxs/", "phy/PKG/chip/bcmi_tsce_xgxs/",
    "board/config/board_bcm53344_svk.c", "board/config/board_bcm56150_10g.c", "board/config/board_bcm56150_13g.c",
)
# One gh api call per file over the LAN: ~1 s typical. 30 s = slow-API backstop; expiry = that file fails, logged.
GH_TIMEOUT_S = 30


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def gh(args: list[str], raw: bool = False) -> bytes:
    cmd = ["gh", "api"] + (["-H", "Accept: application/vnd.github.raw"] if raw else []) + args
    return subprocess.run(cmd, capture_output=True, check=True, timeout=GH_TIMEOUT_S).stdout


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dest", type=Path, default=Path("/vendors/broadcom/openmdk"))
    ap.add_argument("--ref", default=REF)
    a = ap.parse_args()
    tree = json.loads(gh([f"repos/{REPO}/git/trees/{a.ref}?recursive=1"]))
    files = [t["path"] for t in tree["tree"] if t["type"] == "blob" and t["path"].startswith(PREFIXES)]
    root = a.dest / a.ref[:12]
    log(f"{len(files)} files from {REPO}@{a.ref[:12]} -> {root}")
    rows, bad = [], 0
    for p in files:
        out = root / p
        try:
            if not out.exists():
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(gh([f"repos/{REPO}/contents/{p}?ref={a.ref}"], raw=True))
            data = out.read_bytes()
            rows.append(f"| `{p}` | {len(data)} | `{hashlib.sha256(data).hexdigest()[:16]}` |")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            log(f"FAILED {p}: {e}")
            bad += 1
    readme = a.dest / "README.md"
    with readme.open("a") as f:
        f.write(f"\n## {REPO} @ `{a.ref}` (fetched {time.strftime('%Y-%m-%dT%H:%M:%S%z')})\n\n"
                f"- URL: https://github.com/{REPO}/tree/{a.ref}\n"
                f"- Fetched by awto-unifi-us48 `scripts/fetch_openmdk.py` (#47): HR2/BCM56150 subset, {len(rows)} files.\n"
                "- Good copy: every file listed below exists with this size and sha256 prefix.\n"
                "- Licence: Broadcom (see `Legal/`), reference only.\n\n| Path | Bytes | sha256 (16) |\n|---|---|---|\n")
        f.write("\n".join(rows) + "\n")
    log(f"done: {len(rows)} ok, {bad} failed; manifest {readme}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
