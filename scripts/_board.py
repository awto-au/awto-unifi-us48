"""Shared helpers for scripts that talk to the RAM/flash-booted mainline board over ssh (root, key auth).

- ssh(cmd): one BatchMode ssh call; returns (rc, output). Host $US48_SSH (default root@192.168.25.207).
- schan_*: S-channel ops via devmem in one ssh call (same sequence as dev.py `schan`). The driver has no S-channel
  lock: run them only while the driver is idle on S-channel (no link change, no ifup/ifdown, no reload).
- temps(): LM75 + die temperatures (hwmon) in degrees C.
"""
import os
import subprocess

HOST = os.environ.get("US48_SSH", "root@192.168.25.207")
# LAN round trip < 1 s; connect 2 s = board rebooting/unreachable, not slow (as mod_reload.py).
CONNECT_S = 2
# One call is a stall guard: the longest batch here (some 100 devmem calls) runs in about 1 s.
CALL_S = 15.0
CMIC = {0: 0x48000000, 1: 0x0A000000}	# unit 0 AXI CMIC, unit 1 CMIC BAR as mapped on the host (dev.py schan)
SCHAN = 0x31000
OP = {"rreg": 0x0B, "rmem": 0x07, "wreg": 0x0D, "wmem": 0x09}


def ssh(cmd: str, call_s: float = CALL_S) -> tuple[int, str]:
    argv = ["ssh", "-T", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={CONNECT_S}", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", HOST, cmd]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=call_s)
    except subprocess.TimeoutExpired:
        return 255, f"ssh call exceeded {call_s} s: {cmd[:120]}"
    return r.returncode, r.stdout + r.stderr


def _op_sh(unit: int, op: str, blk: int, addr: int, words: int, data: list[int] | None) -> str:
    b = CMIC[unit] + SCHAN
    hdr = OP[op] << 26 | blk << 20 | (4 * words) << 7
    s = f"devmem {b:#x} 32 0; devmem {b + 12:#x} 32 {hdr:#x}; devmem {b + 16:#x} 32 {addr:#x}; "
    for i, v in enumerate(data or []):
        s += f"devmem {b + 20 + 4 * i:#x} 32 {v:#x}; "
    # DONE (b1) answers in us; 50 shell polls (each a devmem exec, ~1 ms) are far above that.
    s += (f"devmem {b:#x} 32 1; n=0; while [ $(( $(devmem {b:#x} 32) & 2 )) = 0 ] && [ $n -lt 50 ]; do n=$((n+1)); done; "
          f"echo S $(devmem {b:#x} 32)")
    if op.startswith("r"):
        s += "".join(f" $(devmem {b + 16 + 4 * i:#x} 32)" for i in range(words))
    return s + f"; devmem {b:#x} 32 0"


def schan_batch(ops: list[tuple]) -> list[list[int]]:
    """ops: (unit, op, blk, addr, words, data|None). Returns per op [ctrl, word0, ...]; raises on ssh/S-channel error."""
    if not ops:
        return []
    rc, out = ssh("; ".join(_op_sh(*o) for o in ops))
    lines = [ln.split()[1:] for ln in out.splitlines() if ln.startswith("S ")]
    if rc or len(lines) != len(ops):
        raise RuntimeError(f"schan batch failed rc={rc}: {out[-300:]}")
    res = []
    for o, ln in zip(ops, lines):
        v = [int(x, 16) for x in ln]
        if not v[0] & 2 or v[0] & 0x700000:
            raise RuntimeError(f"schan op {o[1]} unit {o[0]} blk {o[2]} addr {o[3]:#x}: ctrl {v[0]:#x}")
        res.append(v)
    return res


def temps() -> dict[str, float]:
    rc, out = ssh("for h in /sys/class/hwmon/hwmon*; do [ -e $h/temp1_input ] && echo $(cat $h/name) $(cat $h/temp1_input); done")
    t = {}
    for i, ln in enumerate(out.splitlines()):
        f = ln.split()
        if len(f) == 2 and f[1].isdigit():
            t[f"{f[0]}#{i}"] = int(f[1]) / 1000
    return t


def too_hot(t: dict[str, float]) -> str | None:
    """Stop limits (Dan): die >= 108 C, any LM75 >= 78 C."""
    for k, v in t.items():
        if (k.startswith("die") and v >= 108) or (k.startswith("lm75") and v >= 78):
            return f"{k} = {v} C"
    return None
