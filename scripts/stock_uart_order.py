#!/usr/bin/env python3
"""stock_uart_order — which HR2 UART the stock UniFi kernel registers first (= ttyS0).

- Scans a decompressed stock kernel (machine-ID, no DT) for the plat_serial8250_port table:
  little-endian words 0x18000300 (UART0) / 0x18000400 (UART1) within one struct stride of each other.
- Prints each pair in file order; the first base in a pair is ttyS0.
- Log: tmp/logs/stock_uart_order.log

Usage: stock_uart_order.py <decompressed-kernel.img>
"""

import argparse
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/stock_uart_order.log"
UARTS = {0x18000300: "UART0", 0x18000400: "UART1"}
MAX_STRIDE = 128  # plat_serial8250_port on 3.6 ARM is ~60 bytes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("kernel", type=Path)
    data = ap.parse_args().kernel.read_bytes()
    hits = []
    for base in UARTS:
        needle = struct.pack("<I", base)
        i = data.find(needle)
        while i != -1:
            if i % 4 == 0:
                hits.append((i, base))
            i = data.find(needle, i + 1)
    hits.sort()
    out = [f"{len(hits)} aligned hits: " + ", ".join(f"0x{o:x}={UARTS[b]}" for o, b in hits)]
    for (o1, b1), (o2, b2) in zip(hits, hits[1:]):
        if b1 != b2 and o2 - o1 <= MAX_STRIDE:
            out.append(f"pair @0x{o1:x} stride {o2 - o1}: {UARTS[b1]} then {UARTS[b2]} -> ttyS0 = {UARTS[b1]} (0x{b1:08x})")
    report = "\n".join(out)
    print(report)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n{report}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
