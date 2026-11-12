# Stock debug CLI (switchdrvr 7.5.15)

Reversed from the Ghidra export of `switchdrvr`; live-checked on stock 2026-11-12. Tool: `scripts/stock_cli.py`.

## Access
- FastPath CLI listens on `127.0.0.1:23` of the switch; reach it with `scripts/stock_cli.py` (ssh `-L` tunnel, `ubnt:ubnt`).
- No login prompt: session starts at `(UBNT) >`; `enable` (no password) gives `(UBNT) #`.
- Session has no user identity: admin-only commands answer "Access Denied" / "Unknown user!".

## Hidden commands (registered in `clicommands.c`, mode 0x78 = FUN_00313814)
| Command | Registered at | Result without user |
|---|---|---|
| `devshell <expr>` ("Execute devshell command. Only the administrative user") | FUN_00313814 | Access Denied |
| `bcmsh` ("Enter into BCM Shell") | FUN_003174a8 | Unknown user! |
| `shexec <cmd>`, `drivshell <arg>` | FUN_00313814 | not tried |
- Admin check: per-session flag at `session + 300` (`FUN_003bc68c`); session table `DAT_023abe48 + idx * 0xd6a8`. Who sets it: not found yet.
- `devshell` calls into `devshell_symbols.gz` (`/usr/lib/devshell_symbols.gz`, not in the rootfs). Useful targets (strings): `hapiBroadDebugRegNonZeroDump(unit)`, `hapiBroadDebugMemoryDumpAll(unit,0)`, `hapiBroadDebugPcimDump(unit,off,count,printZero)`, `hapiBroadDebugSocRegDump(reg,unit,pbmp)`.

## Telnet-daemon debug switch (dead on this build)
- CLI command "Start Telnet Daemon" (FUN_003d5f54): writes `/var/run/fastpath/debug-flags/telnetd-port` (default 2323) and `.../telnetd-root-password` (default `lvl7dbg`), then `pm app-restart utelnetd`.
- `pm app-list` on 7.5.15 has no `utelnetd` (apps: switchdrvr, syncdb, syncdb-test, proctest), so nothing starts.

## Unprivileged `show` / `capture`
- `show forwardingdb`, `show hardware`, `capture start|stop` (CPU packet capture) are listed at `#`.

## Other ways to see register writes
- Stock kernel 3.6.5: no debugfs, kprobes, uprobes, ftrace, `/proc/config.gz`.
- Stock has no `devmem`; `/sys/bcm/mem` reads go to dmesg ([board.md](board.md)).
- `ptrace` on `switchdrvr`, breakpoint on the S-channel send function: next step (needs a static ARM tracer).
