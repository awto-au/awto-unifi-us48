#!/usr/bin/env python3
"""Build the open-source BCM59111 PSE firmware (poe-fw/, #26) with sdcc and write the 16 KiB RAM image.

Usage: poe_fw_build.py [--no-power-only]
  Compiles poe-fw/src (sdcc mcs51 small model, awto-sdcc) and links to the stock image layout: CPU base 0x1A00, hook table at
  file offset 0, 0x1B00 ROM entry, host tables (CONST) at 0x4000, tag at 0x5300, padded to 16384 B.
  Writes hwdb/firmware/bcm59111-awto.bin and bcm59111-awto-nopower.bin (-DPOE_FW_NO_POWER: no power-up path, bench stage 1).
  Refuses (exit 1, reason printed) if hook table, size, base, entry, tag, IRAM or code-end checks fail, or if the generated
  header / tables no longer match hwdb/firmware/bcm59111-regs.tsv (poe_fw_genhdr.py --check).
  Also writes the bench probe images bcm59111-awto-probeA / B / C.bin (-DPOE_FW_PROBE on the -nopower base: 16-bit entry counters
  per hook in unused host regs; A = stock arming IP.1 / T2CON.5, B = no arming, C = A plus EXF2 / TF2 re-arm every 50 ticks;
  docs/poe-fw-seq/05-timing.md section 9). The normal images do not change with these defines off.
  Also writes tmp/poe-fw/sizes.tsv (per-function size, ours vs stock v21 functions.tsv). Log: tmp/logs/poe_fw_build.log.
"""
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cli import help_guard  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "poe-fw/src"
INC = ROOT / "poe-fw/inc"
WORK = ROOT / "tmp/poe-fw-build"
LOG = ROOT / "tmp/logs/poe_fw_build.log"
OUT_DIR = ROOT / "hwdb/firmware"
BASE = 0x1A00
SIZE = 16384
LIBDIR = Path("/usr/local/share/sdcc/lib/mcs51-small")
C_SRCS = ["main", "i2c", "hostregs", "hostwd", "meas", "chan", "hal_analog"]
S_SRCS = ["hosttab", "chan_isr", "startup"]   # hosttab first: CONST area starts with the write table
IRAM_DATA_MAX = 0x7F        # stock IRAM use ends at 0x7C; upper half (0x80..) is left to the ROM stack (docs/poe-fw.md section 8)
CODE_END_MAX = 0x5220       # RET stubs at 0x5220, tag at 0x5300


class Fail(Exception):
    pass


def log(msg):
    print(msg)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(msg + "\n")


def run(cmd, **kw):
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise Fail(f"{cmd[0]} failed (exit {r.returncode}):\n" + "\n".join((r.stdout + r.stderr).splitlines()[:30]))
    if r.stderr.strip():
        log(f"{Path(cmd[0]).name}: {r.stderr.strip()}")
    return r


def ihx_to_image(path):
    mem = {}
    ext = 0
    for ln in path.read_text().splitlines():
        if not ln.startswith(":"):
            continue
        n = int(ln[1:3], 16)
        addr = int(ln[3:7], 16)
        typ = int(ln[7:9], 16)
        data = bytes.fromhex(ln[9:9 + 2 * n])
        if typ == 0:
            for i, b in enumerate(data):
                if ext + addr + i in mem:
                    raise Fail(f"two areas write code address 0x{ext + addr + i:04X} (overlap)")
                mem[ext + addr + i] = b
        elif typ == 4:
            ext = int.from_bytes(data, "big") << 16
    return mem


def build(tag, defs, probe=False):
    d = WORK / tag
    d.mkdir(parents=True, exist_ok=True)
    rels = []
    inc = SRC / ("probe" if probe else "normal")      # chanhooks.inc: plain hook table or counting trampolines
    for s in S_SRCS:
        run(["sdas8051", "-plosgff", f"-I{inc}", "-o", d / f"{s}.rel", SRC / f"{s}.s"])
    if probe:
        run(["sdas8051", "-plosgff", "-o", d / "probe.rel", SRC / "probe/probe.s"])
    for c in C_SRCS:
        run(["sdcc", "-mmcs51", "--model-small", "--opt-code-size", "--nooverlay", "--std-c11", f"-I{INC}", *defs, "-c",
             "-o", d / f"{c}.rel", SRC / f"{c}.c"])
    # link order: hosttab first so CONST starts with the tables
    rels = [d / f"{s}.rel" for s in ["hosttab"]] + [d / f"{c}.rel" for c in C_SRCS] + \
           [d / f"{s}.rel" for s in S_SRCS if s != "hosttab"] + ([d / "probe.rel"] if probe else [])
    lnk = d / "link.lnk"
    lnk.write_text("\n".join(["-mjwxiu", f"-i {d / 'out.ihx'}", "-aCSEG=0x1B03", "-aCONST=0x4000", "-aDSEG=0x30",
                              "-aXSEG=0x80C0", f"-k{LIBDIR}", "-lmcs51", *map(str, rels), "-e", ""]))
    run(["sdld", "-f", lnk])
    return d, ihx_to_image(d / "out.ihx")


def parse_map(d):
    txt = (d / "out.map").read_text()
    syms = {}
    for m in re.finditer(r"^\s+([0-9A-F]{8})\s+(_\w+|__\w+)\s", txt, re.M):
        syms[m.group(2)] = int(m.group(1), 16)
    return txt, syms


def crc16(d):
    """CRC-16/BUYPASS (poly 0x8005, init 0, no reflection): the stock v21 and Meraki images carry it big-endian at file offset 0x38FE."""
    c = 0
    for x in d:
        c ^= x << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x8005) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
    return c


def checks(mem, txt, syms, tag):
    errs = []
    lo, hi = min(mem), max(mem)
    if lo < BASE or hi >= BASE + SIZE:
        errs.append(f"image bytes outside 0x{BASE:04X}..0x{BASE + SIZE - 1:04X}: 0x{lo:04X}..0x{hi:04X}")
    img = bytearray(SIZE)
    for a, b in mem.items():
        if BASE <= a < BASE + SIZE:
            img[a - BASE] = b
    # hook table: 15 x [FF hi lo], targets inside the image
    for i in range(15):
        t, h, l = img[3 * i:3 * i + 3]
        tgt = (h << 8) | l
        if t != 0xFF or not (BASE <= tgt < BASE + SIZE) or tgt < 0x1A60:
            errs.append(f"hook slot {i}: {t:02X} {h:02X} {l:02X} invalid")
    if any(img[0x2F:0x60]):
        errs.append("hook table padding 0x1A2F..0x1A5F not zero")
    if img[0x100] != 0x02:
        errs.append(f"0x1B00 is not an LJMP (byte {img[0x100]:02X})")
    if not img[0x3900:0x390D] == b"POETEC_IMAGE+":
        errs.append("tag at file offset 0x3900 (CPU 0x5300) is not POETEC_IMAGE+...")
    if any(img[0x38FC:0x3900]):
        errs.append("CRC field 0x38FC..0x38FF (CPU 0x52FC..0x52FF) is not free")
    else:
        img[0x38FE:0x3900] = crc16(img[:0x38FE]).to_bytes(2, "big")   # ROM checks it after the download (bench 2026-11-21)
        if crc16(img[:0x3900]) != 0:
            errs.append("CRC-16 residue over 0x0000..0x38FF is not 0")
    if img[0x3820:0x3823] != b"\x22\x22\x22":
        errs.append("RET stubs at 0x5220..0x5222 missing")
    cend = max(a for a in mem if a < 0x5220) + 1
    if cend > CODE_END_MAX:
        errs.append(f"code ends at 0x{cend:04X}, beyond 0x{CODE_END_MAX:04X}")
    ld = re.search(r"([0-9A-F]{8})\s+l_DSEG\b", txt)
    dend = 0x30 + int(ld.group(1), 16) if ld else 0xFF
    log(f"[{tag}] IRAM data 0x30..0x{dend - 1:02X}")
    if dend - 1 > IRAM_DATA_MAX:
        errs.append(f"IRAM data ends at 0x{dend - 1:02X}, beyond 0x{IRAM_DATA_MAX:02X}")
    cs = [m for m in re.finditer(r"^CSEG\s+([0-9A-F]{8})\s+([0-9A-F]{8})", txt, re.M)][-1]
    if int(cs.group(1), 16) + int(cs.group(2), 16) > 0x4000:
        errs.append("CSEG runs into CONST at 0x4000")
    return errs, bytes(img), cend


# our function(s) -> stock function names (hwdb/firmware/bcm59111-v21.names.tsv): same role, not the same scope
ROLE = {
    "i2c slave ISR": (["_isr_i2c_body", "_hook_i2c"], ["isr_i2c_slave"]),
    "host table dispatch": (["_host_wr_dispatch", "_host_rd_dispatch"], ["host_reg_dispatch"]),
    "channel ISRs (4)": (["_hook_chan0", "_hook_chan1", "_hook_chan2", "_hook_chan3"],
                         ["isr_chan0", "isr_chan1", "isr_chan2", "isr_chan3"]),
    "event ISR 2": (["_isr2_body", "_hook_isr2"], ["isr_event_ext"]),
    "timer tick": (["_chan_tick", "_tick_body", "_hook_timer1"], ["isr_timer1_tick", "tick_timers"]),
    "periodic ISR + measurement chain (offset trim 0x1B03 not ported)": (["_periodic_body", "_hook_periodic", "_hal_periodic", "_meas_*", "_class_code", "_hal_ind_word", "_hal_meas_calib", "_hal_temp_valid"],
                                                    ["isr_periodic", "meas_cur_offset_trim", "meas_temp_calib",
                                                     "meas_convert_cm_vm_temp", "periodic_dc_mux_scheduler",
                                                     "periodic_detcls_sequencer"]),
    "channel state machine (+ arm / timers)": (["_chan_run", "_arm", "_tmr_set", "_wait_then", "_chan_init", "_chan_request",
                                                "_svc_set", "_opmode", "_sat_set", "_acquire_probe", "_release_probe",
                                                "_chan_restart_all", "_chan_i2c_error_reload"],
                                               ["st_port_reset", "st_port_shutdown", "st_port_cfg", "st_mode_select",
                                                "st_detect_arm", "st_detect_start", "st_detect_wait", "st_detect_result",
                                                "st_detect_invalid", "st_class_gate", "st_class_hw_start", "st_class_wait",
                                                "st_class_done", "st_class_eval", "st_power_request", "st_power_on",
                                                "st_poweron_hold", "st_power_good", "st_fault_shutdown", "st_fault_cooldown",
                                                "st_disconnect_cleanup", "st_idle", "st_wait_b1", "st_wait_b3", "st_wait_b6",
                                                "st_wait_b6b", "st_wait_b7", "st_retry_wait_a", "st_retry_wait_b",
                                                "st_detect_ok_wait", "st_chan_enable", "arm_channel", "arm_channel_cur",
                                                "run_state", "set_timer"]),
    "start-up, main loop, init": (["_fw_main", "_slot0", "_rom_init", "_rom_strap_call", "_hostregs_init", "_hal_sfr_init",
                                   "_hal_timer_init", "_hal_ext_init"],
                                  ["main", "main_loop", "startup_idata_clear", "init_i2c_status", "init_regs_set_a",
                                   "init_regs_set_b", "init_fw_rev_crc", "init_hp_defaults", "init_icut_defaults",
                                   "init_ilim_defaults", "init_hw_channels", "reload_after_i2c_error"]),
    "host write handlers (28 regs)": (["_wh_*"], ["wh_op_mod", "wh_int_mask", "wh_cfg42", "wh_dis_sen_en", "wh_det_class_en", "wh_reg_15",
                                           "wh_reg_17", "wh_pb_detclass", "wh_pb_pwr", "wh_pb_reset", "wh_reg_1e", "wh_reg_1f",
                                           "wh_hp_en", "wh_reg_46", "wh_reg_47", "wh_reg_48", "wh_reg_4b", "wh_reg_4c",
                                           "wh_reg_4d", "wh_reg_50", "wh_reg_51", "wh_reg_52", "wh_reg_55", "wh_reg_56",
                                           "wh_reg_57", "wh_reg_5e", "wh_reg_5f"]),
    "host read handlers (5 events + 16 counters)": (["_rh_*"], ["rh_pwr_evt_clr", "rh_det_evt_clr", "rh_fault_evt_clr", "rh_evt8_clr",
                                                         "rh_therm_evt_clr"] + [f"rh_cnt_p{p}_{k}" for p in range(1, 5) for k in range(4)]),
    "poll / events / INT helpers": (["_poll_common", "_evt_raise", "_sup_event", "_evt_clear_channel", "_cnt_inc"],
                                    ["poll_common", "int_raise_pwr_good", "int_raise_class", "set_sup_event",
                                     "int_line_update", "poll_detect_class_regs", "chan_event_latch"]),
}


def local_funcs():
    """Every code label of the linked .rst listings with its address (static functions included)."""
    out = {}
    for rst in sorted((WORK / "main").glob("*.rst")):
        for ln in rst.read_text().splitlines():
            m = re.match(r"^\s+([0-9A-F]{6})\s+\d+ (_\w+):", ln)
            if m and 0x1B03 <= int(m.group(1), 16) < 0x4000:
                out[m.group(2)] = int(m.group(1), 16)
    return out


def sizes(cend, tag):
    labels = local_funcs()
    cs_end = 0x1B03
    txt = (WORK / "main" / "out.map").read_text()
    cs = [m for m in re.finditer(r"^CSEG\s+([0-9A-F]{8})\s+([0-9A-F]{8})", txt, re.M)][-1]
    cs_end = int(cs.group(1), 16) + int(cs.group(2), 16)
    order = sorted((a, n) for n, a in labels.items())
    size = {}
    for i, (a, n) in enumerate(order):
        size[n] = (order[i + 1][0] if i + 1 < len(order) else cs_end) - a
    stock = {}
    for ln in (ROOT / "hwdb/firmware/bcm59111-v21.functions.tsv").read_text().splitlines()[1:]:
        f = ln.split("\t")
        stock[f[1]] = int(f[2])
    names = {}
    for ln in (ROOT / "hwdb/firmware/bcm59111-v21.names.tsv").read_text().splitlines():
        if ln.startswith("CODE:"):
            f = ln.split("\t")
            names[f[1]] = int(f[0][5:], 16)
    rows = []
    for role, (ours, theirs) in ROLE.items():
        names_o = [m for n in ours for m in ([k for k in size if k.startswith(n[:-1])] if n.endswith("*") else [n])]
        o = sum(size.get(n, 0) for n in names_o)
        missing = [n for n in names_o if n not in size]
        st = sum(stock.get(n, 0) for n in theirs)
        rows.append((role, o, st, missing))
    return rows, size, cs_end - 0x1B03


def main():
    help_guard(__doc__)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("")
    try:
        r = subprocess.run([sys.executable, str(ROOT / "scripts/poe_fw_genhdr.py"), "--check"], capture_output=True, text=True)
        if r.returncode:
            raise Fail("generated files stale vs hwdb regs.tsv: run scripts/poe_fw_genhdr.py\n" + r.stdout)
        base = ["-DPOE_FW_NO_POWER", "-DPOE_FW_PROBE"]
        for tag, defs, out in (("main", [], "bcm59111-awto.bin"), ("nopower", ["-DPOE_FW_NO_POWER"], "bcm59111-awto-nopower.bin"),
                               ("probeA", base + ["-DPOE_FW_PROBE_ID=1", "-DPOE_FW_PROBE_ARM"], "bcm59111-awto-probeA.bin"),
                               ("probeB", base + ["-DPOE_FW_PROBE_ID=2"], "bcm59111-awto-probeB.bin"),
                               ("probeC", base + ["-DPOE_FW_PROBE_ID=3", "-DPOE_FW_PROBE_ARM", "-DPOE_FW_PROBE_T2"], "bcm59111-awto-probeC.bin")):
            d, mem = build(tag, defs, tag.startswith("probe"))
            txt, syms = parse_map(d)
            errs, img, cend = checks(mem, txt, syms, tag)
            log(f"[{tag}] code end 0x{cend:04X}, image bytes used {len(mem)}")
            if errs:
                raise Fail(f"[{tag}] layout checks failed:\n  " + "\n  ".join(errs))
            (OUT_DIR / out).write_bytes(img)
            log(f"[{tag}] wrote {OUT_DIR / out} ({len(img)} B)")
            if tag == "main":
                rows, size, cseg = sizes(cend, tag)
                p = ROOT / "tmp/poe-fw/sizes.tsv"
                p.parent.mkdir(parents=True, exist_ok=True)
                lines = ["role\tours_B\tstock_B\tnote"]
                for role, o, st, missing in rows:
                    lines.append(f"{role}\t{o}\t{st}\t{'missing ' + ','.join(missing) if missing else ''}")
                lines.append(f"all CSEG code\t{cseg}\t\t")
                lines += ["", "function\tsize"] + [f"{n}\t{v}" for n, v in sorted(size.items(), key=lambda kv: -kv[1])]
                p.write_text("\n".join(lines) + "\n")
                for ln in lines[:len(rows) + 2]:
                    log("size " + ln.replace("\t", " | "))
    except Fail as e:
        log(f"BUILD REFUSED: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
