#!/usr/bin/env python3
"""Split serial captures into boots (stock/ours, cold/warm), tabulate errors + stage timing (#50).

Output: tmp/logs/bootlog_mine.log. Read-only on captures; no hardware access.
"""
import hashlib
import os
import re
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


def repo_root() -> Path:
    env = os.environ.get("US48_REPO")
    if env:
        return Path(env)
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True)
        return Path(out.stdout.strip())
    except Exception:
        return Path(__file__).resolve().parent.parent


ROOT = repo_root()
LOG = ROOT / "tmp/logs/bootlog_mine.log"
CAPTURES = [
    "tmp/logs/console/us48-20261014-083748.log",
    "tmp/logs/console/us48-20261014-120052.log",
    "tmp/logs/console/us48-20261013-181009.log",
    "tmp/logs/console/us48-20261013-180646-stock-boot-pasted.log",
    "tmp/logs/tio-us48.log",
]
SSH_LOG = "tmp/logs/us48_ssh.log"
WATCH_LOG = "tmp/logs/us48-watch.log"

RESET = {"0x4": "cold", "0x1": "warm-stock", "0x0": "warm-ours"}
ANSI = re.compile(r"\x1b(\[[0-9;?]*[A-Za-z]|[78=>])")
TS = re.compile(r"^\[\s*(\d+\.\d+)\]\s?")
BANNER = re.compile(r"U-Boot usw-")
KVER = re.compile(r"Linux version (\S+)")
ITS = re.compile(r"soc_pcie_hw_init.*its value = (0x[0-9a-f]+)")
CUTOFF = re.compile(r"Please press Enter|Welcome to UniFi|root@OpenWrt|built-in shell|login:|US-48-500W-US\.\S+#")
ERR = re.compile(
    r"\b(error|errors|warn|warning|fail|failed|failure|abort|aborted|timeout|timed out|retry|retries|"
    r"not found|can't|cannot|unable|bug|no such|invalid|denied|ignored|nak|bad|corrupt|unsupported|"
    r"not active|not present|exceeds|oops|panic|taint|disabled|missing)\b",
    re.I,
)
LATE_OK = re.compile(r"^(\[\s*\d+\.\d+\]|[\w.-]+\[\d+\]:)")

FACTS = [
    ("U-Boot version", r"^U-Boot usw-"),
    ("DEV ID", r"^DEV ID="),
    ("SKU ID", r"^SKU ID"),
    ("DDR", r"^(DDR type|MEMC \d DDR speed|DRAM:)"),
    ("Shmoo", r"Shmoo parameters|Restoring Shmoo|Running simple memory"),
    ("U-Boot PCIe", r"^==>PCIE: LINKSTA|port \d is (not )?active"),
    ("Clocks", r"arm_clk=|GENPLL|UART clock rate|Sel=\d Ovr"),
    ("Image header", r"Image Name:|Data Size:|Load Address|Entry Point"),
    ("Cmdline", r"^boot_prep_linux commandline"),
    ("CPU", r"^CPU: ARMv7|Machine:|Calibrating delay|l2x0:|L310"),
    ("Memory", r"Memory: |vmalloc :|lowmem|NR_IRQS|BUG: mapping"),
    ("Kernel PCIe", r"PCIE0: LINKSTA|PCIe map irq|PCIe port \d|BAR \d: assigned|soc_pcie_bridge_init|PCI host bridge"),
    ("UART", r"serial8250|Serial: 8250"),
    ("SPI flash", r"qspi_iproc|m25p80|mx25l|MTD partitions|^0x[0-9a-f]+-0x[0-9a-f]+ : "),
    ("BDE", r"linux-kernel-bde|bde|BDE"),
    ("GPIO/ubnt", r"gpiodev|iproc gpiochip|GPIOA|ubnt_common|pm_init|iproc_pmu"),
    ("MAC/serial", r"\b([0-9a-f]{2}:){5}[0-9a-f]{2}\b"),
    ("Board/UBNT", r"UBNT application|BS: |Ubiquiti: system|creating /proc/iproc|ubntbootid|Board"),
]

# stage markers: (name, regex) per kind; first match's kernel timestamp is recorded
STAGES = {
    "stock": [
        ("clocksource-switch", r"Switching to clocksource"),
        ("pm_init", r"pm_init: Initializing"),
        ("console-enabled", r"console \[ttyS0\] enabled"),
        ("mtd-parts", r"Creating 7 MTD partitions"),
        ("init-freed", r"Freeing init memory"),
        ("bde-payload-msg", r"linux-kernel-bde"),
        ("cfgmtd-phase1", r"Phase1: Write new cfg"),
        ("cfgmtd-phase4", r"Phase4: Demote"),
    ],
    "ours": [
        ("console-enabled", r"printk: legacy console|console \[ttyS0\] enabled"),
        ("pcie", r"PCI host bridge|pcie"),
        ("mtd", r"Creating \d+ MTD partitions|mtd"),
        ("free-init", r"Freeing unused kernel"),
        ("procd", r"procd"),
        ("ntp/net", r"link becomes ready|entered forwarding"),
    ],
}


def log(msg=""):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")
    print(msg)


def clean_lines(path: Path):
    raw = path.read_bytes().decode("utf-8", errors="replace")
    raw = ANSI.sub("", raw).replace("\x00", "")
    raw = "".join(c for c in raw if c in "\n\r\t" or (c >= " " and c != "�"))
    out = []
    for ln, phys in enumerate(raw.split("\n"), 1):  # ln = physical line in the file
        for line in phys.split("\r"):
            line = line.strip()
            if line.startswith("-- "):
                line = line[3:]
            if line:
                out.append((ln, line))
    return out


class Boot:
    def __init__(self, src, start, reset):
        self.src, self.start, self.reset = src, start, reset
        self.lines = []  # (lineno, text)
        self.kernel = None
        self.markers = []

    @property
    def kind(self):
        return {"3.6.5": "stock", "6.18.55": "ours"}.get(self.kernel, "uboot-only" if not self.kernel else self.kernel)

    @property
    def rtype(self):
        return RESET.get(self.reset, "unknown")

    @property
    def label(self):
        return f"{self.kind}/{self.rtype}"


def split_boots(src, lines):
    boots, cur = [], None
    for ln, text in lines:
        m = TS.match(text)
        if text.startswith("=== "):
            if cur:
                cur.markers.append(text)
            continue
        if BANNER.search(text):
            cur = Boot(src, ln, None)
            boots.append(cur)
        k = KVER.search(text)
        if k:
            if cur is None or cur.kernel:
                cur = Boot(src, ln, "restart" if cur else None)
                boots.append(cur)
            cur.kernel = k.group(1)
        if cur is None:
            cur = Boot(src, ln, None)
            boots.append(cur)
        i = ITS.search(text)
        if i and cur.reset in (None,):
            cur.reset = i.group(1)
        cur.lines.append((ln, text))
        _ = m
    return boots


def fingerprint(b):
    ts = "".join(m.group(1) for _, t in b.lines if (m := TS.match(t)))
    txt = ts or re.sub(r"\s+", "", "".join(t for _, t in b.lines))
    return hashlib.sha1((b.kind + txt[:6000]).encode()).hexdigest()


def normalise(text):
    t = TS.sub("", text)
    t = re.sub(r"^([\w.-]+)\[\d+\]:", r"\1[N]:", t)
    t = re.sub(r"0x[0-9a-fA-F]+", "0xH", t)
    t = re.sub(r"\b[0-9a-f]{2}(:[0-9a-f]{2}){5}\b", "MAC", t)
    t = re.sub(r"\d+", "N", t)
    t = re.sub(r"\s+", " ", t)
    return t[:150]


def boot_text_cutoff(b):
    """Return (boot-phase lines, late lines) split at the first shell/login line."""
    early, late, cut = [], [], False
    for ln, t in b.lines:
        if not cut and CUTOFF.search(t) and b.kernel:
            # the cutoff line itself (e.g. 'Please press Enter') is not an error line
            cut = True
            continue
        (late if cut else early).append((ln, t))
    return early, [(ln, t) for ln, t in late if LATE_OK.match(t)]


def kernel_ts(b, rx):
    for _, t in b.lines:
        if re.search(rx, t):
            m = TS.match(t)
            if m:
                return float(m.group(1))
    return None


def main():
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text(f"# bootlog_mine {datetime.now().astimezone().isoformat(timespec='seconds')}\n", encoding="utf-8")
    seen, boots = {}, []
    for rel in CAPTURES:
        p = ROOT / rel
        if not p.exists():
            log(f"missing {rel}")
            continue
        bs = split_boots(rel, clean_lines(p))
        n_new = 0
        for b in bs:
            if len(b.lines) < 5 or not (b.kernel or any(BANNER.search(t) for _, t in b.lines)):
                continue
            fp = fingerprint(b)
            if fp in seen:
                seen[fp].dups.append(rel)
                continue
            b.dups = []
            seen[fp] = b
            boots.append(b)
            n_new += 1
        log(f"{rel}: {len(bs)} segments, {n_new} new boots after dedupe")

    log("\n## Boots")
    log("idx | source:line | kind/reset | its | kernel | lines | dups")
    for i, b in enumerate(boots):
        log(f"{i:3d} | {b.src.split('/')[-1]}:{b.start} | {b.label} | {b.reset} | {b.kernel} | {len(b.lines)} | {len(b.dups)}")
    cnt = Counter(b.label for b in boots)
    log("\nboot types: " + ", ".join(f"{k}={v}" for k, v in sorted(cnt.items())))

    # errors
    table = defaultdict(lambda: {"count": 0, "types": Counter(), "ex": "", "where": "", "phase": Counter()})
    for b in boots:
        if b.kind not in ("stock", "ours"):
            continue
        early, late = boot_text_cutoff(b)
        for phase, ls in (("boot", early), ("late", late)):
            for ln, t in ls:
                if not ERR.search(t):
                    continue
                key = (b.kind, normalise(t))
                e = table[key]
                e["count"] += 1
                e["types"][b.rtype] += 1
                e["phase"][phase] += 1
                if not e["ex"]:
                    e["ex"], e["where"] = t[:200], f"{b.src.split('/')[-1]}:{ln}"
    log("\n## Error lines (kind | count | reset types | phase | normalised | first example)")
    for (kind, norm), e in sorted(table.items(), key=lambda kv: (kv[0][0], -kv[1]["count"])):
        log(
            f"{kind} | {e['count']} | {dict(e['types'])} | {dict(e['phase'])} | {norm} | {e['where']}: {e['ex']}"
        )

    # per-boot presence of the known examples
    known = {
        "data-abort-0xc8a00000": r"Data abort at addr=0xc8a00000",
        "bde-payload-256": r"Payload size exceeds device capability",
        "rc.platform": r"rc\.platform: applet not found",
        "crypto_hash.ko": r"crypto_hash\.ko",
        "cfgmtd-write": r"cfgmtd_do_write",
        "uboot-net-init-failed": r"Board Net Initialization Failed",
        "uboot-port0-not-active": r"port 0 is not active",
        "bug-mapping-vmalloc": r"BUG: mapping for",
    }
    log("\n## Known messages per boot type (boots containing / boots of that type)")
    tot = Counter(b.label for b in boots)
    for name, rx in known.items():
        has = Counter()
        for b in boots:
            if any(re.search(rx, t) for _, t in b.lines):
                has[b.label] += 1
        log(f"{name}: " + ", ".join(f"{k} {has[k]}/{tot[k]}" for k in sorted(tot) if has[k]))

    # U-Boot PCIe link state vs reset type
    log("\n## U-Boot PCIe LINKSTA by reset type")
    pc = Counter()
    for b in boots:
        v = next((re.search(r"LINKSTA reg 0xbe val (0x[0-9a-f]+)", t).group(1) for _, t in b.lines if "==>PCIE: LINKSTA" in t), None)
        a = next((t for _, t in b.lines if re.search(r"port 0 is (not )?active", t)), None)
        if v:
            pc[(b.rtype, v, a)] += 1
    for k, v in sorted(pc.items(), key=str):
        log(f"{k}: {v}")

    # kernel stage timing
    log("\n## Kernel-timestamp stage timing (seconds in kernel clock; first match)")
    for kind in ("stock", "ours"):
        by = defaultdict(lambda: defaultdict(list))
        for b in boots:
            if b.kind != kind:
                continue
            for name, rx in STAGES[kind]:
                v = kernel_ts(b, rx)
                if v is not None:
                    by[name][b.rtype].append(v)
        for name, d in by.items():
            log(f"{kind} {name}: " + "; ".join(f"{r} n={len(v)} min={min(v):.2f} med={statistics.median(v):.2f} max={max(v):.2f}" for r, v in sorted(d.items())))
    # the 100 Hz sched_clock jump around clocksource switch
    jumps = []
    for b in boots:
        if b.kind != "stock":
            continue
        prev = None
        for _, t in b.lines:
            m = TS.match(t)
            if m:
                v = float(m.group(1))
                if prev is not None and v - prev > 5 and len(jumps) < 200:
                    jumps.append((b.rtype, round(v - prev, 2), prev))
                    break
                prev = v
    log(f"stock timestamp jump >5s: n={len(jumps)} " + str(Counter((r, j) for r, j, _ in jumps).most_common(8)))

    panics(boots)
    watch_timing(boots)
    ssh_errors()
    facts(boots)


def parse_watch():
    p = ROOT / WATCH_LOG
    rows = []
    for line in p.read_text(errors="replace").splitlines():
        m = re.match(r"(\S+) (.*)", line)
        if not m:
            continue
        try:
            ts = datetime.fromisoformat(m.group(1))
        except ValueError:
            continue
        sm = re.search(r"state=(\S+)", m.group(2))
        rows.append((ts, sm.group(1) if sm else None, m.group(2)))
    return rows


def panics(boots):
    log("\n## Boots with a kernel panic / oops")
    for i, b in enumerate(boots):
        for ln, t in b.lines:
            if "Kernel panic" in t or "Unhandled fault" in t:
                log(f"boot {i} {b.src.split('/')[-1]}:{b.start} {b.label}: {ln}: {t[:160]}")
                break


def watch_timing(boots_cap):
    log("\n## Wall-clock stage timing from us48-watch.log (seconds since uboot-banner)")
    wb, cur = [], None
    for ts, st, raw in parse_watch():
        if "watch start" in raw:
            cur = None
            continue
        if st == "uboot-banner":
            cur = {"t0": ts, "ev": {}}
            wb.append(cur)
        if st and cur is not None and st not in cur["ev"]:
            cur["ev"][st] = ts
    rows = []
    for b in wb:
        ev, t0 = b["ev"], b["t0"]
        kind = "stock" if "stock-kernel" in ev else "ours" if "openwrt-kernel" in ev else "uboot-only"
        d = {k: (v - t0).total_seconds() for k, v in ev.items()}
        if kind == "ours":
            kind = "ours-flash" if d["openwrt-kernel"] < 60 else "ours-ymodem"
            d["ours kernel->userspace"] = d.get("openwrt-userspace", 0) - d["openwrt-kernel"]
            d["ours kernel->shell"] = d.get("openwrt-shell", 0) - d["openwrt-kernel"]
        rows.append([t0, kind, d, "unknown"])
    # align 2026-10-14 watch boots (until 11:40) to the 083748 capture by stock/ours order
    cap = [b for b in boots_cap if b.src.endswith("083748.log") and b.reset != "restart" and b.kind in ("stock", "ours")]
    w = [r for r in rows if r[1] != "uboot-only" and "2026-10-14T09:00" <= r[0].isoformat() < "2026-10-14T11:40"]
    wk = [r[1].split("-")[0] for r in w]
    hits = [o for o in range(len(cap) - len(wk) + 1) if all(cap[o + i].kind == wk[i] for i in range(len(wk)))]
    log(f"watch->capture alignment: {len(w)} boots, offsets matching={hits}")
    if len(hits) == 1:
        for i, r in enumerate(w):
            r[3] = cap[hits[0] + i].rtype
    for t0, kind, d, rt in rows:
        log(f"{t0.isoformat()} {kind} reset={rt}: " + " ".join(f"{k}={v:.0f}" for k, v in d.items()))
    keys = ["uboot-countdown", "stock-loading", "stock-kernel", "stock-init", "console-enter", "stock-login", "stock-shell",
            "uboot-prompt", "openwrt-kernel", "openwrt-userspace", "openwrt-shell", "ours kernel->userspace", "ours kernel->shell"]
    groups = defaultdict(list)
    for t0, kind, d, rt in rows:
        groups[(kind, rt)].append(d)
        groups[(kind, "all")].append(d)
    for (kind, rt), sel in sorted(groups.items()):
        parts = []
        for k in keys:
            vs = [d[k] for d in sel if k in d]
            if vs:
                parts.append(f"{k}: n={len(vs)} med={statistics.median(vs):.0f} min={min(vs):.0f} max={max(vs):.0f}")
        log(f"SUMMARY {kind} {rt} (n={len(sel)}): " + "; ".join(parts))


def ssh_errors():
    p = ROOT / SSH_LOG
    if not p.exists():
        return
    log("\n## Stock dmesg lines seen via SSH (deduped by line; error-like only)")
    seen, c = set(), Counter()
    ex = {}
    for _, t in clean_lines(p):
        if not TS.match(t) or t in seen:
            continue
        seen.add(t)
        if ERR.search(t):
            k = normalise(t)
            c[k] += 1
            ex.setdefault(k, t[:200])
    for k, n in c.most_common(60):
        log(f"{n} | {k} | {ex[k]}")


def facts(boots):
    log("\n## Facts (first occurrence per kind; file:line text)")
    for kind in ("stock", "ours"):
        done = defaultdict(set)
        for b in boots:
            if b.kind != kind:
                continue
            for ln, t in b.lines:
                for name, rx in FACTS:
                    if re.search(rx, t):
                        k = normalise(t)
                        if k not in done[name] and len(done[name]) < 40:
                            done[name].add(k)
                            log(f"{kind} | {name} | {b.src.split('/')[-1]}:{ln} {b.label} | {t[:220]}")


if __name__ == "__main__":
    sys.exit(main())
