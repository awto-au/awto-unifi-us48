#!/usr/bin/env python3
"""hwdb loader: hardware facts from hwdb/*.toml merged with the SDK regdb (regdb/*.tsv). Stdlib only.

Import:  sys.path.insert(0, scripts dir); import hwdb; db = hwdb.load(); db.addr("ccu.POLICY_FREQ")
CLI:     python3 scripts/hwdb.py check|stats|addr NAME|find 0xADDR   (schema: docs/hwdb.md)
Log: tmp/logs/hwdb.log (CLI only).
"""
import csv
import os
import pathlib
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from _cli import help_guard  # noqa: E402

CONF = ("C", "I")
SDK_IPROC, SDK_CMIC = "sdk_iproc", "sdk_cmic"


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


@dataclass
class Field:
    name: str
    lo: int
    hi: int
    desc: str = ""
    enum: dict = field(default_factory=dict)

    @property
    def shift(self):
        return self.lo

    @property
    def mask(self):
        return ((1 << (self.hi - self.lo + 1)) - 1) << self.lo


@dataclass
class Reg:
    name: str
    offset: int
    width: int = 32
    access: str = "rw"
    reset: int = None
    count: int = 1
    stride: int = 4
    fields: list = field(default_factory=list)
    desc: str = ""
    src: str = ""
    conf: str = ""
    note: str = ""
    sdk: str = ""


@dataclass
class Block:
    name: str
    space: str
    base: int
    size: int = None
    unit: int = None
    desc: str = ""
    src: str = ""
    conf: str = ""
    note: str = ""
    like: str = ""
    regs: dict = field(default_factory=dict)
    origin: str = "hand"


@dataclass
class Region:
    name: str
    space: str
    base: int
    size: int = None
    kind: str = "device"
    unit: int = None
    desc: str = ""
    src: str = ""
    conf: str = ""
    note: str = ""


@dataclass
class Func:
    binary: str
    name: str
    addr: int
    role: str = ""
    regs: list = field(default_factory=list)
    size: int = None
    src: str = ""
    conf: str = ""


@dataclass
class Binary:
    name: str
    desc: str = ""
    base: int = 0
    arch: str = ""
    funcs: dict = field(default_factory=dict)
    comments: list = field(default_factory=list)


@dataclass
class SdkReg:
    name: str
    regtype: str
    blocks: str
    offset: int
    numels: int
    reset: int
    fields: list
    src: str
    conf: str = "C"


def _int(v):
    return int(v, 0) if isinstance(v, str) else v


def parse_bits(s):
    """'hi:lo' or 'n' or int -> (lo, hi)."""
    if isinstance(s, int):
        return s, s
    if ":" in str(s):
        hi, lo = (int(x) for x in str(s).split(":"))
        return lo, hi
    return int(s), int(s)


def _field(name, spec):
    if isinstance(spec, dict):
        lo, hi = parse_bits(spec["bits"])
        return Field(name, lo, hi, spec.get("desc", ""), {_int(k): v for k, v in spec.get("enum", {}).items()})
    raise ValueError(spec)


def _parse_fields(r):
    """fields = ["NAME hi:lo desc...", ...] and/or [..reg.R.field.F] tables."""
    out = []
    for s in r.get("fields", []):
        parts = s.split(None, 2)
        lo, hi = parse_bits(parts[1])
        out.append(Field(parts[0], lo, hi, parts[2] if len(parts) > 2 else ""))
    for n, spec in r.get("field", {}).items():
        out.append(_field(n, spec))
    return out


class Db:
    def __init__(self):
        self.spaces = {}
        self.regions = {}
        self.consts = {}
        self.blocks = {}
        self.binaries = {}
        self.sdk_sbus_regs = {}
        self.sdk_sbus_mems = {}
        self.ports = []
        self.hw = {}  # "sdk_iproc.NAME" / "sdk_cmic.NAME" -> {seen, reset_ok?, ro_ok?, date?, unit?, why?} from hwdb/validation/*.toml
        self.hw0 = {}  # same for unit 0 (hwdb/validation/u0.toml); self.hw = unit 1 (u1.toml)
        self.blocked = []  # hwdb/validation/*-blocked.toml entries; "unit" key added (from the file name uN-)
        self.files = []
        self._by_addr = None

    # ---- loading
    def load_toml(self, path):
        with open(path, "rb") as f:
            d = tomllib.load(f)
        self.files.append(str(path))
        for n, s in d.get("space", {}).items():
            self.spaces[n] = s
        for n, c in d.get("const", {}).items():
            self.consts[n] = c
        for n, r in d.get("region", {}).items():
            self.regions[n] = Region(n, r["space"], _int(r["base"]), _int(r["size"]) if "size" in r else None,
                                     r.get("kind", "device"), r.get("unit"), r.get("desc", ""), r.get("src", ""),
                                     r.get("conf", ""), r.get("note", ""))
        for n, b in d.get("block", {}).items():
            blk = Block(n, b["space"], _int(b["base"]), _int(b["size"]) if "size" in b else None, b.get("unit"),
                        b.get("desc", ""), b.get("src", ""), b.get("conf", ""), b.get("note", ""), b.get("like", ""))
            for rn, r in b.get("reg", {}).items():
                blk.regs[rn] = Reg(rn, _int(r["offset"]), r.get("width", 32), r.get("access", "rw"),
                                   _int(r["reset"]) if "reset" in r else None, r.get("count", 1),
                                   r.get("stride", r.get("width", 32) // 8), _parse_fields(r), r.get("desc", ""),
                                   r.get("src", blk.src), r.get("conf", blk.conf), r.get("note", ""), r.get("sdk", ""))
            self.blocks[n] = blk
        for n, b in d.get("binary", {}).items():
            bn = Binary(n, b.get("desc", ""), _int(b.get("base", 0)), b.get("arch", ""))
            for fn, f in b.get("func", {}).items():
                bn.funcs[fn] = Func(n, f.get("name", fn), _int(f["addr"]), f.get("role", ""), f.get("regs", []),
                                    f.get("size"), f.get("src", ""), f.get("conf", ""))
            bn.comments = b.get("comment", [])
            self.binaries[n] = bn
        self.ports.extend(d.get("port", []))

    def resolve_like(self):
        for b in self.blocks.values():
            if b.like:
                b.regs = dict(self.blocks[b.like].regs)
                if b.size is None:
                    b.size = self.blocks[b.like].size

    def load_sdk(self, root):
        regs = pathlib.Path(root) / "regdb/hr2_regs.tsv"
        if not regs.exists():
            return
        cmic_base = self.blocks["cmic0"].base if "cmic0" in self.blocks else 0x48000000
        iproc = Block(SDK_IPROC, "axi0", 0, None, 0, "SDK regdb iProc registers (absolute AXI offsets)",
                      "regdb/hr2_regs.tsv regtype iprocreg", "C", origin="sdk")
        cmic = Block(SDK_CMIC, "axi0", cmic_base, None, 0, "SDK regdb CMIC registers (offsets from cmic0 base)",
                     "regdb/hr2_regs.tsv regtype cpureg", "C", origin="sdk")
        with open(regs, newline="") as f:
            for row in csv.DictReader(f, delimiter="\t"):
                fl = []
                for x in filter(None, row["fields"].split(",")):
                    n, bp, ln = x.rsplit(":", 2)
                    fl.append(Field(n, int(bp), int(bp) + int(ln) - 1))
                src = "regdb/hr2_regs.tsv idx %s" % row["idx"]
                off = int(row["offset"], 0)
                rst = int(row["rst_lo"], 0)
                if row["regtype_name"] in ("iprocreg", "cpureg"):
                    tgt = iproc if row["regtype_name"] == "iprocreg" else cmic
                    tgt.regs[row["name"]] = Reg(row["name"], off, 32, "rw", rst, int(row["numels"]), 4, fl,
                                                "", src, "C")
                else:
                    self.sdk_sbus_regs[row["name"]] = SdkReg(row["name"], row["regtype_name"], row["block_labels"],
                                                             off, int(row["numels"]), rst, fl, src)
        self.blocks[SDK_IPROC] = iproc
        self.blocks[SDK_CMIC] = cmic
        mems = pathlib.Path(root) / "regdb/hr2_mems.tsv"
        if mems.exists():
            with open(mems, newline="") as f:
                for row in csv.DictReader(f, delimiter="\t"):
                    fl = []
                    for x in filter(None, row["fields"].split(",")):
                        n, bp, ln = x.rsplit(":", 2)
                        fl.append(Field(n, int(bp), int(bp) + int(ln) - 1))
                    self.sdk_sbus_mems[row["name"]] = SdkReg(row["name"], "mem", row["block_labels"],
                                                             int(row["base"], 0), int(row["index_max"]) + 1, 0, fl,
                                                             "regdb/hr2_mems.tsv idx %s" % row["idx"])

    def load_validation(self, root):
        for p in sorted((pathlib.Path(root) / "hwdb/validation").glob("*.toml")):
            with open(p, "rb") as f:
                d = tomllib.load(f)
            unit = 0 if p.name.startswith("u0") else 1
            self.blocked += [dict(b, unit=unit) for b in d.get("blocked", [])]
            for kind, regs in d.get("hw", {}).items():
                for n, v in regs.items():
                    (self.hw0 if unit == 0 else self.hw)["%s.%s" % (kind, n)] = v

    def hw_of(self, name):
        """Validation record of 'block.REG' (hand reg with sdk= maps to its regdb entry) or 'sdk_iproc.REG'; None = never read."""
        if name in self.hw:
            return self.hw[name]
        try:
            b, r = self._split(name)
            reg = b.regs[r]
        except KeyError:
            return None
        if not reg.sdk:
            return None
        return self.hw.get(("sdk_cmic." if b.base >= 0x48000000 else "sdk_iproc.") + reg.sdk)

    # ---- queries
    def _split(self, name):
        b, _, r = name.partition(".")
        return self.blocks[b], r

    def base(self, block):
        return self.blocks[block].base

    def reg(self, name):
        b, r = self._split(name)
        return b.regs[r]

    def addr(self, name, idx=0):
        """'block.REG' -> absolute address in the block's space; 'block' -> block base; region name -> base."""
        if "." not in name:
            if name in self.blocks:
                return self.blocks[name].base
            return self.regions[name].base
        b, r = self._split(name)
        g = b.regs[r]
        return b.base + g.offset + idx * g.stride

    def field(self, name):
        """'block.REG.FIELD' -> Field (.shift, .mask)."""
        b, _, rest = name.partition(".")
        r, _, f = rest.partition(".")
        for x in self.blocks[b].regs[r].fields:
            if x.name == f:
                return x
        raise KeyError(name)

    def region(self, name):
        return self.regions[name]

    def port(self, front):
        for p in self.ports:
            if p["front"] == front:
                return p
        raise KeyError(front)

    def hand_blocks(self):
        return [b for b in self.blocks.values() if b.origin == "hand"]

    def index(self):
        """addr -> [(kind, name)] for every hand and SDK mmio register in a CPU-visible space, plus block/region bases."""
        if self._by_addr is None:
            ix = {}
            for b in self.blocks.values():
                if b.space not in ("axi0",):
                    continue
                if b.origin == "hand":
                    ix.setdefault(b.base, []).append(("block", b.name))
                for r in b.regs.values():
                    for i in range(r.count):
                        ix.setdefault(b.base + r.offset + i * r.stride, []).append(("reg", "%s.%s" % (b.name, r.name)))
            for g in self.regions.values():
                if g.space == "axi0":
                    ix.setdefault(g.base, []).append(("region", g.name))
            self._by_addr = ix
        return self._by_addr

    def containing(self, a, space="axi0"):
        """Hand blocks/regions whose [base, base+size) contains a."""
        out = []
        for b in self.hand_blocks():
            if b.space == space and b.size and b.base <= a < b.base + b.size:
                out.append(("block", b.name, a - b.base))
        for g in self.regions.values():
            if g.space == space and g.size and g.base <= a < g.base + g.size:
                out.append(("region", g.name, a - g.base))
        return out

    def find(self, a, space="axi0"):
        out = [(k, n, 0) for k, n in self.index().get(a, [])]
        return out + [c for c in self.containing(a, space) if not (c[2] == 0 and any(o[1] == c[1] for o in out))]

    # ---- validation
    def validate(self):
        err = []

        def prov(kind, name, src, conf):
            if not src:
                err.append("%s %s: no src" % (kind, name))
            if conf not in CONF:
                err.append("%s %s: conf %r not C/I" % (kind, name, conf))
        for n, s in self.spaces.items():
            if not s.get("desc"):
                err.append("space %s: no desc" % n)
        for n, c in self.consts.items():
            prov("const", n, c.get("src", ""), c.get("conf", ""))
            if not isinstance(c.get("value"), int):
                err.append("const %s: value is not an integer" % n)
        for g in self.regions.values():
            prov("region", g.name, g.src, g.conf)
            if g.space not in self.spaces:
                err.append("region %s: unknown space %s" % (g.name, g.space))
        for b in self.hand_blocks():
            prov("block", b.name, b.src, b.conf)
            if b.space not in self.spaces:
                err.append("block %s: unknown space %s" % (b.name, b.space))
            if b.like and b.like not in self.blocks:
                err.append("block %s: like %s unknown" % (b.name, b.like))
            for r in b.regs.values():
                prov("reg", "%s.%s" % (b.name, r.name), r.src, r.conf)
                if b.size and r.offset + r.count * r.stride > b.size:
                    err.append("reg %s.%s: beyond block size" % (b.name, r.name))
                for f in r.fields:
                    if f.hi >= r.width or f.lo > f.hi:
                        err.append("field %s.%s.%s: bad bits" % (b.name, r.name, f.name))
                if r.sdk:
                    if b.like or b.name == "cmic1":
                        continue
                    s = self.blocks.get(SDK_CMIC if b.name.startswith("cmic") else SDK_IPROC)
                    sr = s.regs.get(r.sdk) if s else None
                    if s and sr is None:
                        err.append("reg %s.%s: sdk name %s not in regdb" % (b.name, r.name, r.sdk))
                    elif sr and s.base + sr.offset != b.base + r.offset:
                        err.append("reg %s.%s: addr 0x%x != sdk %s 0x%x" % (b.name, r.name, b.base + r.offset,
                                                                         r.sdk, s.base + sr.offset))
        for bn in self.binaries.values():
            for f in bn.funcs.values():
                prov("func", "%s.%s" % (bn.name, f.name), f.src, f.conf)
                for ref in f.regs:
                    if isinstance(ref, str):
                        try:
                            self.addr(ref)
                        except KeyError:
                            err.append("func %s.%s: reg ref %s unresolved" % (bn.name, f.name, ref))
        for p in self.ports:
            for k in ("front", "label", "unit", "logical", "phys", "phy", "src", "conf"):
                if k not in p:
                    err.append("port %s: missing %s" % (p.get("front"), k))
        return err


def load(root=None, sdk=True):
    root = pathlib.Path(root) if root else repo_root()
    db = Db()
    for sub in ("soc", "board", "fw"):
        for p in sorted((root / "hwdb" / sub).glob("*.toml")):
            db.load_toml(p)
    db.resolve_like()
    if sdk:
        db.load_sdk(root)
    db.load_validation(root)
    return db


_default = None


def default():
    global _default
    if _default is None:
        _default = load()
    return _default


def addr(name, idx=0):
    return default().addr(name, idx)


def main(argv):
    import logging
    root = repo_root()
    log = root / "tmp/logs/hwdb.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.FileHandler(log), logging.StreamHandler(sys.stdout)])
    cmd = argv[1] if len(argv) > 1 else "stats"
    db = load()
    if cmd == "check":
        e = db.validate()
        for x in e:
            logging.info("ERR %s", x)
        logging.info("%d errors", len(e))
        return 1 if e else 0
    if cmd == "stats":
        hand = db.hand_blocks()
        logging.info("files %d, spaces %d, regions %d, hand blocks %d, hand regs %d", len(db.files), len(db.spaces),
                     len(db.regions), len(hand), sum(len(b.regs) for b in hand))
        logging.info("sdk iproc regs %d, sdk cmic regs %d, sdk sbus regs %d, sbus mems %d",
                     len(db.blocks[SDK_IPROC].regs), len(db.blocks[SDK_CMIC].regs), len(db.sdk_sbus_regs),
                     len(db.sdk_sbus_mems))
        seen = sum(1 for v in db.hw.values() if v.get("seen"))
        logging.info("validation (hwdb/validation): %d records, %d seen, %d blocked ranges", len(db.hw), seen, len(db.blocked))
        logging.info("binaries %s, funcs %d, ports %d", list(db.binaries),
                     sum(len(b.funcs) for b in db.binaries.values()), len(db.ports))
        return 0
    if cmd == "addr":
        logging.info("0x%08x", db.addr(argv[2]))
        return 0
    if cmd == "find":
        for k, n, off in db.find(int(argv[2], 0)):
            logging.info("%s %s +0x%x", k, n, off)
        return 0
    logging.info(__doc__)
    return 2


if __name__ == "__main__":
    help_guard(__doc__)
    sys.exit(main(sys.argv))
