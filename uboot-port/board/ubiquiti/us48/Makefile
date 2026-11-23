# SPDX-License-Identifier: GPL-2.0+

obj-y	:= board.o
obj-$(CONFIG_US48_FAN)	+= fan.o
obj-$(CONFIG_US48_PCIE)	+= pcie.o
obj-$(CONFIG_US48_POE)	+= poe.o
obj-$(CONFIG_US48_SRAM_CLK)	+= sram_clk.o
# sram_clk.o: position-independent SRAM code, one section in source order, no jump tables
CFLAGS_sram_clk.o	:= -fno-toplevel-reorder -fno-jump-tables -fno-stack-protector -fno-builtin
CFLAGS_fan.o	:= -I$(srctree)/lib/bcm-cmic
# poe.o embeds the staged firmware with .incbin
CFLAGS_poe.o	:= -Wa,-I$(srctree)/board/ubiquiti/us48
