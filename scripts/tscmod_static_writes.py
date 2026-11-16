#!/usr/bin/env python3
"""tscmod_static_writes: static TSC register-access map of stock switchdrvr's phy_tscmod code (#78). Host-only, no board.

Usage: scripts/tscmod_static_writes.py [--c tmp/ghidra/out/switchdrvr/switchdrvr.c] [--out hwdb/fw/tscmod_static_writes.txt]
- Source: Ghidra decompile of switchdrvr (private, not committed). Helpers: FUN_0119e954 = TSC write, FUN_0119ea50 = TSC modify
  (reg, val, mask), FUN_0119e774 = read. Only calls with a constant register are listed; source order, not runtime order.
- Names: TSC_REG_* macros in /vendors/broadcom/openbcm/sdk-6.5.16-tscmod/tscmod_include_tscmod.h (reg -> macro name).
- Diff: registers touched by stock but never named (4-digit hex) in kernel/us48-cmic/us48_tsc_init.h, us48_cmic_core.c, us48_cmic_xe_main.c, us48_tsc_fw.c.
Log: tmp/logs/tscmod_static_writes.log
"""
import argparse
import re
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HDR = Path("/vendors/broadcom/openbcm/sdk-6.5.16-tscmod/tscmod_include_tscmod.h")
HELP = {"FUN_0119e954": "W", "FUN_0119ea50": "M", "FUN_0119e774": "R"}
LO, HI = 0x01182000, 0x011c0000      # phy_tscmod_* names.tsv span 0x01182fa4..0x011b16bc


def names():
    out, cur = {}, None
    for ln in HDR.read_text(errors="replace").splitlines():
        m = re.match(r"#define (?:READ|WRITE|MODIFY)_(\w+?)r\(", ln)
        if m:
            cur = m.group(1)
            continue
        m = re.search(r"TSC_REG_(?:READ|WRITE|MODIFY)\(.*?0x[0-9a-f]+, 0x0*([0-9a-f]+),", ln)
        if m and cur:
            out.setdefault(int(m.group(1), 16), cur)
            cur = None
    return out


def split_args(s):
    args, d, cur = [], 0, ""
    for ch in s:
        if ch in "([":
            d += 1
        elif ch in ")]":
            d -= 1
        if ch == "," and d == 0:
            args.append(cur.strip()); cur = ""
        else:
            cur += ch
    args.append(cur.strip())
    return args


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--c", default=str(ROOT / "tmp/ghidra/out/switchdrvr/switchdrvr.c"))
    ap.add_argument("--out", default=str(ROOT / "hwdb/fw/tscmod_static_writes.txt"))
    a = ap.parse_args()
    nm = names()
    fnames = {}
    for ln in (Path(a.c).parent / "switchdrvr.names.tsv").read_text().splitlines():
        p = ln.split("\t")
        if len(p) > 1:
            fnames[p[0]] = p[1]
    text = Path(a.c).read_text(errors="replace")
    parts = re.split(r"^// ==== (FUN_[0-9a-f]+) @ ([0-9a-f]+) ====$", text, flags=re.M)
    res = OrderedDict()
    for i in range(1, len(parts), 3):
        fn, addr, body = parts[i], int(parts[i + 1], 16), parts[i + 2]
        if not LO <= addr < HI:
            continue
        for m in re.finditer(r"(FUN_0119e954|FUN_0119ea50|FUN_0119e774)\((.*?)\);", body, flags=re.S):
            args = split_args(m.group(2))
            if len(args) < 3 or not re.fullmatch(r"0x[0-9a-f]+|\d+", args[2]):
                continue
            reg = int(args[2], 0)
            res.setdefault((fn, fnames.get(fn, fn)), []).append(
                (HELP[m.group(1)], reg, nm.get(reg, ""), [x if re.fullmatch(r"0x[0-9a-f]+|\d+", x) else "expr" for x in args[3:5]]))
    ours = set()
    for f in ("us48_tsc_init.h", "us48_cmic_core.c", "us48_cmic_xe_main.c", "us48_tsc_fw.c"):
        ours |= {int(x, 16) for x in re.findall(r"\b(0x[0-9a-f]{4})\b", (ROOT / "kernel/us48-cmic" / f).read_text())}
    lines = ["# stock switchdrvr phy_tscmod register accesses with constant registers (static, source order)",
             "# op: W write, M modify (val, mask), R read; '*' = reg not in our init tables"]
    stock_wr = set()
    for (fn, name), ops in res.items():
        wr = [o for o in ops if o[0] != "R"]
        if not wr:
            continue
        lines.append(f"\n## {name} ({fn})")
        for op, reg, rn, rest in wr:
            stock_wr.add(reg)
            lines.append(f"{op} {reg:#06x} {'*' if reg not in ours else ' '} {rn} {' '.join(rest)}")
    lines.append("\n## summary: stock-written registers not in our init tables")
    lines += [f"{r:#06x} {nm.get(r, '')}" for r in sorted(stock_wr - ours)]
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text("\n".join(lines) + "\n")
    (ROOT / "tmp/logs").mkdir(parents=True, exist_ok=True)
    (ROOT / "tmp/logs/tscmod_static_writes.log").write_text(f"{len(res)} functions, {len(stock_wr)} regs written, {len(stock_wr - ours)} not in ours\n")
    print(f"{len(res)} functions, {len(stock_wr)} written regs, {len(stock_wr - ours)} not in our tables -> {a.out}")


main()
