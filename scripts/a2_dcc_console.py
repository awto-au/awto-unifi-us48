#!/usr/bin/env python3
"""a2_dcc_console — read a2's DCC console (Linux hvc0/earlycon=dcc) through the uboot-2 console. RE/bring-up tool.

- a2 writes DBGDTRTX (CP14) and stalls while DSCR.DTRTXfull is set; this drains it: one DSCR+DTRTX
  read pair is `md.l <DSCR> 2` (DSCR first, so TXfull is sampled before the read that clears it).
- 40 pairs per console line (U-Boot line limit ~1 KiB): ~0.3 s round trip per up to 40 characters.
  The product path is the us48-a2 remoteproc tty/debugfs reader (docs/a2-design.md), not this script.
- The a2 side needs this running within ~100 ms of hvc_dcc_check(), else hvc0 is never registered (docs/a2-linux.md).

Usage (board at uboot-2): a2_dcc_console.py [--polls N] [--send TEXT]
  --polls N   console lines to run (default 100 = ~30 s, 4000 reads); stops early after IDLE_STOP empty lines past the first char
  --send T    write T into DTRRX (a2 stdin) one char at a time, whenever DSCR.DTRRXfull is clear
Output: stdout + tmp/logs/a2_dcc_console.txt (raw console), tmp/logs/a2_dcc_console.log.
"""
import argparse
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import console  # noqa: E402
import hwdb  # noqa: E402
from a2_loader import A2, W, log  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent
OUT = REPO / "tmp/logs/a2_dcc_console.txt"
DB = hwdb.default()
CS = DB.addr("cs_debug0")
DSCR, DTRRX, DTRTX = (DB.addr("cs_debug0." + r) - CS for r in ("DSCR", "DTRRX", "DTRTX"))
TXFULL, RXFULL = 1 << 29, 1 << 30     # DSCR.DTRTXfull / DTRRXfull (regdb IHOST_A9CPU0_DSCR)
PAIRS = 40
IDLE_STOP = 20                         # 20 lines x ~0.3 s = 6 s of silence after output = a2 quiet/blocked, stop


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--polls", type=int, default=100)
    ap.add_argument("--send", default="")
    a = ap.parse_args()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    send = list(a.send.encode())
    got = idle = 0
    with console.Session() as s, OUT.open("ab") as raw:
        s.write(b"\x03\r\n")
        s.read_idle(1.0, 3.0)
        a2 = A2(s)
        try:
            a2.page(CS)
            a2.wr(0xFB0, 0xC5ACCE55)   # LAR unlock
            line = ";".join(["md.l 0x%x 2" % (W + DSCR)] * PAIRS)
            for _ in range(a.polls):
                r = a2.cmd(line, 0.25)
                n = 0
                for ln in r.splitlines():
                    m = re.match(r"\s*0800[0-9a-f]{4}:\s+([0-9a-f]{8}) ([0-9a-f]{8})", ln)
                    if m and int(m.group(1), 16) & TXFULL:
                        b = int(m.group(2), 16) & 0xFF
                        raw.write(bytes([b]))
                        sys.stdout.write(chr(b))
                        n += 1
                sys.stdout.flush()
                got += n
                idle = 0 if n else idle + 1
                if send and not (a2.rd(DSCR)[0] & RXFULL):
                    a2.wr(DTRRX, send.pop(0))
                if got and idle >= IDLE_STOP and not send:
                    break
            log("dcc console: %d chars, ended %s" % (got, time.strftime("%Y-%m-%dT%H:%M:%S%z")))
        finally:
            a2.cmd("mw.l 0x%x 0x18032001" % 0x08002C1C)   # restore IMAP0_7 (as a2_loader)


if __name__ == "__main__":
    main()
