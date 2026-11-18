#!/usr/bin/env python3
"""hwdb_gaps — read-only gap probe of unit 1 from a2: addresses regdb does not name (#86 phase 2).

Usage: hwdb_gaps.py plan | step [N] | adhoc ADDR... | temp | state | report
  plan    windows (256 B, aligned) of the unnamed 4-byte words in the probed pages -> tmp/hwgap/state.json (keeps results)
  step    next pending windows (<= RUN_READS reads) -> build regprobe ELF -> one a2 run -> merge. Exit 3 = a window hung a2:
          it is marked blocked, the board needs a power cycle (then `step` again). Exit 0 = batch done, 4 = nothing pending.
  temp    die temperatures from a1 (stop at TEMP_STOP_C)
  report  hwdb/validation/u1-gaps.tsv (non-zero answers), u1-gap-windows.tsv (all windows), gap_* entries in u1-blocked.toml
Order: group (CMIC common, other CMIC, ChipcommonA/B + CRU/DMU, IDM wrappers), inside a group nearest to a named register first.
Only pages whose IDM wrapper/bridge reads released are listed (docs/hwdb.md validation). Log: tmp/logs/hwdb_gaps.log.
"""
import json
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hwdb  # noqa: E402
import hwdb_probe as hp  # noqa: E402
from _cli import help_guard  # noqa: E402

ROOT = hp.ROOT
TMP = ROOT / "tmp/hwgap"
STATE = TMP / "state.json"
LOG = ROOT / "tmp/logs/hwdb_gaps.log"
VAL = ROOT / "hwdb/validation"
WIN = 256
# trace0 = 192 KiB, 16 B per record + 32 B header: 12286 reads max; 8000 keeps the ssh transfer ~130 KiB
RUN_READS = 8000
TEMP_STOP_C = 100.0  # stop well before the 105 C limit given by Dan
CMIC = hwdb.cmic_base(0)
Z1 = 0x1000

# page groups: (name, [page base, ...]). Pages with regdb names, in blocks whose IDM wrapper reads RESET_CONTROL = 0.
IDM_PAGES = [0x18100000, 0x18101000, 0x18103000, 0x18107000, 0x18108000, 0x18109000, 0x1810a000, 0x1810b000, 0x1810d000,
             0x1810f000, 0x18110000, 0x18114000, 0x18119000, 0x1811a000, 0x1811b000, 0x1811c000, 0x1811d000, 0x1811e000,
             0x18120000, 0x18121000, 0x18123000, 0x18130000, 0x18131000, 0x18132000, 0x18141000]
GROUPS = [
    ("cmic_common", [CMIC + o for o in (0x10000, 0x11000, 0x1a000, 0x1b000, 0x1c000)]),
    ("cmic_other", [CMIC + o for o in (0x0, 0x1000, 0x2000, 0x3000, 0x4000, 0x20000, 0x21000, 0x22000, 0x24000, 0x25000, 0x26000,
                                      0x30000, 0x31000, 0x32000, 0x33000)]),
    ("ccb", [hwdb.addr("iproc_axi")] + [0x18030000 + i * Z1 for i in range(16)]),
    ("idm", IDM_PAGES),
    # controllers whose IDM wrapper reads released (NAND, QSPI, PNOR, SRAM, PAXB), then IHOST/A9 debug and the AXI interconnect wrappers
    ("periph", [0x18012000, 0x18021000, 0x18026000, 0x18027000, 0x1802e000]),
    ("ihost_axiic", [0x19000000 + i * Z1 for i in range(11)] + [0x19010000 + i * Z1 for i in range(4)] +
     [0x19020000, 0x19021000, 0x19022000] +
     [0x1a000000 + o for o in (0x1000, 0x2000, 0x3000, 0x4000, 0x6000, 0x7000, 0x8000, 0x9000, 0xa000, 0xe000, 0xf000, 0x15000,
                               0x18000, 0x42000, 0x43000, 0x45000, 0x46000, 0x4a000, 0x4f000, 0x50000)]),
]
# GICC alias registers (AIAR/AHPPIR ack an interrupt): not read
SKIP_RANGES = [(0x19020100, 0x190201ff)]
# byte registers: the ChipcommonA UART windows fault on a word read, so their unnamed bytes are read as bytes
BYTE_PAGES = [(0x18000300, 0x180003ff), (0x18000400, 0x180004ff)]
# re-read of the held-in-reset IDM wrappers, first run only (#91)
IDMCHK = [0x18114408, 0x18114500, 0x18114800, 0x18114804, 0x18110408, 0x18110500, 0x18110800, 0x18110804]


def log(msg):
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def named_maps():
    """(word set of every 4-byte word touched by a named register, byte set, {word: name})."""
    words, bytes_, names = set(), set(), {}
    for r in hp.rows(None):
        a = r["addr"]
        words.add(a & ~3)
        bytes_.add(a)
        names.setdefault(a & ~3, r["key"].split(".", 1)[1])
    return words, bytes_, names


def plan():
    words, bytes_, names = named_maps()
    sorted_named = sorted(words)
    blocked = hp.blocked()
    wins = []
    for gi, (gname, pages) in enumerate(GROUPS):
        for pg in pages:
            near_words = [w for w in sorted_named if pg - 0x100 <= w < pg + Z1 + 0x100]
            for wlo in range(pg, pg + Z1, WIN):
                ws = []
                for a in range(wlo, wlo + WIN, 4):
                    if any(b["lo"] <= a <= b["hi"] for b in blocked) or any(lo <= a <= hi for lo, hi in SKIP_RANGES):
                        continue
                    if any(lo <= a <= hi for lo, hi in BYTE_PAGES):
                        for k in range(4):
                            if a + k not in bytes_:
                                ws.append((a + k, 1))
                    elif a not in words:
                        ws.append((a, 4))
                if not ws:
                    continue
                dist = min((min(abs(w - n) for n in near_words) if near_words else 0x1000) for w, _ in ws)
                wins.append(dict(addr=wlo, group=gname, gi=gi, dist=dist, words=ws, status="pending"))
    wins.sort(key=lambda w: (w["gi"], w["dist"], w["addr"]))
    old = json.loads(STATE.read_text()) if STATE.exists() else {}
    oldw = {w["addr"]: w for w in old.get("win", [])}
    for w in wins:
        if w["addr"] in oldw:
            w["status"] = oldw[w["addr"]]["status"]
    TMP.mkdir(parents=True, exist_ok=True)
    st = dict(win=wins, res=old.get("res", {}), hangs=old.get("hangs", 0), idmchk=old.get("idmchk", False))
    STATE.write_text(json.dumps(st))
    nreads = sum(len(w["words"]) for w in wins)
    log(f"plan: {len(wins)} windows, {nreads} reads, pages {sum(len(p) for _, p in GROUPS)}; " +
        ", ".join(f"{g}={sum(1 for w in wins if w['group'] == g)}" for g, _ in GROUPS))


def load():
    return json.loads(STATE.read_text())


def save(st):
    STATE.write_text(json.dumps(st))


def step(maxreads):
    st = load()
    pend = [w for w in st["win"] if w["status"] == "pending"]
    if not pend:
        log("step: nothing pending")
        return 4
    batch, n = [], 0
    extra = [] if st.get("idmchk") else [(a, 4) for a in IDMCHK]
    n += len(extra)
    for w in pend:
        if n + len(w["words"]) > maxreads and batch:
            break
        batch.append(w)
        n += len(w["words"])
    rows = [dict(key=f"idm.{a:#010x}", addr=a, width=wd) for a, wd in extra]
    owner = [None] * len(rows)
    for w in batch:
        for a, wd in w["words"]:
            rows.append(dict(key=f"gap.{a:#010x}", addr=a, width=wd))
            owner.append(w)
    hp.TMP = TMP
    hp.LOG = LOG
    with open(TMP / "list.tsv", "w") as f:
        f.write("idx\tkey\taddr\twidth\trst\trmask\tflags\tskip\n")
        for i, r in enumerate(rows):
            f.write(f"{i}\t{r['key']}\t{r['addr']:#010x}\t{r['width']}\t0x0\t0x0\t0x0\t\n")
    h = ROOT / "tmp/a2fw/regprobe_list.h"
    h.parent.mkdir(parents=True, exist_ok=True)
    h.write_text("/* generated by scripts/hwdb_gaps.py step, do not edit */\n#define RP_N %d\nstatic const uint32_t rp_addr[RP_N] = {\n%s};\nstatic const uint8_t rp_w[RP_N] = {\n%s};\n" %
                 (len(rows), "".join(f"\t0x{r['addr']:08x},\n" for r in rows), "".join(f"\t{r['width']},\n" for r in rows)))
    r = hp.sh("scripts/build_a2fw.py", "us48-a2-regprobe", timeout=90)
    if r.returncode:
        log(f"build failed rc={r.returncode} {r.stdout[-200:]}{r.stderr[-200:]}")
        return 1
    log(f"step: {len(batch)} windows {batch[0]['addr']:#x}.. ({batch[0]['group']}), {len(rows)} reads")
    pj = TMP / "run.json"
    pj.unlink(missing_ok=True)
    rc = hp.run("run")
    res = json.loads(pj.read_text()) if pj.exists() else {}
    for r_ in rows:
        v = res.get(r_["key"])
        if v is not None:
            st["res"][f"{r_['addr']:#010x}"] = v
    if extra and all(r_["key"] in res for r_ in rows[:len(extra)]):
        st["idmchk"] = True
    first_missing = next((i for i, r_ in enumerate(rows) if r_["key"] not in res), None)
    for w in batch:
        if all(f"gap.{a:#010x}" in res for a, _ in w["words"]):
            w["status"] = "done"
    if rc == 3 and first_missing is not None and owner[first_missing] is not None:
        hw = owner[first_missing]
        hw["status"] = "blocked"
        hw["hang_at"] = rows[first_missing]["addr"]
        st["hangs"] += 1
        log(f"HANG window {hw['addr']:#x} ({hw['group']}) first stalled read {rows[first_missing]['addr']:#x}; hangs so far {st['hangs']}")
    elif rc == 3 and first_missing is not None:
        log(f"HANG in the IDM re-read at {rows[first_missing]['addr']:#x}")
    save(st)
    done = sum(1 for w in st["win"] if w["status"] == "done")
    blk = sum(1 for w in st["win"] if w["status"] == "blocked")
    log(f"state: {done} done, {blk} blocked, {sum(1 for w in st['win'] if w['status'] == 'pending')} pending of {len(st['win'])}")
    return rc


def adhoc(addrs):
    """Read the given word addresses in order in one a2 run (no state); a stall = first address without a record, power cycle."""
    rows = [dict(key=f"adhoc.{a:#010x}", addr=a, width=4) for a in addrs]
    hp.TMP = TMP
    hp.LOG = LOG
    with open(TMP / "list.tsv", "w") as f:
        f.write("idx\tkey\taddr\twidth\trst\trmask\tflags\tskip\n")
        for i, r in enumerate(rows):
            f.write(f"{i}\t{r['key']}\t{r['addr']:#010x}\t4\t0x0\t0x0\t0x0\t\n")
    h = ROOT / "tmp/a2fw/regprobe_list.h"
    h.write_text("/* generated by scripts/hwdb_gaps.py adhoc, do not edit */\n#define RP_N %d\nstatic const uint32_t rp_addr[RP_N] = {\n%s};\nstatic const uint8_t rp_w[RP_N] = {\n%s};\n" %
                 (len(rows), "".join(f"\t0x{r['addr']:08x},\n" for r in rows), "\t4,\n" * len(rows)))
    if hp.sh("scripts/build_a2fw.py", "us48-a2-regprobe", timeout=90).returncode:
        log("build failed")
        return 1
    pj = TMP / "run.json"
    pj.unlink(missing_ok=True)
    rc = hp.run("run")
    res = json.loads(pj.read_text()) if pj.exists() else {}
    for r_ in rows:
        v = res.get(r_["key"])
        log(f"adhoc {r_['addr']:#010x}: " + ("HANG (no record)" if v is None else ("abort dfsr %#x" % v[1] if v[0] == "A" else "%#010x" % v[1])))
    return rc


def temp():
    r = subprocess.run([sys.executable, "scripts/lx.py", "for z in /sys/class/thermal/thermal_zone*; do echo $(cat $z/type) $(cat $z/temp); done"],
                       capture_output=True, text=True, cwd=ROOT, timeout=20)
    log("temp: " + r.stdout.replace("\n", "; ") + r.stderr[-100:])
    hot = [int(m.group(1)) / 1000 for m in re.finditer(r" (\d+)(?:;|$)", r.stdout.replace("\n", ";"))]
    if hot and max(hot) >= TEMP_STOP_C:
        log(f"temperature {max(hot)} C >= {TEMP_STOP_C}: stop")
        return 5
    return 0


def zone(a):
    for g, pages in GROUPS:
        for p in pages:
            if p <= a < p + Z1:
                return g, p
    return "", 0


def report():
    st = load()
    words, bytes_, names = named_maps()
    nm = sorted(words)
    import bisect

    def near(a):
        i = bisect.bisect_left(nm, a & ~3)
        c = [x for x in (nm[i - 1] if i else None, nm[i] if i < len(nm) else None) if x is not None]
        n = min(c, key=lambda x: abs(x - a))
        return names[n], a - n

    rows, wrows = ["addr\twidth\tvalue\tgroup\tnear_named\tdist"], ["window\tgroup\treads\tanswered\tnonzero\taborted\tstatus"]
    tot = dict(win=0, reads=0, ans=0, nz=0, ab=0, blk=0)
    for w in st["win"]:
        ans = nz = ab = 0
        for a, wd in w["words"]:
            v = st["res"].get(f"{a:#010x}")
            if v is None:
                continue
            if v[0] == "A":
                ab += 1
            else:
                ans += 1
                if v[1]:
                    nz += 1
                    n, d = near(a)
                    rows.append(f"{a:#010x}\t{wd}\t{v[1]:#010x}\t{w['group']}\t{n}\t{d:+d}")
        wrows.append(f"{w['addr']:#010x}\t{w['group']}\t{len(w['words'])}\t{ans}\t{nz}\t{ab}\t{w['status']}")
        if w["status"] != "pending":
            tot["win"] += 1
            tot["reads"] += ans + ab
            tot["ans"] += ans
            tot["nz"] += nz
            tot["ab"] += ab
            tot["blk"] += w["status"] == "blocked"
    (VAL / "u1-gaps.tsv").write_text("# generated by scripts/hwdb_gaps.py report (#86 phase 2): unnamed addresses that answered with a non-zero value\n" + "\n".join(rows) + "\n")
    (VAL / "u1-gap-windows.tsv").write_text("# generated by scripts/hwdb_gaps.py report (#86 phase 2): every 256 B gap window\n" + "\n".join(wrows) + "\n")
    # blocked windows -> u1-blocked.toml (gap_* entries are rewritten, the rest is kept)
    bt = (VAL / "u1-blocked.toml").read_text()
    keep = bt.split("\n# gap windows")[0].rstrip() + "\n"
    out = [keep]
    if any(w["status"] == "blocked" for w in st["win"]):
        out.append("\n# gap windows (scripts/hwdb_gaps.py report): a read inside the window stalled a2, whole 256 B window not read again\n")
    for w in st["win"]:
        if w["status"] == "blocked":
            out.append(f'\n[[blocked]]\nname = "gap_{w["addr"]:08x}"\nlo = {w["addr"]:#010x}\nhi = {w["addr"] + WIN - 1:#010x}\n'
                       f'why = "gap probe ({w["group"]}): read at {w.get("hang_at", w["addr"]):#010x} stalled a2"\nsrc = "tmp/logs/hwdb_gaps.log, #86 phase 2"\n')
    (VAL / "u1-blocked.toml").write_text("".join(out))
    log(f"report: {tot['win']} windows probed, {tot['reads']} reads, {tot['ans']} answered ({tot['nz']} non-zero), {tot['ab']} aborted, {tot['blk']} windows blocked")
    chk = {f"{a:#010x}": st["res"].get(f"{a:#010x}") for a in IDMCHK}
    log("idmchk: " + ", ".join(f"{a}={'-' if v is None else v[1]:#x}" if v else f"{a}=-" for a, v in chk.items()))


def main():
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        return 2
    if a[0] == "plan":
        plan()
    elif a[0] == "step":
        return step(int(a[1]) if len(a) > 1 else RUN_READS)
    elif a[0] == "adhoc":
        return adhoc([int(x, 0) for x in a[1:]])
    elif a[0] == "temp":
        return temp()
    elif a[0] == "state":
        st = load()
        for s in ("pending", "done", "blocked"):
            print(s, sum(1 for w in st["win"] if w["status"] == s))
        print("hangs", st["hangs"])
    elif a[0] == "report":
        report()
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
