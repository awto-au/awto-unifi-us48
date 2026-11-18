#!/usr/bin/env python3
"""switchdev_test — board test of the us48_cmic switchdev L2 offload, stage 1 (docs/switchdev.md, #57, #134). Unit 0;
burst also unit 1.

Usage: switchdev_test.py <preflight|bpdu|fdb|linkdown|burst|rate|stp|mgmt|all> [--apply]
- Dry-run by default (prints every board command). all = preflight, bpdu, fdb, linkdown, burst, rate, stp. mgmt runs only when named.
- preflight (read only): module params, ports, ASIC VLANs 4000-4007 free, L2_USER_ENTRY, L2_AGE_TIMER (both units = stock
  0x0010012c, #131), ING_CONFIG_64.SVL_ENABLE.
- bpdu: brA{ge8} + brB{ge17}, STP on (kernel-minimum timers): two ASIC VLANs, the cable joins them, no loop possible.
  Pass = both bridges agree on one root (BPDUs crossed the ASIC to the CPU in listening = HW blocking state), both ports
  forwarding, STG reads 3 for both. Without the BPDU trap each bridge stays its own root: safe fail.
- fdb: brsw{ge8, ge9}, STP off (ge9 has no cable). ge17 = host A in a netns (10.48.30.2/24), brsw 10.48.30.1/24.
  Ping brsw from A; ASIC VLAN/EGR_VLAN/STG/PORT_TAB read back; L2 lookups: A's MAC learned on ge8 (dynamic), brsw MAC
  -> CPU (static host entry); learning off -> CML 0 + flush, on again; flood off refused; ageing 10 s -> L2_AGE_TIMER.
- burst (unit 0: brbu{ge8, ge9}, host ge17; unit 1: brbu{ge32, ge33}, host ge41; ge9/ge33 no cable): static sink MAC on
  the cable-less port; one probe frame (learn latency), idle CPU with vs without the bridge, then 512 new sources back to
  back (tools/l2_burst, DA = the sink: no frame reaches the CPU or a wire, so the pps guards see none; one burst per unit).
  Pass = 'L2 resync (FIFO full)' logged, bridge extern_learn set = the driver's L2X dump (`self`) after it, statics kept.
- linkdown (unit 0, #135): brld{ge8, ge9}, host A on ge17 (netns) learned on ge8; ge8 loses carrier (far end ge17 admin
  down, then ge8 admin down). Pass = A gone from L2X + bridge, static sink kept; latency from dmesg time stamps.
- rate (both units, same ports as burst, #135): one bridge per run, RATE_N sources paced at each RATE_PPS. Logged: learned
  (L2X, bridge, monitor) vs sent by time mark, resync walks, the run's FIFO counters, drain CPU. Pass = bridge = L2X.
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
  fdb also (iproute2 `bridge` in the rootfs): A shows in `bridge fdb show` as extern_learn + offload (driver L2_MOD_FIFO
  drain); static `bridge fdb add` on ge9 lands in L2X; learn latency = probe frame TX stamp -> `bridge -timestamp monitor`
  extern_learn line (limit 1.25 x (10 ms drain + 2 ms)); with ageing 10 s and A silent, HW ageing removes A and the probe
  MAC from L2X and the bridge (limit 1.25 x (2 x 10 s + 2 sampler periods)); statics survive; `bridge fdb del`. Deadman 130 s.
- Log: tmp/logs/switchdev_test.log
"""
import datetime
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
_DB = hwdb.lite()
_LOOP = _DB.loop_pairs()    # [(unit, GPHY end, octal end)]; the bridge's second port is GPHY end + 1 (no cable)


def _names(*fronts):
    return [_DB.netdev(f) for f in fronts]


# lport / private VLAN / GPORT block+lane: hwdb/board/ports.toml (unit 0 shown by LP, PVID, MIB)
_U0 = _LOOP[0]
LP = {_DB.netdev(f): _DB.port(f)["logical"] for f in (_U0[1], _U0[1] + 1, _U0[2])}
PVID = {_DB.netdev(f): _DB.pvid(f) for f in (_U0[1], _U0[1] + 1, _U0[2])}
MIB = {_DB.netdev(f): _DB.gport(f) for f in (_U0[1], _U0[2])}
MAC_GE17 = hwdb.mac(_U0[2])
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
MAC_STATIC = "02:75:73:34:38:aa"          # test-only static FDB entry on ge9/ge33 (also the DA sink of the probe frames)
MAC_LAT = "02:75:73:34:38:b0"             # one probe frame: learn latency + ageing
BURST_BASE = "02:75:73:35:00:00"          # burst sources BURST_BASE + i
BURST_N = 512           # > 4 x the 128-entry L2_MOD_FIFO; DA = MAC_STATIC (no carrier): no frame reaches the CPU or a wire
FIFO_POLL_S = 0.012     # driver idle drain: L2MOD_POLL_MS 10 = 3 jiffies at HZ 250
LAT_LIMIT_S = 1.25 * (FIFO_POLL_S + 0.002)     # one drain period + ~2 ms work/notify scheduling
FIFO_WAIT_S = 1.25 * (2 * SAMPLE_S + FIFO_POLL_S)  # `bridge fdb` checks run once per sampler line
AGE_S = 10                               # ageing_time 1000 cs in the fdb step
AGE_WAIT_S = 1.25 * (2 * AGE_S + FIFO_WAIT_S)  # hit-bit ageing: gone 1-2 periods after the last frame
RESYNC_WAIT_S = 1.25 * (2 * SAMPLE_S + 0.5)    # overflow walk starts at once; unit 1 walk < 0.5 s; checked per sample
RATE_N = 4096           # rate step: sources per run (L2X 16384 x 4 w; 1/4 of it, few hash-bucket-full drops)
RATE_PPS = (0, 20000, 50000, 100000)     # 0 = back to back; the old 10 ms drain capped at 128 / 12 ms = 10.7k/s
RATE_TAIL_S = 1         # CPU window after the burst: drains + resync walk (< 0.5 s) inside it
RATE_SETTLE_S = 1.0     # then 2 sampler lines before the fdb dump (a second walk would have started by then)
RATE_MARKS_MS = (5, 10, 20, 50, 100, 200, 500, 1000)
LD_WAIT_S = 1.25 * (1.0 + 0.4 + 2 * SAMPLE_S)  # link poll LINK_POLL_MS 1 s + flush walk < 0.4 s, fdb checked per sample
LD_ISSUE = "#137"
LD_DEADMAN_S = 60      # linkdown ~25 s (setup 8 + 2 x (carrier 5 + learn 1 + down 3)) x2
RATE_DEADMAN_S = 120   # rate step ~55 s (base CPU 6 + 4 runs x (setup 4 + burst/tail 1.3 + settle 1 + dumps 3 + del 1)) x2
CPU_WIN_S = 6           # idle CPU window (as the 4ea3394 poll measurement, #57)
PING2_S = 2.0           # 2 echoes 0.5 s apart + 1 s reply wait = 1.5 s, 1.25x
FDB_DEADMAN_S = 130     # fdb step ~55 s (20 s base + static 2 + probe 2 + ping 2 + ageing 26.3) x2
BURST_DEADMAN_S = 90    # burst step ~40 s (setup 10 + probe 2 + burst/resync 4.4 + 3 fdb dumps 6 + 2 CPU windows 12) x2
STATE = "/tmp/swd"
BRIDGES = ("brA", "brB", "brsw", "brstp", "brmg", "brbu", "brld")
# burst step per unit: bridge ports (second one without cable), host port (netns), lports, private VIDs, dmesg device
_DEV = {0: "us48_cmic_u0 us48_cmic_u0:", 1: "us48_pci.cmic.0:"}
UNITS = {u: {"br": tuple(_names(g, g + 1)), "host": _DB.netdev(o),
             "lp": {_DB.netdev(f): _DB.port(f)["logical"] for f in (g, g + 1, o)},
             "pvid": {_DB.netdev(f): _DB.pvid(f) for f in (g, g + 1, o)}, "dev": _DEV[u]} for u, g, o in _LOOP}
TOOL = "/tmp/l2_burst"  # tools/l2_burst.c (build_bde_trace.py --l2burst), pushed when missing
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

    def rmems(self, name: str, idxs: range | list[int], unit: int = 0) -> tuple[list[list[int]], dict]:
        """Batched S-channel reads through the driver's schan parameter."""
        base, words, f = mem(name)
        blk = EPIPE if name.startswith("EGR_") else IPIPE
        if not self.apply:
            log(f"DRY read {name}{list(idxs)} unit {unit}")
            return [[0] * words for _ in idxs], f
        res = _board.schan_batch([(unit, "rmem", blk, base + i, words, None) for i in idxs])
        bad = [r["line"] for r in res if r["status"] != "ok"]
        if bad:
            raise Fail(f"read {name}: {bad[0]}")
        return [r["data"][:words] for r in res], f

    def rmem(self, name: str, idx: int, unit: int = 0) -> tuple[list[int], dict]:
        e, f = self.rmems(name, [idx], unit)
        return e[0], f

    def rreg(self, name: str, unit: int = 0) -> tuple[int, dict]:
        a, f = reg(name)
        if not self.apply:
            log(f"DRY read {name} unit {unit}")
            return 0, f
        r = _board.schan_batch([(unit, "rreg", IPIPE, a, 2, None)])[0]
        if r["status"] != "ok":
            raise Fail(f"read {name}: {r['line']}")
        return r["data"][0] | (r["data"][1] << 32 if len(r["data"]) > 1 else 0), f

    def l2_lookup(self, vid: int, mac: str, unit: int = 0) -> dict | None:
        """TABLE_LOOKUP on L2X by {KEY_TYPE 0, VLAN_ID, MAC_ADDR} (read only); None = not found."""
        base, words, f = mem("L2X")
        key = [0] * words
        setf(key, *f["VLAN_ID"], vid)
        setf(key, *f["MAC_ADDR"], int(mac.replace(":", ""), 16))
        if not self.apply:
            log(f"DRY lookup L2X vid {vid} {mac} unit {unit}")
            return None
        r = _board.schan_batch([(unit, "lookup", IPIPE, base, words, key)])[0]
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


def snapshot(b: Board, ports: tuple = tuple(LP)) -> list[str]:
    out = b.sh("; ".join(f"echo \"link {p} $(ip -o link show {p} | grep -c '[<,]UP[,>]') master=$(readlink "
                         f"/sys/class/net/{p}/master)\"; ip -o addr show dev {p} | sed 's/^/addr /'" for p in ports), quiet=True)
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

    def __init__(self, ports: tuple, deadman_s: int = DEADMAN_S):
        self.ports = ports
        st = " ".join(f"$(cat /sys/class/net/{p}/brport/state 2>/dev/null || echo -)" for p in ports)
        sel = " ".join(f"/^ *{p}:/{{v{i}=$3\" \"$11}}" for i, p in enumerate(ports))
        prn = "\" \"".join(f"(v{i}==\"\"?\"0 0\":v{i})" for i in range(len(ports)))
        awk = f"awk '{sel} END{{print {prn}}}' /proc/net/dev"
        n = int(deadman_s / SAMPLE_S)
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


def cleanup_cmd(host: str = "ge17") -> str:
    """Board-side undo, shared by the guard and teardown: fdb monitor killed, bridges down + deleted, netns holder killed
    (the host port returns to the root ns when its namespace dies; it is also moved back first)."""
    brs = "; ".join(f"if [ -d /sys/class/net/{x} ]; then ip link set {x} down; brctl delbr {x}; fi" for x in BRIDGES)
    return (f"kill $(cat {STATE}/mon.pid 2>/dev/null) 2>/dev/null; rm -f {STATE}/mon.pid; "
            f"{brs}; if [ -e {STATE}/A.pid ]; then nsenter -t $(cat {STATE}/A.pid) -n sh -c 'ip addr flush dev {host}; "
            f"ip link set {host} netns 1' 2>/dev/null; kill $(cat {STATE}/A.pid) 2>/dev/null; rm -f {STATE}/A.pid; "
            f"ip link set {host} up; fi")    # fdb/burst precondition: the host port was up (carrier) before the move


def guard_start(b: Board, ports: tuple, deadman_s: int = DEADMAN_S, host: str = "ge17") -> None:
    pat = "|".join(ports)
    loop = (f"i=0; p0=; while [ $i -lt {deadman_s} ]; do read -t 1 x <> {STATE}/f; i=$((i+1)); "
            f"n=$(awk '/^ *({pat}):/{{s+=$3+$11}} END{{print s+0}}' /proc/net/dev); "
            f"if [ -n \"$p0\" ] && [ $((n-p0)) -gt {PPS_LIMIT} ]; then echo \"storm $((n-p0)) pkt/s at ${{i}} s\" > {STATE}/guard; break; fi; "
            f"p0=$n; done; [ -f {STATE}/guard ] || echo \"deadman ${{i}} s\" > {STATE}/guard; {cleanup_cmd(host)}")
    b.sh(f"mkdir -p {STATE}; rm -f {STATE}/guard {STATE}/f; mkfifo {STATE}/f; "
         f"sh -c '{loop.replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}' </dev/null >/dev/null 2>&1 & "
         f"echo $! > {STATE}/guard.pid", allow_ge1=True)


def teardown(b: Board, host: str = "ge17") -> None:
    b.sh(f"kill $(cat {STATE}/guard.pid {STATE}/ping.pid 2>/dev/null) 2>/dev/null; {cleanup_cmd(host)}; rm -rf {STATE}; true",
         check=False, allow_ge1=True)


def verify_restored(b: Board, name: str, vids: list[int], before: list[str], macs: list[tuple[int, str]], unit: int = 0) -> None:
    if not b.apply:
        return
    bad = []
    lp, pvid = UNITS[unit]["lp"], UNITS[unit]["pvid"]
    for v in vids:
        e, f = b.rmem("VLAN_TAB", v, unit)
        if field(e, *f["VALID"]):
            bad.append(f"VLAN_TAB[{v}] still valid")
    for p, (vid, cml) in pt_read(b, unit).items():
        if (vid, cml) not in ((pvid[p], 0), b.pt0.get(p)):
            bad.append(f"PORT_TAB[{lp[p]}] {p} PORT_VID {vid} CML {cml} (before {b.pt0.get(p)})")
    for v, mac in macs:
        if b.l2_lookup(v, mac, unit):
            bad.append(f"L2 {v} {mac} still present")
    after = snapshot(b, tuple(lp))
    if after != before:
        bad.append(f"ports differ: {sorted(set(before) ^ set(after))}")
    b.check(f"{name} teardown", not bad, "; ".join(bad) or "ASIC VLANs invalid, PORT_TAB private + CML 0, L2 clean, ports = pre-test")


def pt_read(b: Board, unit: int = 0) -> dict:
    """{port: (PORT_VID, CML_FLAGS_NEW)}"""
    out = {}
    for p, lp in UNITS[unit]["lp"].items():
        e, f = b.rmem("PORT_TAB", lp, unit)
        out[p] = (field(e, *f["PORT_VID"]), field(e, *f["CML_FLAGS_NEW"]))
    return out


def precondition(b: Board, ports: tuple, unit: int = 0) -> None:
    if not b.apply:
        return
    b.pt0 = pt_read(b, unit)    # a never-opened copper port keeps the init PORT_TAB (VID 1) until copper_enable
    out = b.sh("; ".join([f"for x in {' '.join(BRIDGES)}; do test -d /sys/class/net/$x && echo exists $x; done"] +
                         [f"echo {p} $(cat /sys/class/net/{p}/carrier 2>/dev/null) $(test -e /sys/class/net/{p}/master && "
                          f"echo enslaved)" for p in ports] + [f"test -e {STATE}/A.pid && echo holder", "true"]), quiet=True)
    if "exists" in out or "enslaved" in out or "holder" in out:
        raise Fail(f"precondition: no test bridge, no enslaved port, no holder: {out!r}")
    for p in ports:
        if p not in ("ge9", "ge33") and f"{p} 1" not in out:
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
    ages = [b.rreg("L2_AGE_TIMER", u)[0] & 0xffffffff for u in (0, 1)]
    b.check("L2_AGE_TIMER = stock 0x0010012c (AGE_ENA, 300 s), both units (#131)", ages == [0x0010012c] * 2,
            f"unit 0 {ages[0]:#010x}, unit 1 {ages[1]:#010x}")
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


def fdb_line(b: Board, mac: str, br: str = "brsw") -> str | None:
    """The bridge's own FDB entry for mac (`bridge fdb show`, not the port's self entries from ndo_fdb_dump)."""
    out = b.sh(f"bridge fdb show br {br}", quiet=True)
    return next((ln.strip() for ln in out.splitlines() if ln.startswith(mac) and "self" not in ln.split()), None)


def fdb_wait(b: Board, sm: "Sampler", mac: str, present: bool, limit: float, br: str = "brsw") -> tuple[float, str | None]:
    """Until mac is (not) in the bridge's FDB, one check per sampler line (guards stay on); returns (elapsed, last line)."""
    sm.grace = sm.q.qsize() + 3
    t0 = sm.next()["t"]
    while True:
        sm.grace = sm.q.qsize() + 3    # `bridge fdb show` = one L2X walk per port (S-channel busy-wait); fdb has no loop, pps guard stays
        ln = fdb_line(b, mac, br)
        el = sm.next()["t"] - t0
        if (ln is not None) == present or el > limit:
            return el, ln


def tstamp(s: str) -> datetime.datetime:
    """l2_burst local time stamp: 2026-11-19T00:10:00.123456"""
    return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f")


def mon_tstamp(ln: str) -> datetime.datetime | None:
    """`bridge -timestamp` line: 'Timestamp: Thu Jan  1 00:47:32 1970 667148 usec' (local time, as l2_burst)."""
    f = ln.split()
    if len(f) != 8 or f[0] != "Timestamp:":
        return None
    t = datetime.datetime.strptime(" ".join(f[1:6]), "%a %b %d %H:%M:%S %Y")
    return t + datetime.timedelta(microseconds=int(f[6]))


def tool_push(b: Board) -> None:
    if b.apply and "ok" not in b.sh(f"test -x {TOOL} && echo ok; true", quiet=True):
        subprocess.run([sys.executable, str(ROOT / "scripts/build_bde_trace.py"), "--l2burst"], check=True)
        subprocess.run([sys.executable, str(ROOT / "scripts/lx.py"), "--put", str(ROOT / "tmp/l2_burst"), TOOL], check=True)


def mon_start(b: Board) -> None:
    """`bridge -timestamp monitor fdb` into STATE/mon for the step (killed by cleanup_cmd); iproute2 7.3 has no -tshort."""
    b.sh(f"bridge -timestamp monitor fdb > {STATE}/mon 2>&1 & echo $! > {STATE}/mon.pid")


def mon_event(b: Board, mac: str, deleted: bool) -> tuple[datetime.datetime, str] | None:
    """First monitor event for mac (time of its Timestamp line): extern_learn add, or Deleted."""
    t = None
    for ln in b.sh(f"cat {STATE}/mon", quiet=True).splitlines():
        f = ln.split()
        if mon_tstamp(ln):
            t = mon_tstamp(ln)
            continue
        if len(f) < 2 or t is None:
            continue
        if deleted and f[0] == "Deleted" and f[1] == mac:
            return t, ln
        if not deleted and f[0] == mac and "extern_learn" in f:
            return t, ln
    return None


def burst(b: Board, ns: str, host: str, n: int, src: str) -> tuple[int, datetime.datetime, datetime.datetime]:
    """n frames back to back from the netns host, sources src + i, DA = MAC_STATIC (static sink on the cable-less port)."""
    out = b.sh(f"{ns} {TOOL} {host} {n} {src} {MAC_STATIC}")
    m = re.search(r"tx \S+ (\d+) first (\S+) last (\S+)", out)
    if not m:
        raise Fail(f"l2_burst: {out!r}")
    return int(m.group(1)), tstamp(m.group(2)), tstamp(m.group(3))


def probe_latency(b: Board, sm: "Sampler", ns: str, host: str, name: str) -> datetime.datetime | None:
    """One frame from MAC_LAT: HW learn -> FIFO -> bridge, from the frame's TX stamp to the monitor's extern_learn line."""
    _, t_tx, _ = burst(b, ns, host, 1, MAC_LAT)
    ev, t0 = None, sm.next()["t"]
    while ev is None and sm.hist[-1]["t"] - t0 < FIFO_WAIT_S:
        ev = mon_event(b, MAC_LAT, False)
        if ev is None:
            sm.next()
    lat = (ev[0] - t_tx).total_seconds() if ev else None
    b.check(f"{name} learn latency (frame TX -> bridge extern_learn)", lat is not None and 0 <= lat <= LAT_LIMIT_S,
            f"{lat * 1000:.1f} ms (limit {LAT_LIMIT_S * 1000:.1f} ms): {ev[1].strip()!r}" if ev else
            f"no monitor line within {FIFO_WAIT_S:.2f} s")
    return t_tx


def cpu_busy(b: Board, sm: "Sampler") -> float:
    """Non-idle share of all CPUs over CPU_WIN_S (/proc/stat), one board call."""
    out = b.sh(f"head -n 1 /proc/stat; read -t {CPU_WIN_S} x <> {STATE}/f; head -n 1 /proc/stat", quiet=True).splitlines()
    sm.grace = sm.q.qsize() + 3
    sm.next()
    a, z = ([int(x) for x in ln.split()[1:8]] for ln in out[:2])
    tot = sum(z) - sum(a)
    idle = (z[3] + z[4]) - (a[3] + a[4])
    return 100.0 * (tot - idle) / tot if tot > 0 else 0.0


def fifo_off_line(b: Board, unit: int) -> str:
    out = b.sh(f"dmesg | grep '{UNITS[unit]['dev']} switchdev: L2 change FIFO off' | tail -n 1", quiet=True)
    return out.strip()


def step_fdb(b: Board) -> None:
    log("=== fdb: brsw{ge8, ge9}, STP off; host A = ge17 in a netns")
    ports = ("ge8", "ge9")
    precondition(b, ("ge8", "ge9", "ge17"))
    before = snapshot(b) if b.apply else []
    thermal(b, "before fdb")
    vid, sm, ns = 4000, None, f"nsenter -t $(cat {STATE}/A.pid) -n"
    try:
        guard_start(b, ports, FDB_DEADMAN_S)
        b.sh(f"mkfifo {STATE}/holdA; setsid unshare -n sh -c 'read x <> {STATE}/holdA' </dev/null >/dev/null 2>&1 & "
             f"echo $! > {STATE}/A.pid")
        b.sh(f"ip link set ge17 down; ip link set ge17 netns $(cat {STATE}/A.pid); "
             f"{ns} sh -c 'echo 1 > /proc/sys/net/ipv6/conf/ge17/disable_ipv6 2>/dev/null; ip link set lo up; "
             f"ip link set ge17 up; ip addr add {NET_A}/24 dev ge17'")    # no IPv6 ND/MLD from A: the ageing check needs A silent
        br_make(b, "brsw", False)
        b.sh(f"ip addr add {NET_BR}/24 dev brsw; brctl addif brsw ge8; brctl addif brsw ge9")
        vid, stg = offload_info(b, "brsw")
        if b.apply:
            sm = Sampler(ports, FDB_DEADMAN_S)
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
        el, ln = fdb_wait(b, sm, MAC_GE17, True, FIFO_WAIT_S)
        b.check("fdb HW-learned A in bridge fdb (L2 FIFO)", ln is not None and "dev ge8" in ln and "offload" in ln.split()
                and "extern_learn" in ln.split(), f"{el:.1f} s (limit {FIFO_WAIT_S:.2f} s): {ln!r}")
        brmac = b.sh("cat /sys/class/net/brsw/address", quiet=True)
        h = b.l2_lookup(vid, brmac)
        b.check("fdb host entry brsw -> CPU", bool(h) and h["CPU"] == 1 and h["STATIC_BIT"] == 1 and h["PORT_NUM"] == 0, f"{brmac} {h}")
        # static FDB to a port: ADD_TO_DEVICE -> static L2X entry, OFFLOADED back; stays until the end (probe DA sink)
        b.sh(f"bridge fdb add {MAC_STATIC} dev ge9 master static")
        ln, s = fdb_line(b, MAC_STATIC), b.l2_lookup(vid, MAC_STATIC)
        b.check("fdb static add -> L2X", bool(s) and s["STATIC_BIT"] == 1 and s["PORT_NUM"] == LP["ge9"] and not s["CPU"]
                and ln is not None and "offload" in ln.split(), f"L2 {s}, fdb {ln!r}")
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
        # one frame from MAC_LAT (DA = the static sink on ge9: the CPU never sees it, only HW learning reports it)
        tool_push(b)
        mon_start(b)
        sm.grace = sm.q.qsize() + 4    # queued samples cover the learning/flood steps: L2X flush walks (no loop in fdb)
        t_lat = probe_latency(b, sm, ns, "ge17", "fdb")
        # A again (learning off flushed it), then silent: HW ageing must remove it from L2X and from the bridge
        b.sh(f"{ns} ping -c 2 -i 0.5 -W 1 {NET_BR} > {STATE}/ping 2>&1 & echo $! > {STATE}/ping.pid")
        sm.wait(PING2_S)
        b.sh(f"kill $(cat {STATE}/ping.pid) 2>/dev/null; true", check=False, quiet=True)
        el, ln = fdb_wait(b, sm, MAC_GE17, True, FIFO_WAIT_S)
        b.check("fdb A re-learned in bridge fdb", ln is not None and "extern_learn" in ln.split(), f"{el:.1f} s: {ln!r}")
        el, ln = fdb_wait(b, sm, MAC_GE17, False, AGE_WAIT_S)
        a = b.l2_lookup(vid, MAC_GE17)
        b.check("fdb HW ageing removes A (L2X + bridge fdb)", ln is None and a is None,
                f"{el:.1f} s after ageing 10 s (limit {AGE_WAIT_S:.2f} s): fdb {ln!r}, L2 {a}")
        ev = mon_event(b, MAC_LAT, True)
        age = (ev[0] - t_lat).total_seconds() if ev and t_lat else None
        b.check("fdb HW ageing removes the probe MAC (bridge Deleted after its one frame)", age is not None and age <= AGE_WAIT_S,
                f"{age:.2f} s after the frame (ageing 10 s, limit {AGE_WAIT_S:.2f} s): {ev[1].strip()!r}" if age is not None
                else f"no Deleted line: {ev!r}")
        # statics are not aged: user entry on ge9 + brsw host entry still in L2X and the bridge
        s, h = b.l2_lookup(vid, MAC_STATIC), b.l2_lookup(vid, brmac)
        ln = fdb_line(b, MAC_STATIC)
        b.check("fdb static entries unaffected by ageing", bool(s) and s["STATIC_BIT"] == 1 and bool(h) and h["STATIC_BIT"] == 1
                and ln is not None and "static" in ln.split(), f"user {s}, host {h}, fdb {ln!r}")
        b.sh(f"bridge fdb del {MAC_STATIC} dev ge9 master")
        ln, s = fdb_line(b, MAC_STATIC), b.l2_lookup(vid, MAC_STATIC)
        b.check("fdb static del -> L2X gone", s is None and ln is None, f"L2 {s}, fdb {ln!r}")
        b.sh("echo 30000 > /sys/class/net/brsw/bridge/ageing_time")
        v, f = b.rreg("L2_AGE_TIMER")
        b.check("fdb ageing 300 s restored", (v & 0xffffffff) == 0x0010012c, f"L2_AGE_TIMER {v:#x}")
        if sm:
            sm.next()
    finally:
        if sm:
            sm.stop()
        teardown(b)
        verify_restored(b, "fdb", [vid], before, [(vid, MAC_GE17), (vid, MAC_STATIC), (vid, MAC_LAT)])
        if b.apply:
            off = fifo_off_line(b, 0)
            b.check("fdb FIFO off, no index mismatch", " 0 index mismatches" in off, off)
        thermal(b, "after fdb")


def fdb_sets(b: Board, sm: "Sampler", port: str, br: str) -> tuple[set, set]:
    """Burst MACs on port: (bridge extern_learn entries, the driver's L2X entries = ndo_fdb_dump `self` lines)."""
    sm.grace = sm.q.qsize() + 6    # one L2X walk per dump part under rtnl (S-channel busy-wait); no loop possible, pps guard stays
    out = b.sh(f"bridge fdb show brport {port}", quiet=True)
    sm.next()
    pre = BURST_BASE[:12]
    mine, hw = set(), set()
    for ln in out.splitlines():
        f = ln.split()
        if not f or not f[0].startswith(pre):
            continue
        if "self" in f:
            hw.add(f[0])
        elif "extern_learn" in f and "master" in f and br in f:
            mine.add(f[0])
    return mine, hw


def step_burst(b: Board, unit: int) -> None:
    """> 128 new sources in one burst: FIFO overflow -> one resync walk; bridge fdb = L2X afterwards; CPU with one bridge."""
    cfg = UNITS[unit]
    p0, p1 = cfg["br"]
    host, lp, br = cfg["host"], cfg["lp"], "brbu"
    log(f"=== burst unit {unit}: {br}{{{p0}, {p1}}} STP off; host {host} in a netns sends {BURST_N} sources")
    precondition(b, (p0, p1, host), unit)
    before = snapshot(b, tuple(lp)) if b.apply else []
    thermal(b, f"before burst u{unit}")
    vid, sm, ns = 4000, None, f"nsenter -t $(cat {STATE}/A.pid) -n"
    last = BURST_BASE[:12] + "%02x:%02x" % divmod(BURST_N - 1, 256)
    try:
        tool_push(b)
        guard_start(b, (p0, p1), BURST_DEADMAN_S, host)
        b.sh(f"mkfifo {STATE}/holdA; setsid unshare -n sh -c 'read x <> {STATE}/holdA' </dev/null >/dev/null 2>&1 & "
             f"echo $! > {STATE}/A.pid")
        b.sh(f"ip link set {host} down; ip link set {host} netns $(cat {STATE}/A.pid); "
             f"{ns} sh -c 'echo 1 > /proc/sys/net/ipv6/conf/{host}/disable_ipv6 2>/dev/null; ip link set lo up; "
             f"ip link set {host} up'")
        if b.apply:
            sm = Sampler((p0, p1), BURST_DEADMAN_S)
            sm.next()
        cpu0 = cpu_busy(b, sm) if sm else 0.0
        br_make(b, br, False)
        b.sh(f"brctl addif {br} {p0}; brctl addif {br} {p1}")
        vid, _ = offload_info(b, br)
        while sm:
            c = b.sh(f"{ns} ip -o link show {host} | grep -c LOWER_UP; cat /sys/class/net/{p0}/carrier", check=False, quiet=True).split()
            el = sm.next()["t"] - sm.hist[0]["t"]
            if c == ["1", "1"]:
                break
            if el > LINK_S + CPU_WIN_S:
                raise Fail(f"no carrier {host}/{p0} {el:.1f} s after the move: {c}")
        if not b.apply:
            return
        on = b.sh(f"dmesg | grep '{cfg['dev']} switchdev: L2 change FIFO on' | tail -n 1", quiet=True)
        b.check(f"burst u{unit} FIFO on", "-> " in on and "drained every" in on, on.strip())
        b.sh(f"bridge fdb add {MAC_STATIC} dev {p1} master static")
        mon_start(b)
        probe_latency(b, sm, ns, host, f"burst u{unit}")
        cpu1 = cpu_busy(b, sm)    # bridge up, FIFO drained every 10 ms, nothing learning
        b.check(f"burst u{unit} CPU with one bridge (FIFO drain, idle)", cpu1 - cpu0 < 5.0,
                f"{cpu1:.1f} % busy vs {cpu0:.1f} % before the bridge ({CPU_WIN_S} s windows; old L2 poll: ~5 % on unit 1)")
        grep = f"dmesg | grep '{cfg['dev']} switchdev: L2 resync (FIFO full)'; true"
        n_resync = len(b.sh(grep, quiet=True).splitlines())
        sent, t0, t1 = burst(b, ns, host, BURST_N, BURST_BASE)
        log(f"  burst: {sent} frames in {(t1 - t0).total_seconds() * 1000:.1f} ms")
        res, s0 = "", sm.next()["t"]
        while sm.hist[-1]["t"] - s0 < RESYNC_WAIT_S:
            lines = b.sh(grep, quiet=True).splitlines()
            if len(lines) > n_resync:
                res = lines[n_resync]
                break
            sm.next()
        b.check(f"burst u{unit} FIFO overflow -> resync walk", bool(res),
                f"{res.strip()!r} within {sm.hist[-1]['t'] - s0:.1f} s (limit {RESYNC_WAIT_S:.2f} s)" if res else
                f"no 'L2 resync (FIFO full)' line within {RESYNC_WAIT_S:.2f} s")
        mine, hw = fdb_sets(b, sm, p0, br)
        b.check(f"burst u{unit} bridge fdb = L2X after the resync", mine == hw and len(hw) > 128,
                f"L2X {len(hw)} of {sent} burst MACs, bridge extern_learn {len(mine)}, only L2X {sorted(hw - mine)[:4]}, "
                f"only bridge {sorted(mine - hw)[:4]}")
        a = b.l2_lookup(vid, last, unit)
        log(f"  L2X last burst MAC {last}: {a}")
        s, h = b.l2_lookup(vid, MAC_STATIC, unit), b.l2_lookup(vid, b.sh(f"cat /sys/class/net/{br}/address", quiet=True), unit)
        ln = fdb_line(b, MAC_STATIC, br)
        b.check(f"burst u{unit} static entries unaffected", bool(s) and s["STATIC_BIT"] == 1 and s["PORT_NUM"] == lp[p1]
                and bool(h) and h["CPU"] == 1 and ln is not None and "static" in ln.split(), f"user {s}, host {h}, fdb {ln!r}")
    finally:
        if sm:
            sm.stop()
        teardown(b, host)
        verify_restored(b, f"burst u{unit}", [vid], before, [(vid, MAC_STATIC), (vid, MAC_LAT), (vid, BURST_BASE), (vid, last)], unit)
        if b.apply:
            off = fifo_off_line(b, unit)
            b.check(f"burst u{unit} FIFO off, no index mismatch", " 0 index mismatches" in off and "WRITE FAILED" not in off, off)
        thermal(b, f"after burst u{unit}")


def learn_curve(b: Board, t0: datetime.datetime) -> tuple[int, list[tuple[int, int]]]:
    """Monitor extern_learn adds of burst MACs: total, and the count by each RATE_MARKS_MS mark after the first frame."""
    ts, t = [], None
    for ln in b.sh(f"cat {STATE}/mon", quiet=True).splitlines():
        if mon_tstamp(ln):
            t = mon_tstamp(ln)
            continue
        f = ln.split()
        if t and f and f[0].startswith(BURST_BASE[:12]) and "extern_learn" in f and f[0] != "Deleted":
            ts.append((t - t0).total_seconds() * 1000)
    return len(ts), [(m, sum(1 for x in ts if x <= m)) for m in RATE_MARKS_MS]


def step_rate(b: Board, unit: int) -> None:
    """RATE_N new sources per run at each RATE_PPS: learned (L2X, bridge) vs sent over time, FIFO full / fast drains,
    resync walks, drain CPU. One bridge per run (its 'FIFO off' line carries the run's counters)."""
    cfg = UNITS[unit]
    p0, p1 = cfg["br"]
    host, br = cfg["host"], "brbu"
    log(f"=== rate unit {unit}: {br}{{{p0}, {p1}}} STP off; host {host} sends {RATE_N} sources at {RATE_PPS} pps (0 = back to back)")
    precondition(b, (p0, p1, host), unit)
    before = snapshot(b, tuple(cfg["lp"])) if b.apply else []
    thermal(b, f"before rate u{unit}")
    vid, sm, ns = 4000, None, f"nsenter -t $(cat {STATE}/A.pid) -n"
    try:
        tool_push(b)
        guard_start(b, (p0, p1), RATE_DEADMAN_S, host)
        b.sh(f"mkfifo {STATE}/holdA; setsid unshare -n sh -c 'read x <> {STATE}/holdA' </dev/null >/dev/null 2>&1 & "
             f"echo $! > {STATE}/A.pid")
        b.sh(f"ip link set {host} down; ip link set {host} netns $(cat {STATE}/A.pid); "
             f"{ns} sh -c 'echo 1 > /proc/sys/net/ipv6/conf/{host}/disable_ipv6 2>/dev/null; ip link set lo up; "
             f"ip link set {host} up'")
        if not b.apply:
            return
        sm = Sampler((p0, p1), RATE_DEADMAN_S)
        sm.next()
        base_cpu = cpu_busy(b, sm)
        for pps in RATE_PPS:
            br_make(b, br, False)
            b.sh(f"brctl addif {br} {p0}; brctl addif {br} {p1}")
            vid, _ = offload_info(b, br)
            while True:
                c = b.sh(f"{ns} ip -o link show {host} | grep -c LOWER_UP; cat /sys/class/net/{p0}/carrier", check=False, quiet=True).split()
                el = sm.next()["t"] - sm.hist[0]["t"]
                if c == ["1", "1"]:
                    break
                if el > RATE_DEADMAN_S / 2:
                    raise Fail(f"no carrier {host}/{p0}: {c}")
            b.sh(f"bridge fdb add {MAC_STATIC} dev {p1} master static")
            mon_start(b)
            grep = f"dmesg | grep '{cfg['dev']} switchdev: L2 resync (FIFO full)'; true"
            n_res = len(b.sh(grep, quiet=True).splitlines())
            out = b.sh(f"head -n 1 /proc/stat; {ns} {TOOL} {host} {RATE_N} {BURST_BASE} {MAC_STATIC} {pps}; "
                       f"read -t {RATE_TAIL_S} x <> {STATE}/f; head -n 1 /proc/stat", quiet=True).splitlines()
            sm.grace = sm.q.qsize() + 4
            m = re.search(r"tx \S+ (\d+) first (\S+) last (\S+) cpu_ms (\d+)", " ".join(out))
            if not m:
                raise Fail(f"l2_burst: {out!r}")
            sent, t0, t1, tx_ms = int(m.group(1)), tstamp(m.group(2)), tstamp(m.group(3)), int(m.group(4))
            a, z = ([int(x) for x in ln.split()[1:8]] for ln in (out[0], out[-1]))
            win = (sum(z) - sum(a)) * 10 / 2      # USER_HZ 100: ms per CPU, 2 CPUs
            busy = (sum(z) - sum(a) - (z[3] + z[4]) + (a[3] + a[4])) * 10
            drain_ms = busy - tx_ms - base_cpu / 100 * 2 * win
            sm.wait(RATE_SETTLE_S)
            res = b.sh(grep, quiet=True).splitlines()[n_res:]
            mine, hw = fdb_sets(b, sm, p0, br)
            n_mon, curve = learn_curve(b, t0)
            dur = (t1 - t0).total_seconds() * 1000
            b.sh(f"kill $(cat {STATE}/mon.pid) 2>/dev/null; rm -f {STATE}/mon.pid; ip link set {br} down; brctl delbr {br}")
            off = fifo_off_line(b, unit)
            log(f"  rate u{unit} {pps or 'b2b'} pps: sent {sent} in {dur:.1f} ms ({sent / max(dur, 0.001) * 1000:.0f}/s), "
                f"L2X {len(hw)}, bridge {len(mine)}, monitor {n_mon}; learned by ms {curve}; "
                f"resyncs {[r.split('resync')[1].strip() for r in res]}; drain CPU ~{drain_ms:.0f} ms (busy {busy:.0f}, "
                f"sender {tx_ms}, idle base {base_cpu:.1f} %, window {win:.0f} ms); {off.split('): ')[-1]}")
            b.check(f"rate u{unit} {pps or 'b2b'} bridge fdb = L2X", mine == hw,
                    f"only L2X {sorted(hw - mine)[:4]}, only bridge {sorted(mine - hw)[:4]}")
            b.check(f"rate u{unit} {pps or 'b2b'} FIFO off clean", " 0 index mismatches" in off and "WRITE FAILED" not in off, off)
            thermal(b, f"rate u{unit} {pps or 'b2b'}")
    finally:
        if sm:
            sm.stop()
        teardown(b, host)
        verify_restored(b, f"rate u{unit}", [vid], before, [(vid, MAC_STATIC), (vid, BURST_BASE)], unit)
        thermal(b, f"after rate u{unit}")


def dmesg_t(b: Board, pat: str, after: float) -> tuple[float, str] | None:
    """First dmesg line matching pat with a time stamp > after (s since boot)."""
    for ln in b.sh(f"dmesg | grep -e '{pat}'; true", quiet=True).splitlines():
        m = re.match(r"\[\s*(\d+\.\d+)\]", ln)
        if m and float(m.group(1)) > after:
            return float(m.group(1)), ln.strip()
    return None


def step_linkdown(b: Board) -> None:
    """Carrier loss of a bridged port: its dynamic L2X entries leave the chip and the bridge, statics stay (#135)."""
    log("=== linkdown: brld{ge8, ge9}, host A = ge17 in a netns; A learned on ge8, then ge8 loses carrier")
    ports = ("ge8", "ge9")
    precondition(b, ("ge8", "ge9", "ge17"))
    before = snapshot(b) if b.apply else []
    thermal(b, "before linkdown")
    vid, sm, ns = 4000, None, f"nsenter -t $(cat {STATE}/A.pid) -n"
    try:
        tool_push(b)
        guard_start(b, ports, LD_DEADMAN_S)
        b.sh(f"mkfifo {STATE}/holdA; setsid unshare -n sh -c 'read x <> {STATE}/holdA' </dev/null >/dev/null 2>&1 & "
             f"echo $! > {STATE}/A.pid")
        b.sh(f"ip link set ge17 down; ip link set ge17 netns $(cat {STATE}/A.pid); "
             f"{ns} sh -c 'echo 1 > /proc/sys/net/ipv6/conf/ge17/disable_ipv6 2>/dev/null; ip link set lo up; ip link set ge17 up'")
        br_make(b, "brld", False)
        b.sh("brctl addif brld ge8; brctl addif brld ge9")
        vid, _ = offload_info(b, "brld")
        if not b.apply:
            return
        sm = Sampler(ports, LD_DEADMAN_S)
        sm.next()
        for how, down, up in (("far end ge17 admin down", f"{ns} ip link set ge17 down", f"{ns} ip link set ge17 up"),
                              ("ge8 admin down", "ip link set ge8 down", "ip link set ge8 up")):
            while True:
                c = b.sh(f"{ns} ip -o link show ge17 | grep -c LOWER_UP; cat /sys/class/net/ge8/carrier", check=False, quiet=True).split()
                el = sm.next()["t"] - sm.hist[0]["t"]
                if c == ["1", "1"]:
                    break
                if el > LD_DEADMAN_S / 2:
                    raise Fail(f"no carrier ge17/ge8: {c}")
            b.sh(f"bridge fdb replace {MAC_STATIC} dev ge9 master static")
            burst(b, ns, "ge17", 1, MAC_LAT)
            el, ln = fdb_wait(b, sm, MAC_LAT, True, FIFO_WAIT_S, "brld")
            a = b.l2_lookup(vid, MAC_LAT)
            b.check(f"linkdown [{how}] A learned on ge8 (L2X + bridge)", bool(a) and a["PORT_NUM"] == LP["ge8"] and
                    not a["STATIC_BIT"] and ln is not None and "extern_learn" in ln.split(), f"L2 {a}, fdb {ln!r}")
            t_mark, t_cmd = (float(x) for x in b.sh(f"cut -d' ' -f1 /proc/uptime; {down}; cut -d' ' -f1 /proc/uptime").split()[-2:])
            el, ln = fdb_wait(b, sm, MAC_LAT, False, LD_WAIT_S, "brld")
            if b.sh("cat /sys/class/net/ge8/carrier", check=False, quiet=True) == "1":
                log(f"  INFO linkdown [{how}]: ge8 keeps carrier {el:.1f} s later (admin down = MAC down, PHY stays "
                    f"linked: {LD_ISSUE}); A still learned: {ln!r}")
                b.sh(up)
                continue
            a, s = b.l2_lookup(vid, MAC_LAT), b.l2_lookup(vid, MAC_STATIC)
            dn = dmesg_t(b, "ge8: link down", t_mark)
            fl = dmesg_t(b, "ge8: switchdev: L2 flush (dynamic)", t_mark)
            lat = f"link down +{(dn[0] - t_mark) * 1000:.0f} ms, " if dn else "no 'ge8: link down' line, "
            lat += f"flush +{(fl[0] - t_mark) * 1000:.0f} ms: {fl[1].split('switchdev: ')[-1]!r}" if fl else "no flush line"
            b.check(f"linkdown [{how}] A gone from L2X and the bridge, static kept", a is None and ln is None and bool(s)
                    and s["STATIC_BIT"] == 1, f"{lat} (command {(t_cmd - t_mark) * 1000:.0f} ms); L2 A {a}, fdb {ln!r}, static {s}")
            b.sh(up)
        sm.next()
    finally:
        if sm:
            sm.stop()
        teardown(b)
        verify_restored(b, "linkdown", [vid], before, [(vid, MAC_LAT), (vid, MAC_STATIC)])
        thermal(b, "after linkdown")


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
    steps = ("preflight", "bpdu", "fdb", "linkdown", "burst", "rate", "stp", "mgmt", "all")
    if len(args) != 1 or args[0] not in steps:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    b = Board("--apply" in sys.argv[1:])
    todo = ("preflight", "bpdu", "fdb", "linkdown", "burst", "rate", "stp") if args[0] == "all" else (args[0],)
    rc = 0
    for name in todo:
        try:
            if name == "preflight":
                preflight(b)
            elif name == "bpdu":
                step_bpdu(b)
            elif name == "fdb":
                step_fdb(b)
            elif name == "linkdown":
                step_linkdown(b)
            elif name == "burst":
                for unit in (0, 1):
                    try:
                        step_burst(b, unit)
                    except Fail as e:
                        log(f"FAIL burst u{unit}: {e}")
                        b.failed += 1
            elif name == "rate":
                for unit in (0, 1):
                    try:
                        step_rate(b, unit)
                    except Fail as e:
                        log(f"FAIL rate u{unit}: {e}")
                        b.failed += 1
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
