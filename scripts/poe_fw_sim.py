#!/usr/bin/env python3
"""Simulator tests for the PSE firmware rewrite (poe-fw/, #26) on ucsim_51 with the DW8051 model (-t RL6449, as awto-sdcc's tests).

Usage: poe_fw_sim.py [--image PATH] [--stock-diff]
  Boots the image at its hook-table slot 0 (start-up) up to the main loop, injects the I2C slave SFR sequences of
  docs/poe-fw.md section 9 (SFR 0xDD status, 0xF7 code) by calling hook slot 1 with the return address on a trap, and asserts:
  register write / read round trip, auto-increment, clear-on-read (03/05/07/09/0B, counters), OP_MOD write restarts channel
  flags, INT line, bus error flag, FW_REV / CRC bytes, and the channel state machine with the analog side STUBBED (SFR
  0xD1 forced to a good-detect code; no class / measurement hardware: results below are firmware logic only).
  --stock-diff: run the I2C scenarios on hwdb/firmware/bcm59111-v21.bin too and list differing observations.
  Default image: hwdb/firmware/bcm59111-awto.bin; the no-power image is tested for 'no power-up' as well.
  ROM (<0x1A00) is not available: vectors hold RETI, the ROM strap pointer (0x1900 / 0x1903) points at a RET.
  ucsim is driven over its TCP console (-Z), one command at a time (stdin / -C modes do not run `step` reliably).
  Log: tmp/logs/poe_fw_sim.log. Exit 1 on any failed assertion.
"""
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FW = ROOT / "hwdb/firmware"
WORK = ROOT / "tmp/poe-fw-sim"
LOG = ROOT / "tmp/logs/poe_fw_sim.log"
BASE = 0x1A00
TRAP = 0x1A50          # zero padding inside the hook table area: ucsim fetch breakpoint
STACK = 0xC0           # ISR stack (upper IRAM, clear of the main stack that starts at BOOT_SP)
BOOT_SP = 0x92         # ROM stack stand-in at start-up
STEPS = 20000          # instruction budget per ucsim step, bounded by the trap / breakpoint (longest path: boot); a miss shows as a failed check
STOCK_POLL = 0x230D    # stock main-loop entry (poll_common)
PORT = 5600

results = []


def log(msg):
    print(msg)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(msg + "\n")


def check(name, cond, detail=""):
    results.append(bool(cond))
    log(f"{'PASS' if cond else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not cond else ""))


def to_ihx(binpath, out):
    d = binpath.read_bytes()
    lines = []
    for i in range(0, len(d), 16):
        ch = d[i:i + 16]
        a = BASE + i
        rec = bytes([len(ch), a >> 8, a & 255, 0]) + ch
        lines.append(":" + rec.hex().upper() + "%02X" % ((-sum(rec)) & 255))
    lines.append(":00000001FF")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")


def map_syms(image):
    mp = ROOT / "tmp/poe-fw-build" / ("nopower" if "nopower" in image.name else "main") / "out.map"
    syms = {}
    if mp.exists():
        for m in re.finditer(r"^(?:C:)?\s+([0-9A-F]{8})\s+(_\w+)\s", mp.read_text(), re.M):
            syms[m.group(2)] = int(m.group(1), 16)
    return syms


class Sim:
    """One ucsim_51 -t RL6449 process, driven over the TCP console."""

    def __init__(self, image):
        global PORT
        PORT += 1
        self.image = image
        WORK.mkdir(parents=True, exist_ok=True)
        ihx = WORK / (image.stem + ".ihx")
        to_ihx(image, ihx)
        self.p = subprocess.Popen(["ucsim_51", "-q", "-t", "RL6449", "-Z", str(PORT), str(ihx)], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                self.s = socket.create_connection(("127.0.0.1", PORT), timeout=60)
                self.s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                break
            except OSError:
                time.sleep(0.1)
        self.read()
        raw = image.read_bytes()
        self.hook = {i: int.from_bytes(raw[3 * i + 1:3 * i + 3], "big") for i in range(15)}
        self.nbp = 0
        self.sp_main = BOOT_SP

    def read_until(self, mark):
        buf = b""
        while not re.search(rb"@@" + mark + rb"@@\r?\n\d+> $", buf):
            ch = self.s.recv(65536)
            if not ch:
                raise RuntimeError("ucsim closed the console")
            buf += ch
        return buf.decode(errors="replace")

    def read(self):
        """Banner / first prompt."""
        buf = b""
        while not re.search(rb"\d+> $", buf):
            ch = self.s.recv(65536)
            if not ch:
                raise RuntimeError("ucsim closed the console")
            buf += ch
        return buf.decode(errors="replace")

    def batch(self, cmds):
        """Send several commands in one write (each console round trip costs ~0.1 s). Every command is followed by an
        `echo @@k@@` marker: the console echoes the whole input block and interleaves answers, so only the end of the batch is
        detectable. Returns the full text; callers look for unique rows (dump addresses) or stop messages in it."""
        self.seq = getattr(self, "seq", 0)
        lines = []
        for c in cmds:
            self.seq += 1
            lines += [c, f"echo @@{self.seq}@@"]
        first = self.seq - len(cmds) + 1
        self.s.sendall(("\n".join(lines) + "\n").encode())
        text = self.read_until(str(self.seq).encode())
        return text

    def c(self, text):
        return self.batch([text])

    def close(self):
        try:
            self.s.sendall(b"quit\n")
        except OSError:
            pass
        self.p.kill()
        self.p.wait()

    def dump(self, mem, lo, hi=None):
        hi = lo if hi is None else hi
        return self.parse_dump(self.c(f"dump /h {mem} 0x{lo:x} 0x{hi:x}"), hi - lo + 1)

    @staticmethod
    def parse_dump(text, n):
        out = []
        for ln in text.splitlines():
            m = re.match(r"^0x[0-9a-fA-F]+\s+((?:[0-9a-fA-F]{2}\s)+)", ln + " ")
            if m:
                out += [int(x, 16) for x in m.group(1).split()]
        return out[:n]

    def setmem(self, mem, addr, *vals):
        self.c(f"set mem {mem} 0x{addr:x} " + " ".join(f"0x{v:x}" for v in vals))

    def stack_return(self):
        self.setmem("sfr", 0x81, STACK)
        self.setmem("iram", STACK - 1, TRAP & 255, TRAP >> 8)

    def resume(self, poll):
        """Back to the interrupted main loop: its stack pointer and the loop-top address."""
        self.batch([f"set mem sfr 0x81 0x{self.sp_main:x}", f"pc 0x{poll:x}"])

    def call(self, target):
        """Call a hook: return address = TRAP, run until the trap fetch."""
        return self.batch([f"set mem sfr 0x81 0x{STACK:x}", f"set mem iram 0x{STACK - 1:x} 0x{TRAP & 255:x} 0x{TRAP >> 8:x}",
                           f"pc 0x{target:x}", f"step {STEPS}"])


def boot(sim, poll):
    """Common prologue: vectors, ROM strap pointer, zeroed host window, ROM-provided DEV_ID, run to the main loop."""
    cmds = [f"set mem rom 0x{v:x} 0x32" for v in (0x03, 0x0B, 0x13, 0x1B, 0x23, 0x2B)]      # RETI vectors
    cmds += ["set mem rom 0x1900 0xff 0x19 0x10 0xff 0x19 0x10", "set mem rom 0x1910 0x22"]  # ROM strap pointer -> RET
    cmds += [f"set mem xram 0x{a:x} " + " ".join(["0"] * 16) for a in range(0x8000, 0x8200, 16)]
    cmds += ["set mem xram 0x8043 0xc0"]                                                    # DEV_ID, ROM / hardware
    cmds += [f"set mem sfr 0x81 0x{BOOT_SP:x}", f"break 0x{TRAP:x}", f"break 0x{poll:x}", f"pc 0x{sim.hook[0]:x}"]
    sim.batch(cmds)
    sim.nbp = 2
    out = sim.c(f"step {STEPS * 40}")
    sim.sp_main = sim.dump("sfr", 0x81)[0]
    return int(re.search(r"Stop at 0x([0-9a-f]+)", out).group(1), 16) == poll


def i2c(sim, poll, code, data=None, tx=False, dd=0x11):
    """One I2C slave event through hook slot 1, resuming the main loop afterwards. F7 is read twice by the ISR (status,
    then data for code 0x80): the second value is injected with a read event breakpoint. Returns the first F6 write (TX
    data) if tx."""
    cmds = [f"set mem sfr 0xdd 0x{dd:x}", f"set mem sfr 0xf7 0x{code:x}", f"set mem sfr 0x81 0x{STACK:x}",
            f"set mem iram 0x{STACK - 1:x} 0x{TRAP & 255:x} 0x{TRAP >> 8:x}"]
    if data is not None:
        sim.nbp += 1
        n = sim.nbp
        cmds.append("break sfr r 0xf7")
    if tx:
        sim.nbp += 1
        nw = sim.nbp
        cmds.append("break sfr w 0xf6")
    cmds += [f"pc 0x{sim.hook[1]:x}", f"step {STEPS}"]
    if data is not None:
        cmds += [f"set mem sfr 0xf7 0x{data:x}", f"delete {n}", f"step {STEPS}"]
    if tx:
        cmds.append("dump /h sfr 0xf6 0xf6")
        cmds += [f"delete {nw}", f"step {STEPS}"]
    cmds += [f"set mem sfr 0x81 0x{sim.sp_main:x}", f"pc 0x{poll:x}"]
    text = sim.batch(cmds)
    if not tx:
        return None
    m = re.search(r"0xf6\s+([0-9a-f]{2})\s", text)
    return int(m.group(1), 16) if m else None


def wr(sim, poll, reg, *vals):
    i2c(sim, poll, 0x60)
    i2c(sim, poll, 0x80, reg)
    for v in vals:
        i2c(sim, poll, 0x80, v)
    i2c(sim, poll, 0xA0)


def rd(sim, poll, reg, n=1):
    """Read n bytes from reg: SLA+W, pointer, SLA+R, data acks, stop. Returns the TX bytes."""
    i2c(sim, poll, 0x60)
    i2c(sim, poll, 0x80, reg)
    out = [i2c(sim, poll, 0xA8, tx=True)]
    for _ in range(n - 1):
        out.append(i2c(sim, poll, 0xB8, tx=True))
    i2c(sim, poll, 0xA0)
    return out


def host_scenarios(image, stock):
    poll = STOCK_POLL if stock else map_syms(image)["_poll_common"]
    sim = Sim(image)
    try:
        reached = boot(sim, poll)
        o = {"boot_reached_main_loop": reached}
        o["FW_REV"] = sim.dump("xram", 0x8041)[0]
        o["CRC"] = sim.dump("xram", 0x8075)[0]
        wr(sim, poll, 0x13, 0x0A, 0x5F)              # write round trip + auto-increment (DIS_SEN_EN, DET_CLASS_EN)
        o["w13"] = sim.dump("xram", 0x8013, 0x8014)
        o["dirty"] = sim.dump("iram", 0x20, 0x22)
        o["rd13"] = rd(sim, poll, 0x13, 2)
        wr(sim, poll, 0x20, 0x55)                    # register without handler
        o["w20"] = sim.dump("xram", 0x8020)[0]
        sim.setmem("xram", 0x8000, 0x03, 0x03, 0x11, 0x11)   # clear-on-read 03
        sim.setmem("sfr", 0xF8, 0x01)
        o["rd03"] = rd(sim, poll, 0x03)
        o["c03"] = sim.dump("xram", 0x8000, 0x8003)
        o["f8a_bit0"] = sim.dump("sfr", 0xF8)[0] & 1
        sim.setmem("xram", 0x8000, 0x18, 0xFF, 0, 0, 0x22, 0x22)   # 05
        sim.setmem("sfr", 0xF8, 0x01)
        o["rd05"] = rd(sim, poll, 0x05)
        o["c05"] = sim.dump("xram", 0x8000, 0x8005)
        sim.setmem("xram", 0x8000, 0x05, 0xFF, 0, 0, 0, 0, 0x11, 0x11, 0x20)   # 07, 0x8008 hi nibble set
        sim.setmem("sfr", 0xF8, 0x01)
        o["rd07"] = rd(sim, poll, 0x07)
        o["c07"] = sim.dump("xram", 0x8000, 0x8008)
        o["f8b_bit0"] = sim.dump("sfr", 0xF8)[0] & 1
        sim.setmem("xram", 0x8000, 0x60, 0xFF, 0, 0, 0, 0, 0, 0, 0x40, 0x40)   # 09
        sim.setmem("sfr", 0xF8, 0x01)
        o["rd09"] = rd(sim, poll, 0x09)
        o["c09"] = sim.dump("xram", 0x8000, 0x8009)
        sim.setmem("xram", 0x8000, 0x80, 0xFF, 0, 0, 0, 0, 0, 0, 0, 0, 0x80, 0x80)   # 0B
        sim.setmem("sfr", 0xF8, 0x01)
        o["rd0b"] = rd(sim, poll, 0x0B)
        o["c0b"] = sim.dump("xram", 0x8000, 0x800B)
        o["f8c_bit0"] = sim.dump("sfr", 0xF8)[0] & 1
        sim.setmem("xram", 0x8060, 7)                # counter read-clear
        o["rd60"] = rd(sim, poll, 0x60)
        o["c60"] = sim.dump("xram", 0x8060)[0]
        sim.setmem("iram", 0x23, 0)                  # OP_MOD write sets 0x23.1..4
        wr(sim, poll, 0x12, 0xC6)
        o["w12"] = sim.dump("xram", 0x8012)[0]
        o["svc_1e"] = sim.dump("iram", 0x23)[0] & 0x1E
        sim.setmem("xram", 0x8000, 0x00)             # INT_MASK write drops the line
        sim.setmem("sfr", 0xF8, 0x01)
        wr(sim, poll, 0x01, 0x00)
        o["f8d_bit0"] = sim.dump("sfr", 0xF8)[0] & 1
        sim.setmem("xram", 0x8042, 0x00)             # bus error
        sim.setmem("xram", 0x804A, 0x00)
        i2c(sim, poll, 0xFF, dd=0x88)
        o["err42"] = sim.dump("xram", 0x8042)[0]
        o["err4a"] = sim.dump("xram", 0x804A)[0]
        return o
    finally:
        sim.close()


def verdicts(o, rev):
    check("boot at hook slot 0 reaches the main loop", o["boot_reached_main_loop"])
    check("FW_REV byte", o["FW_REV"] == rev, f"{o['FW_REV']:02X}")
    check("CRC_STATUS 0xAA", o["CRC"] == 0xAA)
    check("write round trip + auto-increment (0x13, 0x14)", o["w13"] == [0x0A, 0x5F], str(o["w13"]))
    check("write 0x13 sets dirty 0x21.0, write 0x14 sets 0x22.1", (o["dirty"][1] & 1) and (o["dirty"][2] & 2), str(o["dirty"]))
    check("read round trip + auto-increment", o["rd13"] == [0x0A, 0x5F], str(o["rd13"]))
    check("write to a register without handler is ignored", o["w20"] == 0)
    check("read 0x03 returns the event, clears 02/03, INT &= 0xFC, line off",
          o["rd03"] == [0x11] and o["c03"][0] == 0 and o["c03"][2:] == [0, 0] and o["f8a_bit0"] == 0, f"{o['rd03']} {o['c03']}")
    check("read 0x05 clears 04/05, INT &= 0xE7", o["rd05"] == [0x22] and o["c05"][0] == 0 and o["c05"][4:] == [0, 0],
          f"{o['rd05']} {o['c05']}")
    check("read 0x07 clears 06/07, INT bit 2 only (0x8008 hi nibble set), line stays",
          o["rd07"] == [0x11] and o["c07"][0] == 0x01 and o["c07"][6:8] == [0, 0] and o["f8b_bit0"] == 1,
          f"{o['rd07']} {o['c07']} {o['f8b_bit0']}")
    check("read 0x09 clears 08/09, INT &= 0x9F", o["rd09"] == [0x40] and o["c09"][0] == 0 and o["c09"][8:] == [0, 0],
          f"{o['rd09']} {o['c09']}")
    check("read 0x0B clears 0A/0B, INT bit 7, line off",
          o["rd0b"] == [0x80] and o["c0b"][0] == 0 and o["c0b"][10:] == [0, 0] and o["f8c_bit0"] == 0, f"{o['rd0b']} {o['c0b']}")
    check("counter 0x60 read-clears", o["rd60"] == [7] and o["c60"] == 0)
    check("OP_MOD write stores and sets service flags 0x23.1..4", o["w12"] == 0xC6 and o["svc_1e"] == 0x1E,
          f"{o['w12']:02X} {o['svc_1e']:02X}")
    check("INT_MASK write with nothing pending drops the INT line", o["f8d_bit0"] == 0)
    check("bus error sets 0x8042.0 and 0x804A", o["err42"] & 1 and o["err4a"] == 1, f"{o['err42']:02X} {o['err4a']:02X}")


def statemachine(image):
    poll = map_syms(image)["_poll_common"]
    sim = Sim(image)
    out = []
    try:
        boot(sim, poll)
        sim.setmem("xram", 0x8014, 0xFF)             # detect + class enabled, all channels (host would write this)
        wr(sim, poll, 0x12, 0x03)                    # channel 1 auto, others shut
        for t in range(330):
            # timer 1 hook = one 10 ms tick, then one main-loop pass (stops at the poll breakpoint), then the analog stub:
            # D1 forced to detect result code 4 (good) for channel 1
            sim.batch([f"set mem sfr 0x81 0x{STACK:x}", f"set mem iram 0x{STACK - 1:x} 0x{TRAP & 255:x} 0x{TRAP >> 8:x}",
                       f"pc 0x{sim.hook[8]:x}", f"step {STEPS}", f"set mem sfr 0x81 0x{sim.sp_main:x}", f"pc 0x{poll:x}",
                       f"step {STEPS}", "set mem sfr 0xd1 0x40"])
            if t in (150, 328):
                rec = (t, sim.dump("xram", 0x800C, 0x800F), sim.dump("xram", 0x8010)[0], sim.dump("sfr", 0xF1)[0],
                       sim.dump("xram", 0x8000)[0], sim.dump("xram", 0x8004)[0])
                out.append(rec)
                log(f"  tick {rec[0]}: SAT {rec[1]} SAT_PWR {rec[2]:02X} F1 {rec[3]:02X} INT {rec[4]:02X} DET_CLASS_EVT {rec[5]:02X}")
    finally:
        sim.close()
    return out


def main():
    help_guard(__doc__)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("")
    img = FW / "bcm59111-awto.bin"
    if "--image" in sys.argv:
        img = Path(sys.argv[sys.argv.index("--image") + 1])
    log(f"== host I2C scenarios on {img.name} (ucsim_51 -t RL6449)")
    obs = host_scenarios(img, False)
    verdicts(obs, 0xA1)
    if "--stock-diff" in sys.argv:
        log("== same scenarios on stock bcm59111-v21.bin (reference behaviour)")
        sobs = host_scenarios(FW / "bcm59111-v21.bin", True)
        verdicts(sobs, 0x15)
        for k in obs:
            if obs[k] != sobs[k]:
                log(f"DIFF vs stock: {k}: ours {obs[k]} stock {sobs[k]}")
    for im, power in ((FW / "bcm59111-awto.bin", True), (FW / "bcm59111-awto-nopower.bin", False)):
        log(f"== channel state machine on {im.name}, analog side stubbed (D1 forced to detect code 4, no class / measurement)")
        (t1, sat1, pw1, f11, int1, ev1), (t2, sat2, pw2, f12, int2, ev2) = statemachine(im)
        check(f"{im.name}: channel 1 detect code 4 in SAT_P1 low nibble", (sat1[0] & 0x0F) == 4, str(sat1))
        check(f"{im.name}: channels 2..4 (shut) untouched", sat1[1:] == [0, 0, 0] and sat2[1:] == [0, 0, 0], str(sat1))
        check(f"{im.name}: detect event latched (DET_CLASS_EVT bit 0)", (ev1 | ev2) & 1, f"{ev1:02X} {ev2:02X}")
        if power:
            check(f"{im.name}: power up after good detect: F1 set, SAT_PWR enable+good bits",
                  (f12 & 0x01) and (pw2 & 0x11) == 0x11, f"F1 {f12:02X} SAT_PWR {pw2:02X}")
        else:
            check(f"{im.name}: no-power build never sets F1 or SAT_PWR", f11 == 0 and f12 == 0 and pw1 == 0 and pw2 == 0,
                  f"F1 {f12:02X} SAT_PWR {pw2:02X}")
    ok = all(results)
    log(f"{sum(results)}/{len(results)} checks passed" + ("" if ok else "  FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
