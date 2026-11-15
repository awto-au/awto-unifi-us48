// SPDX-License-Identifier: GPL-2.0
/*
 * a1 read-only register probe for the Linux state (#86 unit 0): /dev/mem, one load per list entry, SIGBUS = abort (flagged, skipped).
 * stdin: lines "ADDR WIDTH" (hex, 1|4). stdout: "I:VALUE" or "I:A" (SIGBUS), tokens in list order, flushed per line.
 * The kernel maps /dev/mem noncached (strongly ordered); a stalled read = no more output, host side detects the stall.
 */
#include <fcntl.h>
#include <setjmp.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <unistd.h>

static sigjmp_buf jb;

static void on_bus(int sig)
{
	(void)sig;
	siglongjmp(jb, 1);
}

int main(void)
{
	int fd = open("/dev/mem", O_RDONLY | O_SYNC);
	unsigned long addr, w, page = 0;
	volatile uint8_t *map = NULL;
	struct sigaction sa = { .sa_handler = on_bus };
	unsigned int i = 0, aborts = 0;
	char line[64];

	if (fd < 0) {
		perror("/dev/mem");
		return 1;
	}
	sigemptyset(&sa.sa_mask);
	sigaction(SIGBUS, &sa, NULL);
	setvbuf(stdout, NULL, _IOLBF, 0);
	while (fgets(line, sizeof(line), stdin)) {
		uint32_t v = 0xdeadbeef;

		if (sscanf(line, "%lx %lu", &addr, &w) != 2)
			continue;
		if (!map || (addr & ~0xfffUL) != page) {
			if (map)
				munmap((void *)map, 4096);
			page = addr & ~0xfffUL;
			map = mmap(NULL, 4096, PROT_READ, MAP_SHARED, fd, page);
			if (map == MAP_FAILED) {
				printf("%u:M\n", i++);
				map = NULL;
				continue;
			}
		}
		if (sigsetjmp(jb, 1)) {
			aborts++;
			printf("%u:A\n", i++);
			continue;
		}
		if (w == 1)
			v = map[addr & 0xfff];
		else
			v = *(volatile uint32_t *)(map + (addr & 0xfff));
		printf("%u:%08x\n", i++, v);
	}
	printf("DONE n=%u aborts=%u\n", i, aborts);
	return 0;
}
