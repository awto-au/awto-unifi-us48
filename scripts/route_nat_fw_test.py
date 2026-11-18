#!/usr/bin/env python3
"""route_nat_fw_test: copper routing, NAT, firewall and software-flowtable tests on the loop cables (docs/route-nat-fw.md, #40).

Usage: route_nat_fw_test.py <setup|route|nat|fw|flowtable|teardown|all> [--apply] [--no-v6] [--mib N]
- Dry-run by default: prints the board commands, runs none. --apply = ssh to the board (root@192.168.25.207, as scripts/lx.py).
- Topology (loop cables front 8-17 on unit 0, front 32-41 on unit 1; ge1 = management is never named in a write command):
    host A (netns, ge8 10.48.10.2/24 gw .1) -- cable -- ge17 10.48.10.1 | router (root ns, forwarding) | ge41 10.48.20.1 -- cable -- ge32 host B (netns, 10.48.20.2/24 gw .1)
  v6: 2001:db8:48:10::/64 and 2001:db8:48:20::/64 (::1 router, ::2 hosts). No bridge anywhere; no switch-table writes.
- setup: snapshot (admin state, addresses, ipv6 disable, forwarding sysctls, nft ruleset) into /tmp/rnf/snapshot on the board,
  modprobe the netfilter modules, two namespace holders (busybox unshare -n), move ge8/ge32 in, addresses, routes, forwarding.
- route: ping both ways (v4, v6), 10 MiB TCP A->B and B->A with MB/s, packet counts per port (/proc/net/dev of each namespace).
- nat: nft masquerade on ge41 for 10.48.10.0/24; B must see the router (10.48.20.1), conntrack shows the SNAT tuple, 10 MiB both ways.
- fw: stateless drop of TCP 5001 A->B with ping/5002 allowed, counters prove drops and passes; then a stateful forward policy drop.
- flowtable: 10 MiB both ways for route and NAT, without and with the nftables software flowtable (flow add @ft), ratio per case.
- teardown: ge8/ge32 back to the root ns, holders killed, our nft tables deleted (ruleset flushed if it was empty before), forwarding
  and port state restored from the snapshot. Every test command also restores on error (try/finally); `all` ends with teardown.
- Time limits (ssh calls): 15 s per command; TCP transfers 30 s = 10 MiB at 0.35 MB/s (expected 5-20 MB/s on the A9, 1.25x of a 24 s
  worst case); expiry = the step fails, logged with the command and elapsed time, then restore runs. Waits poll, no sleeps.
- Exit: 0 all steps passed, 1 a check failed, 2 usage/setup problem. Log: tmp/logs/route_nat_fw_test.log
"""
import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/route_nat_fw_test.log"
BOARD = "root@192.168.25.207"  # ip-registry us48-boot
SSH = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
       "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=8"]
CALL_S = 15.0      # one board command: busybox/ip/nft take < 1 s each; 15 s covers a slow module load
XFER_S = 30.0      # 10 MiB TCP; see the docstring
STATE = "/tmp/rnf"
MODULES = ["nf_conntrack", "nf_nat", "nf_tables", "nft_ct", "nft_chain_nat", "nft_nat", "nft_masq", "nft_limit", "nft_log",
           "nft_reject", "nft_reject_inet", "nf_flow_table", "nf_flow_table_inet", "nft_flow_offload"]
PORTS = {"A": "ge8", "R1": "ge17", "R2": "ge41", "B": "ge32"}  # front 8, 17, 41, 32
NET = {"A": ("10.48.10.2", "2001:db8:48:10::2"), "R1": ("10.48.10.1", "2001:db8:48:10::1"),
       "R2": ("10.48.20.1", "2001:db8:48:20::1"), "B": ("10.48.20.2", "2001:db8:48:20::2")}
TABLES = ("rnf_nat", "rnf_fw", "rnf_ft")
PORT_RE = r"\bge(8|17|32|41)\b"


class Fail(Exception):
    pass


class Ctx:
    def __init__(self, apply: bool, v6: bool, mib: int):
        self.apply, self.v6, self.mib = apply, v6, mib
        self.failed = 0
        self.results: dict = {}

    def log(self, msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(line + "\n")
        print(line, flush=True)

    def sh(self, cmd: str, limit: float = CALL_S, check: bool = True, quiet: bool = False) -> str:
        """Run `cmd` on the board (dry-run: print it). Write commands may name only the four test ports, never ge1."""
        if re.search(r"\bge1\b", cmd):
            raise Fail(f"refused: command names ge1 (management): {cmd}")
        if not self.apply:
            self.log("DRY " + cmd.replace("\n", " ; "))
            return ""
        t0 = time.monotonic()
        try:
            r = subprocess.run(SSH + [BOARD, cmd], capture_output=True, text=True, timeout=limit, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise Fail(f"timeout after {limit:.0f} s (limit {limit:.0f}): {cmd[:200]}")
        out = (r.stdout + r.stderr).strip()
        if not quiet:
            self.log(f"$ {cmd.replace(chr(10), ' ; ')[:300]} rc={r.returncode} {time.monotonic() - t0:.1f}s" + (f"\n{out}" if out else ""))
        if check and r.returncode:
            raise Fail(f"rc={r.returncode}: {cmd[:200]}: {out[:300]}")
        return out

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        if not self.apply:
            return True
        self.log(f"{'PASS' if ok else 'FAIL'} {name}" + (f": {detail}" if detail else ""))
        if not ok:
            self.failed += 1
        return ok


def ns(which: str, cmd: str) -> str:
    """Command run in host A's or B's namespace through its holder pid."""
    return f"nsenter -t $(cat {STATE}/{which}.pid) -n {cmd}"


def snapshot_ports(ctx: Ctx) -> str:
    return ctx.sh("; ".join(f"echo {p}; ip -o link show {p}; ip -o addr show dev {p}; cat /proc/sys/net/ipv6/conf/{p}/disable_ipv6;"
                            f" cat /sys/class/net/{p}/master 2>/dev/null; true" for p in PORTS.values()), quiet=True)


def setup(ctx: Ctx) -> None:
    ctx.log("setup: snapshot, modules, namespaces, addresses, routes, forwarding")
    if ctx.apply and ctx.sh(f"test -e {STATE}/snapshot && echo yes || echo no", quiet=True) == "yes":
        raise Fail(f"{STATE}/snapshot exists: a setup is already active (run teardown first)")
    ctx.sh(f"mkdir -p {STATE}")
    # guard: none of the four ports may be enslaved to a bridge (never bridge two looped ports)
    for p in PORTS.values():
        out = ctx.sh(f"ls /sys/class/net/{p}/master 2>/dev/null; true", quiet=True)
        if out:
            raise Fail(f"{p} has a master device (bridge?): refusing")
    ctx.sh("{ for p in ge8 ge17 ge32 ge41; do echo \"link $p $(ip -o link show $p | grep -c '[<,]UP[,>]')\"; "
           "echo \"v6dis $p $(cat /proc/sys/net/ipv6/conf/$p/disable_ipv6)\"; "
           "ip -o addr show dev $p | sed \"s/^/addr $p /\"; done; "
           "for f in /proc/sys/net/ipv4/conf/ge17/forwarding /proc/sys/net/ipv4/conf/ge41/forwarding "
           "/proc/sys/net/ipv6/conf/ge17/force_forwarding /proc/sys/net/ipv6/conf/ge41/force_forwarding; do echo \"fwd $f $(cat $f)\"; done; "
           f"echo \"nftn $(nft list ruleset | wc -l)\"; }} > {STATE}/snapshot.tmp && mv {STATE}/snapshot.tmp {STATE}/snapshot")
    try:
        for m in MODULES:
            ctx.sh(f"modprobe {m}")
        for which in ("A", "B"):
            # unshare execs sleep (held until teardown kills it) in a new netns; its pid is the namespace handle (nsenter -t, ip link set ... netns PID)
            ctx.sh(f"setsid unshare -n sleep 2000000000 </dev/null >/dev/null 2>&1 & echo $! > {STATE}/{which}.pid")
            ctx.sh(f"test -d /proc/$(cat {STATE}/{which}.pid)/ns && echo ok")
        for which in ("A", "B"):
            p = PORTS[which]
            ctx.sh(f"ip link set {p} down; ip addr flush dev {p}; ip link set {p} netns $(cat {STATE}/{which}.pid)")
        for which, gw in (("A", 1), ("B", 1)):
            p = PORTS[which]
            v4, v6 = NET[which]
            sub = "10.48.10" if which == "A" else "10.48.20"
            cmds = ["ip link set lo up", f"ip link set {p} up", f"ip addr add {v4}/24 dev {p}", f"ip route add default via {sub}.{gw}"]
            if ctx.v6:
                pre = "2001:db8:48:10" if which == "A" else "2001:db8:48:20"
                cmds += [f"ip -6 addr add {v6}/64 dev {p} nodad", f"ip -6 route add default via {pre}::1"]
            for c in cmds:
                ctx.sh(ns(which, c))
        for which in ("R1", "R2"):
            p = PORTS[which]
            v4, v6 = NET[which]
            ctx.sh(f"ip link set {p} up; ip addr add {v4}/24 dev {p}")
            # per-port forwarding on the router ports only (v4 conf/<if>/forwarding, v6 conf/<if>/force_forwarding: ip6_forward()
            # accepts it on the ingress port); ip_forward, conf/all/* and every other port stay as they are
            ctx.sh(f"echo 1 > /proc/sys/net/ipv4/conf/{p}/forwarding")
            if ctx.v6:
                ctx.sh(f"echo 0 > /proc/sys/net/ipv6/conf/{p}/disable_ipv6; echo 1 > /proc/sys/net/ipv6/conf/{p}/force_forwarding; "
                       f"ip -6 addr add {v6}/64 dev {p} nodad")
        if ctx.apply:
            ctx.sh(ns("A", "ip link show ge8") + "; " + ns("B", "ip link show ge32"))
        ctx.log("setup done")
    except BaseException:
        ctx.log("setup failed: restoring")
        teardown(ctx)
        raise


def parse_dev(text: str) -> dict:
    """/proc/net/dev -> {if: (rx_pkts, tx_pkts)} for the four ports."""
    out = {}
    for ln in text.splitlines():
        m = re.match(r"\s*(ge\d+):\s*(.*)", ln)
        if m and re.fullmatch(PORT_RE, m.group(1)):
            f = m.group(2).split()
            out[m.group(1)] = (int(f[1]), int(f[9]))
    return out


def counters(ctx: Ctx) -> dict:
    d = {}
    if not ctx.apply:
        for w in ("A", "B", "R"):
            ctx.sh(ns(w, "cat /proc/net/dev") if w != "R" else "cat /proc/net/dev")
        return d
    d.update(parse_dev(ctx.sh("cat /proc/net/dev", quiet=True)))
    d.update(parse_dev(ctx.sh(ns("A", "cat /proc/net/dev"), quiet=True)))
    d.update(parse_dev(ctx.sh(ns("B", "cat /proc/net/dev"), quiet=True)))
    return d


def report_counts(ctx: Ctx, before: dict, label: str) -> dict:
    after = counters(ctx)
    delta = {p: (after[p][0] - before[p][0], after[p][1] - before[p][1]) for p in after if p in before}
    ctx.log(f"packets {label} (rx/tx per port): " + ", ".join(f"{p} {r}/{t}" for p, (r, t) in sorted(delta.items())))
    return delta


def uptime(ctx: Ctx, cmd_prefix: str = "") -> str:
    return "$(cut -d' ' -f1 /proc/uptime)"


def transfer(ctx: Ctx, src: str, dst: str, port: int, label: str) -> float:
    """MiB of zeros from host `src` to `dst` (A/B) over TCP; returns MB/s (1 MB = 1e6 B), receiver byte count verified."""
    n = ctx.mib * 16  # 64 KiB blocks
    dst_ip = NET[dst][0]
    rx = f"{STATE}/rx{port}.log"
    listen = f"sh -c 'nc -l -p {port} | dd of=/dev/null bs=64k count={n} iflag=fullblock'"
    ctx.sh(f"{ns(dst, listen)} > {rx} 2>&1 </dev/null & true", quiet=True)
    send = (f"sh -c 'a=$(cut -d\" \" -f1 /proc/uptime); dd if=/dev/zero bs=64k count={n} 2>/dev/null | nc {dst_ip} {port}; "
            f"b=$(cut -d\" \" -f1 /proc/uptime); echo T $a $b'")
    out = ctx.sh(ns(src, send), limit=XFER_S, quiet=True)
    if not ctx.apply:
        return 0.0
    got = ""
    for _ in range(5):  # receiver dd writes its summary after the sender returned; each read is one ssh round trip
        got = ctx.sh(f"cat {rx}", quiet=True)
        if re.search(r"\d+ bytes", got):
            break
    ctx.sh("pkill -f '[n]c -l -p'; true", quiet=True)
    m = re.search(r"T ([\d.]+) ([\d.]+)", out)
    b = re.search(r"(\d+) bytes", got)
    nbytes = int(b.group(1)) if b else 0
    ok = bool(m) and nbytes == ctx.mib * 1048576
    mbps = 0.0
    if m and float(m.group(2)) > float(m.group(1)):
        mbps = nbytes / 1e6 / (float(m.group(2)) - float(m.group(1)))
    ctx.check(f"{label} {src}->{dst} {ctx.mib} MiB", ok, f"{mbps:.2f} MB/s ({mbps * 8:.0f} Mbit/s), receiver {nbytes} B")
    return mbps


def both_ways(ctx: Ctx, label: str) -> tuple:
    return transfer(ctx, "A", "B", 5002, label), transfer(ctx, "B", "A", 5003, label)


def pings(ctx: Ctx, label: str) -> None:
    for src, dst in (("A", "B"), ("B", "A")):
        out = ctx.sh(ns(src, f"ping -c 3 -W 2 {NET[dst][0]}"), check=False, quiet=True)
        ctx.check(f"{label} ping v4 {src}->{dst}", "3 packets received" in out or " 0% packet loss" in out, out.splitlines()[-1] if out else "")
        if ctx.v6:
            out = ctx.sh(ns(src, f"ping6 -c 3 -w 6 {NET[dst][1]}"), check=False, quiet=True)
            ctx.check(f"{label} ping v6 {src}->{dst}", " 0% packet loss" in out, out.splitlines()[-1] if out else "")


def nft(ctx: Ctx, text: str) -> None:
    ctx.sh(f"nft -f - <<'EOF'\n{text}\nEOF")


def del_tables(ctx: Ctx) -> None:
    for t in TABLES:
        for fam in ("ip", "inet"):
            ctx.sh(f"nft delete table {fam} {t} 2>/dev/null; true", quiet=True)


def counter_val(ctx: Ctx, table: str, name: str) -> int:
    out = ctx.sh(f"nft list counter inet {table} {name}", quiet=True)
    m = re.search(r"packets (\d+)", out)
    return int(m.group(1)) if m else -1


def route(ctx: Ctx) -> None:
    ctx.log("route test (no netfilter rules)")
    pings(ctx, "route")
    before = counters(ctx)
    a, b = both_ways(ctx, "route")
    delta = report_counts(ctx, before, "route 2x transfer + pings")
    ctx.check("route packets crossed all four ports", ctx.apply and all(p in delta and delta[p][0] > 0 and delta[p][1] > 0 for p in PORTS.values()) or not ctx.apply)
    ctx.results["route"] = (a, b)


def nat(ctx: Ctx) -> None:
    ctx.log("nat test: masquerade on ge41 for 10.48.10.0/24")
    try:
        nft(ctx, "table ip rnf_nat {\n chain post { type nat hook postrouting priority srcnat; policy accept;\n"
                 "  oifname \"ge41\" ip saddr 10.48.10.0/24 counter masquerade }\n }")
        # hold one A->B connection open so B's socket table shows the peer address it sees
        ctx.sh(ns("B", "sh -c 'nc -l -p 5004 > /dev/null 2>&1 &'") + "; true", quiet=True)
        ctx.sh(ns("A", "sh -c 'tail -f /dev/null | nc 10.48.20.2 5004 > /dev/null 2>&1 &'") + "; true", quiet=True)
        tcp = ctx.sh(ns("B", "cat /proc/net/tcp"), quiet=True)
        seen = any(f[2].startswith("0114300A:") and f[3] == "01" for f in (ln.split() for ln in tcp.splitlines()[1:]) if len(f) > 3)
        ctx.check("nat: B sees the router 10.48.20.1 as peer of A's connection", seen)
        ct = ctx.sh("cat /proc/net/nf_conntrack", quiet=True)
        line = next((ln for ln in ct.splitlines() if "dport=5004" in ln), "")
        ctx.check("nat: conntrack has the SNAT tuple (reply dst 10.48.20.1)", "src=10.48.10.2" in line and "dst=10.48.20.1" in line, line[:160])
        n = ctx.sh("cat /proc/sys/net/netfilter/nf_conntrack_count", quiet=True)
        ctx.log(f"conntrack entries: {n}")
        before = counters(ctx)
        a, b = both_ways(ctx, "nat")
        report_counts(ctx, before, "nat 2x transfer")
        ctx.log("nat counters: " + ctx.sh("nft list table ip rnf_nat | grep counter", check=False, quiet=True))
        ctx.results["nat"] = (a, b)
    finally:
        ctx.sh("pkill -f '[n]c -l -p 5004'; pkill -f '[n]c 10.48.20.2 5004'; pkill -f '[t]ail -f /dev/null'; true", quiet=True)
        del_tables(ctx)


def attempt(ctx: Ctx, src: str, dst: str, port: int) -> bool:
    """One TCP connect with a 3 s busybox nc connect/idle limit (a drop rule leaves the SYN unanswered)."""
    ctx.sh(ns(dst, f"sh -c 'nc -l -p {port} > /dev/null 2>&1 &'") + "; true", quiet=True)
    out = ctx.sh(ns(src, f"sh -c 'echo x | nc -w 3 {NET[dst][0]} {port}; echo rc=$?'"), limit=10.0, check=False, quiet=True)
    ctx.sh(f"pkill -f '[n]c -l -p {port}'; true", quiet=True)
    return "rc=0" in out


def fw(ctx: Ctx) -> None:
    ctx.log("fw test, part 1: stateless")
    try:
        nft(ctx, """table inet rnf_fw {
 counter c_icmp { }
 counter c_drop { }
 counter c_pass { }
 chain fwd { type filter hook forward priority filter; policy accept;
  meta l4proto { icmp, ipv6-icmp } counter name c_icmp accept
  iifname "ge17" oifname "ge41" tcp dport 5001 counter name c_drop drop
  tcp dport 5002 counter name c_pass accept
 }
}""")
        pings(ctx, "fw")
        icmp = counter_val(ctx, "rnf_fw", "c_icmp")
        ctx.check("fw: ping passes and is counted", icmp >= 6 if ctx.apply else True, f"c_icmp {icmp}")
        ok = attempt(ctx, "A", "B", 5001)
        drops = counter_val(ctx, "rnf_fw", "c_drop")
        ctx.check("fw: TCP 5001 A->B dropped", (not ok) and drops >= 1 if ctx.apply else True, f"connected={ok}, c_drop {drops}")
        ok = attempt(ctx, "A", "B", 5002)
        passes = counter_val(ctx, "rnf_fw", "c_pass")
        ctx.check("fw: TCP 5002 A->B passes", ok and passes >= 1 if ctx.apply else True, f"connected={ok}, c_pass {passes}")
        del_tables(ctx)
        ctx.log("fw test, part 2: stateful (policy drop, established/related accepted, new only A->B tcp 5002 + ping)")
        nft(ctx, """table inet rnf_fw {
 counter c_est { }
 counter c_new_ok { }
 counter c_drop_rev { }
 counter c_invalid { }
 chain fwd { type filter hook forward priority filter; policy drop;
  ct state invalid counter name c_invalid drop
  ct state established,related counter name c_est accept
  iifname "ge17" oifname "ge41" ct state new tcp dport 5002 counter name c_new_ok accept
  iifname "ge17" oifname "ge41" ct state new meta l4proto { icmp, ipv6-icmp } counter name c_new_ok accept
  iifname "ge41" oifname "ge17" ct state new counter name c_drop_rev drop
 }
}""")
        ok = attempt(ctx, "A", "B", 5002)
        ctx.check("fw stateful: A->B new TCP 5002 accepted (replies ride on ct established)", ok if ctx.apply else True,
                  f"c_new_ok {counter_val(ctx, 'rnf_fw', 'c_new_ok')}, c_est {counter_val(ctx, 'rnf_fw', 'c_est')}")
        ok = attempt(ctx, "B", "A", 5003)
        ctx.check("fw stateful: B->A new TCP dropped", (not ok) if ctx.apply else True,
                  f"connected={ok}, c_drop_rev {counter_val(ctx, 'rnf_fw', 'c_drop_rev')}")
        out = ctx.sh(ns("A", f"ping -c 2 -W 2 {NET['B'][0]}"), check=False, quiet=True)
        ctx.check("fw stateful: ping A->B ok", " 0% packet loss" in out if ctx.apply else True)
        out = ctx.sh(ns("B", f"ping -c 2 -W 2 {NET['A'][0]}"), check=False, quiet=True)
        ctx.check("fw stateful: ping B->A dropped", "100% packet loss" in out if ctx.apply else True)
        ctx.log("fw counters: " + ctx.sh("nft list table inet rnf_fw | grep -A2 counter", check=False, quiet=True).replace("\n", " "))
    finally:
        ctx.sh("pkill -f '[n]c -l -p'; true", quiet=True)
        del_tables(ctx)


FT = """table inet rnf_ft {
 flowtable ft { hook ingress priority 0; devices = { ge17, ge41 }; }
 chain fwd { type filter hook forward priority filter; policy accept;
  meta l4proto { tcp, udp } flow add @ft
 }
}"""
NAT = ("table ip rnf_nat {\n chain post { type nat hook postrouting priority srcnat; policy accept;\n"
       "  oifname \"ge41\" ip saddr 10.48.10.0/24 masquerade }\n }")


def flowtable(ctx: Ctx) -> None:
    ctx.log("flowtable test: route and NAT, 10 MiB both ways, software flowtable off/on")
    res = {}
    try:
        for mode in ("route", "nat"):
            for ft in (False, True):
                del_tables(ctx)
                if mode == "nat":
                    nft(ctx, NAT)
                if ft:
                    nft(ctx, FT)
                key = f"{mode}{'+flowtable' if ft else ''}"
                a, b = both_ways(ctx, key)
                res[(mode, ft)] = (a, b)
                if ft and ctx.apply:
                    ct = ctx.sh("grep -c OFFLOAD /proc/net/nf_conntrack; true", quiet=True)
                    ctx.log(f"{key}: conntrack entries flagged OFFLOAD at the end: {ct}")
        if ctx.apply:
            for mode in ("route", "nat"):
                off, on = res[(mode, False)], res[(mode, True)]
                for i, d in enumerate(("A->B", "B->A")):
                    ratio = on[i] / off[i] if off[i] else 0.0
                    ctx.log(f"RESULT {mode} {d}: no flowtable {off[i]:.2f} MB/s, flowtable {on[i]:.2f} MB/s, x{ratio:.2f}")
    finally:
        del_tables(ctx)
        ctx.sh("pkill -f '[n]c -l -p'; true", quiet=True)


def teardown(ctx: Ctx) -> None:
    ctx.log("teardown: ports back to the root namespace, nft tables removed, state restored from the snapshot")
    errs = []

    def step(cmd: str) -> str:
        try:
            return ctx.sh(cmd, check=False)
        except Fail as e:
            errs.append(str(e))
            return ""

    step("pkill -f '[n]c -l -p'; pkill -f '[n]c 10.48'; pkill -f '[t]ail -f /dev/null'; true")
    del_tables(ctx)
    snap = ctx.sh(f"cat {STATE}/snapshot 2>/dev/null; true", quiet=True) if ctx.apply else ""
    if ctx.apply and not snap:
        ctx.log("no snapshot on the board: nothing to restore (setup never ran or teardown already done)")
        return
    for which in ("A", "B"):
        p = PORTS[which]
        step(f"test -e {STATE}/{which}.pid && nsenter -t $(cat {STATE}/{which}.pid) -n sh -c 'ip link set {p} down; ip addr flush dev {p}; "
             f"ip link set {p} netns 1'; true")
    for _ in range(10):  # poll: the move is synchronous, the check is the proof
        have = ctx.sh("ls /sys/class/net | grep -c '^ge8$\\|^ge32$'", check=False, quiet=True) if ctx.apply else "2"
        if have.strip() == "2":
            break
    for which in ("A", "B"):
        step(f"test -e {STATE}/{which}.pid && kill $(cat {STATE}/{which}.pid); rm -f {STATE}/{which}.pid; true")
    # restore ports
    for p in PORTS.values():
        step(f"ip addr flush dev {p}; ip link set {p} down")
    for ln in snap.splitlines():
        f = ln.split(None, 3)
        if f[0] == "v6dis" and len(f) >= 3:
            step(f"echo {f[2]} > /proc/sys/net/ipv6/conf/{f[1]}/disable_ipv6")
        elif f[0] == "addr" and len(f) >= 4:
            m = re.search(r"inet6? (\S+)", ln)
            if m and "scope link" not in ln:
                step(f"ip addr add {m.group(1)} dev {f[1]}")
        elif f[0] == "link" and len(f) >= 3 and f[2] != "0":
            step(f"ip link set {f[1]} up")
        elif f[0] == "fwd" and len(f) >= 3:
            step(f"echo {f[2]} > {f[1]}")
        elif f[0] == "nftn" and f[1] == "0":
            step("nft flush ruleset")
    step(f"rm -f {STATE}/snapshot {STATE}/rx*.log; rmdir {STATE} 2>/dev/null; true")
    if ctx.apply:
        out = ctx.sh("for p in ge8 ge17 ge32 ge41; do echo $p $(ip -o link show $p | grep -c 'state UP'); done; nft list ruleset | wc -l; ip netns list 2>/dev/null; true", quiet=True)
        ctx.log("after teardown: " + out.replace("\n", " | "))
    if errs:
        raise Fail("teardown errors: " + "; ".join(errs))
    ctx.log("teardown done")


def run_all(ctx: Ctx) -> None:
    try:
        setup(ctx)
        for t in (route, nat, fw, flowtable):
            t(ctx)
    finally:
        teardown(ctx)


CMDS = {"setup": setup, "route": route, "nat": nat, "fw": fw, "flowtable": flowtable, "teardown": teardown, "all": run_all}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=sorted(CMDS))
    ap.add_argument("--apply", action="store_true", help="run on the board (default: print the commands)")
    ap.add_argument("--no-v6", action="store_true", help="IPv4 only (no IPv6 forwarding write)")
    ap.add_argument("--mib", type=int, default=10, help="TCP transfer size in MiB (default 10)")
    a = ap.parse_args()
    ctx = Ctx(a.apply, not a.no_v6, a.mib)
    ctx.log(f"{a.cmd} {'APPLY' if a.apply else 'dry-run'} v6={ctx.v6} mib={a.mib}")
    try:
        CMDS[a.cmd](ctx)
    except Fail as e:
        ctx.log(f"FAILED: {e}")
        return 2 if a.cmd in ("setup", "teardown") else 1
    return 1 if ctx.failed else 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
