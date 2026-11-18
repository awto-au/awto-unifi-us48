#!/usr/bin/env python3
"""switchdev_test — board test of the us48_cmic switchdev L2 offload, stage 1 (docs/switchdev.md, #57). Unit 0 only.

Usage: switchdev_test.py <preflight|bpdu|fdb|stp|mgmt|all> [--apply]
- Dry-run by default (prints every board command). all = preflight, bpdu, fdb, stp. mgmt runs only when named.
- preflight (read only): module params, ports, ASIC VLANs 4000-4007 free, L2_USER_ENTRY, L2_AGE_TIMER, ING_CONFIG_64.SVL_ENABLE.
- bpdu: brA{ge8} + brB{ge17}, STP on (kernel-minimum timers): two ASIC VLANs, the cable joins them, no loop possible.
  Pass = both bridges agree on one root (BPDUs crossed the ASIC to the CPU in listening = HW blocking state), both ports
  forwarding, STG reads 3 for both. Without the BPDU trap each bridge stays its own root: safe fail.
- fdb: brsw{ge8, ge9}, STP off (ge9 has no cable). ge17 = host A in a netns (10.48.30.2/24), brsw 10.48.30.1/24.
  Ping brsw from A; ASIC VLAN/EGR_VLAN/STG/PORT_TAB read back; L2 lookups: A's MAC learned on ge8 (dynamic), brsw MAC
  -> CPU (static host entry); learning off -> CML 0 + flush, on again; flood off refused; ageing 10 s -> L2_AGE_TIMER.
- stp: runs bpdu first (refuses on its FAIL), then brstp{ge8, ge17} = both ends of the loop cable, STP on before the
  joins. Pass = one port blocking (STG 1), the other forwarding (STG 3), BPDUs still reaching the blocking port, no storm.
- mgmt: brmg + ge1 must be refused (EPERM). Board-side deadman deletes brmg after 5 s whatever happens.
- Teardown after every step: bridges deleted, netns holder killed, ge17 back in the root ns; ASIC VLAN invalid, PORT_TAB
  PORT_VID back to the private VLAN (108/109/117) with CML 0, test-learned L2 entries gone; port state = pre-test snapshot.
- Storm guards (as stp_loop_test.py): host sampler every 0.5 s, abort at > 2000 pps (rx+tx, root-ns test ports), CPU > 90 %
  (1 s windows) on 2 samples in a row, or (stp) both ports forwarding. Board side: busybox loop, 1 s steps, same pps limit,
  deadman 40 s -> bridges + holder removed, reason in /tmp/swd/guard.
- Timers: STP fd 2 s, hello 1 s, max_age 6 s (kernel minimum). Settle limit 1.25 x (2 fd + hello) = 6.25 s; expiry = FAIL.
  bpdu uses fd 6 s: roots must agree while both ports listen (HW SP_TREE 1); GPORT MIB + netdev counters of a 4 s window.
- Temps before/after each step (stop: die >= 108 C, LM75 >= 78 C). Exit 0 pass, 1 fail, 2 usage/precondition.
- Not testable with busybox (no iproute2 `bridge`): static FDB add/del, `bridge fdb show` self entries (ndo_fdb_dump).
- Log: tmp/logs/switchdev_test.log
"""
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402

help_guard(__doc__, strict=True, known=("--apply",))

import _board  # noqa: E402
import hwdb  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/switchdev_test.log"
IPIPE, EPIPE = 10, 11
# hwdb/board/ports.toml (unit 0): front 8 = lport 9, front 9 = lport 10, front 17 = lport 18; private VLAN 100 + front
LP = {"ge8": 9, "ge9": 10, "ge17": 18}
PVID = {"ge8": 108, "ge9": 109, "ge17": 117}
MIB = {"ge8": (2, 0), "ge17": (4, 0)}    # GPORT S-channel block, lane (asic_fwd_test.py PEER/TEST)
MAC_GE17 = "02:75:73:34:38:11"           # driver: 02:75:73:34:38:<front>
FD_CS, HELLO_CS, MAXAGE_CS = 200, 100, 600   # kernel minimum with STP on, centiseconds
SETTLE_S = 1.25 * (2 * FD_CS / 100 + HELLO_CS / 100)
BPDU_FD_CS = 600        # bpdu step: 6 s listening = a 4 s counter window (~4 hellos per port) inside HW SP_TREE 1
HOLD_S = 5.0
PPS_LIMIT = 2000
CPU_LIMIT = 90.0
DEADMAN_S = 40          # longest step ~20 s (6.25 settle + 5 hold + pings + reads); x2 for stalled ssh round trips
MGMT_DEADMAN_S = 5      # brmg exists for one brctl call (< 1 s); the deadman is the backstop if the refusal fails
CALL_S = 15.0           # one board command (busybox/brctl < 1 s)
SAMPLE_S = 0.5
SAMPLE_STALL_S = 2.5    # 5 sampler periods without a line = sampler/ssh dead
PING_S = 4.0            # 3 echoes at 1 s = 2 s + ARP + ssh start
LINK_S = 5.0            # 1000BASE-T AN ~3 s + 1 s driver link poll, 1.25x (copper_check.py LINK_LIMIT_S)
STATE = "/tmp/swd"
BRIDGES = ("brA", "brB", "brsw", "brstp", "brmg")
NET_A, NET_BR = "10.48.30.2", "10.48.30.1"
DB = hwdb.default()


class Fail(Exception):
    pass


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def field(w: list[int], lo: int, n: int) -> int:
    return sum(((w[(lo + i) // 32] >> ((lo + i) % 32)) & 1) << i for i in range(n))


def setf(w: list[int], lo: int, n: int, v: int) -> None:
    for i in range(n):
        b = lo + i
        w[b // 32] = (w[b // 32] & ~(1 << (b % 32))) | (((v >> i) & 1) << (b % 32))


def mem(name: str) -> tuple[int, int, dict]:
    """(base, words, {field: (lo, len)}) from the SDK regdb merged into hwdb."""
    m = DB.sdk_sbus_mems[name]
    return m.offset, (max(f.hi for f in m.fields) + 32) // 32, {f.name: (f.lo, f.hi - f.lo + 1) for f in m.fields}


def reg(name: str) -> tuple[int, dict]:
    r = DB.sdk_sbus_regs[name]
    return r.offset, {f.name: (f.lo, f.hi - f.lo + 1) for f in r.fields}


class Board:
    def __init__(self, apply: bool):
        self.apply = apply
        self.failed = 0
        self.pt0: dict = {}
        self.listen: tuple | None = None    # bpdu listening window (s, MIB deltas, netdev deltas)

    def sh(self, cmd: str, check: bool = True, quiet: bool = False, allow_ge1: bool = False) -> str:
        if re.search(r"\bge1\b", cmd) and not allow_ge1:
            raise Fail(f"refused: command names ge1 (management): {cmd}")
        if not self.apply:
            log("DRY " + cmd)
            return ""
        t0 = time.monotonic()
        rc, out = _board.ssh(cmd, CALL_S)
        out = out.strip()
        if not quiet:
            log(f"$ {cmd[:300]} rc={rc} {time.monotonic() - t0:.1f}s" + (f"\n{out}" if out else ""))
        if check and rc:
            raise Fail(f"rc={rc}: {cmd[:200]}: {out[:300]}")
        return out

    def rmems(self, name: str, idxs: range | list[int]) -> tuple[list[list[int]], dict]:
        """Batched S-channel reads through the driver's schan parameter (unit 0)."""
        base, words, f = mem(name)
        blk = EPIPE if name.startswith("EGR_") else IPIPE
        if not self.apply:
            log(f"DRY read {name}{list(idxs)}")
            return [[0] * words for _ in idxs], f
        res = _board.schan_batch([(0, "rmem", blk, base + i, words, None) for i in idxs])
        bad = [r["line"] for r in res if r["status"] != "ok"]
        if bad:
            raise Fail(f"read {name}: {bad[0]}")
        return [r["data"][:words] for r in res], f

    def rmem(self, name: str, idx: int) -> tuple[list[int], dict]:
        e, f = self.rmems(name, [idx])
        return e[0], f

    def rreg(self, name: str) -> tuple[int, dict]:
        a, f = reg(name)
        if not self.apply:
            log(f"DRY read {name}")
            return 0, f
        r = _board.schan_batch([(0, "rreg", IPIPE, a, 2, None)])[0]
        if r["status"] != "ok":
            raise Fail(f"read {name}: {r['line']}")
        return r["data"][0] | (r["data"][1] << 32 if len(r["data"]) > 1 else 0), f

    def l2_lookup(self, vid: int, mac: str) -> dict | None:
        """TABLE_LOOKUP on L2X by {KEY_TYPE 0, VLAN_ID, MAC_ADDR} (read only); None = not found."""
        base, words, f = mem("L2X")
        key = [0] * words
        setf(key, *f["VLAN_ID"], vid)
        setf(key, *f["MAC_ADDR"], int(mac.replace(":", ""), 16))
        if not self.apply:
            log(f"DRY lookup L2X vid {vid} {mac}")
            return None
        r = _board.schan_batch([(0, "lookup", IPIPE, base, words, key)])[0]
        if r["status"] == "notfound":
            return None
        if r["status"] != "ok":
            raise Fail(f"L2 lookup {vid} {mac}: {r['line']}")
        e = r["data"][1:1 + words]
        return {k: field(e, *f[k]) for k in ("VALID", "STATIC_BIT", "CPU", "MODULE_ID", "PORT_NUM", "T", "VLAN_ID")}

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        if self.apply:
            log(f"{'PASS' if ok else 'FAIL'} {name}" + (f": {detail}" if detail else ""))
            self.failed += not ok
        return ok


def thermal(b: Board, when: str) -> None:
    if not b.apply:
        return
    t = _board.temps()
    log(f"temps {when}: " + ", ".join(f"{k} {v:.1f}" for k, v in sorted(t.items())))
    hot = _board.too_hot(t)
    if hot:
        raise Fail(f"thermal stop {when}: {hot}")


def snapshot(b: Board) -> list[str]:
    out = b.sh("; ".join(f"echo \"link {p} $(ip -o link show {p} | grep -c '[<,]UP[,>]') master=$(readlink "
                         f"/sys/class/net/{p}/master)\"; ip -o addr show dev {p} | sed 's/^/addr /'" for p in LP), quiet=True)
    return sorted(re.sub(r"\s+valid_lft.*", "", ln).replace(" tentative", "").strip() for ln in out.splitlines() if ln.strip())


def offload_info(b: Board, br: str) -> tuple[int, int]:
    """ASIC VLAN + STG the driver logged for the last join of br (us48_cmic_sw.c 'offloaded to <br>')."""
    if not b.apply:
        return 4000, 2
    out = b.sh(f"dmesg | grep 'switchdev: offloaded to {br}:' | tail -n 1", quiet=True)
    m = re.search(r"ASIC VLAN (\d+) STG (\d+)", out)
    if not m:
        raise Fail(f"no 'offloaded to {br}' line in dmesg: {out!r}")
    return int(m.group(1)), int(m.group(2))


def mib(b: Board) -> dict:
    """GPORT GRPKT (0x1b00) / GTPKT (0x3800) | lane, as asic_fwd_test.py counter_ops."""
    if not b.apply:
        return {}
    ops = [(0, "rreg", blk, off | ln, 1, None) for blk, ln in MIB.values() for off in (0x1b00, 0x3800)]
    res = _board.schan_batch(ops)
    names = [f"{p} {c}" for p in MIB for c in ("GRPKT", "GTPKT")]
    return {n: r["data"][0] if r["status"] == "ok" else -1 for n, r in zip(names, res)}


def stg_state(b: Board, stg: int, port: str) -> int:
    e, f = b.rmem("STG_TAB", stg)
    return field(e, *f[f"SP_TREE_PORT{LP[port]}"])


class Sampler:
    """One long-lived ssh: a line every SAMPLE_S with uptime, cpu jiffies, rx/tx of the root-ns ports, brport states,
    ge1 enslaved, guard alive, guard message (as stp_loop_test.py)."""

    def __init__(self, ports: tuple):
        self.ports = ports
        st = " ".join(f"$(cat /sys/class/net/{p}/brport/state 2>/dev/null || echo -)" for p in ports)
        sel = " ".join(f"/^ *{p}:/{{v{i}=$3\" \"$11}}" for i, p in enumerate(ports))
        prn = "\" \"".join(f"(v{i}==\"\"?\"0 0\":v{i})" for i in range(len(ports)))
        awk = f"awk '{sel} END{{print {prn}}}' /proc/net/dev"
        n = int(DEADMAN_S / SAMPLE_S)
        loop = (f"i=0; while [ $i -lt {n} ]; do i=$((i+1)); echo \"S $(cut -d' ' -f1 /proc/uptime) "
                f"$(head -n 1 /proc/stat | cut -d' ' -f3-9) $({awk}) {st} "
                f"$(test -e /sys/class/net/ge1/master && echo 1 || echo 0) "
                f"$(kill -0 $(cat {STATE}/guard.pid) 2>/dev/null && echo 1 || echo 0) $(cat {STATE}/guard 2>/dev/null)\"; "
                f"read -t {SAMPLE_S} x <> {STATE}/f; done")
        argv = ["ssh", "-T", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={_board.CONNECT_S}", "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", _board.HOST, loop]
        self.p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, stdin=subprocess.DEVNULL)
        self.q: queue.Queue = queue.Queue()
        self.hist: list[dict] = []
        self.hot = 0
        self.grace = 0    # samples without the CPU guard (pps guard stays): S-channel MIB reads, bpdu only (no loop)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for ln in self.p.stdout:
            self.q.put(ln)
        self.q.put(None)

    def next(self, both_fwd_fails: bool = False) -> dict:
        """Next sample with every guard applied; raises Fail on a guard or a silent sampler."""
        n = len(self.ports)
        while True:
            try:
                ln = self.q.get(timeout=SAMPLE_STALL_S)
            except queue.Empty:
                raise Fail(f"sampler silent for {SAMPLE_STALL_S} s (period {SAMPLE_S} s)")
            if ln is None:
                raise Fail("sampler ended")
            f = ln.split()
            if f and f[0] == "S" and len(f) >= 9 + 3 * n + 2:
                break
            log(f"  sampler: {ln.strip()}")
        k = 9 + 2 * n
        s = {"t": float(f[1]), "cpu": [int(x) for x in f[2:9]],
             "pk": {p: (int(f[9 + 2 * i]), int(f[10 + 2 * i])) for i, p in enumerate(self.ports)},
             "state": {p: f[k + i] for i, p in enumerate(self.ports)}, "ge1_master": int(f[k + n]),
             "guard_alive": int(f[k + n + 1]), "guard": " ".join(f[k + n + 2:])}
        self.hist.append(s)
        if len(self.hist) > 1:
            pps, _ = rates(self.hist[-2], s)
            _, cpu = rates(self.hist[-3] if len(self.hist) > 2 else self.hist[-2], s)
            self.hot = self.hot + 1 if cpu > CPU_LIMIT else 0
            if self.grace > 0:
                self.grace -= 1
                self.hot = 0
            if pps > PPS_LIMIT or self.hot >= 2:
                raise Fail(f"storm guard: {pps:.0f} pps, CPU {cpu:.0f} % x{self.hot}")
        if s["guard"] or not s["guard_alive"]:
            raise Fail(f"board guard fired or not running: alive {s['guard_alive']} {s['guard']!r}")
        if s["ge1_master"]:
            raise Fail("ge1 has a master")
        if both_fwd_fails and all(v == "3" for v in s["state"].values()):
            raise Fail(f"storm guard: both loop ports forwarding {s['state']}")
        return s

    def wait(self, secs: float, **kw) -> dict:
        t0 = self.next(**kw)["t"]
        while True:
            s = self.next(**kw)
            if s["t"] - t0 >= secs:
                return s

    def stop(self) -> None:
        if self.p.poll() is None:
            self.p.kill()
            self.p.wait()


def rates(a: dict, z: dict) -> tuple[float, float]:
    dt = z["t"] - a["t"]
    tot = sum(z["cpu"]) - sum(a["cpu"])
    idle = (z["cpu"][3] + z["cpu"][4]) - (a["cpu"][3] + a["cpu"][4])
    n = sum(sum(z["pk"][p]) - sum(a["pk"][p]) for p in z["pk"])
    return (n / dt if dt > 0 else 0.0), (100.0 * (tot - idle) / tot if tot > 0 else 0.0)


def cleanup_cmd() -> str:
    """Board-side undo, shared by the guard and teardown: bridges down + deleted, netns holder killed (ge17 returns to the
    root ns when its namespace dies; it is also moved back first)."""
    brs = "; ".join(f"if [ -d /sys/class/net/{x} ]; then ip link set {x} down; brctl delbr {x}; fi" for x in BRIDGES)
    return (f"{brs}; if [ -e {STATE}/A.pid ]; then nsenter -t $(cat {STATE}/A.pid) -n sh -c 'ip addr flush dev ge17; "
            f"ip link set ge17 netns 1' 2>/dev/null; kill $(cat {STATE}/A.pid) 2>/dev/null; rm -f {STATE}/A.pid; "
            f"ip link set ge17 up; fi")    # fdb precondition: ge17 was up (carrier) before the move


def guard_start(b: Board, ports: tuple, deadman_s: int = DEADMAN_S) -> None:
    pat = "|".join(ports)
    loop = (f"i=0; p0=; while [ $i -lt {deadman_s} ]; do read -t 1 x <> {STATE}/f; i=$((i+1)); "
            f"n=$(awk '/^ *({pat}):/{{s+=$3+$11}} END{{print s+0}}' /proc/net/dev); "
            f"if [ -n \"$p0\" ] && [ $((n-p0)) -gt {PPS_LIMIT} ]; then echo \"storm $((n-p0)) pkt/s at ${{i}} s\" > {STATE}/guard; break; fi; "
            f"p0=$n; done; [ -f {STATE}/guard ] || echo \"deadman ${{i}} s\" > {STATE}/guard; {cleanup_cmd()}")
    b.sh(f"mkdir -p {STATE}; rm -f {STATE}/guard {STATE}/f; mkfifo {STATE}/f; "
         f"sh -c '{loop.replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}' </dev/null >/dev/null 2>&1 & "
         f"echo $! > {STATE}/guard.pid", allow_ge1=True)


def teardown(b: Board) -> None:
    b.sh(f"kill $(cat {STATE}/guard.pid {STATE}/ping.pid 2>/dev/null) 2>/dev/null; {cleanup_cmd()}; rm -rf {STATE}; true",
         check=False, allow_ge1=True)


def verify_restored(b: Board, name: str, vids: list[int], before: list[str], macs: list[tuple[int, str]]) -> None:
    if not b.apply:
        return
    bad = []
    for v in vids:
        e, f = b.rmem("VLAN_TAB", v)
        if field(e, *f["VALID"]):
            bad.append(f"VLAN_TAB[{v}] still valid")
    for p, (vid, cml) in pt_read(b).items():
        if (vid, cml) not in ((PVID[p], 0), b.pt0.get(p)):
            bad.append(f"PORT_TAB[{LP[p]}] {p} PORT_VID {vid} CML {cml} (before {b.pt0.get(p)})")
    for v, mac in macs:
        if b.l2_lookup(v, mac):
            bad.append(f"L2 {v} {mac} still present")
    after = snapshot(b)
    if after != before:
        bad.append(f"ports differ: {sorted(set(before) ^ set(after))}")
    b.check(f"{name} teardown", not bad, "; ".join(bad) or "ASIC VLANs invalid, PORT_TAB private + CML 0, L2 clean, ports = pre-test")


def pt_read(b: Board) -> dict:
    """{port: (PORT_VID, CML_FLAGS_NEW)}"""
    out = {}
    for p, lp in LP.items():
        e, f = b.rmem("PORT_TAB", lp)
        out[p] = (field(e, *f["PORT_VID"]), field(e, *f["CML_FLAGS_NEW"]))
    return out


def precondition(b: Board, ports: tuple) -> None:
    if not b.apply:
        return
    b.pt0 = pt_read(b)    # a never-opened copper port keeps the init PORT_TAB (VID 1) until copper_enable
    out = b.sh("; ".join([f"for x in {' '.join(BRIDGES)}; do test -d /sys/class/net/$x && echo exists $x; done"] +
                         [f"echo {p} $(cat /sys/class/net/{p}/carrier 2>/dev/null) $(test -e /sys/class/net/{p}/master && "
                          f"echo enslaved)" for p in ports] + [f"test -e {STATE}/A.pid && echo holder", "true"]), quiet=True)
    if "exists" in out or "enslaved" in out or "holder" in out:
        raise Fail(f"precondition: no test bridge, no enslaved port, no holder: {out!r}")
    for p in ports:
        if p != "ge9" and f"{p} 1" not in out:
            raise Fail(f"precondition: {p} carrier 1: {out!r}")


def br_make(b: Board, br: str, stp: bool, fd_cs: int = FD_CS) -> None:
    cmd = f"brctl addbr {br}"
    if stp:
        cmd += (f"; echo 1 > /sys/class/net/{br}/bridge/stp_state; echo {fd_cs} > /sys/class/net/{br}/bridge/forward_delay; "
                f"echo {HELLO_CS} > /sys/class/net/{br}/bridge/hello_time; echo {MAXAGE_CS} > /sys/class/net/{br}/bridge/max_age")
    b.sh(cmd + f"; ip link set {br} up")


def preflight(b: Board) -> None:
    log("=== preflight (read only)")
    out = b.sh("cat /sys/module/us48_cmic/parameters/switchdev /sys/module/us48_cmic/parameters/mgmt_bridge", quiet=True)
    b.check("module params", out.split() == ["Y", "N"], f"switchdev mgmt_bridge = {out.split()}")
    precondition(b, tuple(LP))
    es, f = b.rmems("VLAN_TAB", range(4000, 4008))
    b.check("VLAN_TAB[4000-4007] free", not any(field(e, *f["VALID"]) for e in es))
    trap = []
    es, f = b.rmems("L2_USER_ENTRY", range(128))
    for i, e in enumerate(es):
        if field(e, *f["VALID"]):
            mac = field(e, *f["MAC_ADDR"])
            log(f"  L2_USER_ENTRY[{i}] MAC {mac:012x} BPDU {field(e, *f['BPDU'])} CPU {field(e, *f['CPU'])} "
                f"DST_DISCARD {field(e, *f['DST_DISCARD'])} words {' '.join('%08x' % x for x in e)}")
            if mac == 0x0180c2000000 and field(e, *f["BPDU"]):
                trap.append(i)
    log(f"  BPDU trap before any bridge: {trap or 'none (the driver adds one with the first bridge)'}")
    v, f = b.rreg("L2_AGE_TIMER")
    log(f"  L2_AGE_TIMER {v:#x}: AGE_ENA {field([v & 0xffffffff], *f['AGE_ENA'])} AGE_VAL {field([v & 0xffffffff], *f['AGE_VAL'])} s")
    v, f = b.rreg("ING_CONFIG_64")
    log(f"  ING_CONFIG_64 {v:#x}: SVL_ENABLE {(v >> f['SVL_ENABLE'][0]) & 1}")
    up = b.sh("; ".join(f"echo {p} $(ip -o link show {p} | grep -c '[<,]UP[,>]')" for p in LP), quiet=True)
    for p, lp in LP.items():
        e, f = b.rmem("PORT_TAB", lp)
        vid, cml = field(e, *f["PORT_VID"]), field(e, *f["CML_FLAGS_NEW"])
        unopened = f"{p} 0" in up.splitlines() and vid == 1    # copper_enable sets the private VLAN at the first open
        b.check(f"PORT_TAB[{lp}] {p} private", (vid == PVID[p] and not cml) or unopened,
                f"PORT_VID {vid} CML {cml}/{field(e, *f['CML_FLAGS_MOVE'])}" + (" (never opened: init PORT_TAB)" if unopened else ""))


def step_bpdu(b: Board) -> bool:
    log("=== bpdu: brA{ge8} + brB{ge17}, STP on: root agreement needs BPDUs through the HW blocking state")
    ports = ("ge8", "ge17")
    precondition(b, ports)
    before = snapshot(b) if b.apply else []
    thermal(b, "before bpdu")
    vids, sm, ok = [], None, False
    try:
        guard_start(b, ports)
        br_make(b, "brA", True, BPDU_FD_CS)
        br_make(b, "brB", True, BPDU_FD_CS)
        if b.apply:
            sm = Sampler(ports)
            sm.next()
        b.sh("brctl addif brA ge8; brctl addif brB ge17")
        (va, sa), (vb, sb) = offload_info(b, "brA"), offload_info(b, "brB")
        vids = [va, vb]
        if not b.apply:
            return True
        sm.grace = 4
        es, f = b.rmems("L2_USER_ENTRY", range(2))
        for i, e in enumerate(es):
            log(f"  L2_USER_ENTRY[{i}] VALID {field(e, *f['VALID'])} MAC {field(e, *f['MAC_ADDR']):012x} MASK "
                f"{field(e, *f['MASK']):018x} CPU {field(e, *f['CPU'])} BPDU {field(e, *f['BPDU'])} words "
                + " ".join("%08x" % x for x in e))
        t0, agree, win = sm.hist[-1]["t"], None, []
        settle = 1.25 * (2 * BPDU_FD_CS / 100 + HELLO_CS / 100)
        while True:
            s = sm.next()
            # listening window: counters at ~1 s and ~5 s (fd 6 s), both ports listening (HW SP_TREE 1)
            if len(win) < 2 and s["t"] - t0 >= (1.0, 5.0)[len(win)] and all(v == "1" for v in s["state"].values()):
                sm.grace = 4    # one ssh with S-channel reads: dropbear + schan load, not a storm
                win.append((s, mib(b)))
                if len(win) == 2:
                    (sa_, ma), (sb_, mb) = win
                    dm = {k: mb[k] - ma[k] for k in ma}
                    dn = {p: (sb_["pk"][p][0] - sa_["pk"][p][0], sb_["pk"][p][1] - sa_["pk"][p][1]) for p in ports}
                    b.listen = (sb_["t"] - sa_["t"], dm, dn)
                    log(f"  listening window {sb_['t'] - sa_['t']:.1f} s: MIB {dm}, netdev (rx, tx) {dn}")
            roots = b.sh("cat /sys/class/net/brA/bridge/root_id /sys/class/net/brB/bridge/root_id", quiet=True).split()
            if agree is None and len(set(roots)) == 1:
                agree = (s["t"] - t0, dict(s["state"]))
                log(f"  roots agree {agree[0]:.1f} s after the joins, port states {agree[1]} (1 = listening = SP_TREE 1)")
            if agree and all(v == "3" for v in s["state"].values()):
                log(f"  root agreed {roots[0]}, both forwarding, {s['t'] - t0:.1f} s after the joins")
                break
            if s["t"] - t0 > settle:
                b.check("bpdu root agreement", False, f"roots {roots} states {s['state']} after {s['t'] - t0:.1f} s "
                        f"(limit {settle:.2f} s): BPDUs not reaching the CPU through the ASIC")
                return False
        st = (stg_state(b, sa, "ge8"), stg_state(b, sb, "ge17"))
        e, f = b.rmem("VLAN_TAB", va)
        bm_a = field(e, *f["PORT_BITMAP"])
        e, f = b.rmem("VLAN_TAB", vb)
        bm_b = field(e, *f["PORT_BITMAP"])
        ok = b.check("bpdu root agreement", True, f"brA VLAN {va} STG {sa}, brB VLAN {vb} STG {sb}")
        ok &= b.check("bpdu agreement in HW blocking (listening)", all(v == "1" for v in agree[1].values()),
                      f"states at agreement {agree[1]}, {agree[0]:.1f} s (forward_delay {BPDU_FD_CS / 100:.0f} s)")
        ok &= b.check("bpdu STG forwarding", st == (3, 3), f"SP_TREE ge8 {st[0]} ge17 {st[1]}")
        ok &= b.check("bpdu VLAN bitmaps", bm_a == 1 | 1 << LP["ge8"] and bm_b == 1 | 1 << LP["ge17"], f"{bm_a:#x} {bm_b:#x}")
        return ok
    finally:
        if sm:
            sm.stop()
        teardown(b)
        verify_restored(b, "bpdu", vids, before, [])
        thermal(b, "after bpdu")


def step_fdb(b: Board) -> None:
    log("=== fdb: brsw{ge8, ge9}, STP off; host A = ge17 in a netns")
    ports = ("ge8", "ge9")
    precondition(b, ("ge8", "ge9", "ge17"))
    before = snapshot(b) if b.apply else []
    thermal(b, "before fdb")
    vid, sm, ns = 4000, None, f"nsenter -t $(cat {STATE}/A.pid) -n"
    try:
        guard_start(b, ports)
        b.sh(f"mkfifo {STATE}/holdA; setsid unshare -n sh -c 'read x <> {STATE}/holdA' </dev/null >/dev/null 2>&1 & "
             f"echo $! > {STATE}/A.pid")
        b.sh(f"ip link set ge17 down; ip link set ge17 netns $(cat {STATE}/A.pid); "
             f"{ns} sh -c 'ip link set lo up; ip link set ge17 up; ip addr add {NET_A}/24 dev ge17'")
        br_make(b, "brsw", False)
        b.sh(f"ip addr add {NET_BR}/24 dev brsw; brctl addif brsw ge8; brctl addif brsw ge9")
        vid, stg = offload_info(b, "brsw")
        if b.apply:
            sm = Sampler(ports)
            sm.next()
        # ge17 went down/up for the netns move: wait for carrier (both cable ends), samples keep the guards running
        while sm:
            # /sys shows the root netns only: carrier inside the netns from ip LOWER_UP
            c = b.sh(f"{ns} ip -o link show ge17 | grep -c LOWER_UP; cat /sys/class/net/ge8/carrier", check=False, quiet=True).split()
            el = sm.next()["t"] - sm.hist[0]["t"]
            if c == ["1", "1"]:
                log(f"  carrier ge17 (netns) + ge8 after {el:.1f} s")
                break
            if el > LINK_S:
                raise Fail(f"no carrier ge17/ge8 {el:.1f} s after the move (limit {LINK_S} s): {c}")
        b.sh(f"{ns} ping -c 3 {NET_BR} > {STATE}/ping 2>&1 & echo $! > {STATE}/ping.pid")
        if sm:
            sm.wait(PING_S)
        out = b.sh(f"kill $(cat {STATE}/ping.pid) 2>/dev/null; cat {STATE}/ping", check=False, quiet=True)
        if not b.apply:
            return
        got = re.search(r"(\d+) packets received", out)
        b.check("fdb ping brsw from host A", bool(got) and int(got.group(1)) >= 1, " | ".join(out.splitlines()[-2:]))
        e, f = b.rmem("VLAN_TAB", vid)
        g, gf = b.rmem("EGR_VLAN", vid)
        want = 1 | 1 << LP["ge8"] | 1 << LP["ge9"]
        b.check("fdb VLAN_TAB", field(e, *f["PORT_BITMAP"]) == want and field(e, *f["STG"]) == stg and field(e, *f["FID_ID"]) == vid,
                f"VLAN {vid} bitmap {field(e, *f['PORT_BITMAP']):#x} STG {field(e, *f['STG'])} FID {field(e, *f['FID_ID'])}")
        b.check("fdb EGR_VLAN", field(g, *gf["PORT_BITMAP"]) == want and field(g, *gf["UT_BITMAP"]) == want and field(g, *gf["STG"]) == stg,
                f"PORT {field(g, *gf['PORT_BITMAP']):#x} UT {field(g, *gf['UT_BITMAP']):#x} STG {field(g, *gf['STG'])}")
        st = (stg_state(b, stg, "ge8"), stg_state(b, stg, "ge9"))
        b.check("fdb STG states", st == (3, 0), f"ge8 {st[0]} (want 3, forwarding), ge9 {st[1]} (want 0, no carrier)")
        e, f = b.rmem("PORT_TAB", LP["ge8"])
        b.check("fdb PORT_TAB ge8", field(e, *f["PORT_VID"]) == vid and field(e, *f["CML_FLAGS_NEW"]) == 8,
                f"PORT_VID {field(e, *f['PORT_VID'])} CML {field(e, *f['CML_FLAGS_NEW'])}")
        a = b.l2_lookup(vid, MAC_GE17)
        b.check("fdb HW learned host A on ge8", bool(a) and a["PORT_NUM"] == LP["ge8"] and not a["STATIC_BIT"] and a["MODULE_ID"] == 0,
                str(a))
        brmac = b.sh("cat /sys/class/net/brsw/address", quiet=True)
        h = b.l2_lookup(vid, brmac)
        b.check("fdb host entry brsw -> CPU", bool(h) and h["CPU"] == 1 and h["STATIC_BIT"] == 1 and h["PORT_NUM"] == 0, f"{brmac} {h}")
        # learning off: CML 0 + dynamic flush; on again
        b.sh("echo 0 > /sys/class/net/ge8/brport/learning")
        e, f = b.rmem("PORT_TAB", LP["ge8"])
        a = b.l2_lookup(vid, MAC_GE17)
        b.check("fdb learning off", field(e, *f["CML_FLAGS_NEW"]) == 0 and a is None, f"CML {field(e, *f['CML_FLAGS_NEW'])}, A {a}")
        b.sh("echo 1 > /sys/class/net/ge8/brport/learning")
        e, f = b.rmem("PORT_TAB", LP["ge8"])
        b.check("fdb learning on again", field(e, *f["CML_FLAGS_NEW"]) == 8)
        rc_out = b.sh("echo 0 > /sys/class/net/ge8/brport/unicast_flood 2>&1; echo rc=$?; cat /sys/class/net/ge8/brport/unicast_flood",
                      check=False, quiet=True)
        b.check("fdb flood off refused", "rc=0" not in rc_out and rc_out.splitlines()[-1:] == ["1"], rc_out.replace("\n", " | "))
        b.sh("echo 1000 > /sys/class/net/brsw/bridge/ageing_time")
        v, f = b.rreg("L2_AGE_TIMER")
        b.check("fdb ageing 10 s", field([v & 0xffffffff], *f["AGE_VAL"]) == 10, f"L2_AGE_TIMER {v:#x}")
        b.sh("echo 30000 > /sys/class/net/brsw/bridge/ageing_time")
        v, f = b.rreg("L2_AGE_TIMER")
        b.check("fdb ageing 300 s restored", (v & 0xffffffff) == 0x0010012c, f"L2_AGE_TIMER {v:#x}")
        if sm:
            sm.next()
    finally:
        if sm:
            sm.stop()
        teardown(b)
        verify_restored(b, "fdb", [vid], before, [(vid, MAC_GE17)])
        thermal(b, "after fdb")


def step_stp(b: Board) -> None:
    log("=== stp: brstp{ge8, ge17} = both ends of the loop cable, STP on before the joins (HW STG blocking)")
    ports = ("ge8", "ge17")
    precondition(b, ports)
    before = snapshot(b) if b.apply else []
    thermal(b, "before stp")
    vid, sm = 4000, None
    try:
        guard_start(b, ports)
        br_make(b, "brstp", True)
        if b.apply:
            sm = Sampler(ports)
            sm.next()
        b.sh("brctl addif brstp ge8; brctl addif brstp ge17")
        vid, stg = offload_info(b, "brstp")
        if not b.apply:
            return
        t0 = sm.hist[-1]["t"]
        while True:
            s = sm.next(both_fwd_fails=True)
            states = tuple(s["state"][p] for p in ports)
            if sorted(states) == ["3", "4"]:
                break
            if s["t"] - t0 > SETTLE_S:
                raise Fail(f"not settled {s['t'] - t0:.1f} s after the joins (limit {SETTLE_S:.2f} s): {states}")
        fwd, blk = ports[states.index("3")], ports[states.index("4")]
        log(f"  settled {s['t'] - t0:.1f} s: {fwd} forwarding, {blk} blocking")
        i0 = len(sm.hist) - 1
        z = sm.wait(HOLD_S, both_fwd_fails=True)
        a = sm.hist[i0]
        pps, _ = rates(a, z)
        rx_blk = z["pk"][blk][0] - a["pk"][blk][0]
        st = {p: stg_state(b, stg, p) for p in ports}
        b.check("stp HW states", st[fwd] == 3 and st[blk] == 1, f"STG {stg}: {fwd} {st[fwd]}, {blk} {st[blk]}")
        b.check("stp BPDUs reach the blocking port", rx_blk >= HOLD_S / (HELLO_CS / 100) - 1,
                f"{blk} rx {rx_blk} in {z['t'] - a['t']:.1f} s (hello {HELLO_CS / 100:.0f} s)")
        b.check("stp no storm while held", pps < PPS_LIMIT, f"{pps:.1f} pps")
        b.check("stp states stable", tuple(z["state"][p] for p in ports) == states, str(z["state"]))
    finally:
        if sm:
            sm.stop()
        teardown(b)
        verify_restored(b, "stp", [vid], before, [])
        thermal(b, "after stp")


def step_mgmt(b: Board) -> None:
    log("=== mgmt: ge1 into an offloaded bridge must be refused (deadman deletes brmg after 5 s)")
    out = b.sh("cat /sys/module/us48_cmic/parameters/mgmt_bridge", quiet=True)
    if b.apply and out != "N":
        raise Fail(f"mgmt_bridge = {out!r}: the refusal is off, not testing")
    try:
        guard_start(b, (), MGMT_DEADMAN_S)
        b.sh("brctl addbr brmg")
        out = b.sh("brctl addif brmg ge1 2>&1; echo rc=$?; test -e /sys/class/net/ge1/master && echo ENSLAVED; "
                   "dmesg | grep 'refused join of brmg' | tail -n 1", check=False, allow_ge1=True)
        b.check("mgmt ge1 refused", "rc=0" not in out and "ENSLAVED" not in out and "management port" in out,
                out.replace("\n", " | "))
    finally:
        teardown(b)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    steps = ("preflight", "bpdu", "fdb", "stp", "mgmt", "all")
    if len(args) != 1 or args[0] not in steps:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    b = Board("--apply" in sys.argv[1:])
    todo = ("preflight", "bpdu", "fdb", "stp") if args[0] == "all" else (args[0],)
    rc = 0
    for name in todo:
        try:
            if name == "preflight":
                preflight(b)
            elif name == "bpdu":
                step_bpdu(b)
            elif name == "fdb":
                step_fdb(b)
            elif name == "stp":
                if name in todo and "bpdu" not in todo and not step_bpdu(b):
                    raise Fail("bpdu step failed: no loop test without a working BPDU trap")
                if b.failed:
                    raise Fail(f"{b.failed} failed checks before stp: no loop test")
                step_stp(b)
            elif name == "mgmt":
                step_mgmt(b)
        except Fail as e:
            log(f"FAIL {name}: {e}")
            b.failed += 1
        except (subprocess.SubprocessError, OSError, RuntimeError) as e:
            log(f"ERROR {name}: {e}")
            rc = 2
            break
    log(f"result: {'PASS' if not b.failed and not rc else 'FAIL'} ({b.failed} failed checks)")
    return rc or (1 if b.failed else 0)


if __name__ == "__main__":
    sys.exit(main())
