#!/usr/bin/env python3
"""Decode HR2 CPU-port meter (MAXBUCKETCONFIG) and CPU_COS_MAP from the stock schan capture.

Offline: reads regdb/hr2_regs.tsv, regdb/hr2_mems.tsv and tmp/schan_stock_writes.txt. Stdlib only.
Output: stdout + tmp/logs/cpu_port_policy.log. Docs: docs/cpu-port-policy.md (#71).
"""
import logging
import os
import pathlib
import re
import subprocess
import sys
from _cli import help_guard  # noqa: E402

LOG_DIR = None


def repo_root():
    env = os.environ.get("US48_REPO")
    if env:
        return pathlib.Path(env)
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                             cwd=pathlib.Path(__file__).resolve().parent, check=True)
        return pathlib.Path(out.stdout.strip())
    except Exception:
        return pathlib.Path(__file__).resolve().parent.parent


def regdb_fields(root, fname, name):
    """Return {field: (lsb, width)} for a regdb row (tsv, last column = fields)."""
    for line in (root / "regdb" / fname).read_text().splitlines():
        cols = line.split("\t")
        if len(cols) >= 2 and cols[1] == name:
            return {f.split(":")[0]: (int(f.split(":")[1]), int(f.split(":")[2])) for f in cols[-1].split(",")}
    raise SystemExit(f"{name} not in regdb/{fname}")


def bits(v, lsb, width):
    return (v >> lsb) & ((1 << width) - 1)


def decode(v, fields):
    return {k: bits(v, lsb, w) for k, (lsb, w) in fields.items()}


def main():
    root = repo_root()
    log_path = root / "tmp" / "logs" / "cpu_port_policy.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.FileHandler(log_path, mode="w"), logging.StreamHandler(sys.stdout)])
    log = logging.info

    mb = regdb_fields(root, "hr2_regs.tsv", "MAXBUCKETCONFIG")
    minb = regdb_fields(root, "hr2_regs.tsv", "MINBUCKETCONFIG")
    cmap = regdb_fields(root, "hr2_mems.tsv", "CPU_COS_MAP")
    log(f"MAXBUCKETCONFIG fields {mb}")
    log(f"MINBUCKETCONFIG fields {minb}")

    # Stock writes: REGW blk12 <addr> xN <name?> <word0>. Port = addr & 0xff, CoS = (addr >> 8) - 0x19c.
    cap = root / "tmp" / "schan_stock_writes.txt"
    rx_reg = re.compile(r"u0 REGW blk12 0x([0-9a-f]{8}) x\d+\s+(?:\S+\s+)?([0-9a-f]{8})\s*$")
    stock = {}
    for line in cap.read_text().splitlines():
        m = rx_reg.search(line)
        if not m:
            continue
        addr, word = int(m.group(1), 16), int(m.group(2), 16)
        if 0x19c00 <= (addr & ~0xff) <= 0x1a400 and (addr >> 8) in range(0x19c, 0x1a5):
            stock[(addr & 0xff, (addr >> 8) - 0x19c)] = word
    log(f"stock MAXBUCKETCONFIG writes, port 0 (addr, cos -> value), from {cap.name}:")
    for cos in sorted(c for p, c in stock if p == 0):
        v = stock[(0, cos)]
        d = decode(v, mb)
        log(f"  cos{cos} 0x{v:08x} " + " ".join(f"{k}={x}" for k, x in d.items()))

    # CPU_COS_MAP: MEMW blk10 ... CPU_COS_MAP[n] w0 w1 w2 w3 w4 (142-bit entry in 5 words, u0 only).
    rx_map = re.compile(r"u0 MEMW blk10 0x444c[0-9a-f]{4} x1\s+CPU_COS_MAP\[(\d+)\]\s+((?:[0-9a-f]{8}\s*){5})")
    entries = {}
    for line in cap.read_text().splitlines():
        m = rx_map.search(line)
        if m:
            entries[int(m.group(1))] = [int(w, 16) for w in m.group(2).split()]
    log(f"CPU_COS_MAP entries captured: {len(entries)} (valid: "
        f"{sum(1 for w in entries.values() if bits(w[0], 0, 1))})")
    cos_use = {}
    for idx in sorted(entries):
        w = entries[idx]
        flat = w[0] | (w[1] << 32) | (w[2] << 64) | (w[3] << 96) | (w[4] << 128)
        d = decode(flat, cmap)
        if not d["VALID"]:
            continue
        cos_use.setdefault(d["COS"], []).append(idx)
        reasons = (d["REASONS_KEY_HIGH"] << 32) | d["REASONS_KEY_LOW"]
        rmask = (d["REASONS_MASK_HIGH"] << 32) | d["REASONS_MASK_LOW"]
        rbits = [i for i in range(52) if (reasons >> i) & 1]
        log(f"  [{idx}] COS={d['COS']} SW_PKT_TYPE={d['SW_PKT_TYPE_KEY']}/{d['SW_PKT_TYPE_MASK']:x} "
            f"INT_PRI={d['INT_PRI_KEY']}/{d['INT_PRI_MASK']:x} MIRR={d['MIRR_PKT_KEY']} "
            f"reason_key_bits={rbits} reason_mask=0x{rmask:x}")
    log("CoS -> CPU_COS_MAP entries (first match wins in index order):")
    for c in sorted(cos_use):
        log(f"  COS{c}: {cos_use[c]}")


if __name__ == "__main__":
    help_guard(__doc__)
    main()
