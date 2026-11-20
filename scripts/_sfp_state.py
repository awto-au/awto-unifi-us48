"""SFP module state of an xe port as the driver exports it (sysfs sfp_state / sfp_info, kernel/us48-cmic/us48_cmic_sfp.c, #147).

State: none | ok | unsupported:<reason> | fault:<reason>; "n/a" = attribute missing (driver without #147).
"""

SYS = "/sys/class/net/xe{f}"


def read_cmd(front: int) -> str:
    d = SYS.format(f=front)
    return f"echo \"$(cat {d}/sfp_state 2>/dev/null)\"; echo \"$(cat {d}/sfp_info 2>/dev/null)\""


def parse(out: str) -> tuple[str, str]:
    lines = out.splitlines() + ["", ""]
    return (lines[0].strip() or "n/a"), lines[1].strip()


def read(sh, front: int) -> tuple[str, str]:
    """sh(cmd) -> stdout on the board. Returns (state, info)."""
    return parse(sh(read_cmd(front)))


def bad(state: str) -> bool:
    """Module in the cage the link cannot come up with: stop waiting for carrier."""
    return state.startswith(("unsupported", "fault"))


def fmt(front: int, state: str, info: str) -> str:
    return f"xe{front} SFP module: {state}" + (f" - {info}" if info else "")
