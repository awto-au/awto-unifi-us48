"""Shared helpers for scripts that talk to the RAM/flash-booted mainline board over ssh (root, key auth).

- ssh(cmd): one BatchMode ssh call; returns (rc, output). Host $US48_SSH (default root@192.168.25.207).
- schan_batch: S-channel ops through us48_cmic's root-only `schan` parameter: each runs under the driver's S-channel lock
  (unit 1: the one us48_pvtmon shares), writes are read, written and read back in the kernel (#125). No devmem.
- temps(): LM75 + die temperatures (hwmon) in degrees C.
"""
import _ipreg  # noqa: E402
import os
import subprocess

HOST = os.environ.get("US48_SSH", f"root@{_ipreg.ip('us48-boot')}")
# LAN round trip < 1 s; connect 2 s = board rebooting/unreachable, not slow (as mod_reload.py).
CONNECT_S = 2
# One call is a stall guard: an S-channel batch (one sysfs write + read) or a temps read runs well under 1 s.
CALL_S = 15.0
SCHAN_PARAM = "/sys/module/us48_cmic/parameters/schan"
OPS = ("rreg", "rmem", "wreg", "wmem", "lookup", "delete")
# Per ssh call: the request is one sysfs write (< 4 KiB) and the result one page; the longest result line is ~400 chars.
BATCH_CHARS = 3000
BATCH_OUT = 3500


def ssh(cmd: str, call_s: float = CALL_S) -> tuple[int, str]:
    argv = ["ssh", "-T", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={CONNECT_S}", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", HOST, cmd]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=call_s)
    except subprocess.TimeoutExpired:
        return 255, f"ssh call exceeded {call_s} s: {cmd[:120]}"
    return r.returncode, r.stdout + r.stderr


def _req(unit: int, op: str, blk: int, addr: int, words: int, data: list[int] | None) -> str:
    if op not in OPS:
        raise ValueError(f"schan op {op!r}")
    return f"{unit} {op} {blk} {addr:x} {words}" + "".join(f" {v:x}" for v in (data or []))


def _out_len(op: str, words: int) -> int:
    return 60 + 9 * (2 * words if op in ("wreg", "wmem") else words + 1)


def schan_batch(ops: list[tuple]) -> list[dict]:
    """ops: (unit, op, blk, addr, words, data|None). Returns per op {status, data, old, back, ack}; raises on ssh/parse error.
    status: ok | mismatch (write read back different) | notfound (lookup/delete NACK) | err (data = [errno])."""
    out: list[dict] = []
    i = 0
    while i < len(ops):
        n, chars, olen = 0, 0, 0
        while i + n < len(ops):
            r = _req(*ops[i + n])
            if n and (chars + len(r) + 1 > BATCH_CHARS or olen + _out_len(ops[i + n][1], ops[i + n][4]) > BATCH_OUT):
                break
            chars += len(r) + 1
            olen += _out_len(ops[i + n][1], ops[i + n][4])
            n += 1
        out += _schan_call(ops[i:i + n])
        i += n
    return out


def _schan_call(ops: list[tuple]) -> list[dict]:
    req = ";".join(_req(*o) for o in ops)
    # busybox echo = one write(): the parameter runs the whole request, cat returns its result
    rc, txt = ssh(f"echo '{req}' > {SCHAN_PARAM} && cat {SCHAN_PARAM}")
    lines = [ln.split() for ln in txt.splitlines() if ln.split() and ln.split()[0] in ("ok", "mismatch", "notfound", "err")]
    if rc or len(lines) != len(ops):
        raise RuntimeError(f"schan request failed rc={rc} ({len(lines)} of {len(ops)} results): {txt[-300:]}")
    res = []
    for o, f in zip(ops, lines):
        st = f[0]
        if st == "err":
            if f[1].rstrip(":") in ("parse", "args", "data", "nodev", "full"):
                raise RuntimeError(f"schan {o[1]} unit {o[0]} blk {o[2]} addr {o[3]:#x}: {' '.join(f)}")
            res.append({"status": "err", "data": [int(f[1])], "line": " ".join(f)})
            continue
        if int(f[1]) != o[0] or f[2] != o[1] or int(f[3]) != o[2] or int(f[4], 16) != o[3]:
            raise RuntimeError(f"schan result does not match request {o[:4]}: {' '.join(f)}")
        r = {"status": st, "line": " ".join(f)}
        if "old" in f:
            k, b = f.index("old"), f.index("back")
            r["old"] = [int(x, 16) for x in f[k + 1:b]]
            r["back"] = [int(x, 16) for x in f[b + 1:]]
        if "ack" in f:
            r["ack"] = int(f[f.index("ack") + 1], 16)
        r["data"] = [int(x, 16) for x in f[f.index(":") + 1:]] if ":" in f else []
        res.append(r)
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
