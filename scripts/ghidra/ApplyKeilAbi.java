// Teach the decompiler the Keil C51 register ABI (cf. awto-ddc re/ghidra/FixKeilReturns.java, which fixes non-returning flags).
// arg0 = hwdb/firmware/bcm59111-v21.abi.tsv: addr TAB ret(void|byte|word|long) TAB args (comma list of Rn or Rm:Rn).
// Sets custom-storage signatures: args/returns in bank-0 R0..R7 (INTMEM 0x00..0x07), big-endian pairs (R6:R7 = msb:lsb).
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.data.*;
import ghidra.program.model.listing.*;
import ghidra.program.model.listing.Function.FunctionUpdateType;
import ghidra.program.model.symbol.SourceType;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;

public class ApplyKeilAbi extends GhidraScript {
    private VariableStorage stor(String spec) throws Exception {
        String[] r = spec.split(":");
        int size = r.length;
        int lo = Integer.parseInt(r[r.length - 1].substring(1));   // lsb register
        int first = Integer.parseInt(r[0].substring(1));
        // registers R<first>..R<lo> are consecutive bytes in INTMEM; one varnode of `size` bytes starting at the msb register
        Address a = currentProgram.getAddressFactory().getAddressSpace("INTMEM").getAddress(first);
        return new VariableStorage(currentProgram, a, size);
    }

    private DataType type(int size) {
        return size == 1 ? new ByteDataType() : size == 2 ? new WordDataType() : new DWordDataType();
    }

    @Override
    public void run() throws Exception {
        int ok = 0, bad = 0;
        for (String line : Files.readAllLines(Paths.get(getScriptArgs()[0]))) {
            if (line.startsWith("#") || line.trim().isEmpty()) continue;
            String[] f = line.split("\t", -1);
            try {
                Function fx = getFunctionAt(toAddr(f[0]));
                if (fx == null) continue;
                Parameter ret;
                if (f[1].equals("void")) ret = new ReturnParameterImpl(VoidDataType.dataType, currentProgram);
                else {
                    int sz = f[1].equals("byte") ? 1 : f[1].equals("word") ? 2 : 4;
                    ret = new ReturnParameterImpl(type(sz), new VariableStorage(currentProgram,
                            currentProgram.getAddressFactory().getAddressSpace("INTMEM").getAddress(8 - sz), sz), currentProgram);
                }
                List<Variable> ps = new ArrayList<>();
                int k = 1;
                if (!f[2].isEmpty()) for (String s : f[2].split(",")) {
                    VariableStorage vs;
                    int size;
                    if (s.equals("R4:R5:R6:R7")) { vs = new VariableStorage(currentProgram, currentProgram.getAddressFactory().getAddressSpace("INTMEM").getAddress(4), 4); size = 4; }
                    else { vs = stor(s); size = s.split(":").length; }
                    ps.add(new ParameterImpl("arg" + k++, type(size), vs, currentProgram));
                }
                fx.updateFunction(null, ret, ps, FunctionUpdateType.CUSTOM_STORAGE, true, SourceType.USER_DEFINED);
                ok++;
            } catch (Exception e) { bad++; if (bad < 6) println("ApplyKeilAbi " + f[0] + ": " + e.getMessage()); }
        }
        println("ApplyKeilAbi: " + ok + " signatures, " + bad + " failed");
    }
}
