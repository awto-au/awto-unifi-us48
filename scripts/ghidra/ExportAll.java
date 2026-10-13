// Export decompiled C + function / call / string-xref tables for the whole program.
// Args: <outdir> [threads] [per-function timeout s]. Used by scripts/ghidra_export.py.
//@category awto

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.Data;
import ghidra.program.model.listing.DataIterator;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionManager;
import ghidra.program.model.symbol.Reference;
import ghidra.program.model.symbol.ReferenceManager;
import ghidra.util.task.TaskMonitor;

import java.io.File;
import java.io.FileWriter;
import java.io.PrintWriter;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

public class ExportAll extends GhidraScript {

	@Override
	public void run() throws Exception {
		String[] a = getScriptArgs();
		File out = new File(a[0]);
		out.mkdirs();
		int threads = a.length > 1 ? Integer.parseInt(a[1]) : 8;
		int timeout = a.length > 2 ? Integer.parseInt(a[2]) : 60;
		String name = currentProgram.getName();
		FunctionManager fm = currentProgram.getFunctionManager();

		List<Function> funcs = new ArrayList<>();
		for (Function f : fm.getFunctions(true)) {
			if (!f.isExternal() && !f.isThunk()) {
				funcs.add(f);
			}
		}
		println("functions: " + funcs.size());

		try (PrintWriter idx = new PrintWriter(new FileWriter(new File(out, name + ".funcs.tsv")));
				PrintWriter calls = new PrintWriter(new FileWriter(new File(out, name + ".calls.tsv")))) {
			for (Function f : funcs) {
				idx.printf("%s\t%s\t%d%n", f.getEntryPoint(), f.getName(), f.getBody().getNumAddresses());
				for (Function g : f.getCalledFunctions(TaskMonitor.DUMMY)) {
					calls.printf("%s\t%s\t%s\t%s%n", f.getEntryPoint(), f.getName(), g.getEntryPoint(), g.getName());
				}
			}
		}

		// Decompile in parallel: one DecompInterface per worker, results kept in function order.
		String[] c = new String[funcs.size()];
		AtomicInteger next = new AtomicInteger();
		AtomicInteger done = new AtomicInteger();
		ExecutorService pool = Executors.newFixedThreadPool(threads);
		for (int t = 0; t < threads; t++) {
			pool.submit(() -> {
				DecompInterface di = new DecompInterface();
				di.openProgram(currentProgram);
				try {
					int i;
					while ((i = next.getAndIncrement()) < funcs.size()) {
						Function f = funcs.get(i);
						DecompileResults r = di.decompileFunction(f, timeout, TaskMonitor.DUMMY);
						String body = (r != null && r.decompileCompleted())
								? r.getDecompiledFunction().getC()
								: "// decompile failed: " + (r == null ? "null" : r.getErrorMessage()) + "\n";
						c[i] = "\n// ==== " + f.getName() + " @ " + f.getEntryPoint() + " ====\n" + body;
						int d = done.incrementAndGet();
						if (d % 2000 == 0) {
							println("decompiled " + d + "/" + funcs.size());
						}
					}
				}
				finally {
					di.dispose();
				}
			});
		}
		pool.shutdown();
		pool.awaitTermination(7, TimeUnit.DAYS);
		try (PrintWriter w = new PrintWriter(new FileWriter(new File(out, name + ".c")))) {
			for (String s : c) {
				w.print(s == null ? "" : s);
			}
		}

		ReferenceManager rm = currentProgram.getReferenceManager();
		try (PrintWriter s = new PrintWriter(new FileWriter(new File(out, name + ".strings.tsv")))) {
			DataIterator it = currentProgram.getListing().getDefinedData(true);
			while (it.hasNext()) {
				Data d = it.next();
				if (!d.hasStringValue()) {
					continue;
				}
				StringBuilder refs = new StringBuilder();
				for (Reference ref : rm.getReferencesTo(d.getAddress())) {
					Function f = fm.getFunctionContaining(ref.getFromAddress());
					refs.append(f == null ? ref.getFromAddress().toString() : f.getName() + "@" + ref.getFromAddress())
							.append(',');
				}
				String v = String.valueOf(d.getValue()).replace("\n", "\\n").replace("\t", "\\t");
				s.printf("%s\t%s\t%s%n", d.getAddress(), v, refs);
			}
		}
		println("export done: " + out);
	}
}
