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

## `ubntdiag` (hidden factory/burn-in CLI, found 2026-11-13)
- Registered in `FUN_00313ed8` (`clicommands.c:0x201e`), handler `FUN_003b5970`. Prompts `Password:`; accepted string is the literal `55424e54` (hex of "UBNT"; `strncmp` with 9 bytes). Admin-only like `devshell`. C
- Subcommands: `snaketest` (`FUN_005f8e8c`: builds port bitmaps, `rcload /usr/etc/switch/snake.es48.{0,1}.soc`), `ledtest green|amber|disable` (`white` is registered but the handler rejects it) -> `FUN_005f948c`, `fantest <0-3|disable>` -> `FUN_0027611c` (`swctrl fan` path, 300 s gate), `pasv_power`, `sys_power` (board classes 4/9/0x13 only), `mode burn-in|normal`, `burn-in show|clear`, `sysled dim` (classes 4/9: I2C fade on `mux0`, not this board). C
- Burn-in state files: `/var/run/fastpath/ubnt_burnin_mode` (`mode=1`), `/var/etc/persistent/ubnt_burnin_{result,times,log}`; log writer `FUN_005f8464`. C
- Config switch: `switch.burnin.status` in system.cfg makes the generated switch start script run `echo mode=1 > /var/run/fastpath/ubnt_burnin_mode` (ubntbox `0x15ac38`/`0x15ac50`). C
- Boot script also runs `swctrl led diagnostic` once per boot (marker `/tmp/.led_boot_check`) and `echo 1 > /proc/iproc-pse/enable`. C
- `swctrl` functions: `port poe env mac sfp led vlan lldp relay`; `swctrl gpio show [pin N]`, `swctrl led set on|off`, `swctrl relay dc1_present on|off`, `swctrl env show`, `swctrl fan diagnostic`. Not tried. C (usage strings, ubntbox `0x155314..`)
- ubntbox applets (`ubntbox` `0x14e510`): `ubntconf cfgmtd fwupdate.real factorytest nettool trace coredump sysmon sysmon_ctrl poed egtool hwcheck swctrl fwutil utermd oopsdump bootsel crash-test`. `factorytest`/`hwcheck`/`egtool` bodies not yet read. C (names)

## `procmgr` debug flags and modes
- Options: `-diag`, `-gdb <addr>` (runs `/usr/bin/gdbserver <addr> switchdrvr`), `-valgrind` (`/bin/valgrind/valgrind`, log `/var/run/fastpath/valgrind.log`), `-console`. None of gdbserver, valgrind, `/bin/utelnetd` exist in the stock rootfs. C
- Flag files under `/var/run/fastpath/debug-flags/`: `telnetd-port`, `telnetd-root-password`, `set-passwd` (script feeding `passwd`), `lxsh-telnetd-port` (`lxshutelnetd_init`; args I: `/bin/utelnetd -p N -l/bin/sh -ilo` from adjacent strings). `procutil`/`libproc_libs` read `devshell-<pid>` and `mem-dump-<pid>` in the same dir (`pm`, `mem`, `devshell`, `stack-trace` are all `procutil` symlinks). `syncdb` reads `PROCTEST`, `PROC_MAX_MEM`, `PROC_SYSLOG_COUNT`. C
- `/tmp/syswrapper.debug` enables `log_dbg` in syswrapper.sh; `/etc/sysconf-trace` enables plugin start tracing. C
- inittab runs `/etc/rc.d/rc.watchdog`, which does not exist in the image: no hardware watchdog service; `reset-handler` (mcad symlink) respawns for the reset button. C

## BDE module parameters (insmod args are none at boot)
- `linux-kernel-bde`: `debug`, `eb_bus`, `spi_devid`/`spi_revid` (create a SPI-slave device), `forceirq`/`forceirqubm`, `nodevices`, `usemsi`, `maxpayload`, `himem`, `dmasize` (default 4 MB). Bus types it knows: PCI, SPI, ICS, AXI, EB. Prints `Ubiquiti: system %#x revision %d`. C
