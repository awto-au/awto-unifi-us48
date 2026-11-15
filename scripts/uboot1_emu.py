#!/usr/bin/env python3
"""uboot1_emu: run vendor U-Boot (uboot-1, usw-v1.1.6.122) from reset under Unicorn, hook all MMIO.

- Image read from private/ (full-flash.bin of the newest backup, or --flash); never committed.
- Device model: register store + defaults (tmp/logs/clock_trial.log md.l dumps) + UART16550 console.
- Polls: same (pc,addr) read > --poll-n times and the loop would continue -> value chosen by
  evaluating the following tst/and/cmp + branch; every override is logged "POLL" and listed in the report.
- Outputs: tmp/logs/uboot1_emu.log (events+console), tmp/uboot1_emu.tsv (accesses),
  tmp/uboot1_emu_report.md (coverage, writes per phase, polls, unmapped, patch list inputs).
Usage: python3 scripts/uboot1_emu.py [--budget N] [--poll-n 16] [--cmd 'version'] [--stop-unmapped]
Needs unicorn + capstone (repo .venv python3).
"""
import argparse
import bisect
import collections
import ctypes
import glob
import mmap
import pathlib
import re
import struct
import sys

import capstone
from capstone import arm_const as AC
from unicorn import *
from unicorn.arm_const import *

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/uboot1_emu.log"
TSV = ROOT / "tmp/uboot1_emu.tsv"
REP = ROOT / "tmp/uboot1_emu_report.md"
FUNCS = ROOT / "tmp/ghidra/out/uboot/00-u-boot.bin.funcs.tsv"
IMG_LEN = 447118
XIP = 0x1C000000
TEXT_END = 0x1C048000
RELOC_SPAN = 0x5814C + 0x40000  # image + bss, for pc->image mapping

# (name, start, size) device windows (mmio). Everything else mapped on demand as UNKNOWN pages.
DEVICES = [
    ("periph", 0x18000000, 0x40000),
    ("ccu+a9priv", 0x19000000, 0x100000),
    ("cmic", 0x48000000, 0x100000),
    ("idm", 0x18100000, 0x100000),  # IDM wrappers (reset/io control), from unmapped faults
]
# phase classification: first match wins
PHASES = [
    ("IDM/reset", [(0x18100000, 0x18200000)]),
    ("PLL/clock", [(0x19000000, 0x19010000), (0x1803F000, 0x18040000), (0x1800C000, 0x1800D000)]),
    ("UART", [(0x18000300, 0x18000500)]),
    ("GPIO", [(0x18000060, 0x180000C0)]),
    ("WDT", [(0x18039000, 0x1803A000), (0x19020600, 0x19020700)]),
    ("QSPI", [(0x18027000, 0x18028000), (0x1C000000, 0x1E000000)]),
    ("PCIe", [(0x18012000, 0x18014000), (0x18013000, 0x18014000)]),
    ("CMIC", [(0x48000000, 0x49000000)]),
    ("SCU/L2/A9-priv", [(0x19020000, 0x19030000)]),
    ("DDR/PHY/shmoo", [(0x18010000, 0x18012000), (0x18014000, 0x18020000), (0x18030000, 0x18039000)]),
]


def phase_of(a):
    for n, rs in PHASES:
        if any(lo <= a < hi for lo, hi in rs):
            return n
    return "other"


REGNAMES = {}
for i in range(13):
    REGNAMES["r%d" % i] = getattr(sys.modules[__name__], "UC_ARM_REG_R%d" % i)
REGNAMES.update(sp=UC_ARM_REG_SP, lr=UC_ARM_REG_LR, pc=UC_ARM_REG_PC, ip=UC_ARM_REG_R12,
                fp=UC_ARM_REG_R11, sl=UC_ARM_REG_R10, sb=UC_ARM_REG_R9)


class SpiFlash:
    """Byte-stream model of the MX25L25635E behind the MSPI queue. Reads from the image, never writes."""
    ID = (0xC2, 0x20, 0x19)

    def __init__(self, data, log):
        self.data, self.log = data, log
        self.reset()
        self.events = []  # (cmd, addr) for writes/erases

    def reset(self):
        self.buf = []

    def xfer(self, b, pc):
        self.buf.append(b)
        n = len(self.buf)
        cmd = self.buf[0]
        if n == 1:
            return 0
        if cmd == 0x9F:
            return self.ID[(n - 2) % 3]
        if cmd in (0x05, 0x35, 0x15):
            return 0x00  # WIP clear, WEL clear
        if cmd in (0x03, 0x0B, 0x13, 0x0C):
            al = 4 if cmd in (0x13, 0x0C) else 3
            dummy = 1 if cmd in (0x0B, 0x0C) else 0
            if n - 1 <= al + dummy:
                return 0
            a = int.from_bytes(bytes(self.buf[1:1 + al]), "big") + (n - 2 - al - dummy)
            return self.data[a] if a < len(self.data) else 0xFF
        if cmd in (0x02, 0x12, 0x20, 0x21, 0x52, 0xD8, 0xDC, 0xC7, 0x60, 0x01, 0x06, 0x04, 0xB7, 0xE9):
            if n == 2 or (cmd in (0xC7, 0x60, 0x06, 0x04, 0xB7, 0xE9) and n == 1):
                pass
            return 0
        return 0

    def end(self, pc):
        self.ncmd = getattr(self, "ncmd", 0) + 1
        if self.buf and self.buf[0] not in (0x05, 0x06, 0x02) and self.ncmd <= 3000:
            self.log("SPI cmd %s len=%d pc=%08x" % (bytes(self.buf[:6]).hex(), len(self.buf), pc))
        if self.buf and self.buf[0] in (0x02, 0x12, 0x20, 0x21, 0x52, 0xD8, 0xDC, 0xC7, 0x60, 0x01):
            al = 0 if self.buf[0] in (0xC7, 0x60, 0x01) else (4 if self.buf[0] in (0x12, 0x21, 0xDC) else 3)
            a = int.from_bytes(bytes(self.buf[1:1 + al]), "big") if al else 0
            self.events.append((self.buf[0], a, len(self.buf), pc))
            self.log("FLASH-WRITE cmd=%02x addr=%08x len=%d pc=%08x (not applied)" % (self.buf[0], a, len(self.buf), pc))
        self.buf = []


class Emu:
    def __init__(self, a):
        self.a = a
        self.out = open(LOG, "w")
        self.tsv = open(TSV, "w")
        self.tsv.write("seq\tpc\tfunc\trw\taddr\tsize\tvalue\tflag\n")
        self.seq = 0
        self.regs = {}  # (addr&~3) word store
        self.defaults = {}
        self.reads_logged = collections.Counter()
        self.pollcnt = collections.Counter()
        self.polls = {}  # (pc,addr) -> (func, value, ndisp)
        self.unmapped = {}
        self.unknown_reads = {}  # addr -> first pc
        self.writes = collections.OrderedDict()  # (phase,addr) -> [first_pc, func, first_val, n]
        self.reads = collections.OrderedDict()
        self.console = ""
        self.rx = collections.deque()
        self.cmds = list(a.cmd)
        self.key_sent = False
        self.blocks = {}
        self.nblk = 0
        self.mspi_tx = {}
        self.mspi_cd = {}
        self.mspi_done = 0
        self.gt = 0
        self.reloc = None
        self.stop_reason = None
        self.cs = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_ARM)
        self.cs.detail = True
        self.load_funcs()
        self.load_defaults()
        self.setup()

    def log(self, m):
        self.out.write(m + "\n")

    # ---------- symbols ----------
    def load_funcs(self):
        self.funcs = []
        for l in FUNCS.read_text().splitlines():
            ad, nm, sz = l.split("\t")
            self.funcs.append([int(ad, 16), nm, int(sz)])
        names = {}
        for f in (ROOT / "docs").glob("*.md"):
            for m in re.finditer(r"`?([A-Za-z_][\w]+)`?\s*@\s*0x(1c[0-9a-f]{6})", f.read_text()):
                names[int(m.group(2), 16)] = m.group(1)
        for f in self.funcs:
            if f[0] in names:
                f[1] = names[f[0]]
        # Ghidra lists data after the text end (rodata strings start ~0x1c048938) as functions: drop them
        self.funcs = [f for f in self.funcs if f[0] < TEXT_END]
        self.funcs.sort()
        self.fstarts = [f[0] for f in self.funcs]

    def to_img(self, pc):
        if self.reloc is not None and self.reloc <= pc < self.reloc + RELOC_SPAN:
            return pc - self.reloc + XIP
        return pc

    def fn(self, pc):
        p = self.to_img(pc)
        i = bisect.bisect_right(self.fstarts, p) - 1
        if i >= 0:
            s, n, sz = self.funcs[i]
            if p < s + sz + 0x10:
                return n
        return "?"

    # ---------- defaults ----------
    def load_defaults(self):
        p = ROOT / "tmp/logs/clock_trial.log"
        self.defaults[0x48010224] = 0x8344  # device id read by board init (docs/uboot.md s5)
        self.defaults[0x18000000] = 0xD060  # chipcommon id, 53344 decimal (I)
        if not p.exists():
            return
        for l in p.read_text().splitlines():
            m = re.match(r"^([0-9a-f]{8}):((?: [0-9a-f]{8})+)", l)
            if m:
                a = int(m.group(1), 16)
                for i, w in enumerate(m.group(2).split()):
                    self.defaults[a + 4 * i] = int(w, 16)

    # ---------- setup ----------
    def setup(self):
        a = self.a
        fl = pathlib.Path(a.flash) if a.flash else pathlib.Path(sorted(glob.glob(str(ROOT / "private/backups/*/full-flash.bin")))[-1])
        data = fl.read_bytes()
        self.log("flash %s %d B" % (fl, len(data)))
        self.flash = SpiFlash(data, self.log)
        uc = Uc(UC_ARCH_ARM, UC_MODE_ARM)
        uc.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_A9)
        self.uc = uc
        uc.mem_map(XIP, 0x2000000, UC_PROT_ALL)
        uc.mem_write(XIP, data.ljust(0x2000000, b"\xff"))
        uc.mem_map(0x1B000000, 0x4000)  # SRAM 16 KiB (docs/sram.md)
        uc.mem_map(0x50000000, 0x20000)  # L2-as-RAM, stack at 0x5001eff0 (docs/sram.md)
        mm = mmap.mmap(-1, 0x10000000)
        self.ddr = mm
        ptr = ctypes.addressof(ctypes.c_char.from_buffer(mm))
        uc.mem_map_ptr(0x60000000, 0x10000000, UC_PROT_ALL, ptr)
        uc.mem_map_ptr(0x00000000, 0x08000000, UC_PROT_ALL, ptr)  # low alias (docs/uboot.md s6)
        uc.mem_map(0xFFFF0000, 0x1000)  # boot ROM vectors: b .
        uc.mem_write(0xFFFF0000, b"\xfe\xff\xff\xea" * 0x400)
        for n, s, z in DEVICES:
            uc.mmio_map(s, z, self.mread, n, self.mwrite, n)
        uc.hook_add(UC_HOOK_MEM_UNMAPPED, self.on_unmapped)
        uc.hook_add(UC_HOOK_BLOCK, self.on_block)
        uc.hook_add(UC_HOOK_INTR, self.on_intr)
        uc.reg_write(UC_ARM_REG_CPSR, 0x1D3)

    # ---------- MMIO ----------
    def pc(self):
        return self.uc.reg_read(UC_ARM_REG_PC)

    def rec(self, rw, pc, addr, size, val, flag=""):
        self.seq += 1
        self.tsv.write("%d\t%08x\t%s\t%s\t%08x\t%d\t%08x\t%s\n" % (self.seq, pc, self.fn(pc), rw, addr, size, val, flag))

    def uart_read(self, addr, size):
        o = (addr & 0xFF)
        reg = o >> self.a.uart_shift
        if reg == 5:  # LSR
            return 0x60 | (1 if self.rx else 0)
        if reg == 0:
            return self.rx.popleft() if self.rx else 0
        if reg == 2:  # IIR
            return 0xC1
        return self.regs.get(addr & ~3, 0) >> (8 * (addr & 3)) & 0xFF

    def is_uart(self, addr):
        return 0x18000400 <= addr < 0x18000420

    def mread(self, uc, off, size, name):
        base = {d[0]: d[1] for d in DEVICES}[name]
        addr = base + off
        pc = self.pc()
        flag = ""
        if self.is_uart(addr):
            v = self.uart_read(addr, size)
            if (addr & 0xFF) >> self.a.uart_shift != 5:
                flag = "UART"
            else:
                return v  # LSR polls: not logged
        elif addr == 0x18027220:  # MSPI_STATUS: SPIF
            v, flag = self.mspi_done, "MSPI"
        elif addr in (0x19020200, 0x19020204):  # A9 global timer counter (udelay)
            if addr == 0x19020200:
                self.gt = (self.gt + self.a.timer_step) & 0xFFFFFFFFFFFFFFFF
            v = (self.gt & 0xFFFFFFFF) if addr == 0x19020200 else self.gt >> 32
            flag = "TIMER"
        else:
            w = self.regs.get(addr & ~3)
            if w is None:
                w = self.defaults.get(addr & ~3)
                if w is None:
                    w = 0
                    if (addr & ~3) not in self.unknown_reads:
                        self.unknown_reads[addr & ~3] = (pc, self.fn(pc))
                    flag = "DEF0"
            v = (w >> (8 * (addr & 3))) & ((1 << (8 * size)) - 1) if size < 4 else w
            v, pf = self.poll(pc, addr, size, v)
            flag = (flag + " " + pf).strip()
        ph = phase_of(addr)
        k = (ph, addr)
        r = self.reads.setdefault(k, [pc, self.fn(pc), v, 0])
        r[3] += 1
        key = (pc, addr)
        self.reads_logged[key] += 1
        if self.reads_logged[key] <= self.a.log_reads or flag.find("POLL") >= 0 and self.reads_logged[key] <= self.a.log_reads + 4:
            self.rec("R", pc, addr, size, v, flag)
        return v

    def mwrite(self, uc, off, size, val, name):
        base = {d[0]: d[1] for d in DEVICES}[name]
        addr = base + off
        pc = self.pc()
        if self.is_uart(addr) and (addr & 0xFF) >> self.a.uart_shift == 0:
            self.uart_tx(val & 0xFF)
            return
        old = self.regs.get(addr & ~3, self.defaults.get(addr & ~3, 0))
        if size < 4:
            sh = 8 * (addr & 3)
            m = ((1 << (8 * size)) - 1) << sh
            val = (old & ~m) | ((val << sh) & m)
        self.regs[addr & ~3] = val & 0xFFFFFFFF
        for k in [x for x in self.pollcnt if x[1] == addr]:
            self.pollcnt[k] = 0
        if 0x18027200 <= addr < 0x18027400:
            self.mspi_write(addr, val, pc)
        ph = phase_of(addr)
        w = self.writes.setdefault((ph, addr), [pc, self.fn(pc), val, 0])
        w[3] += 1
        self.rec("W", pc, addr, size, val)

    # ---------- UART ----------
    def uart_tx(self, b):
        ch = chr(b)
        self.console += ch
        if self.a.echo:
            sys.stdout.write(ch)
            sys.stdout.flush()
        c = self.console
        if not self.key_sent and "autoboot" in c and not self.a.no_key:
            self.rx.extend(b" ")
            self.key_sent = True
            self.log("[inject] key ' ' after autoboot banner")
        if c.endswith("u-boot> ") and self.key_sent:
            if self.cmds:
                cmd = self.cmds.pop(0)
                self.rx.extend(cmd.encode() + b"\r")
                self.log("[inject] cmd %r" % cmd)
            else:
                self.stop_reason = "prompt reached"
                self.uc.emu_stop()

    # ---------- polls ----------
    def poll(self, pc, addr, size, v):
        key = (pc, addr)
        self.pollcnt[key] += 1
        if self.pollcnt[key] <= self.a.poll_n:
            return v, ""
        nv = self.eval_poll(pc, size, v)
        if nv is None or nv == v:
            return v, ""
        if key not in self.polls:
            self.polls[key] = [self.fn(pc), nv, 0, v]
            self.log("POLL pc=%08x %s addr=%08x stored=%08x -> satisfied %08x" % (pc, self.fn(pc), addr, v, nv))
        self.polls[key][2] += 1
        return nv, "POLL"

    def getregs(self):
        return {n: self.uc.reg_read(r) for n, r in REGNAMES.items()}

    def eval_poll(self, pc, size, v):
        try:
            ins = next(self.cs.disasm(bytes(self.uc.mem_read(pc, 4)), pc))
        except StopIteration:
            return None
        if not ins.mnemonic.startswith("ldr") or ins.operands[0].type != AC.ARM_OP_REG:
            return None
        dst = ins.reg_name(ins.operands[0].reg)
        mask = (1 << (8 * size)) - 1
        ahead = []
        pcn = pc + 4
        for _ in range(10):
            try:
                i = next(self.cs.disasm(bytes(self.uc.mem_read(pcn, 4)), pcn))
            except StopIteration:
                break
            ahead.append(i)
            if i.mnemonic.startswith("b") or i.mnemonic.startswith("cb"):
                break
            pcn += 4
        imms = set()
        for i in ahead:
            for o in i.operands:
                if o.type == AC.ARM_OP_IMM and not i.mnemonic.startswith("b"):
                    imms.add(o.imm & 0xFFFFFFFF)
        cands = [v]
        for k in sorted(imms):
            cands += [v | k, v & ~k, k]
        cands += [0, 0xFFFFFFFF, 1, 2]
        regs0 = self.getregs()
        cpsr = self.uc.reg_read(UC_ARM_REG_CPSR)
        for c in cands:
            c &= mask
            r = self.sim(ahead, dict(regs0), dst, c, cpsr, pc)
            if r is True:
                return c
            if r is None:
                return None
        return None

    CC = {AC.ARM_CC_EQ: lambda n, z, c, v: z, AC.ARM_CC_NE: lambda n, z, c, v: not z,
          AC.ARM_CC_HS: lambda n, z, c, v: c, AC.ARM_CC_LO: lambda n, z, c, v: not c,
          AC.ARM_CC_MI: lambda n, z, c, v: n, AC.ARM_CC_PL: lambda n, z, c, v: not n,
          AC.ARM_CC_VS: lambda n, z, c, v: v, AC.ARM_CC_VC: lambda n, z, c, v: not v,
          AC.ARM_CC_HI: lambda n, z, c, v: c and not z, AC.ARM_CC_LS: lambda n, z, c, v: (not c) or z,
          AC.ARM_CC_GE: lambda n, z, c, v: n == v, AC.ARM_CC_LT: lambda n, z, c, v: n != v,
          AC.ARM_CC_GT: lambda n, z, c, v: (not z) and n == v, AC.ARM_CC_LE: lambda n, z, c, v: z or n != v,
          AC.ARM_CC_AL: lambda n, z, c, v: True, AC.ARM_CC_INVALID: lambda n, z, c, v: True}

    def sim(self, ahead, R, dst, val, cpsr, loadpc):
        """True = loop exits with val, False = loop continues, None = cannot decide."""
        R[dst] = val
        smem = {}
        n, z, c, v = [(cpsr >> s) & 1 for s in (31, 30, 29, 28)]

        def opv(o):
            if o.type == AC.ARM_OP_IMM:
                return o.imm & 0xFFFFFFFF
            if o.type == AC.ARM_OP_REG:
                x = R.get(ahead_ins.reg_name(o.reg))
                if x is None:
                    raise KeyError
                return x
            raise KeyError

        for ahead_ins in ahead:
            i = ahead_ins
            m = i.mnemonic
            ok = self.CC[i.cc](n, z, c, v)
            try:
                if m.startswith("b") and i.operands and i.operands[0].type == AC.ARM_OP_IMM and m[:2] != "bi" and m[:2] != "bf":
                    if m.startswith("bl"):
                        return None
                    tgt = i.operands[0].imm & 0xFFFFFFFF
                    final = tgt if ok else i.address + 4
                    return not (final <= i.address)
                if m.startswith("cb"):
                    return None
                if not ok:
                    continue
                base = re.sub(r"(eq|ne|hs|lo|mi|pl|vs|vc|hi|ls|ge|lt|gt|le|cs|cc)?s?$", "", m) if False else m
                o = i.operands
                mn = m
                for sfx in ("eq", "ne", "hs", "lo", "mi", "pl", "vs", "vc", "hi", "ls", "ge", "lt", "gt", "le", "cs", "cc"):
                    if mn.endswith(sfx) and mn[:-2] in ("tst", "teq", "cmp", "cmn", "and", "ands", "mov", "movs", "ubfx", "orr", "bic", "lsl", "lsr", "asr", "lsls", "lsrs", "uxtb", "uxth", "eor", "add", "sub", "mvn"):
                        mn = mn[:-2]
                        break
                res = None
                if mn in ("tst", "teq", "cmp", "cmn"):
                    a, b = opv(o[0]), opv(o[1])
                    if o[1].type == AC.ARM_OP_REG and o[1].shift.type != 0:
                        raise KeyError
                    if mn == "tst":
                        r_ = a & b
                    elif mn == "teq":
                        r_ = a ^ b
                    elif mn == "cmp":
                        r_ = (a - b) & 0xFFFFFFFF
                        c = int(a >= b)
                        v = int(((a ^ b) & (a ^ r_)) >> 31)
                    else:
                        r_ = (a + b) & 0xFFFFFFFF
                        c = int(a + b > 0xFFFFFFFF)
                        v = int((~(a ^ b) & (a ^ r_)) >> 31)
                    n, z = r_ >> 31, int(r_ == 0)
                    continue
                if mn in ("mov", "movs", "mvn", "uxtb", "uxth", "lsl", "lsr", "asr", "lsls", "lsrs", "and", "ands", "orr", "bic", "eor", "ubfx", "add", "sub"):
                    d = i.reg_name(o[0].reg)
                    if mn.startswith("mov"):
                        res = opv(o[1])
                    elif mn == "mvn":
                        res = ~opv(o[1]) & 0xFFFFFFFF
                    elif mn == "uxtb":
                        res = opv(o[1]) & 0xFF
                    elif mn == "uxth":
                        res = opv(o[1]) & 0xFFFF
                    elif mn.startswith("lsl"):
                        res = (opv(o[1]) << opv(o[2])) & 0xFFFFFFFF
                    elif mn.startswith("lsr"):
                        res = opv(o[1]) >> opv(o[2])
                    elif mn.startswith("asr"):
                        x = opv(o[1])
                        x = x - (1 << 32) if x >> 31 else x
                        res = (x >> opv(o[2])) & 0xFFFFFFFF
                    elif mn == "ubfx":
                        res = (opv(o[1]) >> opv(o[2])) & ((1 << opv(o[3])) - 1)
                    else:
                        if len(o) > 2 and o[2].type == AC.ARM_OP_REG and o[2].shift.type != 0:
                            raise KeyError
                        a, b = opv(o[1]), opv(o[2])
                        res = {"and": a & b, "ands": a & b, "orr": a | b, "bic": a & ~b, "eor": a ^ b,
                               "add": a + b, "sub": a - b}[mn] & 0xFFFFFFFF
                    R[d] = res
                    if i.update_flags:
                        n, z = res >> 31, int(res == 0)
                    continue
                if m in ("str", "ldr") and o[1].type == AC.ARM_OP_MEM and o[1].mem.index == 0 and not i.writeback:
                    base = R.get(i.reg_name(o[1].mem.base))
                    if base is not None:
                        key = (base + o[1].mem.disp) & 0xFFFFFFFF
                        if m == "str":
                            smem[key] = R.get(i.reg_name(o[0].reg))
                            continue
                        if key in smem:
                            if smem[key] is None:
                                R.pop(i.reg_name(o[0].reg), None)
                            else:
                                R[i.reg_name(o[0].reg)] = smem[key]
                            continue
                if m.startswith(("str", "push", "stm", "nop", "dmb", "dsb", "isb")):
                    continue
                if not i.update_flags and o and o[0].type == AC.ARM_OP_REG:
                    R.pop(i.reg_name(o[0].reg), None)  # unknown result, only fatal if used later
                    continue
                return None
            except KeyError:
                return None
        return None

    def mspi_write(self, addr, val, pc):
        # Probe code @0x1c025234 mixes two TXRAM layouts (cmd/addr: words 0,1,2,3; data: words 0,2,4..).
        # Model = flat list of the TXRAM words written since the last start; rx byte i lands at the
        # same word index j and j+1 (data path reads 0x2c4+8k).
        o = addr - 0x18027000
        if 0x240 <= o < 0x2C0:
            self.mspi_tx[(o - 0x240) // 4] = val & 0xFF
        elif 0x340 <= o < 0x380:
            self.mspi_cd[(o - 0x340) // 4] = val & 0xFF
        elif o == 0x220:
            self.mspi_done = 0
        elif o == 0x218 and val & 0x40:  # SPE: run the queued words
            cont_in = bool(self.flash.buf)
            js = sorted(self.mspi_tx)
            data_layout = len(js) > 1 and all(j % 2 == 0 for j in js)
            for j in js:
                rb = self.flash.xfer(self.mspi_tx[j], pc)
                if data_layout:  # data queue: slot k at TX 0x240+8k, rx lsb read at 0x2c4+8k (@0x1c025488)
                    k = j // 2
                    self.regs[0x18027000 + 0x2C0 + 8 * k] = rb
                    self.regs[0x18027000 + 0x2C4 + 8 * k] = rb
                else:
                    self.regs[0x18027000 + 0x2C0 + 4 * j] = rb
            cont = (self.mspi_cd[max(self.mspi_cd)] >> 7) & 1 if self.mspi_cd else 0
            if not cont:
                self.flash.end(pc)
            self.mspi_tx, self.mspi_cd = {}, {}
            self.mspi_done = 1

    # ---------- faults/blocks ----------
    def on_block(self, uc, addr, size, ud):
        self.blocks[addr] = size
        self.nblk += 1
        if self.nblk % 200000 == 0:
            self.log("[progress] blocks=%d pc=%08x %s" % (self.nblk, addr, self.fn(addr)))
            self.out.flush()
            self.tsv.flush()
        if self.reloc is None and addr >= 0x60000000 and addr < 0x70000000:
            self.find_reloc(addr)

    def find_reloc(self, addr):
        pat = bytes(self.uc.mem_read(addr, 16))
        data = bytes(self.uc.mem_read(XIP, IMG_LEN))
        i = data.find(pat)
        if i >= 0 and data.find(pat, i + 1) < 0:
            self.reloc = addr - i
            self.log("[reloc] image relocated to base 0x%08x" % self.reloc)
        else:
            self.reloc = None

    def on_intr(self, uc, no, ud):
        self.log("INTR %d pc=%08x" % (no, self.pc()))

    def on_unmapped(self, uc, access, addr, size, value, ud):
        pc = self.pc()
        rw = {UC_MEM_READ_UNMAPPED: "R", UC_MEM_WRITE_UNMAPPED: "W", UC_MEM_FETCH_UNMAPPED: "X"}.get(access, "?")
        pg = addr & ~0xFFF
        self.unmapped.setdefault(pg, [pc, self.fn(pc), rw, addr, 0])[4] += 1
        self.log("UNMAPPED %s addr=%08x size=%d pc=%08x %s" % (rw, addr, size, pc, self.fn(pc)))
        self.rec(rw, pc, addr, size, value if rw == "W" else 0, "UNMAPPED")
        if rw == "X" or self.a.stop_unmapped:
            self.stop_reason = "unmapped %s %08x pc=%08x" % (rw, addr, pc)
            return False
        name = "unk%08x" % pg
        DEVICES.append((name, pg, 0x1000))
        uc.mmio_map(pg, 0x1000, self.mread, name, self.mwrite, name)
        return True

    # ---------- run ----------
    def run(self):
        uc = self.uc
        try:
            uc.emu_start(XIP, 0xFFFFFFFF, count=self.a.budget)
            if not self.stop_reason:
                self.stop_reason = "instruction budget (%d) exhausted" % self.a.budget
        except UcError as e:
            self.stop_reason = "UcError %s at pc=%08x (%s)" % (e, self.pc(), self.fn(self.pc()))
        self.log("STOP: %s (final pc=%08x %s)" % (self.stop_reason, self.pc(), self.fn(self.pc())))
        self.log("regs: " + " ".join("%s=%08x" % (n, v) for n, v in self.getregs().items()))
        self.log("---- console ----\n" + self.console)
        self.report()
        self.out.close()
        self.tsv.close()

    def report(self):
        covered = set()
        for s, z in self.blocks.items():
            p = self.to_img(s)
            i = bisect.bisect_right(self.fstarts, p) - 1
            while i >= 0 and i < len(self.funcs) and self.funcs[i][0] < p + z:
                f = self.funcs[i]
                if f[0] + f[2] > p:
                    covered.add(f[0])
                i += 1
                if i < len(self.funcs) and self.funcs[i][0] >= p + z:
                    break
        pcs = len(self.blocks)
        o = []
        o.append("# uboot1_emu report\n")
        o.append("- stop: %s" % self.stop_reason)
        o.append("- mmio accesses logged: %d; blocks %d" % (self.seq, pcs))
        o.append("- relocation: %s" % ("0x%08x" % self.reloc if self.reloc else "not reached"))
        o.append("- coverage: %d / %d functions (%.1f%%)" % (len(covered), len(self.funcs), 100.0 * len(covered) / len(self.funcs)))
        o.append("\n## Console\n```\n%s\n```" % self.console)
        o.append("\n## Writes per phase (distinct regs / total writes)\n")
        byp = collections.defaultdict(list)
        for (ph, a), w in self.writes.items():
            byp[ph].append((a, w))
        for ph in [p[0] for p in PHASES] + ["other"]:
            L = byp.get(ph, [])
            o.append("### %s: %d regs, %d writes" % (ph, len(L), sum(w[3] for a, w in L)))
            for a, w in L[: self.a.rep_regs]:
                o.append("- %08x first pc=%08x %s val=%08x n=%d" % (a, w[0], w[1], w[2], w[3]))
            if len(L) > self.a.rep_regs:
                o.append("- ... %d more (see tsv)" % (len(L) - self.a.rep_regs))
        o.append("\n## Reads per phase (distinct regs)\n")
        cnt = collections.Counter(ph for (ph, a) in self.reads)
        for ph, n in cnt.items():
            o.append("- %s: %d regs" % (ph, n))
        o.append("\n## Auto-satisfied polls (flagged)\n")
        for (pc, a), (fn, nv, n, old) in self.polls.items():
            o.append("- pc=%08x %s addr=%08x stored=%08x -> %08x (x%d)" % (pc, fn, a, old, nv, n))
        o.append("\n## Unmapped accesses (page, first pc)\n")
        for pg, (pc, fn, rw, a, n) in self.unmapped.items():
            o.append("- %s %08x (page %08x) first pc=%08x %s n=%d" % (rw, a, pg, pc, fn, n))
        o.append("\n## Reads of registers with no default (returned 0)\n")
        ur = sorted(self.unknown_reads.items())
        for a, (pc, fn) in ur[:60]:
            o.append("- %08x first pc=%08x %s" % (a, pc, fn))
        if len(ur) > 60:
            o.append("- ... %d more (%d total)" % (len(ur) - 60, len(ur)))
        o.append("\n## SPI flash writes seen (not applied)\n")
        ev = collections.Counter((c, a & ~0xFFFF) for c, a, n, pc in self.flash.events)
        for (c, a), n in sorted(ev.items()):
            o.append("- cmd %02x block %08x x%d" % (c, a, n))
        o.append("\n## Functions not run\n")
        miss = [f for f in self.funcs if f[0] not in covered]
        o.append(", ".join("%s@%x" % (f[1], f[0]) for f in miss))
        REP.write_text("\n".join(o) + "\n")
        print("\n".join(o[:6]))
        print("report: %s" % REP)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--flash")
    ap.add_argument("--budget", type=int, default=20_000_000)
    ap.add_argument("--poll-n", type=int, default=16)
    ap.add_argument("--log-reads", type=int, default=8, help="reads logged per (pc,addr) before suppression")
    ap.add_argument("--cmd", action="append", default=[])
    ap.add_argument("--timer-step", type=int, default=20000, help="global timer ticks added per counter read (200 MHz assumed: 100 us)")
    ap.add_argument("--no-key", action="store_true")
    ap.add_argument("--stop-unmapped", action="store_true")
    ap.add_argument("--uart-shift", type=int, default=0)
    ap.add_argument("--rep-regs", type=int, default=400)
    ap.add_argument("--echo", action="store_true")
    a = ap.parse_args()
    (ROOT / "tmp/logs").mkdir(parents=True, exist_ok=True)
    Emu(a).run()


if __name__ == "__main__":
    main()
