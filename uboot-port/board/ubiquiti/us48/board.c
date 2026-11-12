// SPDX-License-Identifier: GPL-2.0+
/*
 * Ubiquiti UniFi Switch 48 PoE 500W (Broadcom Hurricane 2), second stage.
 * Entered from the stock U-Boot via bootm: DDR, PLLs and UART are set up.
 */

#include <fdtdec.h>
#include <init.h>
#include <asm/global_data.h>

DECLARE_GLOBAL_DATA_PTR;

/* RAM = bank 0 (0x0, 128 MiB) + bank 1 (0x68000000, 128 MiB), both from the DT */
int dram_init(void)
{
	return fdtdec_setup_mem_size_base();
}

int dram_init_banksize(void)
{
	return fdtdec_setup_memory_banksize();
}

/* Relocate to the top of bank 1: the low bank holds image, kernel and stock leftovers */
phys_addr_t board_get_usable_ram_top(phys_size_t total_size)
{
	return 0x70000000;
}
