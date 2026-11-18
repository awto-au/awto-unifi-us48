// Apply hwdb/firmware/bcm59111-v21.names.tsv to the open program. arg0 = names TSV.
// Row: <SPACE>:<hex addr> TAB name TAB kind(func|label|var) TAB comment TAB source TAB conf. Spaces: CODE, EXTMEM, INTMEM, SFR.
// Label-only handling for var/label follows awto-ddc re/ghidra/ApplyRegLabels.java (no data creation, neighbours stay intact).
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.CodeUnit;
import ghidra.program.model.symbol.SourceType;
import java.nio.file.Files;
import java.nio.file.Paths;

public class ApplyPoeNames extends GhidraScript {
    @Override
    public void run() throws Exception {
        int fn = 0, lab = 0, bad = 0;
        for (String line : Files.readAllLines(Paths.get(getScriptArgs()[0]))) {
            if (line.startsWith("#") || line.trim().isEmpty()) continue;
            String[] f = line.split("\t", -1);
            if (f.length < 3) continue;
            try {
                Address a = toAddr(f[0].trim());
                String cm = (f.length > 3 ? f[3] : "") + (f.length > 5 ? " [" + f[5] + "]" : "");
                if (f[2].equals("func")) {
                    if (getInstructionAt(a) == null) disassemble(a);
                    Function fx = getFunctionAt(a);
                    if (fx == null) fx = createFunction(a, f[1].trim());
                    if (fx == null) { bad++; println("ApplyPoeNames: no function at " + f[0]); continue; }
                    fx.setName(f[1].trim(), SourceType.USER_DEFINED);
                    fx.setComment(cm);
                    fn++;
                } else {
                    createLabel(a, f[1].trim(), true, SourceType.USER_DEFINED);
                    if (!cm.trim().isEmpty()) setEOLComment(a, cm);
                    lab++;
                }
            } catch (Exception e) { bad++; println("ApplyPoeNames " + f[0] + ": " + e.getMessage()); }
        }
        println("ApplyPoeNames: " + fn + " functions, " + lab + " labels, " + bad + " failed");
    }
}
