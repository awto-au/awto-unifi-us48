"""Offline handoff checks: compile real C functions with fake MMIO, never contact a board.

Run: python3 -m unittest discover -s tests -p test_cmic_handoff.py -v
Requires a host C compiler (CC, default cc). This is not a hardware DMA model.
"""
import os
from pathlib import Path
import re
import runpy
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CORE = (ROOT / "common/bcm-cmic/bcm_cmic_hr2.c").read_text()
HEADER = (ROOT / "common/bcm-cmic/bcm_cmic.h").read_text()
UBOOT = (ROOT / "uboot-port/drivers/net/us48_cmic_eth.c").read_text()
LINUX = (ROOT / "kernel/us48-cmic/us48_cmic_core.c").read_text()


def function(source, name):
    """Extract a definition, including balanced nested blocks, not a prototype/call."""
    match = re.search(r"^[\w *]+\b" + name + r"\([^;]*?\)\n\{", source, re.M)
    if not match:
        raise AssertionError(f"missing definition: {name}")
    start = source.index("{", match.start())
    depth = 1
    end = start + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[match.start():end]


class HandoffSource(unittest.TestCase):
    def test_register_contract(self):
        regs = runpy.run_path(str(ROOT / "regdb/hr2_regs.py"))["REGS"]
        for ch in (0, 1):
            row = regs[f"CMIC_CMC0_CH{ch}_DMA_CTRL"]
            self.assertEqual(row[3], 0x31140 + 4 * ch)
            self.assertEqual(regs[f"CMIC_CMC0_DMA_DESC{ch}"][3], 0x31158 + 4 * ch)
            fields = {n: (b, w) for n, b, w in row[6]}
            self.assertEqual(fields["DIRECTION"], (0, 1))
            self.assertEqual(fields["DMA_EN"], (1, 1))
            self.assertEqual(fields["ABORT_DMA"], (2, 1))
        fields = {n: (b, w) for n, b, w in regs["CMIC_CMC0_DMA_STAT"][6]}
        self.assertEqual(regs["CMIC_CMC0_DMA_STAT"][3], 0x31150)
        self.assertEqual(fields["CH0_DMA_ACTIVE"], (8, 1))
        self.assertEqual(fields["CH1_DMA_ACTIVE"], (9, 1))
        for source in (CORE, (ROOT / "kernel/us48-cmic/us48_cmic.h").read_text()):
            for macro, bit in (("DMA_DIR_TX", 0), ("DMA_EN", 1), ("DMA_ABORT", 2)):
                self.assertRegex(source, rf"#define\s+{macro}\s+BIT\({bit}\)")
            self.assertRegex(source, r"#define\s+DMA_ACTIVE\(ch\)\s+BIT\(8 \+ \(ch\)\)")

    def test_both_drivers_request_handoff_removal(self):
        for name in ("us48_cmic_eth", "us48_cmic_ge"):
            block = UBOOT.split(f"U_BOOT_DRIVER({name}) = {{", 1)[1].split("};", 1)[0]
            self.assertRegex(block, r"\.remove\s*=\s*us48_eth_remove")
            self.assertRegex(block, r"\.flags\s*=[^;]*DM_FLAG_ACTIVE_DMA")
        boot = (ROOT / "uboot-port/cmd/us48boot.c").read_text()
        self.assertIn('"bootm 0x%x"', function(boot, "do_us48boot"))
        self.assertIn('TAG = "v2026.07"', (ROOT / "scripts/build_uboot.py").read_text())
        config = (ROOT / "uboot-port/configs/us48_defconfig").read_text()
        self.assertNotIn("# CONFIG_DM_DEVICE_REMOVE is not set", config)

    def test_start_only_arms_after_cleanup_and_success(self):
        start = function(UBOOT, "us48_eth_start")
        self.assertLess(start.index("bcm_hr2_dma_init"), start.index("bcm_hr2_rx_arm"))
        self.assertLess(start.index("p->up = true"), start.index("bcm_hr2_rx_arm"))
        off = function(UBOOT, "us48_eth_dma_off")
        for ch in (0, 1):
            self.assertIn(f"bcm_hr2_dma_abort(&p->unit, {ch})", off)

    def test_linux_aborts_before_reset_and_ring_allocation(self):
        cold = function(LINUX, "us48_chip_cold_init")
        self.assertIn("ch < 2", cold)
        self.assertLess(cold.index('"probe-before-abort"'), cold.index("bcm_hr2_dma_abort"))
        self.assertLess(cold.index("bcm_hr2_dma_abort"), cold.index('"probe-after-abort"'))
        self.assertLess(cold.index('"probe-after-abort"'), cold.index("bcm_hr2_u1_cold_init"))
        plat = function(LINUX, "us48_cmic_plat_probe")
        xe = function((ROOT / "kernel/us48-cmic/us48_cmic_xe_main.c").read_text(), "us48_xe_probe")
        for probe in (plat, xe):
            self.assertLess(probe.index("us48_chip_cold_init"), probe.index("us48_probe_common"))
        self.assertIn("dma_set_mask_and_coherent", function(LINUX, "us48_probe_common"))

    def test_diagnostics_are_opt_in_and_read_only(self):
        sample = function(CORE, "bcm_hr2_dma_read_state")
        self.assertNotRegex(sample, r"\b(wr|writel|udelay)\s*\(")
        self.assertIn('env_get_ulong("us48_handoff_diag", 10, 0)', UBOOT)
        self.assertIn("module_param(handoff_diag, bool, 0444)", LINUX)
        self.assertNotIn("handoff_diag = true", LINUX)
        for source, name in ((UBOOT, "us48_eth_handoff_state"), (LINUX, "us48_handoff_state")):
            body = function(source, name)
            self.assertNotRegex(body, r"\b(wr|writel|bcm_hr2_dma_abort)\s*\(")
            self.assertLess(body.index("bcm_hr2_dma_read_state"), body.index("s.stat_before, s.stat_after"))


class HandoffMMIO(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="us48-handoff-")
        cls.addClassCleanup(cls.tmp.cleanup)
        path = Path(cls.tmp.name)
        macros = "\n".join(line for line in CORE.splitlines()
                           if re.match(r"#define\s+(DMA_\w+|TX_CH|ABORT_WAIT_US|DCB_W15_DONE)\b", line))
        state = re.search(r"struct bcm_hr2_dma_state \{.*?\n\};", HEADER, re.S).group()
        shared = "\n".join(function(CORE, name) for name in
                           ("bcm_hr2_dma_read_state", "bcm_hr2_dma_abort", "bcm_hr2_tx_reap"))
        driver = "\n".join(function(UBOOT, name) for name in
                           ("us48_eth_dma_off", "us48_eth_handoff_state", "us48_eth_remove"))
        fixture = (ROOT / "tests/cmic_handoff_mmio.c").read_text()
        c = fixture.replace("/* PRODUCTION_MACROS */", macros).replace("/* PRODUCTION_STATE */", state)
        c = c.replace("/* PRODUCTION_SHARED */", shared).replace("/* PRODUCTION_DRIVER */", driver)
        (path / "handoff.c").write_text(c)
        cls.exe = path / "handoff"
        build = subprocess.run(shlex.split(os.environ.get("CC", "cc")) +
                       ["-std=c11", "-Wall", "-Wextra", "-Werror", str(path / "handoff.c"), "-o", str(cls.exe)],
                       capture_output=True, text=True, timeout=30)
        if build.returncode:
            raise AssertionError(build.stdout + build.stderr)

    def test_real_functions_with_fake_mmio(self):
        for case in ("idle", "enabled_idle", "active", "timeout", "snapshot", "tx_reap", "remove", "diag_guards"):
            with self.subTest(case=case):
                result = subprocess.run([str(self.exe), case], capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
