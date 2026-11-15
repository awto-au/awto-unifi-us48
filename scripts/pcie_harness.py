#!/usr/bin/env python3
"""PCIe bus harness host driver (#80): unit 0 (a1) <-> unit 1 (a2) performance + integrity, over ssh to mainline Linux.

Usage: pcie_harness.py smoke [--a2-fw NAME] [--no-a2]
       pcie_harness.py soak --hours 8 [--a2-fw NAME] [--no-a2]
Board side: kernel/us48-bustest (debugfs us48_bustest/) + a2fw/bustest via remoteproc. Doc: docs/pcie-harness.md.
Host $US48_SSH (default root@192.168.25.207), ssh BatchMode like sfp_ddm.py; recovery text from the console socket.
Logs: tmp/logs/pcie_harness.log (all), pcie_soak.log (one line per minute + hourly), pcie_soak_report.md, pcie_freeze-*.txt.
Exit: 0 = pass / completed or operator stop, 1 = smoke failure or setup error, 2 = soak stopped on a failure (state frozen).
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import re
import selectors
import signal
import subprocess
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOGDIR = ROOT / "tmp/logs"
LOG = LOGDIR / "pcie_harness.log"
SOAK_LOG = LOGDIR / "pcie_soak.log"
REPORT = LOGDIR / "pcie_soak_report.md"
SMOKE_JSON = LOGDIR / "pcie_smoke.json"
HOST = os.environ.get("US48_SSH", "root@192.168.25.207")
DBG = "/sys/kernel/debug/us48_bustest"
DEFAULT_FW = "us48-a2-bustest.elf"

# Host-process timeouts (CLAUDE.md exception for host processes): stall = no output for 60 s = hung; overall = 3x the
# measured smoke wall time of that call (backstop). Kernel-side per-test bound = 1.25x the smoke duration, floor below.
STALL_S = 60.0
OVERALL_X = 3.0
KERNEL_X = 1.25
KERNEL_FLOOR_MS = 50	# smallest bound: IRQ + scheduler jitter on a loaded A9 (xe traffic, fan driver) is of this order
FIRST_RUN_LIMIT_MS = 2000	# smoke has no measurement yet: kernel default backstop
SSH_FIRST_BOUND_S = 30.0	# first ssh calls of smoke (connect + insmod + one cycle), no measurement yet
# a2 first pass: remoteproc start (~ms) + 1 MiB cached + 64 KiB uncached word tests + sweep, arithmetic ~0.5 s. Backstop
# until the first board run measures it; expiry = setup fails with the trace0 text.
A2_READY_S = 5.0
DIE_STOP_C = 100.0
TEMP_BLIND_CYCLES = 3
SLOW_STOP = 3	# consecutive kernel-side test timeouts that stop the soak (one = counted only)
HEARTBEAT_STALL_CYCLES = 3

# test name -> (debugfs cmd, count)
TESTS = [("lat", "lat", 20000), ("burst", "burst", 200), ("schan", "schan", 500),
         ("pat_walk1", "pat_walk1", 1), ("pat_walk0", "pat_walk0", 1), ("pat_addr", "pat_addr", 1),
         ("pat_prbs31", "pat_prbs31", 1)]


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def log(msg: str, f=None) -> None:
    line = f"{now_iso()} {msg}"
    print(line, flush=True)
    LOGDIR.mkdir(parents=True, exist_ok=True)
    for p in (LOG, f):
        if p:
            with p.open("a") as fh:
                fh.write(line + "\n")


def wait(s: float) -> None:
    threading.Event().wait(s)


class Stall(Exception):
    pass


class Unreachable(Exception):
    pass


def ssh(cmd: str, bound_s: float) -> tuple[int, str]:
    """One ssh call. Killed on 60 s without output (Stall) or after bound_s overall (Stall)."""
    argv = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=3", HOST, cmd]
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    sel = selectors.DefaultSelector()
    sel.register(p.stdout, selectors.EVENT_READ)
    out = b""
    t0 = last = time.monotonic()
    while True:
        for _ in sel.select(1.0):
            chunk = os.read(p.stdout.fileno(), 65536)
            if chunk:
                out += chunk
                last = time.monotonic()
            else:
                sel.unregister(p.stdout)
        if p.poll() is not None and not sel.get_map():
            break
        t = time.monotonic()
        if t - last > STALL_S or t - t0 > bound_s:
            p.kill()
            p.wait()
            why = f"no output for {STALL_S:.0f} s" if t - last > STALL_S else f"over the {bound_s:.1f} s bound"
            raise Stall(f"ssh `{cmd[:60]}`: {why} (last output {out[-200:]!r})")
    if p.returncode == 255:
        raise Unreachable(out.decode(errors="replace").strip()[-200:])
    return p.returncode, out.decode(errors="replace")


def header_consts() -> dict:
    """BT_* constants and field names from a2fw/bustest.h (single source with the firmware and the module)."""
    txt = (ROOT / "a2fw/bustest.h").read_text()
    c = {m[0]: int(m[1].rstrip("Uu"), 0) for m in re.findall(r"#define (BT_[A-Z_]+)\s+(0x[0-9a-fA-F]+U?|\d+U?)\b", txt)}
    return c


def kv(line: str) -> dict:
    out = {}
    for m in re.finditer(r"(\w+)=(\S+)", line):
        out[m[1]] = m[2]
    return out


def num(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


class Agg:
    """min/max/mean per metric."""

    def __init__(self):
        self.d = {}

    def add(self, k, v):
        if v is None:
            return
        e = self.d.setdefault(k, [0, 0.0, float("inf"), float("-inf")])
        e[0] += 1
        e[1] += v
        e[2] = min(e[2], v)
        e[3] = max(e[3], v)

    def mean(self, k):
        e = self.d.get(k)
        return e[1] / e[0] if e else None

    def table(self):
        return {k: (e[2], e[1] / e[0], e[3], e[0]) for k, e in sorted(self.d.items())}


def a2_metrics(a: dict, c: dict) -> dict:
    """a2 results struct -> MB/s and ns. Ticks are of the last measurement of each kind."""
    hz = c["BT_TICK_HZ"]

    def mbps(nbytes, ticks):
        return nbytes * hz / ticks / 1e6 if ticks else None

    def ns(ticks, n=1):
        return ticks * 1e9 / hz / n if ticks else None

    g = lambda k: num(a.get(k))
    cb, ub, rb = c["BT_CBYTES"], c["BT_UBYTES"], c["BT_RBUF_SIZE"]
    m = {"a2_rd_u_MBps": mbps(ub, g("rd_u")), "a2_wr_u_MBps": mbps(ub, g("wr_u")), "a2_cpy_u_MBps": mbps(ub, g("cpy_u")),
         "a2_lat_u_ns": ns(g("lat_u_sum"), c["BT_LAT_N"]), "a2_lat_u_min_ns": ns(g("lat_u_min")),
         "a2_lat_u_max_ns": ns(g("lat_u_max")), "a2_ifetch_cold_MBps": mbps(c["BT_IFETCH_BYTES"], g("ifetch_cold")),
         "a2_ifetch_warm_MBps": mbps(c["BT_IFETCH_BYTES"], g("ifetch_warm"))}
    if g("mmu"):
        m.update({"a2_rd_c_MBps": mbps(rb, g("rd_c")), "a2_wr_c_MBps": mbps(rb, g("wr_c")),
                  "a2_cpy_c_MBps": mbps(cb, g("cpy_c")), "a2_lat_line_ns": ns(g("lat_line_sum"), c["BT_LAT_N"])})
    return m


class Board:
    def __init__(self, a2_fw: str | None, consts: dict):
        self.a2_fw = a2_fw
        self.c = consts
        self.limits = {}	# test -> kernel limit ms
        self.ssh_bound = SSH_FIRST_BOUND_S

    def run(self, cmd: str, bound: float | None = None) -> str:
        rc, out = ssh(cmd, bound or self.ssh_bound)
        return out

    def setup(self) -> None:
        sh = f"""d=/sys/kernel/debug
[ -d $d/remoteproc ] || mount -t debugfs none $d
for m in us48_pci us48_a2 us48_bustest; do [ -d /sys/module/$m ] || insmod /lib/us48/$m.ko || echo "FAIL insmod $m"; done
[ -d {DBG} ] || echo "FAIL no {DBG}"
"""
        if self.a2_fw:
            sh += f"""for r in /sys/class/remoteproc/*; do [ "$(cat $r/name)" = us48-a2 ] && rp=$r; done
[ -n "$rp" ] || echo "FAIL no remoteproc us48-a2"
[ "$(cat $rp/state)" = offline ] || echo stop > $rp/state
echo a2clear > {DBG}/cmd
echo {self.a2_fw} > $rp/firmware
echo start > $rp/state || echo "FAIL a2 start"
echo "rp=$rp state=$(cat $rp/state)"
"""
        out = self.run(sh)
        log("setup: " + out.strip().replace("\n", " | "))
        if "FAIL" in out:
            raise SystemExit(f"setup failed: {out.strip()}")
        if self.a2_fw:
            t0 = time.monotonic()
            while time.monotonic() - t0 < A2_READY_S:
                a = kv(self.run(f"cat {DBG}/a2"))
                if a.get("a2") == "running" and num(a.get("pass")) >= 1:
                    log(f"a2 running after {time.monotonic() - t0:.1f} s: mmu={a.get('mmu')} sctlr={int(a.get('sctlr', 0)):08x}")
                    return
            tr = self.run("cat /sys/kernel/debug/remoteproc/remoteproc*/trace0; dmesg | tail -15")
            raise SystemExit(f"a2 not running {A2_READY_S} s after start; trace/dmesg:\n{tr}")

    def cycle_cmd(self) -> str:
        parts = []
        for name, cmd, n in TESTS:
            lim = self.limits.get(name, FIRST_RUN_LIMIT_MS)
            parts.append(f'echo "{cmd} {n} {lim}" > {DBG}/cmd; cat {DBG}/result')
        if self.a2_fw:
            parts += [f"echo a2push > {DBG}/cmd; cat {DBG}/result", f"echo a2xverify > {DBG}/cmd; cat {DBG}/result",
                      f"cat {DBG}/a2"]
        parts += [f"cat {DBG}/stats", 'for f in /sys/class/hwmon/hwmon*/temp1_input; do echo "lm75 $(cat $f 2>/dev/null)"; done']
        return "; ".join(parts)

    def cycle(self) -> dict:
        """One pass of every test; returns parsed pieces."""
        t0 = time.monotonic()
        out = self.run(self.cycle_cmd())
        r = {"wall_s": time.monotonic() - t0, "tests": {}, "a2": None, "stats": None, "lm75": [], "raw": out}
        for line in out.splitlines():
            if line.startswith("test="):
                d = kv(line)
                r["tests"][d["test"]] = d
            elif line.startswith("a2="):
                r["a2"] = kv(line)
            elif line.startswith("t_ns="):
                r["stats"] = kv(line)
            elif line.startswith("lm75 "):
                r["lm75"].append(num(line.split()[1], -1))
        return r


def metrics_of(r: dict, c: dict) -> dict:
    t, m = r["tests"], {}
    if "lat" in t:
        m.update(lat_rd_ns=num(t["lat"].get("rd_ns")), lat_wr_ns=num(t["lat"].get("wr_ns")),
                 lat_rd_ns_max=num(t["lat"].get("rd_ns_max")))
    if "burst" in t:
        m.update(burst_rd_MBps=num(t["burst"].get("rd_MBps")), burst_wr_MBps=num(t["burst"].get("wr_MBps")))
    if "schan" in t:
        m.update(schan_ops_per_s=num(t["schan"].get("ops_per_s")))
    if r["a2"] and r["a2"].get("a2") == "running":
        m.update({k: v for k, v in a2_metrics(r["a2"], c).items() if v is not None})
    return m


def temps_of(r: dict) -> dict:
    s = r["stats"] or {}
    return {"u0": int(num(s.get("t0_mc"), -1)) / 1000.0 if num(s.get("t0_mc"), -1) >= 0 else None,
            "u1": int(num(s.get("t1_mc"), -1)) / 1000.0 if num(s.get("t1_mc"), -1) >= 0 else None,
            "lm75": [v / 1000.0 for v in r["lm75"] if v >= 0]}


def failures_of(r: dict, prev_stats: dict | None, prev_a2: dict | None, hb_still: int) -> tuple[list, list]:
    """(stop reasons, counted-only events) for one cycle."""
    stop, count = [], []
    for name, d in r["tests"].items():
        st = d.get("status")
        if st == "mismatch" or num(d.get("mismatches")) > 0:
            stop.append(f"data mismatch in {name}: {d}")
        elif st == "link":
            stop.append(f"link lost during {name}")
        elif st == "fail":
            stop.append(f"test {name} failed: {d}")
        elif st == "timeout":
            count.append(f"slow:{name}")
    s, p = r["stats"], prev_stats
    if s is None:
        stop.append("no stats line from the board")
    elif p is not None:
        def delta(k):
            return num(s.get(k)) - num(p.get(k))
        if delta("link_down") > 0 or delta("dl_inactive") > 0:
            stop.append(f"link loss: link_down +{delta('link_down'):.0f} dl_inactive +{delta('dl_inactive'):.0f}")
        if delta("aer_ep_unc") > 0 or delta("aer_rc_unc") > 0:
            stop.append("AER uncorrectable error")
        if delta("mismatches") > 0:
            stop.append("kernel mismatch log grew")
        for k in ("ep_link_chg", "rc_link_chg", "lt_seen", "aer_ep_cor", "aer_rc_cor", "pci_status_err"):
            if delta(k) > 0:
                count.append(f"{k}:+{delta(k):.0f}")
    if r["a2"] and r["a2"].get("a2") == "running":
        a = r["a2"]
        if num(a.get("mismatch")) > 0 or num(a.get("y_mismatch")) > 0:
            stop.append(f"a2 reports mismatch={a.get('mismatch')} y_mismatch={a.get('y_mismatch')} (miss0 addr {a.get('miss0_addr')} exp {a.get('miss0_exp')} got {a.get('miss0_got')})")
        if hb_still >= HEARTBEAT_STALL_CYCLES:
            stop.append(f"a2 heartbeat did not advance for {hb_still} cycles")
    elif r["a2"] is not None:
        stop.append("a2 struct absent (a2 stopped or magic lost)")
    t = temps_of(r)
    hot = [v for v in [t["u0"], t["u1"], *t["lm75"]] if v is not None]
    if hot and max(hot) >= DIE_STOP_C:
        stop.append(f"temperature {max(hot):.1f} C >= {DIE_STOP_C:.0f} C (u0 {t['u0']} u1 {t['u1']} lm75 {t['lm75']})")
    return stop, count


def smoke(b: Board, c: dict) -> int:
    log("smoke: setup")
    b.setup()
    walls, rows = [], []
    for i in range(2):	# run 0 is cold (first insmod, caches), run 1 is the measured one; a2 pushes in each
        r = b.cycle()
        walls.append(r["wall_s"])
    r1 = b.cycle()
    walls.append(r1["wall_s"])
    ok = True
    for name, cmd, n in TESTS:
        d = r1["tests"].get(name, {})
        good = d.get("status") == "ok"
        ok &= good
        key = {"lat": "rd_ns", "burst": "rd_MBps", "schan": "ops_per_s"}.get(name, "mismatches")
        rows.append((name, d.get("status", "missing"), f"{key}={d.get(key, '?')}", d.get("dur_ms", "?")))
    if b.a2_fw:
        a = r1["a2"] or {}
        run_ok = a.get("a2") == "running" and num(a.get("mismatch")) == 0 and num(a.get("y_mismatch")) == 0
        push = r1["tests"].get("a2push", {}).get("status") in ("ok", "busy")
        xv = r1["tests"].get("a2xverify", {}).get("status") == "ok"
        # heartbeat must advance between two reads separated by a full cycle
        hb0 = num((kv(b.run(f"cat {DBG}/a2")).get("hb")))
        r2 = b.cycle()
        hb1 = num((r2["a2"] or {}).get("hb"))
        hb_ok = hb1 > hb0
        ok &= run_ok and push and xv and hb_ok
        rows += [("a2_running", "ok" if run_ok else "FAIL", f"pass={a.get('pass')} mmu={a.get('mmu')} y_ack={a.get('y_ack')}", "-"),
                 ("a2push", "ok" if push else "FAIL", f"seq={a.get('y_seq')}", "-"),
                 ("a2xverify", "ok" if xv else "FAIL", "", "-"),
                 ("a2_heartbeat", "ok" if hb_ok else "FAIL", f"hb {hb0:.0f} -> {hb1:.0f}", "-")]
        r1 = r2
    s = r1["stats"] or {}
    link_ok = num(s.get("link_down")) == 0 and num(s.get("dl_inactive")) == 0 and num(s.get("ep_width")) >= 1
    ok &= link_ok
    rows.append(("link", "ok" if link_ok else "FAIL",
                 f"ep x{s.get('ep_width')} gen{s.get('ep_speed')} rc x{s.get('rc_width')} gen{s.get('rc_speed')} retrain={s.get('ep_link_chg')}/{s.get('rc_link_chg')}", "-"))
    t = temps_of(r1)
    rows.append(("temps", "ok" if t["u1"] is not None or t["lm75"] else "WARN", f"u0={t['u0']} u1={t['u1']} lm75={t['lm75']}", "-"))
    m = metrics_of(r1, c)
    log("smoke result (last cycle):")
    for row in rows:
        log("  %-14s %-9s %-62s dur_ms=%s" % row)
    log("  metrics: " + " ".join(f"{k}={v:.1f}" for k, v in sorted(m.items())))
    log(f"  cycle wall s: {', '.join(f'{w:.2f}' for w in walls)}")
    # soak bounds: kernel = 1.25 x the measured test duration, ssh = 3 x the longest cycle wall
    lim = {}
    for name, _cmd, _n in TESTS:
        d = r1["tests"].get(name, {})
        lim[name] = max(KERNEL_FLOOR_MS, int(num(d.get("dur_ms")) * KERNEL_X + 0.999))
    SMOKE_JSON.write_text(json.dumps({"limits_ms": lim, "ssh_bound_s": OVERALL_X * max(walls), "walls_s": walls,
                                      "metrics": m, "ok": ok, "t": now_iso()}, indent=1))
    log(f"smoke {'PASS' if ok else 'FAIL'}; bounds -> {SMOKE_JSON.relative_to(ROOT)}")
    return 0 if ok else 1


class Soak:
    def __init__(self, b: Board, c: dict, hours: float):
        self.b, self.c, self.hours = b, c, hours
        self.tot = Agg()	# whole run
        self.hour = Agg()
        self.minute = Agg()
        self.temps_tot = Agg()
        self.counts = {}	# counted-only events
        self.cycles = 0
        self.slow_run = 0
        self.hb_still = 0
        self.first_failure = None
        self.stop_flag = False
        self.t_start = time.monotonic()
        self.start_iso = now_iso()

    def bump(self, ev):
        for e in ev:
            key, amt = (e.split(":+")[0], int(float(e.split(":+")[1]))) if ":+" in e else (e, 1)
            self.counts[key] = self.counts.get(key, 0) + amt

    def line(self, tag, agg, extra=""):
        parts = [f"{k}={v[1]:.0f}" for k, v in agg.table().items()
                 if k in ("lat_rd_ns", "lat_wr_ns", "burst_rd_MBps", "burst_wr_MBps", "schan_ops_per_s", "a2_rd_c_MBps",
                          "a2_wr_c_MBps", "a2_rd_u_MBps", "a2_wr_u_MBps", "a2_lat_u_ns", "a2_lat_line_ns")]
        return f"{tag} {' '.join(parts)} {extra}"

    def snapshot(self, why: str) -> str:
        f = LOGDIR / f"pcie_freeze-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
        sh = (f"echo '== mismatch'; cat {DBG}/mismatch; echo '== a2'; cat {DBG}/a2; echo '== stats'; cat {DBG}/stats; "
              "echo '== trace0'; cat /sys/kernel/debug/remoteproc/remoteproc*/trace0; echo '== dmesg'; dmesg | tail -60")
        try:
            body = self.b.run(sh, 30.0)
        except (Stall, Unreachable) as e:
            body = f"board unreachable for the snapshot: {e}\n"
        cons = ""
        try:
            sys.path.insert(0, str(ROOT / "scripts"))
            import console
            with console.Session() as s:
                cons = s.read_idle(0.5, 3.0).decode(errors="replace")[-3000:]
        except Exception as e:  # console socket is best effort
            cons = f"(no console: {e})"
        f.write_text(f"reason: {why}\ntime: {now_iso()}\n{body}\n== console tail\n{cons}\n")
        return str(f.relative_to(ROOT))

    def report(self, outcome: str, snap: str | None) -> None:
        el = (time.monotonic() - self.t_start) / 3600
        L = [f"# PCIe soak report", "", f"- start {self.start_iso}, end {now_iso()}, elapsed {el:.2f} h of {self.hours} h, cycles {self.cycles}",
             f"- outcome: {outcome}", f"- a2 firmware: {self.b.a2_fw or 'none (a1 only)'}"]
        if self.first_failure:
            L.append(f"- first failure: {self.first_failure}")
        if snap:
            L.append(f"- frozen state: {snap}")
        L += ["", "## Counted events (run continued)", ""]
        L += [f"- {k}: {v}" for k, v in sorted(self.counts.items())] or ["- none"]
        L += ["", "## Metrics (min / mean / max / samples)", "", "| metric | min | mean | max | n |", "|---|---|---|---|---|"]
        L += [f"| {k} | {v[0]:.1f} | {v[1]:.1f} | {v[2]:.1f} | {v[3]} |" for k, v in self.tot.table().items()]
        L += ["", "## Temperatures C (min / mean / max)", "", "| sensor | min | mean | max | n |", "|---|---|---|---|---|"]
        L += [f"| {k} | {v[0]:.1f} | {v[1]:.1f} | {v[2]:.1f} | {v[3]} |" for k, v in self.temps_tot.table().items()]
        REPORT.write_text("\n".join(L) + "\n")
        log(f"report -> {REPORT.relative_to(ROOT)}")

    def run(self) -> int:
        b = self.b
        prev_stats = prev_a2 = None
        last_hb = None
        t_min = t_hour = time.monotonic()
        n_min = n_hour = 0
        end = self.t_start + self.hours * 3600
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "stop_flag", True))
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stop_flag", True))
        log(f"soak start: {self.hours} h, a2 fw {b.a2_fw}", SOAK_LOG)
        blind = 0
        while not self.stop_flag and time.monotonic() < end:
            try:
                try:
                    r = b.cycle()
                except Unreachable:
                    wait(3.0)	# one retry after a short pause: a reset of the ssh path is not yet a failure
                    r = b.cycle()
            except (Stall, Unreachable) as e:
                self.first_failure = self.first_failure or f"board unreachable: {e}"
                snap = self.snapshot(str(e))
                log(f"STOP: {e} ({snap})", SOAK_LOG)
                self.report("stopped: board unreachable / ssh stalled", snap)
                return 2
            self.cycles += 1
            n_min += 1
            n_hour += 1
            a = r["a2"]
            hb = num(a.get("hb")) if a and a.get("a2") == "running" else None
            self.hb_still = self.hb_still + 1 if (hb is not None and last_hb is not None and hb <= last_hb) else 0
            last_hb = hb if hb is not None else last_hb
            stop, count = failures_of(r, prev_stats, prev_a2, self.hb_still)
            slow = any(c.startswith("slow") for c in count)
            self.slow_run = self.slow_run + 1 if slow else 0
            if self.slow_run >= SLOW_STOP:
                stop.append(f"{SLOW_STOP} consecutive kernel-side test timeouts")
            t = temps_of(r)
            if t["u0"] is None and t["u1"] is None and not t["lm75"]:
                blind += 1
                if blind >= TEMP_BLIND_CYCLES:
                    stop.append("no temperature sensor readable")
            else:
                blind = 0
            self.bump(count)
            m = metrics_of(r, self.c)
            for k, v in m.items():
                for ag in (self.tot, self.hour, self.minute):
                    ag.add(k, v)
            for k, v in (("t_u0", t["u0"]), ("t_u1", t["u1"]), ("t_lm75_max", max(t["lm75"]) if t["lm75"] else None)):
                self.temps_tot.add(k, v)
                self.minute.add(k, v)
                self.hour.add(k, v)
            prev_stats = r["stats"] or prev_stats
            prev_a2 = a
            if stop:
                self.first_failure = stop[0]
                snap = self.snapshot("; ".join(stop))
                log("STOP: " + "; ".join(stop) + f" (state {snap})", SOAK_LOG)
                self.report("stopped on failure, state frozen (a2 left running, nothing reloaded)", snap)
                return 2
            if time.monotonic() - t_min >= 60:
                s = r["stats"] or {}
                ev = " ".join(f"{k}={v}" for k, v in sorted(self.counts.items())) or "events=0"
                tt = " ".join(f"{k}={self.minute.mean(k):.1f}" for k in ("t_u0", "t_u1", "t_lm75_max") if self.minute.mean(k) is not None)
                log(self.line(f"min cyc={n_min} wall={r['wall_s']:.1f}s", self.minute,
                              f"link=x{s.get('ep_width')}/g{s.get('ep_speed')} hb={a.get('hb') if a else '-'} {tt} {ev}"), SOAK_LOG)
                self.minute, t_min, n_min = Agg(), time.monotonic(), 0
            if time.monotonic() - t_hour >= 3600:
                log("HOURLY " + json.dumps({k: [round(x, 1) for x in v[:3]] for k, v in self.hour.table().items()}) +
                    f" cycles={n_hour} events={self.counts}", SOAK_LOG)
                self.hour, t_hour, n_hour = Agg(), time.monotonic(), 0
        outcome = "stopped by operator" if self.stop_flag else "completed"
        log(f"soak {outcome} after {self.cycles} cycles", SOAK_LOG)
        self.report(outcome, None)
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mode", choices=["smoke", "soak"])
    ap.add_argument("--hours", type=float, default=8.0)
    ap.add_argument("--a2-fw", default=DEFAULT_FW, help=f"firmware in /lib/firmware (default {DEFAULT_FW}; the -nommu variant is a manual fallback)")
    ap.add_argument("--no-a2", action="store_true", help="a1 side tests only")
    a = ap.parse_args()
    c = header_consts()
    b = Board(None if a.no_a2 else a.a2_fw, c)
    try:
        if a.mode == "smoke":
            return smoke(b, c)
        rc = smoke(b, c)
        if rc:
            log("soak not started: smoke failed", SOAK_LOG)
            return 1
        sm = json.loads(SMOKE_JSON.read_text())
        b.limits = sm["limits_ms"]
        b.ssh_bound = max(sm["ssh_bound_s"], 5.0)
        log(f"soak bounds: kernel {b.limits} ms, ssh {b.ssh_bound:.1f} s per cycle", SOAK_LOG)
        return Soak(b, c, a.hours).run()
    except (Stall, Unreachable) as e:
        log(f"FATAL: {e}")
        return 1
    except SystemExit as e:
        log(f"FATAL: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
