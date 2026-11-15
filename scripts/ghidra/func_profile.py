# @category awto
# PyGhidra: per-function profile for naming. Args: <out.tsv> [max_entry_hex] (skip functions at/after it: data)
# Columns: addr, name, size, callers, callees, strings (xref'd), regs (device constants: pool dwords and movw/movt pairs).
# Device constants = 0x18000000..0x1bffffff, 0x19xxxxxx, 0x48xxxxxx, 0x0axxxxxx; flash/code 0x1c0xxxxx excluded.
import re
from ghidra.program.model.symbol import RefType


def dev(v):
    return (0x18000000 <= v < 0x1c000000) or (0x48000000 <= v < 0x48400000) or (0x0a000000 <= v < 0x0a040000)


def run():
    out = getScriptArgs()[0]  # noqa: F821
    mx = int(getScriptArgs()[1], 16) if len(getScriptArgs()) > 1 else 0xffffffff  # noqa: F821
    prog = currentProgram  # noqa: F821
    fm, lst, mem, rm = prog.getFunctionManager(), prog.getListing(), prog.getMemory(), prog.getReferenceManager()
    af = prog.getAddressFactory().getDefaultAddressSpace()
    rows = []
    for f in fm.getFunctions(True):
        ep = f.getEntryPoint()
        if ep.getOffset() >= mx:
            continue
        body = f.getBody()
        callers = sorted(set(r.getFromAddress().getOffset() for r in rm.getReferencesTo(ep) if r.getReferenceType().isCall()))
        callees, strs, regs = set(), [], set()
        movw = {}
        for ins in lst.getInstructions(body, True):
            for r in ins.getReferencesFrom():
                if r.getReferenceType().isCall():
                    callees.add(r.getToAddress().getOffset())
                else:
                    d = lst.getDataAt(r.getToAddress())
                    if d is not None and d.hasStringValue():
                        strs.append('%x:%s' % (r.getToAddress().getOffset(), repr(str(d.getValue()))[1:60]))
                    elif r.getReferenceType().isRead() and mem.contains(r.getToAddress()):
                        try:
                            v = mem.getInt(r.getToAddress()) & 0xffffffff
                            if dev(v):
                                regs.add(v)
                        except Exception:
                            pass
            mn = ins.getMnemonicString()
            if mn.startswith('movw'):
                try:
                    movw[str(ins.getRegister(0))] = ins.getScalar(1).getValue() & 0xffff
                except Exception:
                    pass
            elif mn.startswith('movt'):
                try:
                    rg = str(ins.getRegister(0))
                    v = ((ins.getScalar(1).getValue() & 0xffff) << 16) | movw.get(rg, 0)
                    if dev(v):
                        regs.add(v)
                except Exception:
                    pass
        rows.append('%08x\t%s\t%d\t%s\t%s\t%s\t%s' % (
            ep.getOffset(), f.getName(), body.getNumAddresses(),
            ','.join('%x' % c for c in callers), ','.join('%x' % c for c in sorted(callees)),
            ' | '.join(strs[:6]), ','.join('%x' % r for r in sorted(regs))))
    open(out, 'w').write('\n'.join(rows) + '\n')
    print('func_profile: %d functions -> %s' % (len(rows), out))


try:
    currentProgram  # noqa: F821
except NameError:
    pass
else:
    run()
