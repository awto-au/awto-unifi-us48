# @category awto
# PyGhidra: apply function names from a TSV. Args: <names.tsv> [text_lo text_hi]  (hex; functions outside are removed).
# TSV columns: addr, name ("-" = remove the function), conf (C|I), role, regs (comma list, optional).
# Comment becomes "[C] role @regs a.B,c.D", parsed back by hwdb_export.py into role/conf/regs.
# A missing function is created; a function created 4 bytes late (cmd handlers start with `cmp r2,#n`) is moved.
from ghidra.program.model.symbol import SourceType


def run():
    a = getScriptArgs()  # noqa: F821
    lo = int(a[1], 16) if len(a) > 2 else 0
    hi = int(a[2], 16) if len(a) > 2 else 0
    prog = currentProgram  # noqa: F821
    fm, mem = prog.getFunctionManager(), prog.getMemory()
    af = prog.getAddressFactory().getDefaultAddressSpace()
    A = lambda v: af.getAddress(v)
    n_rm = n_ok = n_new = 0
    if hi:
        for f in list(fm.getFunctions(True)):
            o = f.getEntryPoint().getOffset()
            if o < lo or o >= hi:
                fm.removeFunction(f.getEntryPoint())
                n_rm += 1
    for line in open(a[0]):
        c = line.rstrip("\n").split("\t")
        if len(c) < 4:
            continue
        ad = A(int(c[0], 16))
        f = fm.getFunctionAt(ad)
        if c[1] == "-":
            if f is not None:
                fm.removeFunction(ad)
                n_rm += 1
            continue
        if f is None:
            nx = fm.getFunctionAt(ad.add(4))
            if nx is not None and (mem.getInt(ad) & 0xfffff000) == 0xe3520000:
                fm.removeFunction(ad.add(4))
            disassemble(ad)  # noqa: F821
            f = createFunction(ad, None)  # noqa: F821
            if f is None:
                print("apply_names: cannot create %s %s" % (c[0], c[1]))
                continue
            n_new += 1
        try:
            f.setName(c[1], SourceType.USER_DEFINED)
        except Exception:
            f.setName("%s_%s" % (c[1], c[0]), SourceType.USER_DEFINED)
        cm = "[%s] %s" % (c[2].strip(), c[3])
        if len(c) > 4 and c[4]:
            cm += " @regs " + c[4]
        f.setComment(cm)
        n_ok += 1
    print("apply_names: %d named, %d created, %d removed, %d functions total" % (n_ok, n_new, n_rm, fm.getFunctionCount()))


try:
    currentProgram  # noqa: F821
except NameError:
    pass
else:
    run()
