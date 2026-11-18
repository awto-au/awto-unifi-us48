// Copied from awto-ddc re/ghidra (same author), unchanged.
// 8051 seeding: arg0 = file with one hex CODE address per line ('#' comments).
// Disassembles + creates a function at each, follows an LJMP (0x02) once, then runs auto-analysis.
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import java.nio.file.Files;
import java.nio.file.Paths;

public class SeedFile extends GhidraScript {
    @Override
    public void run() throws Exception {
        int n = 0;
        for (String line : Files.readAllLines(Paths.get(getScriptArgs()[0]))) {
            line = line.trim();
            if (line.isEmpty() || line.startsWith("#")) continue;
            Address a = toAddr(Long.decode(line));
            disassemble(a);
            createFunction(a, null);
            n++;
            if ((getByte(a) & 0xff) == 0x02) {
                Address t = toAddr((((getByte(a.add(1)) & 0xff) << 8) | (getByte(a.add(2)) & 0xff)));
                disassemble(t);
                createFunction(t, null);
                n++;
            }
        }
        println("SeedFile: seeded " + n + " entry points");
        analyzeAll(currentProgram);
    }
}
