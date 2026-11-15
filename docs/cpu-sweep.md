# A9 clock sweep under Linux (#17)

- Variant images: `python3 scripts/build_uboot.py --cpu-mhz N` -> `tmp/uboot/us48-uboot-cpuN.bin` (build dir `tmp/uboot-build-cpuN`).
  - Sets `CONFIG_US48_CPU_MHZ=N` + `CONFIG_US48_CPU_REENTRY=y` (N 1000..1999, relock path).
  - REENTRY relaxes the E0 gate (fid 6/7, any start rate) so the image can be chain-booted from the flashed 1550 awto-uboot; the E2 sequence goes via fid 2.
  - RAM boot only; never flashed (cold entry is at 400).
- Step: `python3 scripts/cpu_sweep.py N` = cold power-cycle (`autochain_boot.py`), `u2_ramboot.py` variant, `dev.py u2-boot --from tftp` mainline, 60 s dmesg/console watch, 10 s md5 stress on a tmpfs, temperatures.
- Results: `tmp/logs/cpu_sweep.log`, table in #17.
- Emergency (fan quiet rule, [fan.md](fan.md)): die >= 110 C or LM75 >= 80 C -> `pwm1` 255, stop.
