// Export function table + decompile. args: <functions.tsv> <decompiled.c> [<listing.asm>]
// functions.tsv: addr, name, size, callers (count), comment. Output paths live under tmp/ (not committed) except functions.tsv.
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionIterator;
import ghidra.program.model.listing.Instruction;
import java.io.PrintWriter;

public class ExportPoe extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] a = getScriptArgs();
        DecompInterface di = new DecompInterface();
        di.openProgram(currentProgram);
        int n = 0, fail = 0;
        try (PrintWriter tsv = new PrintWriter(a[0]); PrintWriter c = new PrintWriter(a[1])) {
            tsv.println("addr\tname\tsize\tcallers\tcomment");
            FunctionIterator it = currentProgram.getFunctionManager().getFunctions(true);
            while (it.hasNext()) {
                Function f = it.next();
                String cm = f.getComment() == null ? "" : f.getComment();
                tsv.println(String.format("%04X\t%s\t%d\t%d\t%s", f.getEntryPoint().getOffset(), f.getName(),
                        f.getBody().getNumAddresses(), f.getCallingFunctions(monitor).size(), cm));
                DecompileResults r = di.decompileFunction(f, 20, monitor);
                if (r.decompileCompleted()) c.println(r.getDecompiledFunction().getC());
                else { fail++; c.println("// decompile failed: " + f.getName()); }
                n++;
            }
        }
        if (a.length > 2) {
            try (PrintWriter l = new PrintWriter(a[2])) {
                for (Instruction i = currentProgram.getListing().getInstructions(true).next(); i != null; i = i.getNext())
                    l.println(i.getAddress() + "\t" + i);
            }
        }
        println("ExportPoe: " + n + " functions, " + fail + " decompile failures");
    }
}
