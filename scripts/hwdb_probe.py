#!/usr/bin/env python3
"""hwdb_probe — read-only register validation of unit 1 from a2 (#86 phase 1).

Usage: hwdb_probe.py [--unit 0|1] gen|build [--suspect NAME] | run PHASE | report   (no --suspect = main pass; results merge by key into tmp/hwprobe/PHASE.json)
  gen     list from regdb (iprocreg at its AXI address, cpureg at cmic0 + offset) -> tmp/hwprobe/list.tsv + tmp/a2fw/regprobe_list.h
          (skipped rows: write-only, read side effects; blocked rows: hwdb/validation/u1-blocked.toml)
  build   gen + scripts/build_a2fw.py us48-a2-regprobe
  run     push the ELF, run a2 once, parse trace0 -> tmp/hwprobe/PHASE.json (PHASE = cold | warm); board must be up with debugfs
  report  tmp/hwprobe/{cold,warm}.json -> hwdb/validation/u1.toml + u1-values.tsv (needs cold)
--unit 0 (default 1): list for unit 0 (a1), blocklist hwdb/validation/u0-blocked.toml, work dir tmp/hwprobe0, build = a1fw/regprobe_u0.c
payload (run from awto-uboot with scripts/hwdb_probe_u0.py, #86 unit 0), report -> u0.toml + u0-values.tsv (needs uboot.json).
a2 runs the probe with its own data-abort handler (a2fw/regprobe.c): aborts are logged and skipped; a bus stall = hang (cur= stops).
Hang bound: cur= unchanged for HANG_S -> run stops, board needs a power cycle. Log tmp/logs/hwdb_probe.log.
"""
import csv
import json
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hwdb  # noqa: E402

ROOT = hwdb.repo_root()
TMP = ROOT / "tmp/hwprobe"
LOG = ROOT / "tmp/logs/hwdb_probe.log"
BLOCKED = ROOT / "hwdb/validation/u1-blocked.toml"
ELF = "us48-a2-regprobe.elf"
UNIT = 1


def set_unit(n):
    """Select unit 1 (a2, default) or unit 0 (a1): work dir, blocklist."""
    global UNIT, TMP, BLOCKED
    UNIT = n
    TMP = ROOT / ("tmp/hwprobe" if n == 1 else "tmp/hwprobe0")
    BLOCKED = ROOT / f"hwdb/validation/u{n}-blocked.toml"
# 6.7k reads x ~20 PCIe stores each: run measured in ms; cur= frozen for 1 s = stalled bus (expected worst case per block 0.1 s, x10)
HANG_S = 1.0
RUN_CAP_S = 12.0

# regdb flags (SDK soc_reg flags): 0x10 RO, 0x20 WO
SKIP_NAME = [
    (r"_(SRBR_STHR|RBR_THR_DLL)$", "UART FIFO: read pops the RX byte"),
    (r"^CMIC_SEMAPHORE_\d+$", "semaphore: read = test-and-set (acquire)"),
    (r"RNG_DATA$", "RNG FIFO: read pops a word"),
    (r"SMBUS1?_(MASTER|SLAVE)_DATA_READ$", "SMBus RX FIFO: read pops a byte"),
    (r"PKA_DATA_OUTPUT$", "PKA output FIFO: read pops"),
    (r"^IHOST_A9CPU0_DTR(RX|TX)$", "debug comms channel: read clears RXfull/TXfull (a2 console)"),
    (r"^IHOST_GICCPU_INT_ACK$", "GIC interrupt acknowledge: read acks the interrupt"),
    (r"^QSPI_RAF_READ_DATA$", "BSPI read-ahead FIFO: read pops"),
    (r"^GMAC0_FIFOACCESSDATA$", "GMAC FIFO access port: read advances the access"),
    (r"^CMIC_TIMESYNC_INPUT_TIME_FIFO_TS_(LOWER|UPPER)$", "timesync capture FIFO: read pops"),
    (r"^NAND_NAND_FLASH_SEMAPHORE$", "NAND semaphore: read = acquire"),
    (r"^DDR_PHY_WORD_LANE_0_READ_FIFO_DATA_BL\d_\d$", "DDR PHY read FIFO data port"),
    (r"^CHIPCOMMONA_UART[01]_(IIR_FCR|LSR|MSR)$", "UART: read clears the interrupt id / line status / modem status bits"),
    (r"^PAXB_0_CFG_DATA$", "read starts a PCIe config cycle; unit 1 PAXB is an endpoint, the read never completes (hang, 2026-11-16)"),
]


# Ranges suspected to stall (clock gated / IDM reset). Never in the main pass: probed one per run (`gen --suspect NAME`).
# Evidence for the guess: docs/hwdb-map.md block notes (stall when in reset). A stall moves the range to u1-blocked.toml.
SUSPECT = {
    "pnor": (0x18021000, 0x18021fff), "amac": (0x18022000, 0x18025fff), "nand": (0x18026000, 0x18026fff),
    "qspi": (0x18027000, 0x18027fff), "sram": (0x1802e000, 0x1802efff), "ddrctl": (0x18010000, 0x18010fff),
}


PHASE = ""  # unit 0: blocked entries with phase = X apply to pass X only
UNBLOCK = ()  # unit 0: blocked entries (names) tried once in this run (--unblock NAME)
ONLY = None  # (lo, hi): probe only this address range (unit 0 --only)


def log(msg):
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def blocked():
    if not BLOCKED.exists():
        return []
    return tomllib.loads(BLOCKED.read_text()).get("blocked", [])


def rows(suspect=None):
    """suspect=None: main pass (suspect ranges skipped as deferred); suspect=NAME: only that range.
    [dict(key, kind, addr, rst, rmask, flags, skip)] in probe order: iproc by address, then cmic by address."""
    cmic_base = hwdb.default().blocks["cmic0"].base
    out = []
    with open(ROOT / "regdb/hr2_regs.tsv", newline="") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            if r["regtype_name"] not in ("iprocreg", "cpureg"):
                continue
            kind = "sdk_iproc" if r["regtype_name"] == "iprocreg" else "sdk_cmic"
            base = 0 if kind == "sdk_iproc" else cmic_base
            n = int(r["numels"])
            flags = int(r["flags"], 0)
            for i in range(n):
                name = r["name"] if n == 1 else f"{r['name']}[{i}]"
                skip = ""
                if flags & 0x20:
                    skip = "write-only (regdb flag 0x20)"
                for pat, why in SKIP_NAME:
                    if re.search(pat, r["name"]):
                        skip = why
                addr = base + int(r["offset"], 0) + 4 * i
                # byte registers: ChipcommonA UARTs (8-bit APB, a word read aborts) and every offset that is not word aligned
                width = 1 if (addr & 3 or r["name"].startswith("CHIPCOMMONA_UART")) else 4
                out.append(dict(key=f"{kind}.{name}", kind=kind, addr=addr, width=width,
                                rst=int(r["rst_lo"], 0), rmask=int(r["rmask_lo"], 0), flags=flags, skip=skip))
    bl = blocked()
    for x in out:
        for b in bl:
            if b.get("phase", PHASE) != PHASE or b["name"] in UNBLOCK:
                continue
            if b["lo"] <= x["addr"] <= b["hi"] and not x["skip"]:
                x["skip"] = f"blocked ({b.get('state', 'hang')}): {b['name']}"
    for x in out:
        sus = [n for n, (lo, hi) in (SUSPECT | ({"dmac": (0x18020000, 0x18020fff)} if UNIT == 0 else {})).items() if x["kind"] == "sdk_iproc" and lo <= x["addr"] <= hi]
        if x["kind"] == "sdk_cmic" and UNIT == 1:
            sus = ["cmic"]  # unit 1 CMIC as seen by a2 at 0x48000000 is untested: own run
        if ONLY and not x["skip"] and not (ONLY[0] <= x["addr"] <= ONLY[1]):
            x["skip"] = "not in this run"
        elif ONLY:
            pass
        elif suspect is None and sus and not x["skip"]:
            x["skip"] = f"deferred: {sus[0]} range probed in its own run"
        elif suspect is not None and not (sus and sus[0] == suspect) and not x["skip"]:
            x["skip"] = "not in this run"
    order = {"sdk_iproc": 0, "sdk_cmic": 1}
    out.sort(key=lambda x: (order[x["kind"]], x["addr"], x["key"]))
    return out


def gen(suspect=None):
    TMP.mkdir(parents=True, exist_ok=True)
    rs = rows(suspect)
    with open(TMP / "list.tsv", "w") as f:
        f.write("idx\tkey\taddr\twidth\trst\trmask\tflags\tskip\n")
        probe = [x for x in rs if not x["skip"]]
        for i, x in enumerate(probe):
            x["idx"] = i
        for x in rs:
            f.write(f"{x.get('idx', '')}\t{x['key']}\t{x['addr']:#010x}\t{x['width']}\t{x['rst']:#x}\t{x['rmask']:#x}\t{x['flags']:#x}\t{x['skip']}\n")
    h = ROOT / ("tmp/a2fw/regprobe_list.h" if UNIT == 1 else "tmp/hwprobe0/regprobe_list.h")
    h.parent.mkdir(parents=True, exist_ok=True)
    h.write_text("/* generated by scripts/hwdb_probe.py gen, do not edit */\n#define RP_N %d\nstatic const uint32_t rp_addr[RP_N] = {\n%s};\nstatic const uint8_t rp_w[RP_N] = {\n%s};\n" %
                 (len(probe), "".join(f"\t0x{x['addr']:08x},\n" for x in probe), "".join(f"\t{x['width']},\n" for x in probe)))
    log(f"gen{' --suspect ' + suspect if suspect else ''}: {len(rs)} registers, {len(probe)} to read, {len(rs) - len(probe)} skipped")
    return probe


def sh(*a, timeout=60):
    return subprocess.run([sys.executable, *a], capture_output=True, text=True, cwd=ROOT, timeout=timeout)


def build0(suspect=None):
    """Unit 0: list + a1fw/regprobe_u0.c -> tmp/hwprobe0/regprobe_u0.bin (load 0x01000000, `go`)."""
    gen(suspect)
    d = TMP
    cc = ["arm-linux-gnu-gcc", "-mcpu=cortex-a9", "-marm", "-mfloat-abi=soft", "-O2", "-Wall", "-Wextra", "-Werror", "-ffreestanding",
          "-fno-pic", "-nostdlib", "-fno-builtin", "-I", str(d), "-T", str(ROOT / "a1fw/regprobe_u0.ld"), "-Wl,--build-id=none",
          "-o", str(d / "regprobe_u0.elf"), str(ROOT / "a1fw/regprobe_u0_asm.S"), str(ROOT / "a1fw/regprobe_u0.c")]
    # host compile is ~0.1 s; 30 s = hung tool
    r = subprocess.run(cc, capture_output=True, text=True, timeout=30)
    log(f"build0 rc={r.returncode} {r.stdout[-300:]}{r.stderr[-300:]}")
    if r.returncode:
        return r.returncode
    r = subprocess.run(["arm-linux-gnu-objcopy", "-O", "binary", str(d / "regprobe_u0.elf"), str(d / "regprobe_u0.bin")],
                       capture_output=True, text=True, timeout=30)
    log(f"objcopy rc={r.returncode} {(d / 'regprobe_u0.bin').stat().st_size} B")
    return r.returncode


def build(suspect=None):
    if UNIT == 0:
        return build0(suspect)
    gen(suspect)
    r = sh("scripts/build_a2fw.py", "us48-a2-regprobe", timeout=90)
    log(f"build rc={r.returncode} {r.stdout[-200:]}{r.stderr[-200:]}")
    return r.returncode


REMOTE = r"""
D=/sys/class/remoteproc/remoteproc0; T=/sys/kernel/debug/remoteproc/remoteproc0; P=/sys/module/us48_a2/parameters
mount | grep -q debugfs || mount -t debugfs none /sys/kernel/debug
[ -p /tmp/f ] || mkfifo /tmp/f; exec 3<>/tmp/f
lsmod | grep -q us48_a2 || insmod /lib/modules/*/us48_a2.ko
[ "$(cat $D/state)" = offline ] || echo stop > $D/state
echo 100 > $P/cpu_mhz; echo 1 > $P/dcc_poll; echo 0 > $P/hdbgen_off
echo @ELF@ > $D/firmware
echo start > $D/state
n=0; last=""; same=0
while [ $n -lt @N@ ]; do
  read -t 0.1 x <&3; n=$((n+1)); cur=$(head -c 64 $T/trace0)
  case "$cur" in *cur=*) ;; *) continue;; esac
  if [ "$cur" = "$last" ]; then same=$((same+1)); else same=0; fi
  last="$cur"
  [ $same -ge @SAME@ ] && break
  [ -n "$(tail -c 4096 $T/trace0 | grep -m1 '^done')" ] && break
done
echo "poll=$n"
cat $T/trace0
"""


def run(phase):
    TMP.mkdir(parents=True, exist_ok=True)
    probe = [r for r in csv.DictReader(open(TMP / "list.tsv"), delimiter="\t") if r["idx"] != ""]
    sh("scripts/lx.py", "--put", str(ROOT / f"tmp/a2fw/{ELF}"), f"/lib/firmware/{ELF}")
    cmd = REMOTE.replace("@ELF@", ELF).replace("@N@", str(int(RUN_CAP_S / 0.1))).replace("@SAME@", str(int(HANG_S / 0.1)))
    t0 = time.monotonic()
    try:
        r = sh("scripts/lx.py", cmd, timeout=RUN_CAP_S + 25)
    except subprocess.TimeoutExpired:
        log("run: ssh/a1 stalled (timeout): power cycle the board")
        return 2
    out = r.stdout
    (TMP / f"{phase}.raw").write_text(out)
    res, done, cur = {}, False, None
    for ln in out.splitlines():
        m = re.match(r"regprobe v1 n=([0-9a-f]{4}) cur=([0-9a-f]{4})", ln)
        if m:
            cur = int(m.group(2), 16)
        m = re.match(r"([0-9a-f]{4}) (\.|A) ([0-9a-f]{8})$", ln)
        if m:
            res[int(m.group(1), 16)] = (m.group(2), int(m.group(3), 16))
        if ln.startswith("done"):
            done = True
    log(f"run {phase}: {len(res)}/{len(probe)} records, done={done}, cur={cur}, {time.monotonic() - t0:.1f} s; {out.splitlines()[-1][:80] if out else ''}")
    pj = TMP / f"{phase}.json"
    allres = json.loads(pj.read_text()) if pj.exists() else {}
    for k, v in res.items():
        allres[probe[k]["key"]] = v
    pj.write_text(json.dumps(allres))
    if not done:
        i = cur if cur is not None else len(res)
        row = probe[i] if i < len(probe) else None
        log(f"HANG at idx {i}: {row['key'] if row else '?'} {row['addr'] if row else ''}: power cycle the board")
        return 3
    return 0


# mismatch classes (first match wins): (id, name regex on the regdb name, text). Classes are causes, not registers.
CLASSES = [
    ("a2-boot-path", r"^(IHOST_PROC_CLK_|CRU_IHOST|DMU_CRU_IHOST|PAXB_0_)", "written or live-changed by the a2 start path / us48_pci before the read (cpu_mhz PLL, PAXB windows, link state)"),
    ("ddr-raz", r"^(DDR_DENALI|DDR_PHY|DDR_BIST)", "unit 1 DDR controller / PHY / BIST block reads 0, regdb reset is not 0 (block in reset or unclocked; DDR unused on unit 1)"),
    ("nand", r"^NAND_(NAND|RO|COPY|ECC)", "unit 1 NAND controller registers hold values other than regdb reset (cold != warm for some)"),
    ("idm-errlog", r"_IDM_ERROR_LOG_|^IHOST_SECTRAP_|IDM_IDM_ERROR", "IDM / secure-trap error capture: holds the faults of the boot path and of this probe's own aborted reads"),
    ("idm-ids", r"_IDM_(IDM_)?RESET_(READ|WRITE)_ID$|IO_STATUS$|RESET_STATUS$", "IDM RESET_READ_ID/WRITE_ID constants (0x2/0xc), IO_STATUS, RESET_STATUS: hardware-fixed values, regdb reset 0"),
    ("arm-ip", r"^IHOST_(A9|GIC|L2C|SCU|GTIM|AXITRACE|ARM)", "ARM IP (CoreSight debug/PTM/PMU/CTI, GIC, L2C, SCU, global timer): regdb reset 0 or generic, hardware shows ID/config/live values"),
]


def classify(key, row):
    name = re.sub(r"\[\d+\]$", "", key.split(".", 1)[1])
    for cid, pat, text in CLASSES:
        if cid == "a2-boot-path" and UNIT == 0:
            continue
        if re.search(pat, name):
            return cid
    if row["flags"] & 0x10:
        return "ro-status"
    return "other"


CLASS_TEXT = dict((c, t) for c, _, t in CLASSES) | {
    "ro-status": "read-only status / strap / ID registers: regdb reset 0 or generic, hardware shows the live or fixed value",
    "ddr-live": "unit 0 DDR controller / PHY / BIST hold the configuration and training results of the live DRAM; regdb reset differs",
    "other": "read-write registers holding a non-reset value (set by boot ROM, strap or the boot path)"}


def group(key):
    n = key.split(".", 1)[1]
    t = n.split("_")
    return "_".join(t[:2]) if key.startswith("sdk_cmic") else t[0]


def report():
    global PHASE
    p1, p2 = ("cold", "warm") if UNIT == 1 else ("uboot", "linux")
    PHASE = p1  # unit 0: no cold read possible from a1 (uboot-1 + awto-uboot ran first)
    cold = json.loads((TMP / f"{p1}.json").read_text())
    warm = json.loads((TMP / f"{p2}.json").read_text()) if (TMP / f"{p2}.json").exists() else {}
    date = time.strftime("%Y-%m-%d")
    warm0 = dict(warm)
    allrows = rows(None)
    lin_skip = {}
    if UNIT == 0:
        PHASE = p2
        lin_skip = {x["key"]: x["skip"] for x in rows(None)}  # rows blocked only in the uboot state (PL330) but read in the linux state
        PHASE = p1
    # skip reasons of rows that were not read in any pass (main pass skip text only, deferred/not-in-run are reads)
    who = "a2 reads of unit 1" if UNIT == 1 else "a1 reads of unit 0 (awto-uboot payload a1fw/regprobe_u0.c + Linux /dev/mem)"
    st = "the cold power-on state" if UNIT == 1 else "the awto-uboot state (after uboot-1 + awto-uboot init; a1 cannot read a cold chip)"
    out = [f"# generated by scripts/hwdb_probe.py --unit {UNIT} report from {who} (#86), do not edit.",
           f"# hw = {{seen, reset_ok, ro_ok, date, unit}}; reset_ok = ((first pass ^ regdb reset) & rmask) == 0 on {st};",
           "# ro_ok = bits outside regdb rmask equal regdb reset (omitted when rmask covers all 32 bits). seen = false: why = skip/block/abort reason.", ""]
    vals = [f"key\taddr\trst\trmask\t{p1}\t{p2}\tclass"]
    cur = None
    mism = {}
    for r in allrows:
        k = r["key"]
        if r["kind"] != cur:
            cur = r["kind"]
            out.append(f"[hw.{cur}]")
        nm = k.split(".", 1)[1]
        c, w = cold.get(k), warm.get(k)
        if r["skip"] and not r["skip"].startswith(("deferred", "not in this run")):
            c = w = None  # skipped or blocked now: ignore a result left over from an older list
            if UNIT == 0 and k in warm0 and k in lin_skip and (not lin_skip[k] or lin_skip[k].startswith("deferred")):
                c = warm0[k]  # only the linux state read it: first-pass value = linux value
        cls = ""
        if c is None:
            why = r["skip"] or "not read"
            why = re.sub(r"^(deferred|not in this run).*", "not read", why)
            entry = f'{{seen = false, why = "{why}"}}'
        elif c[0] == "A":
            entry = f'{{seen = false, why = "abort dfsr {c[1]:#x}", date = "{date}", unit = {UNIT}}}'
            cls = "abort"
        else:
            v = c[1]
            ok = ((v ^ r["rst"]) & r["rmask"]) == 0
            f = f'seen = true, reset_ok = {str(ok).lower()}'
            if r["rmask"] != 0xFFFFFFFF:
                f += f', ro_ok = {str(((v ^ r["rst"]) & ~r["rmask"] & 0xFFFFFFFF) == 0).lower()}'
            entry = "{" + f + f', date = "{date}", unit = {UNIT}' + "}"
            if not ok:
                cls = classify(k, r)
                if UNIT == 0 and cls == "ddr-raz":
                    cls = "ddr-live"  # unit 0 DDR is initialised: values are the trained config, not zeros
                mism.setdefault(cls, []).append(k)
        out.append(f'"{nm}" = {entry}')
        vals.append(f"{k}\t{r['addr']:#010x}\t{r['rst']:#x}\t{r['rmask']:#x}\t" +
                    ("-" if c is None else (f"A{c[1]:#x}" if c[0] == "A" else f"{c[1]:#010x}")) + "\t" +
                    ("-" if w is None else (f"A{w[1]:#x}" if w[0] == "A" else f"{w[1]:#010x}")) + f"\t{cls}")
    vd = ROOT / "hwdb/validation"
    vd.mkdir(exist_ok=True)
    (vd / f"u{UNIT}.toml").write_text("\n".join(out) + "\n")
    (vd / f"u{UNIT}-values.tsv").write_text("\n".join(vals) + "\n")
    byk = {r["key"]: r for r in allrows}
    lines = []
    for cls, ks in sorted(mism.items(), key=lambda x: -len(x[1])):
        lines.append(f"## {cls}: {len(ks)} registers\n{CLASS_TEXT.get(cls, '')}")
        for k in ks[:12]:
            r = byk[k]
            lines.append(f"- {k.split('.', 1)[1]} {r['addr']:#x} cold {cold[k][1]:#010x} regdb {r['rst']:#x} rmask {r['rmask']:#x}")
        if len(ks) > 12:
            lines.append(f"- ... {len(ks) - 12} more (hwdb/validation/u{UNIT}-values.tsv, class column)")
    (TMP / "mismatch.md").write_text("\n".join(lines) + "\n")
    log(f"report: {len(allrows)} registers, mismatch classes {{{', '.join(f'{c}: {len(k)}' for c, k in mism.items())}}}")


def main():
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        return 2
    if "--unit" in a:
        i = a.index("--unit")
        set_unit(int(a[i + 1]))
        del a[i:i + 2]
    sus = a[a.index("--suspect") + 1] if "--suspect" in a else None
    if a[0] == "gen":
        gen(sus)
        return 0
    if a[0] == "build":
        return build(sus)
    if a[0] == "run":
        return run(a[1])
    if a[0] == "report":
        report()
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
