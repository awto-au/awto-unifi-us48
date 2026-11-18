#!/usr/bin/env python3
"""Headless Ghidra import / labelling of the BCM59111 v21 PSE firmware (docs/poe-fw.md section 13, #26).

Usage: poe_fw_ghidra.py import|regen [--image PATH]  (v21 default; Meraki: --image hwdb/firmware/bcm59111-meraki.bin)
  import = create tmp/ghidra/poefw project, import the image (BinaryLoader, 8051:BE:16:default, base 0x1A00, ROM < 0x1A00
           undefined), seed functions (hook table, host dispatch tables, state-handler pointers, call targets), analyse,
           apply hwdb/firmware/bcm59111-v21.names.tsv, export.
  regen  = re-apply the names TSV to the existing program and export (daily use).
  Exports: hwdb/firmware/bcm59111-v21.functions.tsv (committed), tmp/poe-fw/ghidra/decompiled.c, listing.tsv (not committed).
  Needs analyzeHeadless on PATH (~/.local/bin/analyzeHeadless). Scripts: scripts/ghidra/*.java (ApplyPoeNames, ExportPoe,
  SeedFile + EnsureFunctions copied from awto-ddc re/ghidra, same author).
  Stall guard: no output for 60 s = hung, kill. Backstop 600 s. Log: tmp/logs/poe_fw_ghidra.log.
"""
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402
import poe_fw_dis as D  # noqa: E402
import poe_fw_re as R  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PROJ, MAIN = "poefw", ROOT
if any(x.startswith(".") for x in ROOT.parts):  # Ghidra rejects dot-prefixed path elements (.claude/worktrees): use the main checkout
    MAIN = Path(*ROOT.parts[:[i for i, x in enumerate(ROOT.parts) if x.startswith(".")][0]])
PROJ_DIR = MAIN / "tmp/ghidra"
LANG = "8051:BE:16:default"
STALL_S, TOTAL_S = 60, 600


def run(cmd, log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("a")
    log.write("\n$ " + " ".join(map(str, cmd)) + "\n")
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    last, t0 = [time.time()], time.time()

    def pump():
        for line in p.stdout:
            last[0] = time.time()
            log.write(line)
            log.flush()
    th = threading.Thread(target=pump, daemon=True)
    th.start()
    while True:
        try:
            p.wait(timeout=2)
            break
        except subprocess.TimeoutExpired:
            if time.time() - last[0] > STALL_S or time.time() - t0 > TOTAL_S:
                p.kill()
                msg = f"KILLED: silence {time.time() - last[0]:.0f}s, elapsed {time.time() - t0:.0f}s"
                log.write("\n" + msg + "\n")
                print(msg, "see", log_path)
                return 2
    th.join()
    print(f"exit {p.returncode} in {time.time() - t0:.0f}s, log {log_path}")
    return p.returncode


def main() -> int:
    help_guard(__doc__, strict=True, known=("import", "regen", "--image"))
    a = sys.argv[1:]
    img = ROOT / "hwdb/firmware/bcm59111-v21.bin"
    if "--image" in a:
        img = Path(a[a.index("--image") + 1])
    cmd = a[0]
    v21 = img.stem.endswith("v21")
    names = ROOT / f"hwdb/firmware/{img.stem}.names.tsv"
    out = ROOT / "tmp/poe-fw/ghidra" / img.stem
    out.mkdir(parents=True, exist_ok=True)
    PROJ_DIR.mkdir(parents=True, exist_ok=True)
    code = bytes(D.BASE) + img.read_bytes()
    insns, L, ents, host, states, tables = R.analyse(code)
    seeds = out / "seeds.txt"
    seeds.write_text("# CODE entry points (poe_fw_re.analyse)\n" + "".join(f"0x{e:04X}\n" for e in ents if e < 0x5220))
    abi = ROOT / f"hwdb/firmware/{img.stem}.abi.tsv"
    subprocess.run([sys.executable, str(ROOT / "scripts/poe_fw_autoname.py"), str(img), "--names", str(names)], check=True)
    subprocess.run([sys.executable, str(ROOT / "scripts/poe_fw_abi.py"), str(img), "--out", str(abi)], check=True)
    sp = ROOT / "scripts/ghidra"
    tail = ["-scriptPath", str(sp), "-postScript", "EnsureFunctions.java", str(names),
            "-postScript", "ApplyPoeNames.java", str(names),
            *(["-postScript", "ApplyPoeProtocol.java", str(ROOT / "hwdb/firmware/bcm59111-regs.tsv"),
               str(ROOT / "hwdb/firmware/bcm59111-protocol.tsv")] if v21 else []),  # regs TSV handler addresses are v21
            "-postScript", "ApplyKeilAbi.java", str(abi),
            "-postScript", "ExportPoe.java", str(ROOT / f"hwdb/firmware/{img.stem}.functions.tsv"),
            str(out / "decompiled.c"), str(out / "listing.tsv")]
    base = ["analyzeHeadless", str(PROJ_DIR), PROJ]
    if cmd == "import":
        c = base + ["-import", str(img), "-overwrite", "-loader", "BinaryLoader", "-loader-baseAddr", f"0x{D.BASE:X}",
                    "-processor", LANG, "-scriptPath", str(sp), "-postScript", "SeedFile.java", str(seeds)] + tail[2:]
        # EnsureFunctions reads CODE: func rows of the names TSV; ApplyPoeNames creates the rest
    elif cmd == "regen":
        c = base + ["-process", img.name, "-noanalysis"] + tail
    else:
        return 2
    return run(c, ROOT / "tmp/logs/poe_fw_ghidra.log")


if __name__ == "__main__":
    sys.exit(main())
