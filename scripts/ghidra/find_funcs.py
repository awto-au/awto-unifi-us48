# @category awto
# PyGhidra: complete function discovery in a raw ARM blob (no entry points, so auto-analysis misses
# functions only reached via tables/pointers). Args: <text_start> <text_end> (hex, ARM mode, 4-aligned).
# - Re-creates 1-byte stub functions (created by the hwdb import before the code was disassembled).
# - Adds a function at every 4-aligned `push {..,lr}` / `str lr,[sp,#-4]!` outside existing bodies, then re-analyses.
from ghidra.program.model.symbol import SourceType


def is_prologue(w):
    return (w & 0xffffc000) == 0xe92d4000 or w == 0xe52de004


def run():
    a = getScriptArgs()  # noqa: F821
    lo, hi = int(a[0], 16), int(a[1], 16)
    prog = currentProgram  # noqa: F821
    fm, mem, lst = prog.getFunctionManager(), prog.getMemory(), prog.getListing()
    af = prog.getAddressFactory().getDefaultAddressSpace()
    A = lambda v: af.getAddress(v)
    stubs = []
    for f in list(fm.getFunctions(True)):
        if f.getBody().getNumAddresses() < 4 and lo <= f.getEntryPoint().getOffset() < hi:
            stubs.append((f.getEntryPoint(), f.getName(), f.getComment()))
            fm.removeFunction(f.getEntryPoint())
    for ep, nm, cm in stubs:
        disassemble(ep)  # noqa: F821
        f = createFunction(ep, nm)  # noqa: F821
        if f is not None and cm:
            f.setComment(cm)
    n_new = 0
    off = lo
    while off < hi:
        ad = A(off)
        if fm.getFunctionContaining(ad) is None:
            try:
                w = mem.getInt(ad) & 0xffffffff
            except Exception:
                w = 0
            if is_prologue(w):
                disassemble(ad)  # noqa: F821
                if createFunction(ad, None) is not None:  # noqa: F821
                    n_new += 1
        off += 4
    # cmd handlers start with `cmp r2,#n` before the push (argc check): entry is 4 bytes earlier
    n_mv = 0
    for f in list(fm.getFunctions(True)):
        ep = f.getEntryPoint()
        pv = ep.subtract(4)
        try:
            w = mem.getInt(pv) & 0xffffffff
        except Exception:
            continue
        if (w & 0xfffff000) == 0xe3520000 and fm.getFunctionContaining(pv) is None:
            nm, cm = f.getName(), f.getComment()
            fm.removeFunction(ep)
            disassemble(pv)  # noqa: F821
            g = createFunction(pv, None if nm.startswith("FUN_") else nm)  # noqa: F821
            if g is not None and cm:
                g.setComment(cm)
            n_mv += 1
    analyzeAll(prog)  # noqa: F821
    print("find_funcs: %d stubs fixed, %d new, %d entry moved, %d functions total" % (len(stubs), n_new, n_mv, fm.getFunctionCount()))


try:
    currentProgram  # noqa: F821
except NameError:
    pass
else:
    run()
