#!/usr/bin/env python3
"""hwdb_probe_u0 — unit 0 read-only register validation from a1 at the awto-uboot prompt (#86).

Usage: hwdb_probe_u0.py run PHASE [--suspect NAME] [--only LO HI] [--unblock NAME] | prompt [--cycle] | temp
  run   hwdb_probe.py --unit 0 build, TFTP a1fw/regprobe_u0.bin to 0x01000000, `go`, parse the UART stream -> tmp/hwprobe0/PHASE.json
        (PHASE = uboot). Board must be at the awto-uboot> prompt (scripts/dev.py uboot). --only: probe just that address range.
  prompt  back at the prompt: `reset` (warm) or --cycle = relay power cycle
  temp  `fan` at the prompt (die temperatures)
Hang bound: no console byte for HANG_S while the payload runs = a1 stalled: log idx + register, board needs a power cycle (exit 3).
Log tmp/logs/hwdb_probe_u0.log.
"""
import json
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import console  # noqa: E402
import dev  # noqa: E402
import hwdb_probe as hp  # noqa: E402

hp.set_unit(0)
ROOT = hp.ROOT
LOG = ROOT / "tmp/logs/hwdb_probe_u0.log"
# Payload prints ~40 KiB at 115200 baud (3.5 s, expected). A register read is < 1 ms, so no byte for 1.0 s = stalled read (x1000).
HANG_S = 1.0
TOP = False  # --top: log TOP reset/PLL registers over S-channel before `go`
TOP_REGS = [("TOP_SOFT_RESET_REG", 0x2030400), ("TOP_SOFT_RESET_REG_2", 0x2030800), ("TOP_MISC_CONTROL_1", 0x2031400),
            ("TOP_TS_PLL_STATUS", 0x204ac00)]
# 3956 tokens x 9 chars = 36 KB = 3.1 s of UART time; backstop = 4x
RUN_CAP_S = 14.0


def log(msg):
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def run(phase, suspect=None, only=None):
    hp.PHASE = phase
    if only:
        hp.ONLY = only
    if hp.build(suspect):
        return 1
    probe = [r for r in __import__("csv").DictReader(open(hp.TMP / "list.tsv"), delimiter="\t") if r["idx"] != ""]
    shutil.copy(hp.TMP / "regprobe_u0.bin", dev.TFTP_DIR / "regprobe_u0.bin")
    s = console.Session()
    try:
        s.write(b"\x03\x03")
        s.send_line("")
        s.expect(dev.U2_PROMPTS, 5.0, "awto-uboot prompt")
        if not dev._u2_tftp(s, "regprobe_u0.bin"):
            return 1
        s.send_line("")
        s.expect(dev.U2_PROMPTS, 5.0, "prompt after tftp")
        # tftpboot leaves the payload dirty in the D-cache: `dcache flush` cleans it to RAM, `icache flush` drops stale lines
        # (not `dcache off/on`: `on` rebuilds the page table and drops the uncached DMA regions of the eth driver)
        for c in ("dcache flush", "icache flush"):
            s.send_line(c)
            s.expect(dev.U2_PROMPTS, 5.0, c)
        if TOP:  # read-only S-channel reads of the TS/BS reset + PLL state (#91), after the TFTP bring-up
            for nm, a in TOP_REGS:
                s.buf = b""
                s.send_line(f"schan r 16 {a:#x}")
                t1 = time.monotonic()
                while not any(p in s.buf for p in dev.U2_PROMPTS) and time.monotonic() - t1 < 5.0:  # schan answers in ms
                    s._pump(0.05)
                m = re.search(r"\[0\] = 0x([0-9a-f]{8})", s.buf.decode("latin1", "replace"))
                log(f"TOP {nm} {a:#x} = {"0x" + m.group(1) if m else '?'}")
        if TOP == "only":
            return 0
        s.write(b"\x03")
        s.buf = b""
        s.send_line("go 0x01000000")
        t0 = last = time.monotonic()
        while True:
            if s._pump(0.05):
                last = time.monotonic()
            now = time.monotonic()
            if re.search(rb"DONE aborts=[0-9a-f]{4} ticks=[0-9a-f]{8}\n", s.buf):
                break
            if now - last > HANG_S:
                break
            if now - t0 > RUN_CAP_S:
                log(f"run backstop {RUN_CAP_S} s")
                break
        raw = s.buf.decode("latin1", "replace")
        if re.search(r"DONE aborts=", raw):
            s.expect(dev.U2_PROMPTS, 5.0, "prompt after go")
    finally:
        s.close()
    (hp.TMP / f"{phase}.raw").write_text(raw)
    res, done, ntok = {}, "DONE aborts=" in raw, 0
    m = re.search(r"U0P1 n=([0-9a-f]{4})", raw)
    body = raw[m.end():] if m else ""
    for ln in body.splitlines():
        mm = re.match(r"([0-9a-f]{4}):(.*)$", ln)
        if not mm:
            continue
        i = int(mm.group(1), 16)
        for tok in mm.group(2).split(" "):
            if len(tok) != 8:
                continue
            k = probe[i]["key"] if i < len(probe) else None
            if tok[0] == "A" and re.fullmatch(r"A[0-9a-f]{7}", tok):
                res[i] = ("A", int(tok[1:], 16))
            elif re.fullmatch(r"[0-9a-f]{8}", tok):
                res[i] = (".", int(tok, 16))
            else:
                continue
            i += 1
    ntok = len(res)
    log(f"run {phase}: {ntok}/{len(probe)} records, done={done}, {time.monotonic() - t0:.1f} s")
    pj = hp.TMP / f"{phase}.json"
    allres = json.loads(pj.read_text()) if pj.exists() else {}
    for k, v in res.items():
        allres[probe[k]["key"]] = v
    pj.write_text(json.dumps(allres))
    if not done:
        i = ntok
        row = probe[i] if i < len(probe) else None
        log(f"HANG at idx {i}: {row['key'] if row else '?'} {row['addr'] if row else ''}: tail {raw[-80:]!r}: power cycle the board")
        return 3
    return 0


def run_linux(phase, suspect=None, only=None):
    """Linux state: a1fw/regprobe_linux.c (static musl, /dev/mem, SIGBUS = abort) over ssh; same list, merged into PHASE.json."""
    import csv
    import select
    import subprocess
    import lx
    hp.PHASE = phase
    if only:
        hp.ONLY = only
    hp.gen(suspect)
    probe = [r for r in csv.DictReader(open(hp.TMP / "list.tsv"), delimiter="\t") if r["idx"] != ""]
    # host compile ~0.2 s; 30 s = hung tool
    r = subprocess.run([str(ROOT / "private/tools/arm-linux-musleabi-cross/bin/arm-linux-musleabi-gcc"), "-static", "-O2", "-Wall",
                        "-o", str(hp.TMP / "regprobe_linux"), str(ROOT / "a1fw/regprobe_linux.c")], capture_output=True, text=True, timeout=30)
    if r.returncode:
        log(f"compile failed: {r.stderr[-300:]}")
        return 1
    subprocess.run([sys.executable, str(ROOT / "scripts/lx.py"), "--put", str(hp.TMP / "regprobe_linux"), "/tmp/regprobe_linux"],
                   capture_output=True, timeout=30)
    inp = "".join(f"{int(x['addr'], 16):x} {x['width']}\n" for x in probe)
    p = subprocess.Popen(["ssh", *lx.OPTS, f"root@{lx.HOST}", "/tmp/regprobe_linux"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT)
    p.stdin.write(inp.encode())
    p.stdin.close()
    t0 = last = time.monotonic()
    raw, done = b"", False
    # ssh connect + exec ~1 s; first byte within 10 s, then HANG_S between bytes; backstop RUN_CAP_S
    while True:
        rd, _, _ = select.select([p.stdout], [], [], 0.05)
        now = time.monotonic()
        if rd:
            d = __import__("os").read(p.stdout.fileno(), 65536)
            if not d:
                break
            raw += d
            last = now
            if b"DONE n=" in raw:
                done = True
                break
        elif now - last > (HANG_S if raw else 10.0) or now - t0 > RUN_CAP_S:
            break
    p.kill()
    text = raw.decode("latin1", "replace")
    (hp.TMP / f"{phase}-{suspect or 'main'}.raw").write_text(text)
    res = {}
    for ln in text.splitlines():
        m = re.fullmatch(r"(\d+):([0-9a-f]{8}|A|M)", ln.strip())
        if m:
            res[int(m.group(1))] = ("A", 0) if m.group(2) in ("A", "M") else (".", int(m.group(2), 16))
    log(f"run {phase} (linux, suspect={suspect}): {len(res)}/{len(probe)} records, done={done}, {time.monotonic() - t0:.1f} s")
    pj = hp.TMP / f"{phase}.json"
    allres = json.loads(pj.read_text()) if pj.exists() else {}
    for k, v in res.items():
        allres[probe[k]["key"]] = v
    pj.write_text(json.dumps(allres))
    if not done:
        i = len(res)
        row = probe[i] if i < len(probe) else None
        log(f"HANG at idx {i}: {row['key'] if row else '?'} {row['addr'] if row else ''}: power cycle the board")
        return 3
    return 0


def prompt(cycle=False):
    """Back at the awto-uboot> prompt: `reset` from the prompt (warm, no relay), or cycle=True a relay power cycle."""
    import u2_run
    s = console.Session()
    try:
        if cycle:
            return u2_run.catch_window(s, True)
        s.write(b"\x03\x03")
        s.send_line("")
        s.expect(dev.U2_PROMPTS, 5.0, "prompt before reset")
        s.buf = b""
        s.send_line("reset")
        t0 = time.monotonic()
        while time.monotonic() - t0 < u2_run.WINDOW_S:
            s._pump(0.02)
            tail = s.buf[-3000:]
            i = tail.rfind(b"awto-uboot 20")
            if i >= 0 and b"awto-uboot> " in tail[i:]:
                return True
            if i >= 0:
                s.write(b" ")
        return False
    finally:
        s.close()


def temp():
    s = console.Session()
    try:
        s.write(b"\x03\x03")
        s.send_line("")
        s.expect(dev.U2_PROMPTS, 5.0, "prompt")
        s.send_line("fan")
        print(s.read_idle(0.5, 5.0).decode("latin1", "replace"))
    finally:
        s.close()


def main():
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        return 2
    if a[0] == "prompt":
        ok = prompt("--cycle" in a)
        log("at awto-uboot>" if ok else "no prompt")
        return 0 if ok else 1
    if a[0] == "temp":
        temp()
        return 0
    if a[0] == "run":
        global TOP
        TOP = "only" if "--top-only" in a else "--top" in a
        sus = a[a.index("--suspect") + 1] if "--suspect" in a else None
        if "--unblock" in a:
            hp.UNBLOCK = (a[a.index("--unblock") + 1],)
        only = (int(a[a.index("--only") + 1], 0), int(a[a.index("--only") + 2], 0)) if "--only" in a else None
        return (run_linux if a[1] == "linux" else run)(a[1], sus, only)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
