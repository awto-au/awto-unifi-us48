#!/usr/bin/env python3
"""health_check — collect the board's boot health line over ssh (lx.py) and compare with the previous collection (#35, #18).

Usage: health_check.py [--selftest] [--dry-run] [--old FILE --new FILE]
- Collects: /run/us48-health.last (this boot's line) + a fresh `us48-health --show`; --selftest also runs us48-selftest.
- History: tmp/health/history.jsonl (one JSON object per run); the previous run is the baseline. Fields: docs/selftest.md.
- Checks: boot counter step (0 or 1 ok, >1 = boots unseen, lower = RAM record lost), prev=unclean, cause wdt/pcie,
  CRC / JEDEC / ident changes, sensor jumps (LM75 > 15 C, die > 25 C) and limits, fan frame errors, PCIe link, PoE chip count.
- --dry-run: no board, no history write; compares built-in sample lines (or --old/--new files holding one line each).
- No board and no --dry-run: exit 2 (nothing is substituted). Exit 1 = a FAIL, 0 = none. Log: tmp/logs/health_check.log.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/health_check.log"
HIST = ROOT / "tmp/health/history.jsonl"
LX = ROOT / "scripts/lx.py"
# ssh connect 8 s (lx.py) + health line: 850 KiB flash CRC + ~40 forks, a few seconds on the A9 (not measured: first board run sets it)
LX_TIMEOUT_S = 20
LM_JUMP_C, DIE_JUMP_C = 15.0, 25.0   # one sample apart; the LM75 / die time constants are minutes
LM_WARN_C, LM_FAIL_C, DIE_WARN_C, DIE_FAIL_C = 70.0, 80.0, 100.0, 110.0   # docs/fan.md emergency trips
POE_CHIPS = 12
IDENT_KEYS = ("img", "kern", "uboot", "ubcrc", "kslot", "kcrc", "kdcrc", "jedec")
results: list[tuple[str, str, str]] = []

SAMPLE_OLD = ("us48-health up=61.2 boot=7 cause=reboot rst=0x00000002(soft) cru=0x00000000 prev=clean rec=ram "
              "img=a1b2c3d_20261118-120000 kern=7.4.0-rc1-norm#12 uboot=awto-uboot_2026.01 ubcrc=e325f81a kslot=us48-mainline "
              "kcrc=0df9acc3 kdcrc=06881787 jedec=c22019 pcie=rc:0x9012,ep:5.0_GT/s_PCIe_x2 die=die0:70.2,die1:62.0 "
              "lm75=48:41.5,49:43.0,4a:40.0,4b:39.0 fan=L0/0x00 fanerr=0 quiet=Y poe=12/12 fw=0x15:12")
SAMPLE_NEW = ("us48-health up=58.9 boot=9 cause=wdt rst=0x00000001(wdt) cru=0x00000001 prev=unclean rec=ram "
              "img=a1b2c3d_20261118-120000 kern=7.4.0-rc1-norm#12 uboot=awto-uboot_2026.01 ubcrc=ffff0000 kslot=us48-mainline "
              "kcrc=0df9acc3 kdcrc=06881787 jedec=c22019 pcie=rc:0x9012,ep:5.0_GT/s_PCIe_x2 die=die0:98.5,die1:62.0 "
              "lm75=48:41.5,49:61.0,4a:40.0,4b:39.0 fan=L0/0x00 fanerr=2 quiet=Y poe=11/12 fw=0x15:11")


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def res(level: str, name: str, detail: str) -> None:
    results.append((level, name, detail))
    log(f"{level} {name} {detail}")


def parse(line: str) -> dict:
    return dict(w.split("=", 1) for w in line.split() if "=" in w)


def temps(s: str) -> dict:
    """'48:41.5,49:err' -> {'48': 41.5, '49': None}"""
    out = {}
    for it in filter(None, s.split(",")):
        k, _, v = it.partition(":")
        try:
            out[k] = float(v)
        except ValueError:
            out[k] = None
    return out


def lx(cmd: str) -> str:
    r = subprocess.run([sys.executable, str(LX), cmd], capture_output=True, text=True, timeout=LX_TIMEOUT_S)
    if r.returncode == 255:
        sys.exit(f"board unreachable over ssh (lx.py rc 255): {r.stderr.strip()}; use --dry-run for a host-only check")
    if r.returncode != 0:
        log(f"lx rc={r.returncode} for {cmd}: {r.stderr.strip()}")
    return r.stdout


def check_one(tag: str, d: dict) -> None:
    """Absolute checks on one line."""
    if d.get("prev") == "unclean":
        res("WARN", f"{tag}:prev", "previous boot did not shut down cleanly (power loss, panic, wdt or reboot -f)")
    if d.get("cause") in ("wdt", "pcie", "soft_unclean"):
        res("WARN", f"{tag}:cause", f"reset cause {d['cause']} rst={d.get('rst')} cru={d.get('cru')}")
    for k, lim_w, lim_f in (("lm75", LM_WARN_C, LM_FAIL_C), ("die", DIE_WARN_C, DIE_FAIL_C)):
        t = temps(d.get(k, ""))
        bad = [n for n, v in t.items() if v is None]
        if bad or not t:
            res("FAIL", f"{tag}:{k}", f"unreadable or missing: {bad or 'all'}")
            continue
        m = max(t.values())
        lvl = "FAIL" if m >= lim_f else "WARN" if m >= lim_w else "PASS"
        res(lvl, f"{tag}:{k}", f"max {m} C of {len(t)} (warn {lim_w}, fail {lim_f})")
    if d.get("fan") in (None, "none", "unknown") or d.get("fanerr") not in ("0",):
        res("FAIL", f"{tag}:fan", f"fan={d.get('fan')} fanerr={d.get('fanerr')}")
    else:
        res("PASS", f"{tag}:fan", f"fan={d['fan']} errors 0")
    pc = d.get("pcie", "none")
    try:
        sta = int(pc.split("rc:")[1].split(",")[0], 16)
        res("PASS" if sta & 0x3f0 else "FAIL", f"{tag}:pcie", f"LINKSTA 0x{sta:04x} width field {(sta >> 4) & 0x3f} ({pc})")
    except (IndexError, ValueError):
        res("FAIL", f"{tag}:pcie", pc)
    if d.get("jedec") != "c22019":
        res("FAIL", f"{tag}:jedec", f"{d.get('jedec')} want c22019")
    poe = d.get("poe", "")
    if poe.startswith("skip"):
        res("PASS", f"{tag}:poe", poe)
    else:
        n = poe.split("/")[0]
        ok = n.isdigit() and int(n) == POE_CHIPS and "," not in d.get("fw", "")
        res("PASS" if ok else "WARN", f"{tag}:poe", f"{poe} fw={d.get('fw')}")


def compare(old: dict, new: dict) -> None:
    try:
        ob, nb = int(old["boot"]), int(new["boot"])
    except (KeyError, ValueError):
        res("WARN", "boot-counter", f"no counter: old {old.get('boot')} new {new.get('boot')} (rec={new.get('rec')})")
    else:
        step = nb - ob
        if step in (0, 1):
            res("PASS", "boot-counter", f"{ob} -> {nb}" + (" (same boot)" if step == 0 else ""))
        elif step > 1:
            res("WARN", "boot-counter", f"{ob} -> {nb}: {step - 1} boot(s) not observed")
        else:
            res("WARN", "boot-counter", f"{ob} -> {nb}: went back (RAM record lost over a power cycle, or a new image)")
    for k in IDENT_KEYS:
        if old.get(k) != new.get(k):
            res("FAIL" if k == "jedec" else "WARN", f"changed:{k}", f"{old.get(k)} -> {new.get(k)}")
    for k, jump in (("lm75", LM_JUMP_C), ("die", DIE_JUMP_C)):
        a, b = temps(old.get(k, "")), temps(new.get(k, ""))
        for n in sorted(set(a) & set(b)):
            if a[n] is not None and b[n] is not None and abs(b[n] - a[n]) > jump:
                res("WARN", f"jump:{k}:{n}", f"{a[n]} -> {b[n]} C (> {jump})")


def main() -> int:
    help_guard(__doc__, strict=True, known=("--selftest", "--dry-run", "--old", "--new"))
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--old", type=Path)
    ap.add_argument("--new", type=Path)
    a = ap.parse_args()

    if a.dry_run:
        old = a.old.read_text().strip() if a.old else SAMPLE_OLD
        new = a.new.read_text().strip() if a.new else SAMPLE_NEW
        log("dry-run: no board, no history write")
        boot_line = fresh = new
        prev = {"fresh": old}
    else:
        boot_line = lx("cat /run/us48-health.last").strip()
        fresh = lx("/usr/sbin/us48-health --show").strip().splitlines()[-1:] or [""]
        fresh = fresh[0]
        if not boot_line.startswith("us48-health ") or not fresh.startswith("us48-health "):
            sys.exit(f"no health line from the board: boot={boot_line!r} fresh={fresh!r} (image without us48-health?)")
        prev = None
        if HIST.exists():
            rows = [json.loads(x) for x in HIST.read_text().splitlines() if x.strip()]
            prev = rows[-1] if rows else None
        log("boot line: " + boot_line)
        log("fresh line: " + fresh)
    b, f = parse(boot_line), parse(fresh)
    check_one("boot", b)
    check_one("now", f)
    if prev:
        compare(parse(prev["fresh"]), f)
    else:
        res("PASS", "baseline", "no previous collection (first run)")
    if a.selftest and not a.dry_run:
        out = lx("/usr/sbin/us48-selftest")
        log("selftest:\n" + out)
        for ln in out.splitlines():
            if ln.startswith("FAIL ") and not ln.startswith("FAIL summary"):
                res("FAIL", "selftest", ln[5:])
    if not a.dry_run:
        HIST.parent.mkdir(parents=True, exist_ok=True)
        with HIST.open("a") as fh:
            fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "boot": boot_line, "fresh": fresh}) + "\n")
    fails = sum(1 for lvl, _, _ in results if lvl == "FAIL")
    warns = sum(1 for lvl, _, _ in results if lvl == "WARN")
    log(f"RESULT {fails} FAIL, {warns} WARN")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
