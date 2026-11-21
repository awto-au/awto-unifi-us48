#!/usr/bin/env python3
"""Function-level diff v21 vs Meraki (v18) BCM59111 images and name transfer (docs/poe-fw.md section 16, #26).

Usage: poe_fw_diff.py [--write-names]
  Functions from poe_fw_re.analyse for both images. Function bytes are normalised (absolute code targets of LCALL/LJMP/ACALL/
  AJMP, DPTR code immediates, state-pointer constants MOV R4/R5 and MOV A,# pairs masked), then aligned in address order
  (difflib on the hash sequences). Classes: same (identical normalised bytes), changed (aligned, different), new (Meraki only),
  gone (v21 only). Output: tmp/poe-fw/diff.tsv. --write-names writes hwdb/firmware/bcm59111-meraki.names.tsv: v21 names carried
  to same/changed functions (changed ones tagged), host-table and hook rows regenerated from the Meraki image.
  Log: tmp/logs/poe_fw_diff.log.
"""
import difflib
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402
import poe_fw_dis as D  # noqa: E402
import poe_fw_re as R  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BASE = D.BASE


def funcs(img):
    code = bytes(BASE) + img.read_bytes()
    insns, L, ents, host, states, tables = R.analyse(code)
    ents = [e for e in ents if BASE + 0x60 <= e < 0x5220]
    out = {}
    for n, e in enumerate(ents):
        end = ents[n + 1] if n + 1 < len(ents) else e + 1
        # extent = decoded instructions until next entry
        seq, pc = [], e
        while pc < end and pc in insns:
            seq.append(insns[pc])
            pc += insns[pc].n
        norm = bytearray()
        for k, i in enumerate(seq):
            r = bytearray(i.raw)
            op = r[0]
            if op in (0x02, 0x12):
                r[1:3] = b"\0\0"
            elif (op & 0x1F) in (0x01, 0x11):
                r[1:] = b"\0" * (len(r) - 1)
            elif op == 0x90 and BASE <= (r[1] << 8 | r[2]) < 0x5400:
                r[1:3] = b"\0\0"
            elif op == 0x7C and BASE <= (r[1] << 8) < 0x5400 and 0x1A <= r[1] <= 0x53:
                r[1] = 0
            elif op == 0x7D and k and seq[k - 1].raw[0] == 0x7C:
                r[1] = 0
            elif op == 0x74 and 0x1A <= r[1] <= 0x53 and k + 3 < len(seq) and seq[k + 1].raw[0] == 0xF0 and seq[k + 3].raw[0] == 0x74:
                r[1] = 0
            elif op == 0x74 and k >= 3 and seq[k - 1].raw[0] == 0xA3 and seq[k - 3].raw[0] == 0x74:
                r[1] = 0
            norm += r
        out[e] = (bytes(norm), len(seq), seq)
    return code, out, ents, host


def main() -> int:
    help_guard(__doc__, strict=True, known=("--write-names",))
    c1, f1, e1, h1 = funcs(ROOT / "hwdb/firmware/bcm59111-v21.bin")
    c2, f2, e2, h2 = funcs(ROOT / "hwdb/firmware/bcm59111-meraki.bin")
    hs = lambda f, es: [hashlib.md5(f[e][0]).hexdigest() for e in es]
    s1, s2 = hs(f1, e1), hs(f2, e2)
    sm = difflib.SequenceMatcher(None, s1, s2, autojunk=False)
    rows, mapping = [], {}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                rows.append((e1[i1 + k], e2[j1 + k], "same"))
                mapping[e1[i1 + k]] = (e2[j1 + k], "same")
        elif tag == "replace":
            a, b = e1[i1:i2], e2[j1:j2]
            # pair by similarity inside the block
            used = set()
            for x in a:
                best, bs = None, 0.0
                for y in b:
                    if y in used:
                        continue
                    r = difflib.SequenceMatcher(None, f1[x][0], f2[y][0], autojunk=False).quick_ratio()
                    if r > bs:
                        best, bs = y, r
                if best is not None and bs > 0.6:
                    used.add(best)
                    r = difflib.SequenceMatcher(None, f1[x][0], f2[best][0], autojunk=False).ratio()
                    rows.append((x, best, f"changed {r:.2f}"))
                    mapping[x] = (best, f"changed {r:.2f}")
                else:
                    rows.append((x, None, "gone"))
            for y in b:
                if y not in used:
                    rows.append((None, y, "new"))
        elif tag == "delete":
            rows += [(x, None, "gone") for x in e1[i1:i2]]
        elif tag == "insert":
            rows += [(None, y, "new") for y in e2[j1:j2]]
    # second pass: order-independent matching of gone/new (exact hash, then similarity > 0.7)
    gone = [x for x, y, cl in rows if cl == "gone"]
    new = [y for x, y, cl in rows if cl == "new"]
    for x in list(gone):
        for y in list(new):
            if f1[x][0] == f2[y][0] and len(f1[x][0]) > 1:
                mapping[x] = (y, "same")
                gone.remove(x)
                new.remove(y)
                break
    for x in list(gone):
        best, bs = None, 0.7
        for y in new:
            if abs(len(f1[x][0]) - len(f2[y][0])) > max(8, len(f1[x][0]) // 3):
                continue
            r = difflib.SequenceMatcher(None, f1[x][0], f2[y][0], autojunk=False).ratio()
            if r > bs:
                best, bs = y, r
        if best is not None:
            mapping[x] = (best, f"changed {bs:.2f}")
            gone.remove(x)
            new.remove(best)
    rows = [r for r in rows if r[2].split()[0] not in ("gone", "new")]
    rows += [(x, y, cl) for x, (y, cl) in mapping.items() if (x, y, cl) not in rows]
    rows += [(x, None, "gone") for x in gone] + [(None, y, "new") for y in new]
    rows = sorted({r for r in rows}, key=lambda r: (r[0] if r[0] is not None else 1 << 20, r[1] or 0))
    names = {}
    for ln in (ROOT / "hwdb/firmware/bcm59111-v21.names.tsv").read_text().splitlines():
        f = ln.split("\t")
        if ln and not ln.startswith("#") and f[0].startswith("CODE:") and f[2] == "func":
            names[int(f[0][5:], 16)] = f
    out = ROOT / "tmp/poe-fw/diff.tsv"
    out.parent.mkdir(parents=True, exist_ok=True)
    cnt = {}
    with out.open("w") as fo:
        fo.write("v21\tmeraki\tclass\tsize21\tsize18\tname\n")
        for x, y, cl in rows:
            c0 = cl.split()[0]
            cnt[c0] = cnt.get(c0, 0) + 1
            fo.write(f"{x and f'{x:04X}' or ''}\t{y and f'{y:04X}' or ''}\t{cl}\t{x and f1[x][1] or ''}\t{y and f2[y][1] or ''}\t"
                     f"{names.get(x, ['', ''])[1] if x else ''}\n")
    msg = f"v21 {len(e1)} funcs, meraki {len(e2)} funcs: {cnt} -> {out}"
    print(msg)
    (ROOT / "tmp/logs").mkdir(parents=True, exist_ok=True)
    with (ROOT / "tmp/logs/poe_fw_diff.log").open("a") as f:
        f.write(msg + "\n")
    if "--write-names" in sys.argv:
        o = ["# BCM59111 Meraki (v18) names, carried from v21 by scripts/poe_fw_diff.py (same = identical normalised bytes, changed = aligned, differs). CODE base 0x1A00.",
             "# addr<TAB>name<TAB>kind<TAB>comment<TAB>source<TAB>confidence"]
        for x, (y, cl) in sorted(mapping.items()):
            if x in names:
                f = names[x]
                tag = "" if cl == "same" else f" [{cl}]"
                o.append("\t".join((f"CODE:{y:04X}", f[1], "func", f[3] + tag, "poe-fw-diff", "I" if cl == "same" else "I")))
        # hook + dispatch tables + hosts from Meraki bytes
        for k in range(15):
            o.append("\t".join((f"CODE:{BASE + 3 * k:04X}", f"hook_slot{k}", "label", "hook table record [FF hi lo]", "poe-fw-diff", "C")))
        (ROOT / "hwdb/firmware/bcm59111-meraki.names.tsv").write_text("\n".join(o) + "\n")
        print("wrote bcm59111-meraki.names.tsv", len(o) - 2, "rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
