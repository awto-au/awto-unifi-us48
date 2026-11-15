#!/usr/bin/env python3
"""gen_tsc — TSC SerDes microcode + init step table from the public OpenMDK source (#78).

Usage: scripts/gen_tsc.py [--src DIR]
- Source: Broadcom/OpenMDK phy/PKG/chip/bcmi_tsc_xgxs/ (default /vendors/broadcom/openmdk/db9c67869680, tag 2.10.9).
- Writes hwdb/firmware/us48-tsc.bin (bcmi_tsc_xgxs_ucode.c tsc_ucode_bin, loaded via request_firmware) and
  kernel/us48-cmic/us48_tsc_init.h (stage 0 / stage 2 of bcmi_tsc_xgxs_drv.c as register steps: 1-lane SFI, 4x1 mode).
- Register address + field shift/mask are read from bcmi_tsc_xgxs_defs.h; the step order and values are _tsc_init_stage_0/2.
- Log: tmp/logs/gen_tsc.log
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tmp/logs/gen_tsc.log"
SRC = Path("/vendors/broadcom/openmdk/db9c67869680")
PHY = "phy/PKG/chip/bcmi_tsc_xgxs"
FW_OUT = ROOT / "hwdb/firmware/us48-tsc.bin"
HDR_OUT = ROOT / "kernel/us48-cmic/us48_tsc_init.h"

# step kinds: (kind, reg, fields) ; kind W = RMW of listed fields, F = full write (CLR + fields)
# lane scope from the register address (copies bits): 0 per lane, 1 shared, 2 per lane pair.
# ALL = stage 0 WRITEALL_*: every lane of the TSC.
S0 = [
    ("W", "ANATX_DRIVER", {"ELEC_IDLE": 1}, "ALL"),
    ("W", "RESET", {"RESET_PLL": 0}, ""),
    ("W", "POWER", {"POWER_ON_TIMER_ENABLE": 1}, ""),
    ("W", "POWER", {"TX_PWRDWN_ANALOG": 0, "RX_PWRDWN_ANALOG": 0}, ""),
    ("D", 1000),
    ("W", "DSC_MISC_CTRL0", {"RXSEQSTART_AN_DISABLE": 1}, "ALL"),
    ("F", "TEMPERATURE", {"FORCE_TEMPERATURE": 1, "TEMP_IDX": 8, "TEMPERATURE": 678}, ""),
    ("RST", 1),
    ("W", "DSC_MISC_CTRL0", {"RXSEQSTART": 1}, "ALL"),
    ("D", 1000),
    ("W", "SETUP", {"START_SEQUENCER": 0}, ""),
    ("W", "SETUP", {"REFCLK_SEL": 3}, ""),
    ("D", 2000),
    ("W", "LANETEST", {"LFCK_BYPASS": 0}, ""),
    ("W", "ANAPLL_TIMER1", {"VCOSTEPTIME": 1}, ""),
    ("F", "CL73_BREAK_LINK", {"TX_DISABLE_TIMER_PERIOD": 0x10ed}, ""),
    ("F", "CL37_RESTART", {"CL37_RESTART_TIMER_PERIOD": 0x029a}, ""),
    ("F", "CL37_ACK", {"CL37_ACK_TIMER_PERIOD": 0x029a}, ""),
    ("F", "CL37_ERROR", {"CL37_ERROR_TIMER_PERIOD": 0x055d}, ""),
    ("F", "CL73_ERROR", {"CL73_ERROR_TIMER_PERIOD": 0x1a10}, ""),
    ("F", "CL73_DME_LOCK", {"PD_DME_LOCK_TIMER_PERIOD": 0x14d4}, ""),
    ("F", "IGNORE_LINK_TIMER", {"IGNORE_LINK_TIMER_PERIOD": 0x07d0}, ""),
    ("F", "LINK_FAIL_INHIBIT_TIMER_CL72", {"LINK_FAIL_INHIBIT_TIMER_CL72_PERIOD": 0x8236}, ""),
    ("F", "LINK_FAIL_INHIBIT_TIMER_NOT_CL72", {"LINK_FAIL_INHIBIT_TIMER_NCL72_PERIOD": 0x1a10}, ""),
    ("F", "PD_SD_TIMER", {"PD_SD_TIMER_PERIOD": 0x029a}, ""),
    ("F", "LINK_UP", {"CL73_LINK_UP_TIMER_PERIOD": 0x029a}, ""),
    ("F", "CL72_MAX_WAIT_TIMER", {"CL72_MAX_WAIT_TIMER_PERIOD": 0x8236}, ""),
    ("F", "CL72_WAIT_TIMER", {"CL72_WAIT_TIMER_PERIOD": 0x01fe}, ""),
    ("W", "PRBSCTRL", {"PRBS_INV_TX": 1, "PRBS_INV_RX": 1}, "ALL"),
    ("W", "CL72_MISC1_CONTROL", {"TX_FIR_TAP_MAIN_KX_INIT_VAL": 0x3f}, "ALL"),
    ("W", "CL72_AN_OS_DEFAULT_CONTROL", {"TX_FIR_TAP_POST_OS_INIT_VAL": 0, "TX_FIR_TAP_MAIN_OS_INIT_VAL": 0x3f,
                                         "TX_FIR_TAP_PRE_OS_INIT_VAL": 0}, ""),
    ("W", "CL72_AN_BR_DEFAULT_CONTROL", {"TX_FIR_TAP_POST_BR_INIT_VAL": 0x16, "TX_FIR_TAP_MAIN_BR_INIT_VAL": 0x25,
                                         "TX_FIR_TAP_PRE_BR_INIT_VAL": 4}, ""),
    ("W", "TX_X4_KR_DEFAULT_CONTROL", {"TX_FIR_TAP_POST_KR_INIT_VAL": 0x16, "TX_FIR_TAP_MAIN_KR_INIT_VAL": 0x25,
                                       "TX_FIR_TAP_PRE_KR_INIT_VAL": 4}, "ALL"),
    ("W", "CL72_AN_TWOP5_DEFAULT_CONTROL", {"TX_FIR_TAP_POST_2P5_INIT_VAL": 0, "TX_FIR_TAP_MAIN_2P5_INIT_VAL": 0x3f,
                                            "TX_FIR_TAP_PRE_2P5_INIT_VAL": 0}, ""),
    ("W", "SETUP", {"FORCE_PLL_MODE_AFE_SEL": 1, "DEFAULT_PLL_MODE_AFE": 0xa}, ""),
    ("D", 1000),
    ("F", "TX_X4_CREDIT0", {}, "ALL"),
    ("F", "TX_X4_PCS_CLOCKCNT0", {}, "ALL"),
    ("FW",),
    ("W", "SETUP", {"START_SEQUENCER": 1}, ""),
    ("PLL",),
    ("CRC",),
]
S2 = [
    ("D", 1000),
    ("W", "CONTROLS", {"PD_KX4_EN": 0, "PD_KX_EN": 0}, ""),
    ("W", "TX_X4_CREDIT0", {"CREDITENABLE": 0}, ""),
    ("W", "TX_X4_PCS_CLOCKCNT0", {"PCS_CREDITENABLE": 0}, ""),
    ("W", "TX_X4_MISC", {"ENABLE_TX_LANE": 0}, ""),
    ("W", "TX_X4_MISC", {"RSTB_TX_LANE": 0}, ""),
    ("UC", 0),
    ("W", "RX_X4_PMA_CONTROL_0", {"RSTB_LANE": 0}, ""),
    ("F", "CL73_BASE_ABIL_0", {"CL73_PAUSE": 1, "BASE_1000BASE_KX": 1, "BASE_10GBASE_KR": 1}, ""),
    ("F", "OVER1G_ABIL_0", {"BAM_2P5GBASE_X": 1}, ""),
    ("F", "OVER1G_ABIL_1", {}, ""),
    ("W", "CL37_BASE_ABIL", {"CL37_PAUSE": 1, "SGMII_MASTER_MODE": 0, "SGMII_SPEED": 2}, ""),
    ("W", "OVER1G_ABIL_1", {"HG2": 0, "FEC": 0, "CL72": 1}, ""),
    ("W", "CL72_MISC2_CONTROL", {"SW_ACTUAL_SPEED": 0x1b}, ""),
    ("F", "FIRMWARE_MODE", {"FIRMWARE_MODE": 0}, ""),
    ("W", "TX_X4_CREDIT0", {"CLOCKCNT0": 0x21, "CREDITENABLE": 1, "SGMII_SPD_SWITCH": 0}, ""),
    ("W", "TX_X4_CREDIT1", {"RESERVED0": 0x84, "CLOCKCNT1": 0}, ""),
    ("W", "TX_X4_LOOPCNT", {"LOOPCNT0": 1, "LOOPCNT1": 0}, ""),
    ("W", "TX_X4_MAC_CREDITGENCNT", {"MAC_CREDITGENCNT": 4}, ""),
    ("W", "TX_X4_PCS_CLOCKCNT0", {"PCS_CLOCKCNT0": 0, "REPLICATION_CNT": 0}, ""),
    ("W", "TX_X4_PCS_CREDITGENCNT", {"PCS_CREDITGENCNT": 0}, ""),
    ("W", "TX_X4_ENCODE_0", {"HG2_CODEC": 0, "HG2_MESSAGE_INVALID_CODE_ENABLE": 0, "HG2_ENABLE": 0,
                             "CL49_BYPASS_TXSM": 0, "ENCODEMODE": 5}, ""),
    ("W", "TX_X4_MISC", {"SCR_MODE": 3}, ""),
    ("W", "TX_X4_CL36_TX_0", {"DISABLE_PACKET_MISALIGN": 0}, ""),
    ("W", "TX_X2_BRCM_MODE", {"ACOL_SWAP_COUNT64B66B": 2}, ""),
    ("W", "RX_X4_PCS_CONTROL_0", {"DESCRAMBLERMODE": 0, "DECODERMODE": 1, "DESKEWMODE": 0, "DESC2_MODE": 1,
                                  "CL36BYTEDELETEMODE": 0}, ""),
    ("W", "RX_X4_CL36_RX_0", {"CL36_EN": 0, "REORDER_EN": 0, "DISABLE_CARRIER_EXTEND": 0}, ""),
    ("W", "RX_X4_DECODE_CONTROL_0", {"BYPASS_CL49RXSM": 0, "HG2_MESSAGE_INVALID_CODE_ENABLE": 1, "HG2_ENABLE": 0,
                                     "CL48_SYNCACQ_EN": 0, "CL48_CGBAD_EN": 0, "HG2_CODEC": 0,
                                     "BLOCK_SYNC_MODE": 1}, ""),
    ("W", "RX_X4_PMA_CONTROL_0", {"OS_MODE": 0, "RX_GBOX_AFRST_EN": 0}, ""),
    ("W", "RX_X2_MISC_0", {"CHK_END_EN": 1}, ""),
    ("W", "CL72_MISC1_CONTROL", {"TAP_DEFAULT_MUXSEL_FORCEVAL": 3, "TAP_DEFAULT_MUXSEL_FORCE": 1}, ""),
    ("W", "TX_X4_MISC", {"CL49_TX_LI_ENABLE": 1, "CL49_TX_LF_ENABLE": 1, "CL49_TX_RF_ENABLE": 1}, ""),
    ("W", "RX_X4_DECODE_CONTROL_0", {"CL49_RX_RF_ENABLE": 1, "CL49_RX_LF_ENABLE": 1, "CL49_RX_LI_ENABLE": 1}, ""),
    ("W", "TX_X2_CL48_0", {"CL48_TX_RF_ENABLE": 1, "CL48_TX_LF_ENABLE": 1, "CL48_TX_LI_ENABLE": 1}, ""),
    ("W", "RX_X2_MISC_1", {"CL48_RX_RF_ENABLE": 1, "CL48_RX_LF_ENABLE": 1, "CL48_RX_LI_ENABLE": 1}, ""),
    ("W", "TX_X2_CL82_0", {"CL82_TX_RF_ENABLE": 1, "CL82_TX_LF_ENABLE": 1, "CL82_TX_LI_ENABLE": 1}, ""),
    ("W", "RX_X2_MISC_1", {"CL82_RX_RF_ENABLE": 1, "CL82_RX_LF_ENABLE": 1, "CL82_RX_LI_ENABLE": 1}, ""),
    ("W", "ANATX_DRIVER", {"IDRIVER": 9}, ""),
    ("W", "ANATX_DRIVER", {"IPREDRIVER": 9}, ""),
    ("W", "ANATX_DRIVER", {"POST2_COEFF": 0}, ""),
    ("W", "CL72_TX_FIR_TAP", {"TX_FIR_TAP_FORCE": 1, "TX_FIR_TAP_POST": 0, "TX_FIR_TAP_MAIN": 0x3f, "TX_FIR_TAP_PRE": 0}, ""),
    ("RST", 0),
    ("D", 1000),
    ("UC", 1),
]
KIND = {"W": "TSC_W", "F": "TSC_F"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", type=Path, default=SRC)
    a = ap.parse_args()
    d = a.src / PHY
    defs = (d / "bcmi_tsc_xgxs_defs.h").read_text()
    uc = (d / "bcmi_tsc_xgxs_ucode.c").read_text()
    ver = re.search(r"/\* (Version [^*]*?) \*/", uc).group(1)
    body = uc[uc.index("tsc_ucode_bin[]"):]
    body = body[body.index("{") + 1:body.index("};")]
    fw = bytes(int(x, 16) for x in re.findall(r"0x([0-9a-fA-F]{2})", body))
    n = int(re.search(r"tsc_ucode_bin_len\s*=\s*(\d+)", uc).group(1))
    if len(fw) != n:
        sys.exit(f"ucode length {len(fw)} != tsc_ucode_bin_len {n}")
    FW_OUT.parent.mkdir(parents=True, exist_ok=True)
    FW_OUT.write_bytes(fw)

    def reg(name):
        m = re.search(rf"#define BCMI_TSC_XGXS_{name}r \(0x([0-9a-f]{{8}}) \| PHY_REG_ACC_TSC_IBLK\)", defs)
        if not m:
            sys.exit(f"register {name} not in defs")
        v = int(m.group(1), 16)
        return v & 0xffff, (v >> 20) & 7

    def field(r, f):
        m = re.search(rf"BCMI_TSC_XGXS_{r}r_{f}f_SET\(r,f\)\s+\(r\)\.[a-z0-9_]+\[0\]=\(\(\(r\)\.[a-z0-9_]+\[0\] & "
                      rf"~\(\(uint32_t\)0x([0-9a-f]+)(?: << (\d+))?\)\)", defs)
        if not m:
            sys.exit(f"field {r}.{f} not in defs")
        return int(m.group(1), 16), int(m.group(2) or 0)

    def emit(name, steps):
        out = [f"static const struct tsc_step {name}[] = {{"]
        for s in steps:
            k = s[0]
            if k in ("W", "F"):
                r, fl, al = s[1], s[2], s[3]
                addr, copies = reg(r)
                mask = val = 0
                for f, v in fl.items():
                    m, sh = field(r, f)
                    if v & ~m:
                        sys.exit(f"{r}.{f}: {v:#x} exceeds mask {m:#x}")
                    mask |= m << sh
                    val |= v << sh
                if k == "F":
                    mask = 0xffff
                scope = "TSC_ALL" if al else ("TSC_LANE", "TSC_SHARED", "TSC_PAIR")[copies]
                out.append(f"\t{{ TSC_RMW, {scope}, {addr:#06x}, {mask:#06x}, {val:#06x} }},\t/* {r} {','.join(fl)} */")
            elif k == "D":
                out.append(f"\t{{ TSC_DELAY, 0, 0, 0, {s[1]} }},")
            elif k == "RST":
                out.append(f"\t{{ TSC_LANE_RESET, 0, 0, 0, {s[1]} }},\t/* RESET RX/TX_RESET_ANALOG: 1 = hold, 0 = release */")
            elif k == "UC":
                out.append(f"\t{{ TSC_UC_RX, 0, 0, 0, {s[1]} }},")
            else:
                out.append(f"\t{{ TSC_{k}, 0, 0, 0, 0 }},")
        out.append("};")
        return out

    # RESET.RX/TX_RESET_ANALOG layout (lane mask fields) for the TSC_LANE_RESET op
    ra, _ = reg("RESET")
    rm, rs = field("RESET", "RX_RESET_ANALOG")
    tm, ts = field("RESET", "TX_RESET_ANALOG")
    sa, _ = reg("SETUP")
    pm, ps = field("SETUP", "PORT_MODE_SEL")
    qa, _ = reg("ANAPLL_STATUS")
    qm, qs = field("ANAPLL_STATUS", "PLLSEQPASS")
    hdr = [
        "/* SPDX-License-Identifier: GPL-2.0 */",
        "/*",
        " * Generated by scripts/gen_tsc.py from Broadcom/OpenMDK " + PHY + "/ (tag 2.10.9, ucode " + ver + "):",
        " * _tsc_init_stage_0 / _tsc_init_stage_2 of bcmi_tsc_xgxs_drv.c, 1-lane SFI. Do not edit.",
        " */",
        f"#define TSC_FW_VERSION\t{int(re.search(r'Version (0x[0-9a-f]+)', ver).group(1), 16):#06x}",
        f"#define TSC_FW_CRC\t{int(re.search(r'CRC: (0x[0-9a-f]+)', ver).group(1), 16):#06x}",
        f"#define TSC_RESET_REG\t{ra:#06x}",
        f"#define TSC_RESET_RX_SHIFT\t{rs}",
        f"#define TSC_RESET_TX_SHIFT\t{ts}",
        f"#define TSC_SETUP_REG\t{sa:#06x}",
        f"#define TSC_SETUP_MODE_MASK\t{pm << ps:#06x}",
        f"#define TSC_PLLSTAT_REG\t{qa:#06x}",
        f"#define TSC_PLLSTAT_PASS\t{qm << qs:#06x}",
        "",
    ] + emit("tsc_stage0", S0) + [""] + emit("tsc_stage2", S2) + [""]
    HDR_OUT.write_text("\n".join(hdr))
    LOG.parent.mkdir(parents=True, exist_ok=True)
    msg = f"{FW_OUT.relative_to(ROOT)}: {len(fw)} bytes, {ver}; {HDR_OUT.relative_to(ROOT)}: {len(S0)} + {len(S2)} steps"
    LOG.write_text(msg + "\n")
    print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
