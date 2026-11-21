"""Minimal MCS-51 interpreter with SFR / XRAM access hooks (used by poe_fw_oracle.py).

- Full 8051 opcode set, 256 B IRAM (upper half indirect only), 64 KiB XRAM / CODE, interrupts not modelled (callers invoke ISR bodies).
- Hooks: sfr_rd(addr, val) -> val, sfr_wr(addr, val), xram_wr(addr, val), called on every direct SFR access (bit ops included).
- Cycle counter = classic 8051 machine cycles (DW8051 runs 4 clk / cycle: same count, 3x shorter).
"""

def _cycles():
    t = [1] * 256
    for op in range(256):
        lo, hi = op & 15, op >> 4
        if op in (0x02, 0x12, 0x22, 0x32, 0x73, 0x80, 0x10, 0x20, 0x30, 0x40, 0x50, 0x60, 0x70, 0x01, 0x11, 0x21, 0x31, 0x41, 0x51, 0x61, 0x71, 0x81, 0x91, 0xA1, 0xB1, 0xC1, 0xD1, 0xE1, 0xF1, 0x83, 0x93,
                  0xA3, 0x84, 0xA4, 0x85, 0x86, 0x87, 0x75, 0x90, 0xA0, 0xB0, 0xA2, 0x82, 0x72, 0xC0, 0xD0, 0xE0, 0xE2, 0xE3, 0xF0, 0xF2, 0xF3, 0xB4, 0xB5, 0xB6, 0xB7, 0xD5, 0x05 * 0 + 0xD5):
            t[op] = 2
        if lo in (8, 9, 10, 11, 12, 13, 14, 15) and hi in (0x8, 0xA, 0xB, 0xD):
            t[op] = 2
        if hi == 0xB and lo >= 4:
            t[op] = 2
    t[0x84] = t[0xA4] = 4
    return t


CYCLES = _cycles()
PSW, ACC, B, SP, DPL, DPH = 0xD0, 0xE0, 0xF0, 0x81, 0x82, 0x83


class CPU:
    def __init__(self):
        self.code = bytearray(65536)
        self.iram = bytearray(256)
        self.sfr = bytearray(256)          # index = address (0x80..0xFF used)
        self.xram = bytearray(65536)
        self.pc = 0
        self.cycles = 0
        self.insns = 0
        self.sfr_rd = None
        self.sfr_wr = None
        self.xram_wr = None
        self.xram_rd = None
        self.sfr[SP] = 7

    # ---- register / memory access ----
    def R(self, n):
        return self.iram[(self.sfr[PSW] & 0x18) | n]

    def setR(self, n, v):
        self.iram[(self.sfr[PSW] & 0x18) | n] = v & 255

    def rd_dir(self, a):
        if a < 0x80:
            return self.iram[a]
        v = self.sfr[a]
        if a == PSW:
            v = (v & 0xFE) | (bin(self.sfr[ACC]).count("1") & 1)
        if self.sfr_rd:
            v = self.sfr_rd(a, v)
        return v

    def wr_dir(self, a, v):
        v &= 255
        if a < 0x80:
            self.iram[a] = v
            return
        self.sfr[a] = v
        if self.sfr_wr:
            self.sfr_wr(a, v)

    def rd_bit(self, b):
        if b < 0x80:
            return (self.iram[0x20 + (b >> 3)] >> (b & 7)) & 1
        return (self.rd_dir(b & 0xF8) >> (b & 7)) & 1

    def wr_bit(self, b, v):
        if b < 0x80:
            a = 0x20 + (b >> 3)
            m = 1 << (b & 7)
            self.iram[a] = (self.iram[a] | m) if v else (self.iram[a] & ~m & 255)
            return
        a = b & 0xF8
        m = 1 << (b & 7)
        o = self.rd_dir(a)
        self.wr_dir(a, (o | m) if v else (o & ~m & 255))

    def push(self, v):
        sp = (self.sfr[SP] + 1) & 255
        self.sfr[SP] = sp
        self.iram[sp] = v & 255

    def pop(self):
        sp = self.sfr[SP]
        v = self.iram[sp]
        self.sfr[SP] = (sp - 1) & 255
        return v

    def dptr(self):
        return (self.sfr[DPH] << 8) | self.sfr[DPL]

    def mread(self, a):
        if self.xram_rd:
            return self.xram_rd(a, self.xram[a])
        return self.xram[a]

    def mwrite(self, a, v):
        self.xram[a] = v & 255
        if self.xram_wr:
            self.xram_wr(a, v & 255)

    @property
    def cy(self):
        return (self.sfr[PSW] >> 7) & 1

    def setflags(self, cy=None, ac=None, ov=None):
        p = self.sfr[PSW]
        if cy is not None:
            p = (p & 0x7F) | (cy << 7)
        if ac is not None:
            p = (p & 0xBF) | (ac << 6)
        if ov is not None:
            p = (p & 0xFB) | (ov << 2)
        self.sfr[PSW] = p

    def alu(self, kind, b):
        a = self.sfr[ACC]
        if kind in ("add", "addc"):
            c = self.cy if kind == "addc" else 0
            r = a + b + c
            ac = 1 if ((a & 15) + (b & 15) + c) > 15 else 0
            c6 = 1 if ((a & 127) + (b & 127) + c) > 127 else 0
            cy = 1 if r > 255 else 0
            self.setflags(cy, ac, cy ^ c6)
            self.sfr[ACC] = r & 255
        elif kind == "subb":
            c = self.cy
            r = a - b - c
            ac = 1 if ((a & 15) - (b & 15) - c) < 0 else 0
            c6 = 1 if ((a & 127) - (b & 127) - c) < 0 else 0
            cy = 1 if r < 0 else 0
            self.setflags(cy, ac, cy ^ c6)
            self.sfr[ACC] = r & 255
        elif kind == "orl":
            self.sfr[ACC] = a | b
        elif kind == "anl":
            self.sfr[ACC] = a & b
        elif kind == "xrl":
            self.sfr[ACC] = a ^ b

    # ---- execution ----
    def step(self):
        pc = self.pc
        code = self.code
        op = code[pc]
        self.cycles += CYCLES[op]
        self.insns += 1
        lo, hi = op & 15, op >> 4
        n1 = code[(pc + 1) & 0xFFFF]
        n2 = code[(pc + 2) & 0xFFFF]
        ln = 1

        def rel(off, base):
            return (base + (off - 256 if off > 127 else off)) & 0xFFFF

        # ----- ALU group with A as destination: 2 ADD, 3 ADDC, 4 ORL, 5 ANL, 6 XRL, 9 SUBB (lo 4..15) -----
        kinds = {2: "add", 3: "addc", 4: "orl", 5: "anl", 6: "xrl", 9: "subb"}
        if hi in kinds and lo >= 4:
            if lo == 4:
                v, ln = n1, 2
            elif lo == 5:
                v, ln = self.rd_dir(n1), 2
            elif lo <= 7:
                v = self.iram[self.R(lo & 1)]
            else:
                v = self.R(lo & 7)
            self.alu(kinds[hi], v)
            self.pc = (pc + ln) & 0xFFFF
            return
        # ----- INC / DEC -----
        if hi in (0, 1) and lo >= 5:
            d = 1 if hi == 0 else -1
            if lo == 5:
                self.wr_dir(n1, self.rd_dir(n1) + d)
                ln = 2
            elif lo <= 7:
                a = self.R(lo & 1)
                self.iram[a] = (self.iram[a] + d) & 255
            else:
                self.setR(lo & 7, self.R(lo & 7) + d)
            self.pc = (pc + ln) & 0xFFFF
            return
        # ----- MOV A,src (E4..) / XCH (C) / MOV dst,A (F5..) -----
        if hi == 0xE and lo >= 4 and lo != 4:
            if lo == 5:
                self.sfr[ACC] = self.rd_dir(n1)
                ln = 2
            elif lo <= 7:
                self.sfr[ACC] = self.iram[self.R(lo & 1)]
            else:
                self.sfr[ACC] = self.R(lo & 7)
            self.pc = (pc + ln) & 0xFFFF
            return
        if hi == 0xF and lo >= 5:
            if lo == 5:
                self.wr_dir(n1, self.sfr[ACC])
                ln = 2
            elif lo <= 7:
                self.iram[self.R(lo & 1)] = self.sfr[ACC]
            else:
                self.setR(lo & 7, self.sfr[ACC])
            self.pc = (pc + ln) & 0xFFFF
            return
        if hi == 0xC and lo >= 5:
            a = self.sfr[ACC]
            if lo == 5:
                v = self.rd_dir(n1)
                self.wr_dir(n1, a)
                ln = 2
            elif lo <= 7:
                p = self.R(lo & 1)
                v = self.iram[p]
                self.iram[p] = a
            else:
                v = self.R(lo & 7)
                self.setR(lo & 7, a)
            self.sfr[ACC] = v
            self.pc = (pc + ln) & 0xFFFF
            return
        # ----- MOV Rn / @Ri,#imm (7x lo 6..F), MOV Rn,direct (Ax lo 8..F), MOV direct,Rn (8x lo 8..F), MOV direct,@Ri (8x 6,7) -----
        if hi == 7 and lo >= 6:
            if lo <= 7:
                self.iram[self.R(lo & 1)] = n1
            else:
                self.setR(lo & 7, n1)
            self.pc = (pc + 2) & 0xFFFF
            return
        if hi == 0xA and lo >= 6:
            if lo <= 7:
                self.iram[self.R(lo & 1)] = self.rd_dir(n1)
            else:
                self.setR(lo & 7, self.rd_dir(n1))
            self.pc = (pc + 2) & 0xFFFF
            return
        if hi == 8 and lo >= 6:
            if lo <= 7:
                self.wr_dir(n1, self.iram[self.R(lo & 1)])
            else:
                self.wr_dir(n1, self.R(lo & 7))
            self.pc = (pc + 2) & 0xFFFF
            return
        # ----- CJNE (B4..BF), DJNZ (D5, D8..DF) -----
        if hi == 0xB and lo >= 4:
            if lo == 4:
                l, r, ln = self.sfr[ACC], n1, 3
            elif lo == 5:
                l, r, ln = self.sfr[ACC], self.rd_dir(n1), 3
            elif lo <= 7:
                l, r, ln = self.iram[self.R(lo & 1)], n1, 3
            else:
                l, r, ln = self.R(lo & 7), n1, 3
            self.setflags(cy=1 if l < r else 0)
            nxt = (pc + ln) & 0xFFFF
            self.pc = rel(n2, nxt) if l != r else nxt
            return
        if op == 0xD5:
            v = (self.rd_dir(n1) - 1) & 255
            self.wr_dir(n1, v)
            nxt = (pc + 3) & 0xFFFF
            self.pc = rel(n2, nxt) if v else nxt
            return
        if hi == 0xD and lo >= 8:
            v = (self.R(lo & 7) - 1) & 255
            self.setR(lo & 7, v)
            nxt = (pc + 2) & 0xFFFF
            self.pc = rel(n1, nxt) if v else nxt
            return
        # ----- AJMP / ACALL (lo == 1) -----
        if lo == 1:
            nxt = (pc + 2) & 0xFFFF
            tgt = (nxt & 0xF800) | ((op >> 5) << 8) | n1
            if op & 0x10:
                self.push(nxt & 255)
                self.push(nxt >> 8)
            self.pc = tgt
            return
        # ----- remaining, by opcode -----
        a = self.sfr[ACC]
        nxt = pc + 1
        if op == 0x00:
            pass
        elif op == 0x02:
            nxt = (n1 << 8) | n2
        elif op == 0x12:
            r = (pc + 3) & 0xFFFF
            self.push(r & 255)
            self.push(r >> 8)
            nxt = (n1 << 8) | n2
        elif op in (0x22, 0x32):
            h = self.pop()
            l = self.pop()
            nxt = (h << 8) | l
        elif op == 0x03:
            self.sfr[ACC] = ((a >> 1) | (a << 7)) & 255
        elif op == 0x13:
            c = self.cy
            self.setflags(cy=a & 1)
            self.sfr[ACC] = (a >> 1) | (c << 7)
        elif op == 0x23:
            self.sfr[ACC] = ((a << 1) | (a >> 7)) & 255
        elif op == 0x33:
            c = self.cy
            self.setflags(cy=a >> 7)
            self.sfr[ACC] = ((a << 1) | c) & 255
        elif op == 0x04:
            self.sfr[ACC] = (a + 1) & 255
        elif op == 0x14:
            self.sfr[ACC] = (a - 1) & 255
        elif op == 0x10:       # JBC
            if self.rd_bit(n1):
                self.wr_bit(n1, 0)
                nxt = rel(n2, pc + 3)
            else:
                nxt = pc + 3
        elif op == 0x20:
            nxt = rel(n2, pc + 3) if self.rd_bit(n1) else pc + 3
        elif op == 0x30:
            nxt = pc + 3 if self.rd_bit(n1) else rel(n2, pc + 3)
        elif op == 0x40:
            nxt = rel(n1, pc + 2) if self.cy else pc + 2
        elif op == 0x50:
            nxt = pc + 2 if self.cy else rel(n1, pc + 2)
        elif op == 0x60:
            nxt = rel(n1, pc + 2) if a == 0 else pc + 2
        elif op == 0x70:
            nxt = pc + 2 if a == 0 else rel(n1, pc + 2)
        elif op == 0x80:
            nxt = rel(n1, pc + 2)
        elif op == 0x42:
            self.wr_dir(n1, self.rd_dir(n1) | a); nxt = pc + 2
        elif op == 0x43:
            self.wr_dir(n1, self.rd_dir(n1) | n2); nxt = pc + 3
        elif op == 0x52:
            self.wr_dir(n1, self.rd_dir(n1) & a); nxt = pc + 2
        elif op == 0x53:
            self.wr_dir(n1, self.rd_dir(n1) & n2); nxt = pc + 3
        elif op == 0x62:
            self.wr_dir(n1, self.rd_dir(n1) ^ a); nxt = pc + 2
        elif op == 0x63:
            self.wr_dir(n1, self.rd_dir(n1) ^ n2); nxt = pc + 3
        elif op == 0x72:
            self.setflags(cy=self.cy | self.rd_bit(n1)); nxt = pc + 2
        elif op == 0x82:
            self.setflags(cy=self.cy & self.rd_bit(n1)); nxt = pc + 2
        elif op == 0xA0:
            self.setflags(cy=self.cy | (1 - self.rd_bit(n1))); nxt = pc + 2
        elif op == 0xB0:
            self.setflags(cy=self.cy & (1 - self.rd_bit(n1))); nxt = pc + 2
        elif op == 0x74:
            self.sfr[ACC] = n1; nxt = pc + 2
        elif op == 0x75:
            self.wr_dir(n1, n2); nxt = pc + 3
        elif op == 0x85:       # MOV dst,src: src = n1, dst = n2
            self.wr_dir(n2, self.rd_dir(n1)); nxt = pc + 3
        elif op == 0x90:
            self.sfr[DPH] = n1; self.sfr[DPL] = n2; nxt = pc + 3
        elif op == 0x92:
            self.wr_bit(n1, self.cy); nxt = pc + 2
        elif op == 0xA2:
            self.setflags(cy=self.rd_bit(n1)); nxt = pc + 2
        elif op == 0xB2:
            self.wr_bit(n1, 1 - self.rd_bit(n1)); nxt = pc + 2
        elif op == 0xC2:
            self.wr_bit(n1, 0); nxt = pc + 2
        elif op == 0xD2:
            self.wr_bit(n1, 1); nxt = pc + 2
        elif op == 0xB3:
            self.setflags(cy=1 - self.cy)
        elif op == 0xC3:
            self.setflags(cy=0)
        elif op == 0xD3:
            self.setflags(cy=1)
        elif op == 0xC0:
            self.push(self.rd_dir(n1)); nxt = pc + 2
        elif op == 0xD0:
            self.wr_dir(n1, self.pop()); nxt = pc + 2
        elif op == 0xC4:
            self.sfr[ACC] = ((a << 4) | (a >> 4)) & 255
        elif op == 0xD4:       # DA
            r = a
            c = self.cy
            if (r & 15) > 9 or (self.sfr[PSW] & 0x40):
                r += 6
                if r > 255:
                    c = 1
            if ((r >> 4) & 15) > 9 or c or (r > 0x99 and r <= 255):
                r += 0x60
                if r > 255:
                    c = 1
            self.sfr[ACC] = r & 255
            self.setflags(cy=c)
        elif op in (0xD6, 0xD7):
            p = self.R(op & 1)
            v = self.iram[p]
            self.iram[p] = (v & 0xF0) | (a & 15)
            self.sfr[ACC] = (a & 0xF0) | (v & 15)
        elif op == 0xE0:
            self.sfr[ACC] = self.mread(self.dptr())
        elif op == 0xF0:
            self.mwrite(self.dptr(), a)
        elif op in (0xE2, 0xE3):
            self.sfr[ACC] = self.mread(self.R(op & 1))
        elif op in (0xF2, 0xF3):
            self.mwrite(self.R(op & 1), a)
        elif op == 0xE4:
            self.sfr[ACC] = 0
        elif op == 0xF4:
            self.sfr[ACC] = a ^ 255
        elif op == 0xA3:
            d = (self.dptr() + 1) & 0xFFFF
            self.sfr[DPH] = d >> 8; self.sfr[DPL] = d & 255
        elif op == 0x83:
            self.sfr[ACC] = code[(pc + 1 + a) & 0xFFFF]
        elif op == 0x93:
            self.sfr[ACC] = code[(self.dptr() + a) & 0xFFFF]
        elif op == 0x73:
            nxt = (self.dptr() + a) & 0xFFFF
        elif op == 0xA4:
            r = a * self.sfr[B]
            self.sfr[ACC] = r & 255; self.sfr[B] = r >> 8
            self.setflags(cy=0, ov=1 if r > 255 else 0)
        elif op == 0x84:
            b = self.sfr[B]
            if b == 0:
                self.setflags(cy=0, ov=1)
            else:
                self.sfr[ACC] = a // b; self.sfr[B] = a % b
                self.setflags(cy=0, ov=0)
        elif op == 0xA5:
            raise RuntimeError("reserved opcode 0xA5 at %04X" % pc)
        else:
            raise RuntimeError("unhandled opcode %02X at %04X" % (op, pc))
        self.pc = nxt & 0xFFFF
