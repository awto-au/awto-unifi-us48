#!/usr/bin/env python3
"""stp_loop_test — Linux bridge STP on a loop cable, through the CPU path (#57).

Usage: stp_loop_test.py <u0|u1|all> [--apply]
- u0 = ge8 + ge17 (front 8 <-> 17, unit 0), u1 = ge32 + ge41 (front 32 <-> 41, unit 1). Dry-run by default (prints commands).
- Per pair: bridge `brstp`, kernel STP on and timers at the kernel minimum BEFORE any port joins, then both cable ends
  join. Pass = one port forwarding (3) and the other blocking (4) in /sys/class/net/brstp/brif/*/state, no storm, ge1
  never a bridge port; then teardown (bridge deleted, ports' admin state/addresses/master = pre-test).
- Each port stays in its private ASIC VLAN: a loop can only run through the CPU (rx on one end -> bridge -> tx other end).
  us48_cmic `switchdev` is set to N for the run (restored after); the offloaded variant is `switchdev_test.py stp`.
- Timers (kernel minimum with STP on, net/bridge/br_private.h BR_MIN_*; sysfs unit = 1/100 s): forward_delay 2 s,
  hello_time 1 s, max_age 6 s. Forwarding after listening + learning = 2 x forward_delay = 4 s.
- Settle limit: 2 x forward_delay + 1 hello = 5 s, x1.25 = 6.25 s; expiry = FAIL + teardown.
- Sampling: one long-lived ssh runs a board loop printing a line every 0.5 s (a new ssh per sample costs a dropbear
  handshake, which alone shows ~90 % CPU on the single A9 over a 0.2 s window).
- Storm guard, host side: packet rate (rx+tx, both ports) > 2000 pps in any 0.5 s sample, both ports forwarding at once,
  or CPU busy > 90 % (1 s window) on 2 samples in a row -> teardown at once. Normal load: BPDUs 1/s + a few ND/MLD frames.
- Storm guard, board side (survives a lost ssh): busybox loop, 1 s steps (`read -t 1` on a fifo, no sleep): same 2000 pps
  limit, deadman 40 s (expected run ~15 s: 6.25 s settle + 5 s hold + ping + teardown; x2.5 because ssh round trips can
  stall under load) -> bridge down + deleted. Reason in /tmp/stp/guard on the board.
- Hold: 5 s of samples, then 3 x ping6 ff02::1 out of brstp (flood: must leave by the forwarding port only).
- Temps before/after (stop: die >= 108 C, LM75 >= 78 C). Exit: 0 pass, 1 fail, 2 usage/precondition. Log: tmp/logs/stp_loop_test.log
"""
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard

help_guard(__doc__, strict=True, known=("--apply",))

import _board

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/stp_loop_test.log"
PAIRS = {"u0": ("ge8", "ge17"), "u1": ("ge32", "ge41")}
BR = "brstp"
FD_CS, HELLO_CS, MAXAGE_CS = 200, 100, 600   # kernel minimum with STP on, centiseconds
SETTLE_S = 1.25 * (2 * FD_CS / 100 + HELLO_CS / 100)
HOLD_S = 5.0
PPS_LIMIT = 2000
CPU_LIMIT = 90.0
DEADMAN_S = 40
CALL_S = 15.0   # one board command (busybox/brctl < 1 s)
SAMPLE_S = 0.5  # sampler period; CPU judged over 1 s windows (2 periods)
SAMPLE_STALL_S = 2.5  # no sample line for 5 periods = sampler/ssh dead
PING_S = 4.0    # 3 echo requests at 1 s interval = 2 s + the ssh start; then the ping is killed

STATE = "/tmp/stp"
SWD = "/sys/module/us48_cmic/parameters/switchdev"   # absent on images before the switchdev offload
STATES = {0: "disabled", 1: "listening", 2: "learning", 3: "forwarding", 4: "blocking"}


class Fail(Exception):
    pass


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")
    print(line, flush=True)


class Board:
    def __init__(self, apply: bool):
        self.apply = apply
        self.failed = 0

    def sh(self, cmd: str, check: bool = True, quiet: bool = False) -> str:
        if re.search(r"\bge1\b", cmd):
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

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        if self.apply:
            log(f"{'PASS' if ok else 'FAIL'} {name}" + (f": {detail}" if detail else ""))
            self.failed += not ok
        return ok


def snapshot(b: Board, ports: tuple) -> list[str]:
    out = b.sh("; ".join(f"echo \"link {p} $(ip -o link show {p} | grep -c '[<,]UP[,>]') master=$(readlink "
                         f"/sys/class/net/{p}/master)\"; ip -o addr show dev {p} | sed 's/^/addr /'"
                         for p in ports), quiet=True)
    return sorted(re.sub(r"\s+valid_lft.*", "", ln).strip() for ln in out.splitlines() if ln.strip())


def thermal(b: Board, when: str) -> None:
    if not b.apply:
        return
    t = _board.temps()
    log(f"temps {when}: " + ", ".join(f"{k} {v:.1f}" for k, v in sorted(t.items())))
    hot = _board.too_hot(t)
    if hot:
        raise Fail(f"thermal stop {when}: {hot}")


class Sampler:
    """One long-lived ssh running a board loop: a line every SAMPLE_S (new ssh per sample = a dropbear handshake, which
    alone loads the single A9 to ~90 % over a 0.2 s window). Fields: uptime, cpu jiffies (7), rx/tx packets per port,
    STP state per port, ge1 enslaved (read only), guard alive, guard message."""

    def __init__(self, ports: tuple):
        self.ports = ports
        st = " ".join(f"$(cat /sys/class/net/{BR}/brif/{p}/state 2>/dev/null || echo -)" for p in ports)
        awk = (f"awk '/^ *{ports[0]}:/{{a=$3\" \"$11}} /^ *{ports[1]}:/{{b=$3\" \"$11}} END{{print a\" \"b}}' /proc/net/dev")
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
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for ln in self.p.stdout:
            self.q.put(ln)
        self.q.put(None)

    def next(self) -> dict:
        """Next sample; a gap > SAMPLE_STALL_S = sampler dead -> Fail."""
        while True:
            try:
                ln = self.q.get(timeout=SAMPLE_STALL_S)
            except queue.Empty:
                raise Fail(f"sampler silent for {SAMPLE_STALL_S} s (period {SAMPLE_S} s)")
            if ln is None:
                raise Fail("sampler ended")
            f = ln.split()
            if f and f[0] == "S" and len(f) >= 16:
                break
            log(f"  sampler: {ln.strip()}")
        p0, p1 = self.ports
        return {"t": float(f[1]), "cpu": [int(x) for x in f[2:9]],
                "pk": {p0: (int(f[9]), int(f[10])), p1: (int(f[11]), int(f[12]))},
                "state": {p0: f[13], p1: f[14]}, "ge1_master": int(f[15]), "guard_alive": int(f[16]) if len(f) > 16 else 0,
                "guard": " ".join(f[17:])}

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


def guard_cmd(ports: tuple) -> str:
    pat = "|".join(ports)
    loop = (f"i=0; p0=; while [ $i -lt {DEADMAN_S} ]; do read -t 1 x <> {STATE}/f; i=$((i+1)); "
            f"n=$(awk '/^ *({pat}):/{{s+=$3+$11}} END{{print s+0}}' /proc/net/dev); "
            f"if [ -n \"$p0\" ] && [ $((n-p0)) -gt {PPS_LIMIT} ]; then echo \"storm $((n-p0)) pkt/s at ${{i}} s\" > {STATE}/guard; break; fi; "
            f"p0=$n; done; [ -f {STATE}/guard ] || echo \"deadman ${{i}} s\" > {STATE}/guard; "
            f"ip link set {BR} down; brctl delbr {BR}")
    return (f"rm -f {STATE}/guard {STATE}/f; mkfifo {STATE}/f; "
            f"sh -c '{loop.replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}' </dev/null >/dev/null 2>&1 & echo $! > {STATE}/guard.pid")


def teardown(b: Board, ports: tuple) -> None:
    b.sh(f"kill $(cat {STATE}/guard.pid {STATE}/ping.pid 2>/dev/null) 2>/dev/null; "
         f"if [ -d /sys/class/net/{BR} ]; then ip link set {BR} down; brctl delbr {BR}; fi; rm -rf {STATE}; true", check=False)


def run_pair(b: Board, name: str) -> None:
    ports = PAIRS[name]
    log(f"=== {name}: {ports[0]} + {ports[1]} in {BR}, STP on (fd {FD_CS / 100:.0f} s, hello {HELLO_CS / 100:.0f} s, "
        f"max_age {MAXAGE_CS / 100:.0f} s)")
    if b.apply:
        pre = b.sh(f"test -d /sys/class/net/{BR} && echo exists; for p in {' '.join(ports)}; do "
                   f"echo $p $(cat /sys/class/net/$p/carrier) $(test -e /sys/class/net/$p/master && echo enslaved); done", quiet=True)
        if "exists" in pre or "enslaved" in pre or pre.count(" 1") < 2:
            raise Fail(f"precondition: {BR} absent, both ports carrier 1 and not enslaved: {pre!r}")
    before = snapshot(b, ports) if b.apply else []
    thermal(b, f"before {name}")
    b.sh(f"mkdir -p {STATE}")
    swd = b.sh(f"cat {SWD} 2>/dev/null; true", quiet=True)
    try:
        if swd:
            b.sh(f"echo N > {SWD}")
        b.sh(f"brctl addbr {BR}; echo 1 > /sys/class/net/{BR}/bridge/stp_state; "
             f"echo {FD_CS} > /sys/class/net/{BR}/bridge/forward_delay; echo {HELLO_CS} > /sys/class/net/{BR}/bridge/hello_time; "
             f"echo {MAXAGE_CS} > /sys/class/net/{BR}/bridge/max_age; ip link set {BR} up")
        got = b.sh(f"cd /sys/class/net/{BR}/bridge; echo $(cat stp_state forward_delay hello_time max_age)", quiet=True)
        if b.apply and got.split() != ["1", str(FD_CS), str(HELLO_CS), str(MAXAGE_CS)]:
            raise Fail(f"bridge settings read back {got!r}, want stp 1 fd {FD_CS} hello {HELLO_CS} max_age {MAXAGE_CS}")
        b.sh(guard_cmd(ports))
        if b.apply:
            sm = Sampler(ports)
            s0 = sm.next()
        b.sh(f"brctl addif {BR} {ports[0]}; brctl addif {BR} {ports[1]}")
        if not b.apply:
            return
        t_join = s0["t"]
        hist, hot, settled, seen = [s0], 0, None, []
        ping_at = ping_end = None
        while True:
            s = sm.next()
            hist.append(s)
            el = s["t"] - t_join
            pps, _ = rates(hist[-2], s)
            _, cpu = rates(hist[-3] if len(hist) > 2 else hist[-2], s)   # 1 s window
            states = tuple(s["state"][p] for p in ports)
            if not seen or seen[-1][1] != states:
                seen.append((round(el, 1), states))
                log(f"  t={el:4.1f} s states {dict(zip(ports, (STATES.get(int(x), x) if x.isdigit() else x for x in states)))}"
                    f" {pps:.0f} pps CPU(1 s) {cpu:.0f} %")
            hot = hot + 1 if cpu > CPU_LIMIT else 0
            if s["guard"] or not s["guard_alive"]:
                raise Fail(f"board guard fired or not running: alive {s['guard_alive']} {s['guard']!r}")
            if s["ge1_master"]:
                raise Fail("ge1 has a master")
            if pps > PPS_LIMIT or hot >= 2 or states == ("3", "3"):
                raise Fail(f"storm guard: {pps:.0f} pps, CPU {cpu:.0f} % x{hot}, states {states}")
            if settled is None and sorted(states) == ["3", "4"]:
                settled = el
                fwd, blk = ports[states.index("3")], ports[states.index("4")]
                log(f"settled {el:.1f} s after the joins: {fwd} forwarding, {blk} blocking")
                i_hold = len(hist) - 1
            if settled is None and el > SETTLE_S:
                raise Fail(f"not settled {el:.1f} s after the joins (limit {SETTLE_S:.2f} s): {states}")
            if settled is not None and ping_at is None and s["t"] - hist[i_hold]["t"] >= HOLD_S:
                i_end = ping_at = len(hist) - 1
                # background: busybox ping6 ignores -w here (12 s with no reply); sampling and the guards keep running
                b.sh(f"ping6 -c 3 -I {BR} ff02::1 > {STATE}/ping 2>&1 & echo $! > {STATE}/ping.pid")
            elif ping_at is not None and s["t"] - hist[ping_at]["t"] >= PING_S:
                ping_end = len(hist) - 1
                out = b.sh(f"kill $(cat {STATE}/ping.pid) 2>/dev/null; cat {STATE}/ping", check=False, quiet=True)
                break
        a, z = hist[i_hold], hist[i_end]
        pps_hold, _ = rates(a, z)
        cpus = [rates(hist[i - 2], hist[i])[1] for i in range(i_hold + 2, i_end + 1)]
        b.check(f"{name} one port blocking", True, f"{blk} blocking, {fwd} forwarding, settled {settled:.1f} s after the "
                f"joins (limit {SETTLE_S:.2f} s), transitions {seen}")
        b.check(f"{name} no storm while held {z['t'] - a['t']:.1f} s", pps_hold < PPS_LIMIT and max(cpus or [0]) < CPU_LIMIT,
                f"{pps_hold:.1f} pps (both ports rx+tx), CPU max {max(cpus or [0]):.0f} % (1 s windows)")
        a, z = hist[ping_at], hist[ping_end]
        d = {p: (z["pk"][p][0] - a["pk"][p][0], z["pk"][p][1] - a["pk"][p][1]) for p in ports}
        pps_p, _ = rates(a, z)
        log(f"  ping6 ff02::1 via {BR}: rx/tx per port {d}, {pps_p:.1f} pps; "
            f"{out.splitlines()[-2] if len(out.splitlines()) > 1 else out}")
        b.check(f"{name} flood leaves by the forwarding port only", d[fwd][1] >= 3 and d[blk][1] < 3 and pps_p < PPS_LIMIT,
                f"{fwd} tx {d[fwd][1]}, {blk} tx {d[blk][1]}, {blk} rx {d[blk][0]} (not re-flooded)")
        b.check(f"{name} states stable", tuple(z['state'][p] for p in ports) == states, str(z["state"]))
        b.check(f"{name} ge1 never in the bridge", all(h["ge1_master"] == 0 for h in hist))
        log("  packets since join (rx, tx): " + ", ".join(f"{p} {z['pk'][p][0] - s0['pk'][p][0]}/{z['pk'][p][1] - s0['pk'][p][1]}"
                                                       for p in ports))
    finally:
        if b.apply and "sm" in locals():
            sm.stop()
        teardown(b, ports)
        if swd:
            b.sh(f"echo {swd} > {SWD}", check=False)
        if b.apply:
            after = snapshot(b, ports)
            gone = b.sh(f"test -d /sys/class/net/{BR} && echo present || echo gone", quiet=True)
            b.check(f"{name} teardown", gone == "gone" and after == before,
                    f"{BR} {gone}; ports " + ("= pre-test" if after == before else f"differ: {sorted(set(before) ^ set(after))}"))
            thermal(b, f"after {name}")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1 or args[0] not in ("u0", "u1", "all"):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    b = Board("--apply" in sys.argv[1:])
    rc = 0
    for name in (("u0", "u1") if args[0] == "all" else (args[0],)):
        try:
            run_pair(b, name)
        except Fail as e:
            log(f"FAIL {name}: {e}")
            b.failed += 1
        except (subprocess.SubprocessError, OSError, RuntimeError) as e:
            log(f"ERROR {name}: {e}")
            rc = 2
    log(f"result: {'PASS' if not b.failed and not rc else 'FAIL'} ({b.failed} failed checks)")
    return rc or (1 if b.failed else 0)


if __name__ == "__main__":
    sys.exit(main())
