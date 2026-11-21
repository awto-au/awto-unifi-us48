// Protocol documentation into the Ghidra DB. args: <bcm59111-regs.tsv> <bcm59111-protocol.tsv>
//  - enum poe_host_reg (register numbers) and poe_cmd (12-byte protocol opcodes, reference only) under /BCM59111
//  - EXTMEM 0x8000+reg labels HR_xx_<name> + plate comment (access, clear-on-read, meaning, source)
//  - write/read dispatch-table handler functions renamed wh_<name> / rh_<name> with plate comments
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.data.CategoryPath;
import ghidra.program.model.data.DataTypeConflictHandler;
import ghidra.program.model.data.EnumDataType;
import ghidra.program.model.listing.Function;
import ghidra.program.model.symbol.SourceType;
import java.nio.file.Files;
import java.nio.file.Paths;

public class ApplyPoeProtocol extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] a = getScriptArgs();
        EnumDataType regs = new EnumDataType(new CategoryPath("/BCM59111"), "poe_host_reg", 1);
        EnumDataType cmds = new EnumDataType(new CategoryPath("/BCM59111"), "poe_cmd", 1);
        int nr = 0, nf = 0, nc = 0, bad = 0;
        for (String line : Files.readAllLines(Paths.get(a[0]))) {
            if (line.startsWith("#") || line.startsWith("reg\t") || line.trim().isEmpty()) continue;
            String[] f = line.split("\t", -1);
            try {
                int r = Integer.parseInt(f[0], 16);
                String nm = f[1];
                regs.add(nm, r);
                Address x = toAddr("EXTMEM:" + String.format("%04x", 0x8000 + r));
                String cm = "BCM59111 host register 0x" + f[0] + " " + nm + " [" + f[2] + (f[4].isEmpty() ? "" : ", " + f[4]) + "]\n"
                        + f[7] + "\nsource: " + f[8] + " [" + f[9] + "]";
                createLabel(x, String.format("HR_%02X_%s", r, nm), true, SourceType.USER_DEFINED);
                setPlateComment(x, cm);
                nr++;
                for (int k = 5; k <= 6; k++) {
                    if (f[k].isEmpty() || !f[k].startsWith("CODE:")) continue;
                    Function fx = getFunctionAt(toAddr(f[k]));
                    if (fx == null) continue;
                    String p = k == 5 ? "wh_" : "rh_";
                    fx.setName(p + nm.toLowerCase(), SourceType.USER_DEFINED);
                    fx.setComment((k == 5 ? "host write handler" : "host read handler") + " reg 0x" + f[0] + " " + nm + ": " + f[7]
                            + (f[4].isEmpty() ? "" : " [" + f[4] + "]") + " [" + f[9] + "]");
                    nf++;
                }
            } catch (Exception e) { bad++; println("ApplyPoeProtocol reg " + f[0] + ": " + e.getMessage()); }
        }
        for (String line : Files.readAllLines(Paths.get(a[1]))) {
            if (line.startsWith("#") || line.startsWith("cmd\t") || line.trim().isEmpty()) continue;
            String[] f = line.split("\t", -1);
            try { cmds.add(f[1], Integer.parseInt(f[0], 16)); nc++; } catch (Exception e) { bad++; println("ApplyPoeProtocol cmd " + f[0] + ": " + e.getMessage()); }
        }
        currentProgram.getDataTypeManager().addDataType(regs, DataTypeConflictHandler.REPLACE_HANDLER);
        currentProgram.getDataTypeManager().addDataType(cmds, DataTypeConflictHandler.REPLACE_HANDLER);
        println("ApplyPoeProtocol: " + nr + " registers, " + nf + " handlers, " + nc + " opcodes (reference enum only), " + bad + " failed");
    }
}
