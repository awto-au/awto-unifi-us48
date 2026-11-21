// Copied from awto-ddc re/ghidra (same author), unchanged.
// Make every `func` row of the names TSV a real function entry (rows often point at jump targets that Ghidra folded into a
// neighbouring function, so ApplyNames would rename the neighbour). args: <names.tsv> [all-names.txt]
// all-names.txt (one name per line, every name any TSV ever assigned): functions carrying such a name at an address the
// TSV no longer names are reset to FUN_CODE_<addr> (stale names from superseded rows).
import ghidra.app.script.GhidraScript;
import ghidra.app.cmd.function.CreateFunctionCmd;
import ghidra.program.model.address.*;
import ghidra.program.model.listing.*;
import java.nio.file.*;

public class EnsureFunctions extends GhidraScript {
    @Override
    public void run() throws Exception {
        int made = 0, split = 0, bad = 0, reset = 0;
        java.util.Set<Long> named = new java.util.HashSet<Long>();
        for (String l : Files.readAllLines(Paths.get(getScriptArgs()[0])))
            if (l.startsWith("CODE:") && l.split("\t", -1).length > 2 && l.split("\t", -1)[2].equals("func")) named.add(Long.parseLong(l.substring(5, 9), 16));
        if (getScriptArgs().length > 1) {
            java.util.Set<String> known = new java.util.HashSet<String>(Files.readAllLines(Paths.get(getScriptArgs()[1])));
            for (Function f : currentProgram.getFunctionManager().getFunctions(true)) {
                long a = f.getEntryPoint().getOffset();
                if (!named.contains(a) && known.contains(f.getName())) { f.setName(String.format("FUN_CODE_%04x", a), ghidra.program.model.symbol.SourceType.USER_DEFINED); reset++; }
            }
        }
        FunctionManager fm = currentProgram.getFunctionManager();
        for (String l : Files.readAllLines(Paths.get(getScriptArgs()[0]))) {
            if (l.startsWith("#") || l.trim().isEmpty()) continue;
            String[] f = l.split("\t", -1);
            if (f.length < 3 || !f[0].startsWith("CODE:") || !f[2].equals("func")) continue;
            Address a = toAddr(Long.parseLong(f[0].substring(5), 16));
            if (fm.getFunctionAt(a) != null) continue;
            try {
                if (getInstructionAt(a) == null) disassemble(a);
                Function c = fm.getFunctionContaining(a);
                AddressSetView nb = CreateFunctionCmd.getFunctionBody(currentProgram, a, monitor);
                if (c != null) { c.setBody(c.getBody().subtract(nb)); split++; }
                if (createFunction(a, null) != null) made++; else bad++;
            } catch (Exception e) { bad++; println("EnsureFunctions " + f[0] + ": " + e.getMessage()); }
        }
        println("EnsureFunctions: " + made + " created (" + split + " carved from a containing function), " + bad + " failed, " + reset + " stale names reset");
    }
}
