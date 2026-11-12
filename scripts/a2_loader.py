#!/usr/bin/env python3
"""Host loader for a2 (unit 1 Cortex-A9): start the SRAM stub, then load/run code and peek/poke a2's bus view.

Usage (board at uboot-2, US48_HOST=lizard):
  a2_loader.py start                 halt a2 via CoreSight, inject jump to the stub (tmp/a2/a2_stub.bin), wait for ready
  a2_loader.py status                ready magic, seq, heartbeat (heartbeat moving = stub alive)
  a2_loader.py read ADDR [N]         a2 reads N words at ADDR in ITS address map
  a2_loader.py write ADDR VALUE      a2 writes one word
  a2_loader.py load FILE [ADDR]      host writes FILE into SRAM (default 0x1b000200)
  a2_loader.py call ADDR [A B]       a2 calls ADDR(A, B); prints r0
  a2_loader.py run FILE [A B]        load at 0x1b000200 and call it
Host path to unit 1: PAXB IMAP0_7 (BAR0+0x2c1c) + 4 KiB window at BAR0+0x7000, restored on exit. Never touches a1.
Waits: one U-Boot console round trip is ~0.3 s; the stub answers in microseconds, so an ack is awaited for
POLLS x ~0.3 s = 6 s (payload run time is unknown; on expiry the error names the command and mailbox state).
Log: tmp/logs/a2_loader.log. Layout/protocol: tools/a2_stub.S, docs/sram.md.
"""
import pathlib
import re
import struct
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import console  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent
LOG = REPO / "tmp/logs/a2_loader.log"
STUB = REPO / "tmp/a2/a2_stub.bin"
W, IM = 0x08007000, 0x08002C1C           # data window, IMAP0_7 (host side, stock-enumerated BARs)
SRAM, MBOX, PAYLOAD = 0x1B000000, 0x1B000100, 0x1B000200
MAGIC = 0x57B10ADE
DSCR, ITR, DTRRX, DRCR = 0x88, 0x84, 0x80, 0x90
POLLS = 20


def log(*a):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    with LOG.open("a") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%S%z ") + msg + "\n")


class A2:
    def __init__(self, s):
        self.s = s

    def cmd(self, c, w=0.3):
        self.s.send_line(c)
        return self.s.read_idle(w, 3.0).decode(errors="replace")

    def page(self, axi):
        self.cmd("mw.l 0x%x %#x" % (IM, axi | 1), 0.2)

    def rd(self, off, n=1):
        r = self.cmd("md.l 0x%x %x" % (W + off, n))
        o = []
        for ln in r.splitlines():
            m = re.match(r"\s*0800[0-9a-f]{4}:((?: [0-9a-f]{8})+)", ln)
            if m:
                o += [int(x, 16) for x in m.group(1).split()]
        return o[:n]

    def wr(self, off, v):
        self.cmd("mw.l 0x%x 0x%08x" % (W + off, v), 0.15)

    def mbox(self):
        self.page(MBOX & ~0xFFF)
        return self.rd(MBOX & 0xFFF, 8)          # cmd arg0 arg1 arg2 result seq heartbeat magic

    def command(self, cmd, a0=0, a1=0, a2=0):
        self.page(MBOX & ~0xFFF)
        base = MBOX & 0xFFF
        seq0 = self.rd(base + 20)[0]
        for i, v in enumerate((a0, a1, a2)):
            self.wr(base + 4 + 4 * i, v & 0xFFFFFFFF)
        self.wr(base, cmd)                        # cmd last
        for _ in range(POLLS):
            if self.rd(base + 20)[0] != seq0:
                return self.rd(base + 16)[0]
        raise SystemExit("a2 did not ack cmd %d within %d polls: mailbox %s" % (cmd, POLLS, [hex(x) for x in self.mbox()]))

    def itr(self, ins):
        self.wr(ITR, ins)
        return any(self.rd(DSCR)[0] & (1 << 24) for _ in range(10))

    def start(self):
        prog = open(STUB, "rb").read()
        prog += b"\0" * (-len(prog) % 4)
        self.page(SRAM)
        for i, w in enumerate(struct.unpack("<%dI" % (len(prog) // 4), prog)):
            self.wr(4 * i, w)
        for off in range(0, 0x20, 4):
            self.wr(0x100 + off, 0)
        self.page(0x19010000)
        self.wr(0xFB0, 0xC5ACCE55)
        d = self.rd(DSCR)[0]
        self.wr(DSCR, d | (1 << 14))
        self.wr(DRCR, 1)
        for _ in range(10):
            d = self.rd(DSCR)[0]
            if d & 1:
                break
        else:
            raise SystemExit("a2 did not halt (DSCR %#x)" % d)
        self.wr(DSCR, d | (1 << 13) | (1 << 14))
        self.wr(DTRRX, SRAM)
        ok = self.itr(0xEE100E15) and self.itr(0xE1A0F000)   # mrc p14 DTRRX->r0 ; mov pc,r0
        d = self.rd(DSCR)[0]
        self.wr(DSCR, d & ~(1 << 13))
        self.wr(DRCR, 2 | 4)
        if not ok:
            raise SystemExit("ITR injection did not complete; a2 left halted: pulse ARM_RST_N to recover")
        for _ in range(10):
            m = self.mbox()
            if m[7] == MAGIC:
                log("a2 stub ready, heartbeat", hex(m[6]))
                return
        raise SystemExit("stub did not report ready: mailbox %s" % [hex(x) for x in m])

    def load(self, data, addr):
        data += b"\0" * (-len(data) % 4)
        words = struct.unpack("<%dI" % (len(data) // 4), data)
        cur = None
        for i, w in enumerate(words):
            a = addr + 4 * i
            if a >> 12 != cur:
                cur = a >> 12
                self.page(cur << 12)
            self.wr(a & 0xFFF, w)
        log("loaded %d bytes at %#x" % (len(data), addr))


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    op, args = sys.argv[1], sys.argv[2:]
    with console.Session() as s:
        s.write(b"\x03\r\n")
        s.read_idle(1.0, 3.0)
        a2 = A2(s)
        try:
            if op == "start":
                a2.start()
            elif op == "status":
                m = a2.mbox()
                time.sleep(0.5) if False else None
                m2 = a2.mbox()
                log("magic", hex(m[7]), "seq", m[5], "heartbeat", m[6], "->", m2[6], "(moving)" if m2[6] != m[6] else "(STOPPED)")
            elif op == "read":
                addr, n = int(args[0], 0), int(args[1], 0) if len(args) > 1 else 1
                for i in range(n):
                    log("%08x: %08x" % (addr + 4 * i, a2.command(2, addr + 4 * i)))
            elif op == "write":
                log("write rc", a2.command(3, int(args[0], 0), int(args[1], 0)))
            elif op == "load":
                a2.load(open(args[0], "rb").read(), int(args[1], 0) if len(args) > 1 else PAYLOAD)
            elif op in ("call", "run"):
                if op == "run":
                    a2.load(open(args[0], "rb").read(), PAYLOAD)
                    addr, rest = PAYLOAD, args[1:]
                else:
                    addr, rest = int(args[0], 0), args[1:]
                p = [int(x, 0) for x in rest] + [0, 0]
                log("call %#x -> r0 = %#x" % (addr, a2.command(1, addr, p[0], p[1])))
            else:
                raise SystemExit(__doc__)
        finally:
            a2.cmd("mw.l 0x%x 0x18032001" % IM)


if __name__ == "__main__":
    main()
