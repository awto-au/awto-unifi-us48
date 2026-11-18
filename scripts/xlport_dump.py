#!/usr/bin/env python3
"""xlport_dump — read-only XLPORT / XLMAC register scan of unit 1 (block 5) for lanes 0 and 2, ours vs stock (#78).

Usage: xlport_dump.py ours|stock [--out FILE] | diff A B
Uses tools/tsc_read.c S lines (S-channel register reads); addresses: XLPORT core 0x2020000|idx<<8 (1 word), XLPORT per port 0x20000|idx<<8|lane,
XLMAC per port 0x60000|idx<<8|lane (2 words), idx 0..0x3f. Unimplemented registers answer NAK = `-`. Log: tmp/logs/xlport_dump.log
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tsc_lane_dump as d  # noqa: E402
import hwdb  # noqa: E402
from _cli import help_guard  # noqa: E402

ROOT = d.ROOT


def requests() -> list[tuple[int, int]]:
    out = [(1, 0x2020000 | i << 8) for i in range(0x40)]
    for lane in (0, 2):
        out += [(1, 0x20000 | i << 8 | lane) for i in range(0x40)]
        out += [(2, 0x60000 | i << 8 | lane) for i in range(0x40)]
    return out


def run(kind: str, out: Path) -> None:
    req = requests()
    txt = "".join(f"S {w} {a:x}\n" for w, a in req).encode()
    if kind == "stock":
        r = d.ssh("192.168.25.206", f"/tmp/tsc_read {hwdb.cmic_base(1):#010x}", stdin=txt, timeout=60)
        got = r.stdout.decode().splitlines()
    else:
        scratch = ROOT / "tmp/xlport_req.txt"
        scratch.write_bytes(txt)
        subprocess.run([sys.executable, str(ROOT / "scripts/lx.py"), "--put", str(scratch), "/tmp/xlreq.txt"], check=True, capture_output=True)
        r = subprocess.run([sys.executable, str(ROOT / "scripts/lx.py"), f"/tmp/tsc_read {hwdb.cmic_base(1):#010x} < /tmp/xlreq.txt"], capture_output=True, text=True)
        got = r.stdout.splitlines()
    lines = []
    for ln in got:
        t = ln.split()
        if t[0] == "S":
            lines.append(f"{int(t[2], 16):08x} " + ("-" if t[4] == "ffffffff" and t[5] == "ffffffff" else f"{int(t[4], 16):08x} {int(t[5], 16):08x}" if t[1] == "2" else f"{int(t[4], 16):08x}"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    d.log(f"{kind}: {len(lines)} regs -> {out}")


def main() -> int:
    a = sys.argv[1:]
    if a[:1] in (["ours"], ["stock"]):
        run(a[0], ROOT / (a[a.index("--out") + 1] if "--out" in a else f"tmp/tsc_dump/xlport-{a[0]}.txt"))
    elif a[:1] == ["diff"] and len(a) == 3:
        la = {l.split()[0]: l.split()[1:] for l in Path(a[1]).read_text().splitlines()}
        lb = {l.split()[0]: l.split()[1:] for l in Path(a[2]).read_text().splitlines()}
        n = 0
        for k in sorted(set(la) & set(lb)):
            if la[k] != lb[k]:
                n += 1
                print(f"{k}: {' '.join(la[k])} | {' '.join(lb[k])}")
        d.log(f"{n} differ of {len(set(la) & set(lb))}")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main())
