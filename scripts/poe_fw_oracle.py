#!/usr/bin/env python3
"""Stock-image oracle for the BCM59111 PSE firmware rewrite (docs/poe-fw-seq/06-oracle.md).

Usage: poe_fw_oracle.py [--image v21|meraki|both] [--ch 1,0] [--exp NAME ...] [--list] [--ticks N] [--pt R] [--passes N] [--xcheck]
  Runs the STOCK images (v21 and Meraki v18 side by side by default, CODE base 0x1A00) with a modelled analog front end and logs what the
  stock detect / class / power chain publishes and which SFRs it writes. Offline only, no board.
  Full run (all experiments, --ch 1,0, both images) takes ~3 min wall clock; exit code 0. Per-image entry points: PROFILES.
  Engine: scripts/mcs51.py (pure-Python MCS-51 core with SFR / XRAM hooks). ucsim_51 cannot hold the model: a console round trip is
    ~40 ms and every ADC / D-register access would need a break + set (thousands per tick). --xcheck runs the stock boot on ucsim_51
    (-t RL6449, scripts/poe_fw_sim.py harness) and compares IRAM / XRAM with the Python core at the main loop (core validation).
  Harness (mirrors poe_fw_sim.py): ROM 0x03..0xFF = RETI, ROM strap pointer 0x1900 -> RET, IRAM / XRAM zeroed, DEV_ID 0x8043 = 0xC0,
    boot = hook slot 0 until the main loop 0x230D, interrupts off. Interrupts are injected at the main-loop top 0x3D1A: per tick the
    timer-1 ISR (hook 8, 0x4FCD) once and the periodic ISR (hook 3, 0x4EBA) --pt times (default 1), then --passes main-loop passes.
    ACC / B / DPTR / PSW / R0..R7 are saved and restored around each injection (what the ROM vector glue does).
  Host setup like the kernel driver after probe: 0x13 = 0x0F, 0x14 = 0xFF, 0x44 = 0x0F, HP_Pn_MOD 0x01, OV_CUT 0xE2, ILIM 0xC0 per channel, OP_MOD = auto for the channel under test (--ch).
  Analog model (class Analog): AB selects an indirect word (AD:AC), AC / AD writes store into the selected word, B5 selects the ADC input
    (B7 / B6 data), D1..D4 = per-channel detect result (D >> 4 = code), AF bit 7 reads 0, E9..EF math unit (16x16 multiply, 32-bit shift).
  Experiments (--exp, default all): see --list. Log: tmp/logs/poe_fw_oracle.log (stdout too). Stall guard: no progress in 60 s -> abort.
"""
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402
from mcs51 import CPU  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FW = ROOT / "hwdb/firmware"
LOG = ROOT / "tmp/logs/poe_fw_oracle.log"
BASE = 0x1A00
TRAP = 0x1A50          # return address for injected ISRs (zero padding in the hook table)
STALL_S = 60           # wall-clock stall guard: a run that makes no tick progress for 60 s is a hang
MAXINSN = 400000       # instruction budget per ISR / pass: longest stock path is ~10k, a miss = runaway, reported

SFRN = {0x80: "P0", 0x81: "SP", 0x88: "TCON", 0x89: "TMOD", 0x8B: "TL1", 0x8D: "TH1", 0x90: "P1", 0x93: "S93", 0x95: "S95", 0x96: "S96",
        0xA8: "IE", 0xA1: "A1_LIM0", 0xA2: "A2_LIM1", 0xA3: "A3_LIM2", 0xA4: "A4_LIM3", 0xAB: "AB_IDX", 0xAC: "AC_LO", 0xAD: "AD_HI",
        0xAE: "AE", 0xAF: "AF_MUX", 0xB1: "B1_CTL0", 0xB2: "B2_CTL1", 0xB3: "B3_CTL2", 0xB4: "B4_CTL3", 0xB5: "B5_ADCSEL", 0xB6: "B6_ADC",
        0xB7: "B7_ADC", 0xB8: "IP", 0xBC: "BC_EVT0", 0xBD: "BD_EVT1", 0xBE: "BE_EVT2", 0xBF: "BF_EVT3", 0xC8: "T2CON", 0xC9: "C9_GATE",
        0xD1: "D1_DET0", 0xD2: "D2_DET1", 0xD3: "D3_DET2", 0xD4: "D4_DET3", 0xD5: "D5_CLS", 0xD6: "D6_CLS", 0xD7: "D7", 0xD9: "D9", 0xDA: "DA",
        0xDB: "DB", 0xDD: "DD_I2C", 0xE5: "E5", 0xE6: "E6", 0xE7: "E7", 0xE8: "E8", 0xE9: "E9", 0xEA: "EA", 0xEB: "EB", 0xEC: "EC",
        0xED: "ED", 0xEE: "EE", 0xEF: "EF_MATH", 0xF1: "F1_PWR_SET", 0xF2: "F2_PWR_CLR", 0xF4: "F4", 0xF5: "F5", 0xF6: "F6_TX",
        0xF7: "F7_RX", 0xF8: "F8_INT", 0xF9: "F9_WDT", 0xFF: "FF"}
QUIET = {0x81, 0x82, 0x83, 0xD0, 0xE0, 0xF0}     # SP DPL DPH PSW ACC B: not logged

HOSTREG = {0x01: "INT_MASK", 0x02: "PWR_EVT", 0x03: "PWR_EVT_CLR", 0x04: "DET_CLASS_EVT", 0x0A: "SUP_EVT", 0x0C: "SAT_P0", 0x0D: "SAT_P1", 0x0E: "SAT_P2",
           0x0F: "SAT_P3", 0x10: "SAT_PWR", 0x11: "R11", 0x12: "OP_MOD", 0x13: "DIS_SEN_EN", 0x14: "DET_CLASS_EN", 0x2C: "TEMP", 0x2E: "VSUP_L",
           0x2F: "VSUP_H", 0x44: "R44", 0x4C: "ICUT0", 0x4E: "R4E", 0x56: "ICUT_P?", 0x58: "R58"}


class Log:
    """stdout + LOG (truncated on the first message of a run, so --help / --list leave the previous log alone)."""

    def __init__(self):
        self.f = None

    def __call__(self, msg=""):
        print(msg, flush=True)
        if self.f is None:
            LOG.parent.mkdir(parents=True, exist_ok=True)
            self.f = LOG.open("w")
        self.f.write(msg + "\n")
        self.f.flush()


log = Log()


# ------------------------------------------------------------------ analog model
class Analog:
    """Analog front end: all values are chosen by the experiment (attributes), reads are logged per (SFR, AB / B5 index)."""

    def __init__(self):
        self.ab = 0
        self.af = 0                          # last value written to AF (mux select / trigger)
        self.b5 = 0
        self.words = {}                      # indirect register file: index -> 16-bit word (AD:AC)
        self.word_fn = None                  # optional fn(idx, tick) -> word overriding defaults
        self.det_code = [6, 6, 6, 6]         # detect result code per channel (D >> 4): 4 good, 6 open
        self.det_low = [0, 0, 0, 0]
        self.d9 = 0                          # D9 = word select 4c + k (k = 0..3), data in DA:DB (hi:lo)
        self.det_words = [[0, 0, 0, 0] for _ in range(4)]   # 4 detect sample words per channel (read by 0x3651 when D >> 4 == 0xF)
        self.det_start = [0, 0, 0, 0]        # last value written to Dn
        self.adc = {}                        # B5 select -> signed 8-bit (B7 / B6)
        self.ae = 0
        self.bc = [0, 0, 0, 0]               # BC..BF per-channel event latches (write-1-clear)
        self.s95 = 0                         # SFR 0x95 status latch (write-1-clear)
        self.sched = []                      # [due_tick, chan, bc_bits, s95_bits]
        self.det_latency = 1                 # ticks from D = 1 (detect start) to the detect-complete event (BC bit 6)
        self.f3 = 0                          # SFR F3: per-channel power-good status (read by 5074 on the power-good event)
        self.pd_ok = [True] * 4              # power-up succeeds (F3 bit set when the BC.4 event fires)
        self.pg_latency = 2                  # ticks from the F1 enable bit to the power-up-complete event (BC bit 4)
        self.on_write = None                 # optional model hook fn(machine_an, sfr, val)
        self.tick = 0
        self.reads = {}                      # (sfr name, key) -> count
        self.default_words = {0x2C: 0x0130, 0x30: 0x0A87}  # AB 0x2C temperature word -> TEMP 0xA0; AB 0x30 supply word -> 0x2E/0x2F ~ 0x22FA (52 V)
        self.power = [0, 0, 0, 0]            # channel powered (F1 bit seen)
        self.math = {}
        self.mark = lambda *a: None

    # words per AB index
    def word(self, idx):
        if self.word_fn:
            w = self.word_fn(self, idx)
            if w is not None:
                return w & 0xFFFF
        if idx in self.words:
            return self.words[idx] & 0xFFFF
        return self.default_words.get(idx, 0) & 0xFFFF

    def rd(self, cpu, a, v):
        if a == 0xAC:
            self.cnt("AC", (self.ab, self.af))
            return self.word(self.ab) & 0xFF
        if a == 0xAD:
            self.cnt("AD", (self.ab, self.af))
            return self.word(self.ab) >> 8
        if a == 0xB7 or a == 0xB6:
            self.cnt("B7" if a == 0xB7 else "B6", self.b5)
            return self.adc.get(self.b5, 0) & 0xFF
        if a in (0xD1, 0xD2, 0xD3, 0xD4):
            c = a - 0xD1
            self.cnt("D%d" % (c + 1), self.det_start[c])
            return (self.det_code[c] << 4) | self.det_low[c]
        if a == 0xDA or a == 0xDB:
            w = self.det_words[(self.d9 >> 2) & 3][self.d9 & 3]
            self.cnt("DA" if a == 0xDA else "DB", self.d9)
            return (w >> 8) & 255 if a == 0xDA else w & 255
        if a == 0xAE:
            self.cnt("AE", 0)
            return self.ae
        if a == 0xAF:
            return v & 0x7F
        if 0xBC <= a <= 0xBF:
            return self.bc[a - 0xBC]
        if a == 0x95:
            return self.s95
        if a == 0xF3:
            return self.f3
        if a == 0xD7:
            self.cnt("D7", 0)
            return v
        return v

    def cnt(self, n, k):
        self.reads[(n, k)] = self.reads.get((n, k), 0) + 1

    def wr(self, cpu, a, v):
        if a == 0xAB:
            self.ab = v
        elif a == 0xB5:
            self.b5 = v
        elif a == 0xD9:
            self.d9 = v
        elif a == 0xAF:
            self.af = v
        elif a == 0xAC:
            self.words[self.ab] = (self.words.get(self.ab, self.default_words.get(self.ab, 0)) & 0xFF00) | v
        elif a == 0xAD:
            self.words[self.ab] = (self.words.get(self.ab, self.default_words.get(self.ab, 0)) & 0x00FF) | (v << 8)
        elif a in (0xD1, 0xD2, 0xD3, 0xD4):
            self.det_start[a - 0xD1] = v
            if v == 1:
                self.sched.append([self.tick + self.det_latency, a - 0xD1, 0x40, 0, 0])
        elif 0xBC <= a <= 0xBF:
            self.bc[a - 0xBC] &= ~v & 255
        elif a == 0x95:
            self.s95 &= ~v & 255
        elif a == 0xF1:                      # level semantics: low nibble bit c = channel c powered; firmware clears it with ANL to cut power
            for c in range(4):
                if v & (1 << c) and not self.power[c]:
                    self.power[c] = 1
                    self.sched.append([self.tick + self.pg_latency, c, 0x10, 0, 1 << c if self.pd_ok[c] else 0])
                elif not v & (1 << c) and self.power[c]:
                    self.power[c] = 0
                    self.f3 &= ~(1 << c)
                    self.sched = [e for e in self.sched if not (e[1] == c and e[2] == 0x10)]
        # F2: plain register in the model (per-channel bits ORed / cleared by the firmware at power-up; no power effect assumed)
        elif a == 0xEE:                      # 16x16 -> 32 unsigned multiply, result E9..EC
            x = cpu.sfr[0xE9] | (cpu.sfr[0xEA] << 8)
            y = cpu.sfr[0xED] | (cpu.sfr[0xEE] << 8)
            r = x * y
            for i in range(4):
                cpu.sfr[0xE9 + i] = (r >> (8 * i)) & 255
        elif a == 0xEF:                      # 32-bit logical shift right by (EF & 0x1F)
            x = sum(cpu.sfr[0xE9 + i] << (8 * i) for i in range(4))
            r = x >> (v & 0x1F)
            for i in range(4):
                cpu.sfr[0xE9 + i] = (r >> (8 * i)) & 255


# ------------------------------------------------------------------ machine
PROFILES = {   # per-image entry points (docs/poe-fw.md section 3 / 16): common poll, main-loop top, host write table
    "v21": {"file": "bcm59111-v21.bin", "poll": 0x230D, "loop": 0x3D1A, "wtab": 0x26EE, "det": 0x3651},
    "meraki": {"file": "bcm59111-meraki.bin", "poll": 0x2304, "loop": 0x3D54, "wtab": 0x26E5, "det": 0x368B},
}


class Machine:
    def __init__(self, image, pt=1, passes=2, plain=False):
        prof = PROFILES[image] if image in PROFILES else PROFILES["meraki" if "meraki" in str(image) else "v21"]
        self.prof = prof
        self.poll, self.loop, self.wtab = prof["poll"], prof["loop"], prof["wtab"]
        raw = Path(image if image not in PROFILES else FW / prof["file"]).read_bytes()
        self.plain = plain
        tabs = [int.from_bytes(raw[self.wtab - BASE + 3 * r + 1:self.wtab - BASE + 3 * r + 3], "big") for r in range(128)]
        self.wdefault = max(set(tabs), key=tabs.count)
        self.raw = raw
        self.hook = {i: int.from_bytes(raw[3 * i + 1:3 * i + 3], "big") for i in range(15)}
        self.pt = pt
        self.passes = passes
        self.an = Analog()
        c = self.cpu = CPU()
        c.code[BASE:BASE + len(raw)] = raw
        for a in range(0x03, 0x100):
            c.code[a] = 0x32                         # RETI: ROM vectors
        c.code[0x1900:0x1906] = bytes([0xFF, 0x19, 0x10, 0xFF, 0x19, 0x10])
        c.code[0x1910] = 0x22                        # ROM strap pointer -> RET
        c.xram[0x8043] = 0xC0                        # DEV_ID
        c.sfr[0x81] = 0x92
        c.sfr[0x80] = c.sfr[0x90] = c.sfr[0xA0] = c.sfr[0xB0] = 0xFF   # port latches reset high (ucsim value); P0 strap / P1 address pins unknown
        self.tick = 0
        self.wlog = []                               # (tick, phase, sfr, val)
        self.xlog = []                               # host-register (XRAM 0x8000..0x8075) writes by the firmware
        self.phase = "boot"
        self.cur_f1 = 0
        c.sfr_rd = (lambda a, v: v) if plain else (lambda a, v: self.an.rd(c, a, v))
        c.sfr_wr = self.sfr_wr
        c.xram_wr = self.xram_wr
        self.watch_x = set()

    def sfr_wr(self, a, v):
        if not self.plain:
            self.an.wr(self.cpu, a, v)
        if a not in QUIET and not (0xE9 <= a <= 0xEE):
            self.wlog.append((self.tick, self.phase, a, v, self.an.ab if a in (0xAC, 0xAD) else None))

    def xram_wr(self, a, v):
        if 0x8000 <= a <= 0x8075:
            self.xlog.append((self.tick, self.phase, a, v))

    def run_until(self, stop, maxn=MAXINSN):
        c = self.cpu
        n = 0
        while c.pc != stop:
            c.step()
            n += 1
            if n > maxn:
                raise RuntimeError("runaway: pc=%04X after %d insns" % (c.pc, n))
        return n

    def boot(self):
        c = self.cpu
        c.pc = self.hook[0]
        # constant analog values the existing harness stubs for the init calibration (415C): same here
        self.an.adc = {0x14: 0x10, 0x10: 0, 0x11: 0, 0x12: 0, 0x13: 0, 0x01: 0x20, 0x00: 0x20, 0x15: 0}
        self.run_until(self.poll)
        self.phase = "run"

    def call(self, addr, **regs):
        """Call a stock function directly (bank 0 registers R0..R7 from regs, return on TRAP); caller restores state with save / restore."""
        c = self.cpu
        c.sfr[0xD0] = 0
        for k, v in regs.items():
            c.iram[int(k[1:])] = v
        c.push(TRAP & 255)
        c.push(TRAP >> 8)
        c.pc = addr
        self.run_until(TRAP)

    def inject(self, slot):
        """ISR injection at the main-loop top: save, call the hook body with the return on TRAP-equivalent (the interrupted pc), restore."""
        c = self.cpu
        save = (c.pc, c.sfr[0xE0], c.sfr[0xF0], c.sfr[0x82], c.sfr[0x83], c.sfr[0xD0], c.sfr[0x81], bytes(c.iram[0:32]))
        pc0, sp0 = c.pc, c.sfr[0x81]
        c.push(TRAP & 255)
        c.push(TRAP >> 8)
        c.pc = self.hook[slot]
        self.run_until(TRAP)
        assert c.sfr[0x81] == sp0, "stack imbalance after ISR slot %d: %02X vs %02X" % (slot, c.sfr[0x81], sp0)
        c.pc, c.sfr[0xE0], c.sfr[0xF0], c.sfr[0x82], c.sfr[0x83], c.sfr[0xD0], c.sfr[0x81] = save[:7]

    def to_loop(self):
        """Run the main loop until its top (one pass)."""
        c = self.cpu
        c.step()
        self.run_until(self.loop)

    def step_tick(self):
        """One 10 ms tick: --pt periodic ISRs interleaved with the timer-1 tick ISR and --passes main-loop passes."""
        self.tick += 1
        self.an.tick = self.tick
        # make sure we sit on the main-loop top
        if self.cpu.pc != self.loop:
            self.run_until(self.loop)
        self.inject(8)
        for ev in [e for e in self.an.sched if e[0] <= self.tick]:
            self.an.sched.remove(ev)
            self.an.bc[ev[1]] |= ev[2]
            self.an.s95 |= ev[3]
            self.an.f3 |= ev[4]
            self.inject(4 + ev[1])
        for _ in range(self.pt):
            self.inject(3)
            self.to_loop()
        for _ in range(self.passes):
            self.to_loop()

    # -- host register helpers (XRAM 0x8000 + r) --
    def reg(self, r):
        return self.cpu.xram[0x8000 + r]

    def hostwrite(self, r, *vals):
        """Host I2C write through the firmware's own write handler table (hook slot 1 path is long; the write handlers are called
        directly: XRAM byte + dirty flag, exactly what the ISR does)."""
        c = self.cpu
        for i, v in enumerate(vals):
            rr = r + i
            tab = int.from_bytes(self.raw[self.wtab - BASE + 3 * rr + 1:self.wtab - BASE + 3 * rr + 3], "big")
            c.iram[0x32] = v
            c.xram[0x8000 + rr] = v
            c.iram[0x35] = rr
            if tab != self.wdefault:
                sp0, pc0, sav = c.sfr[0x81], c.pc, (c.sfr[0xE0], c.sfr[0x82], c.sfr[0x83], c.sfr[0xD0], bytes(c.iram[0:32]))
                c.push(TRAP & 255)
                c.push(TRAP >> 8)
                c.pc = tab
                self.run_until(TRAP)
                c.pc = pc0
                c.sfr[0xE0], c.sfr[0x82], c.sfr[0x83], c.sfr[0xD0] = sav[:4]
                c.iram[0:32] = sav[4]

    def save(self):
        c, a = self.cpu, self.an
        return (bytes(c.iram), bytes(c.sfr), bytes(c.xram), c.pc, self.tick, len(self.wlog), len(self.xlog), dict(a.words), list(a.bc), a.s95,
                [list(e) for e in a.sched], list(a.det_start), dict(a.reads), a.ab, a.b5, a.f3, list(a.power), a.af)

    def restore(self, st):
        c, a = self.cpu, self.an
        c.iram[:], c.sfr[:], c.xram[:] = st[0], st[1], st[2]
        c.pc, self.tick = st[3], st[4]
        del self.wlog[st[5]:]
        del self.xlog[st[6]:]
        a.words, a.bc, a.s95, a.sched, a.det_start, a.reads, a.ab, a.b5 = dict(st[7]), list(st[8]), st[9], [list(e) for e in st[10]], list(st[11]), dict(st[12]), st[13], st[14]
        a.f3, a.power, a.af = st[15], list(st[16]), st[17]
        a.tick = self.tick

    def snapshot(self):
        c = self.cpu
        x = c.xram
        return {"SAT": list(x[0x800C:0x8010]), "PWR": x[0x8010], "INT": x[0x8000], "PWR_EVT": x[0x8002], "DETCLS_EVT": x[0x8004], "SUP": x[0x800A],
                "OPMOD": x[0x8012], "F1": c.sfr[0xF1], "F2": c.sfr[0xF2], "C9": c.sfr[0xC9], "ICUT": [x[0x8047 + 5 * i] for i in range(4)], "ILIM": [x[0x8048 + 5 * i] for i in range(4)], "HPSAT": [x[0x8049 + 5 * i] for i in range(4)],
                "A": [c.sfr[0xA1], c.sfr[0xA2], c.sfr[0xA3], c.sfr[0xA4]]}


KEY = {0xA1, 0xA2, 0xA3, 0xA4, 0xB1, 0xB2, 0xB3, 0xB4, 0xC9, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD9, 0xDA, 0xDB, 0xF1, 0xF2, 0x93, 0x95, 0x96, 0xF9, 0xBC, 0xBD, 0xBE, 0xBF}


def keylog(m, since=0, keys=KEY, dedup=True):
    """Key SFR writes (limits, controls, detect / class start, power set / clear), consecutive identical writes collapsed."""
    out, last = [], None
    for w in m.wlog:
        if w[0] < since or w[2] not in keys or w[1] == "boot":
            continue
        k = (w[2], w[3])
        if dedup and k == last:
            continue
        last = k
        out.append(fmt_w(w))
    return out


def hx(v):
    return " ".join("%02X" % b for b in v)


def fmt_w(w):
    t, ph, a, v, ab = w
    n = SFRN.get(a, "%02X" % a)
    return "t%-4d %-4s %-10s = %02X%s" % (t, ph, n, v, ("  (AB=%02X)" % ab) if ab is not None else "")


def host_setup(m, opmod=0x03):
    """Host state after the kernel driver's probe (docs/poe.md section 8): 0x01 = 0x66, 0x13 = 0x0F, 0x14 = 0xFF, 0x44 = 0x0F, per channel
    HP_Pn_MOD (0x46 + 5c) = 0x01, OV_CUT (0x47 + 5c) = 0xE2, CL_FBC / ILIM (0x48 + 5c) = 0xC0, then OP_MOD."""
    m.hostwrite(0x01, 0x66)
    m.hostwrite(0x13, 0x0F)
    m.hostwrite(0x14, 0xFF)
    m.hostwrite(0x44, 0x0F)
    for c in range(4):
        m.hostwrite(0x46 + 5 * c, 0x01, 0xE2, 0xC0)
    m.hostwrite(0x12, opmod)


def snapfmt(m):
    s = m.snapshot()
    return "SAT %s PWR %02X INT %02X PWR_EVT %02X DC_EVT %02X SUP %02X OPMOD %02X F1 %02X F2 %02X C9 %02X A %s OVCUT %s ILIM %s HPSAT %s" % (
        hx(s["SAT"]), s["PWR"], s["INT"], s["PWR_EVT"], s["DETCLS_EVT"], s["SUP"], s["OPMOD"], s["F1"], s["F2"], s["C9"], hx(s["A"]), hx(s["ICUT"]), hx(s["ILIM"]), hx(s["HPSAT"]))


def run_ticks(m, n, show=False):
    """Run n ticks; with show, log the published state whenever it changes."""
    last = None
    t0 = time.time()
    for _ in range(n):
        m.step_tick()
        if show:
            cur = snapfmt(m)
            if cur != last:
                log("  t%-4d %s" % (m.tick, cur))
                last = cur
        if time.time() - t0 > 3600:
            raise RuntimeError("overall guard: 1 h wall clock (a full run takes ~1 min)")


def first_tick(m, sfr, pred=lambda v: True, since=0):
    for w in m.wlog:
        if w[2] == sfr and w[0] >= since and w[1] != "boot" and pred(w[3]):
            return w[0]
    return None


def counters(m):
    return [m.cpu.xram[0x8060 + i] for i in range(16)]


def summary(m):
    s = m.snapshot()
    return {"SAT": hx(s["SAT"]), "PWR": "%02X" % s["PWR"], "F1": "%02X" % s["F1"], "F2": "%02X" % s["F2"], "Alim": "%02X" % s["A"][CH],
            "OVCUT": "%02X" % s["ICUT"][CH], "ILIM": "%02X" % s["ILIM"][CH], "HPSAT": "%02X" % s["HPSAT"][CH], "PWR_EVT": "%02X" % s["PWR_EVT"], "SUP": "%02X" % s["SUP"],
            "ctr": hx(counters(m)[:8]), "D1starts": sum(1 for w in m.wlog if w[2] == 0xD1 + CH and w[3] == 1 and w[1] != "boot"),
            "tF1": first_tick(m, 0xF1, lambda v: v & (1 << CH)), "tPG": first_tick(m, 0xBC + CH, lambda v: v == 0x10)}


def new(img, args, opmod=None):
    m = Machine(img, args.pt, args.passes)
    m.boot()
    host_setup(m, (3 << (2 * CH)) if opmod is None else opmod)
    return m


EXPS = {}
IMAGES = ["v21", "meraki"]
CH = 0                  # channel under test (--ch)


def exp(f):
    EXPS[f.__name__] = f
    return f


def sidebyside(title, rows, args, fn):
    """rows: list of (label, kwargs); fn(img, kwargs) -> dict. Logs v21 and v18 per row and whether they agree."""
    log("-- " + title)
    agree = 0
    for lab, kw in rows:
        res = {img: fn(img, kw) for img in IMAGES}
        if len(IMAGES) == 2:
            same = res["v21"] == res["meraki"]
            agree += same
        for img in IMAGES:
            log("%-34s %-5s: %s%s" % (lab if img == IMAGES[0] else "", "v21" if img == "v21" else "v18",
                                      " ".join("%s=%s" % kv for kv in res[img].items()),
                                      "   <-- DIFFERS from v21" if img == "meraki" and len(IMAGES) == 2 and not same else ""))
    if len(IMAGES) == 2:
        log("   v21 == v18 in %d / %d rows" % (agree, len(rows)))


def mkword(m, det=None, cls=None, cm=None, vm=None, dsamp=None, temp=None, supply=None):
    """Analog model: indirect words by AB index (4..7 detect-sample / VM, 8..11 class-probe / CM, 0x2C temp, 0x30 supply)."""
    an = m.an
    if det is not None:
        an.det_code[:] = det if isinstance(det, list) else [det] * 4
    if temp is not None:
        an.default_words[0x2C] = temp
    if supply is not None:
        an.default_words[0x30] = supply
    cls_w = cls if isinstance(cls, list) else [cls or 0] * 4
    cm_w = cm if isinstance(cm, list) else [cm or 0] * 4
    ds = dsamp if isinstance(dsamp, list) else [dsamp or 0] * 4
    vm_w = vm if isinstance(vm, list) else [vm or 0] * 4

    def fn(a, idx):
        if 8 <= idx <= 11:
            c = idx - 8
            return cm_w[c] if a.power[c] else cls_w[c]
        if 4 <= idx <= 7:
            c = idx - 4
            return vm_w[c] if a.power[c] else ds[c]
        return None
    an.word_fn = fn


@exp
def open_port(args):
    """(a) open port: detect code 6 with every ADC word value tried: SAT_P0 = 0x06, nothing else happens."""
    def fn(img, kw):
        m = new(img, args)
        mkword(m, det=6, cls=kw["w"], dsamp=kw["w"])
        run_ticks(m, 200)
        return summary(m)
    sidebyside("open port: detect code 6, class / detect-sample words = w", [("w=%04X" % w, {"w": w}) for w in (0, 0x40, 0x100, 0x300, 0x3FF, 0xFFF)], args, fn)


@exp
def det_codes(args):
    """Detect result code (D >> 4) 0..15 -> SAT low nibble, class start, power, retry cadence, invalid counter."""
    def fn(img, kw):
        m = new(img, args)
        mkword(m, det=kw["c"], cls=0x300)
        run_ticks(m, 300)
        r = summary(m)
        r["D5D6"] = sum(1 for w in m.wlog if w[2] in (0xD5, 0xD6) and w[3] and w[1] != "boot")
        return r
    sidebyside("detect code D>>4 (class word 0x300, 300 ticks)", [("code %X" % c, {"c": c}) for c in range(16)], args, fn)


def det_call(m, base, words, code=0xF, ch=None):
    ch = CH if ch is None else ch
    """Stock detect classifier (v21 0x3651 / v18 0x368B) on 4 sample words of one channel; returns the detect nibble (IRAM 0x42 + ch, copied to SAT_Pn by 0x3B3C)."""
    m.restore(base)
    m.an.det_words[ch] = list(words)
    m.cpu.xram[0x8133] = ch
    m.call(m.prof["det"], R5=code, R7=ch)
    return m.cpu.iram[0x42 + ch] & 15


GRID = (0, 0x20, 0x50, 0x56, 0x6F, 0x90, 0x97, 0xC0)


def words_grid():
    ws = []
    for d0 in GRID:
        for d1 in GRID:
            for d2 in GRID:
                ws.append((0x100 + d0 + d1 + d2, 0x100 + d1 + d2, 0x100 + d2, 0x100))
    for w2 in (0x300, 0x34F, 0x354, 0x355, 0x356, 0x400):          # open threshold on W2 (absolute)
        ws.append((w2 + 0x80, w2 + 0x40, w2, w2 - 0x10))
    for lo in (0x30, 0x5B, 0x5C, 0x5D):                              # open threshold on W2 - W3
        ws.append((0x300 + lo + 0x10, 0x300 + lo + 8, 0x300 + lo, 0x300))
    return ws


def with_mod(m, mod):
    m.cpu.xram[0x8046 + 5 * CH] = mod                              # HP_Pn_MOD: bit 2 gates the classifier's "good" result (0x4D83 mask 4)


@exp
def detect_words(args):
    """(a)/(b) detect classifier 0x3651: 4 sample words W0..W3 (D9 = 4c + k, via DA:DB) -> detect nibble, for HP_Pn_MOD 0x01 (driver value) and 0x05."""
    res = {}
    for img in IMAGES:
        m = new(img, args)
        for mod in (0x01, 0x05):
            with_mod(m, mod)
            base = m.save()
            out = {w: det_call(m, base, w) for w in words_grid()}
            out["D>>4=0 passthrough"] = det_call(m, base, (0, 0, 0, 0), code=0)
            out["D>>4=1 passthrough"] = det_call(m, base, (0x400, 0x300, 0x200, 0x100), code=1)
            out["D>>4=4 passthrough"] = det_call(m, base, (0, 0, 0, 0), code=4)
            res[(img, mod)] = out
            hist = {}
            for k, v in out.items():
                hist[v] = hist.get(v, 0) + 1
            log("%-6s MOD=%02X: detect nibble histogram over %d inputs: %s" % (img, mod, len(out), dict(sorted(hist.items()))))
            for code in sorted(hist):
                ex = [k for k, v in out.items() if v == code and isinstance(k, tuple)][:2]
                log("%-6s MOD=%02X   nibble %X e.g. (W0 W1 W2 W3): %s" % (img, mod, code, ["%03X %03X %03X %03X" % k for k in ex]))
            good = [k for k, v in out.items() if v == 4 and isinstance(k, tuple)]
            if good:
                log("%-6s MOD=%02X   good example: %s" % (img, mod, ["%03X" % x for x in min(good, key=lambda w: w[0] - w[3])]))
    if len(IMAGES) == 2:
        for mod in (0x01, 0x05):
            a, b = res[("v21", mod)], res[("meraki", mod)]
            diff = [k for k in a if a[k] != b[k]]
            log("   MOD=%02X: v21 == v18 on %d / %d inputs" % (mod, len(a) - len(diff), len(a)))


@exp
def detect_chain(args):
    """Full chain with D >> 4 = 0xF and sample words (first grid example per classifier result): 0x44 + power for good, 0x06 for open, ..."""
    m0 = new(IMAGES[0], args)
    rows = []
    for mod in (5, 1):
        with_mod(m0, mod)
        base = m0.save()
        ex = {}
        for w in words_grid():
            ex.setdefault(det_call(m0, base, w), w)
        for nib in (4, 6, 5, 3, 2):
            if nib in ex:
                rows.append(("MOD %02X words %s -> %X" % (mod, "/".join("%03X" % x for x in ex[nib]), nib), {"w": ex[nib], "mod": mod}))

    def fn(img, kw):
        m = new(img, args)
        with_mod(m, kw["mod"])
        mkword(m, det=0xF, cls=0x300, cm=0x30, vm=0x32E)
        m.an.det_words[CH] = list(kw["w"])
        run_ticks(m, 80)
        return summary(m)
    sidebyside("detect via sample words (D >> 4 = 0xF), class word 0x300, 80 ticks; row label = expected classifier nibble", rows, args, fn)


@exp
def class_words(args):
    """(b) class word -> SAT class nibble, limits after power-up. Detect code 4."""
    def fn(img, kw):
        m = new(img, args)
        mkword(m, det=4, cls=kw["w"], cm=kw.get("cm", 0))
        run_ticks(m, 120)
        return summary(m)
    ws = (0, 0x6E, 0x6F, 0xF6, 0xF7, 0x188, 0x189, 0x232, 0x233, 0x321, 0x322, 0x3FF)
    sidebyside("class word (AB = 8, any AF) with detect code 4, 120 ticks", [("w=%03X" % w, {"w": w}) for w in ws], args, fn)


@exp
def power_trace(args):
    """(b)/(d) camera: detect 4 + class word 0x300 (class 4): ordered key SFR writes and published state of both images."""
    out = {}
    machines = {}
    for img in IMAGES:
        m = new(img, args)
        machines[img] = m
        mkword(m, det=4, cls=0x300, cm=0x30, vm=0x32E)
        log("== %s published state changes" % img)
        run_ticks(m, 60, show=True)
        out[img] = keylog(m, since=1)
        log("== %s key SFR writes (consecutive duplicates collapsed)" % img)
        for ln in out[img]:
            log(ln)
        log("   host XRAM writes by the firmware (reg = XRAM - 0x8000), regs 0x0C..0x10 / 0x46..0x58 only")
        for t, ph, a, v in m.xlog:
            if ph != "boot" and (0x0C <= a - 0x8000 <= 0x10 or 0x46 <= a - 0x8000 <= 0x58):
                log("   t%-4d reg %02X = %02X" % (t, a - 0x8000, v))
    if len(IMAGES) == 2:
        strip = {i: [ln[ln.index("run"):] for ln in out[i]] for i in IMAGES}
        log("   key write sequence identical v21 / v18 (SFR + value, order only): %s; with ticks: %s" % (strip["v21"] == strip["meraki"], out["v21"] == out["meraki"]))
        if strip["v21"] != strip["meraki"]:
            log("   only v21: %s" % sorted(set(strip["v21"]) - set(strip["meraki"]))[:10])
            log("   only v18: %s" % sorted(set(strip["meraki"]) - set(strip["v21"]))[:10])
        for lab, sfr, pred in (("F1 enable", 0xF1 , lambda v: v & (1 << CH)), ("limit A%d" % (CH + 1), 0xA1 + CH, lambda v: v != 0xF0), ("C9 gate released", 0xC9, lambda v: not v & (1 << CH))):
            log("   first %-16s v21 t%s  v18 t%s" % (lab, first_tick(machines["v21"], sfr, pred, 2), first_tick(machines["meraki"], sfr, pred, 2)))


@exp
def power_good(args):
    """(c) power-good: BC bit 4 event + F3 bit; variants: no event, event without F3, delayed."""
    def fn(img, kw):
        m = new(img, args)
        mkword(m, det=4, cls=0x300)
        m.an.pg_latency = kw["lat"]
        m.an.pd_ok[CH] = kw["ok"]
        run_ticks(m, 400)
        return summary(m)
    rows = [("event +2, F3 set", {"lat": 2, "ok": True}), ("event +10, F3 set", {"lat": 10, "ok": True}), ("event +60, F3 set", {"lat": 60, "ok": True}),
            ("event +2, F3 clear (power-up failed)", {"lat": 2, "ok": False}), ("no event at all", {"lat": 10 ** 6, "ok": True})]
    sidebyside("power-good variants (class 4 camera, 400 ticks)", rows, args, fn)


@exp
def reactions(args):
    """(e) disconnect / overload: steady powered class-4 port, then one hardware event or analog change."""
    def fn(img, kw):
        m = new(img, args)
        mkword(m, det=4, cls=0x300, cm=0x30, vm=0x32E)
        run_ticks(m, 60)
        pre = summary(m)
        if "bc" in kw:
            m.an.bc[CH] |= kw["bc"]
            m.an.s95 |= kw.get("s95", 0)
            m.inject(4 + CH)
        if "cm" in kw:
            mkword(m, det=4, cls=0x300, cm=kw["cm"], vm=0x32E)
        if "supply" in kw:
            m.an.default_words[0x30] = kw["supply"]
        if "temp" in kw:
            m.an.default_words[0x2C] = kw["temp"]
        t0 = m.tick
        run_ticks(m, 150)
        r = summary(m)
        r["PWR_before"] = pre["PWR"]
        r["tF1off"] = first_tick(m, 0xF1, lambda v: not v & (1 << CH), since=t0 + 1)
        r["regs"] = " ".join("%02X=%02X" % (i, m.cpu.xram[0x8000 + i]) for i in (0x00, 0x02, 0x04, 0x06, 0x08, 0x0A))
        return r
    rows = [("steady (no event, 150 ticks)", {})]
    rows += [("BC bit %d" % b, {"bc": 1 << b}) for b in range(8)]
    rows += [("BC bit 1 + S95 bit 1", {"bc": 2, "s95": 2}), ("CM word 0xFFF", {"cm": 0xFFF}), ("CM word 0 (no load)", {"cm": 0}),
             ("supply word 0x0400 (low)", {"supply": 0x0400}), ("temp word 0x600 (hot)", {"temp": 0x600})]
    sidebyside("reactions after steady power", rows, args, fn)


@exp
def timing(args):
    """Sensitivity to the unknown periodic-ISR rate: periodic ISR calls per 10 ms tick (--pt) 1..5, class-4 camera."""
    def fn(img, kw):
        m = Machine(img, kw["pt"], args.passes)
        m.boot()
        host_setup(m, 3 << (2 * CH))
        mkword(m, det=4, cls=0x300)
        run_ticks(m, 120)
        return summary(m)
    sidebyside("periodic ISR calls per tick", [("pt=%d" % p, {"pt": p}) for p in (1, 2, 3, 5)], args, fn)


@exp
def smoke(args):
    """boot + host setup + --ticks ticks with an open port: state changes and key writes."""
    m = new("v21" if args.image == "both" else args.image, args)
    run_ticks(m, args.ticks, show=True)
    for ln in keylog(m)[:150]:
        log(ln)


def xcheck(args):
    """Core validation: stock boot + 20 ticks on ucsim_51 (poe_fw_sim harness, plain SFR registers) vs this core in plain mode."""
    import poe_fw_sim as S
    ok = True
    for img in IMAGES:
        prof = PROFILES[img]
        sim = S.Sim(FW / prof["file"])
        S.boot(sim, prof["poll"])
        m = Machine(img, 1, 1, plain=True)
        m.cpu.sfr[0xAC], m.cpu.sfr[0xAD], m.cpu.sfr[0xB6], m.cpu.sfr[0xB7], m.cpu.sfr[0xD7] = 0xE0, 1, 0x10, 0x20, 5
        m.boot()
        for t in range(21):
            if t:
                sim.batch([f"set mem sfr 0x81 0x{S.STACK:x}", f"set mem iram 0x{S.STACK - 1:x} 0x{S.TRAP & 255:x} 0x{S.TRAP >> 8:x}",
                           f"pc 0x{sim.hook[8]:x}", f"step {S.STEPS}", "set mem sfr 0x88 0", "set mem sfr 0xa8 0", f"set mem sfr 0x81 0x{sim.sp_main:x}",
                           f"pc 0x{prof['poll']:x}", f"step {S.STEPS}", "set mem sfr 0xd1 0x40"])
                m.inject(8)
                c = m.cpu
                c.sfr[0x88], c.sfr[0xA8] = 0, 0
                c.pc, c.sfr[0x81] = prof["poll"], sim.sp_main
                c.step()
                m.run_until(prof["poll"])
                c.sfr[0xD1] = 0x40
            ui, ux = sim.dump("iram", 0x10, 0x7F), sim.dump("xram", 0x8000, 0x8140)
            di = [hex(0x10 + i) for i in range(len(ui)) if ui[i] != m.cpu.iram[0x10 + i]]
            dx = [hex(0x8000 + i) for i in range(len(ux)) if ux[i] != m.cpu.xram[0x8000 + i]]
            if t in (0, 1, 5, 20):
                log("xcheck %-6s tick %2d: IRAM 0x10..0x7F diffs %s, XRAM 0x8000..0x8140 diffs %s" % (img, t, di[:12], dx[:12]))
            ok &= not di and not dx
        sim.close()
    log("xcheck: %s" % ("identical" if ok else "DIFFERENCES (see above)"))
    return ok


def main():
    help_guard(__doc__)
    import argparse
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--image", default="both", help="v21 | meraki | both (default)")
    ap.add_argument("--exp", nargs="*", default=[])
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--ticks", type=int, default=200)
    ap.add_argument("--ch", default="1", help="channel(s) under test 0..3, comma list (default 1 = the board camera port, regs 0x4C / 0x4E)")
    ap.add_argument("--pt", type=int, default=1)
    ap.add_argument("--passes", type=int, default=2)
    ap.add_argument("--xcheck", action="store_true")
    args = ap.parse_args()
    global IMAGES, CH
    if args.image != "both":
        IMAGES = [args.image]
    if args.list:
        for k, f in EXPS.items():
            print(k, "-", (f.__doc__ or "").strip().splitlines()[0])
        return 0
    if args.xcheck:
        return 0 if xcheck(args) else 1
    for ch in [int(x) for x in args.ch.split(",")]:
        CH = ch
        log("######## channel under test: %d (images: %s)" % (ch, ", ".join(IMAGES)))
        for k in (args.exp or [e for e in EXPS if e != "smoke"]):
            log("== " + k + ": " + (EXPS[k].__doc__ or "").strip().splitlines()[0])
            EXPS[k](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
