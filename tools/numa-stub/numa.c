// SPDX-License-Identifier: GPL-2.0-or-later OR MIT
/* Stand-in for libnuma: one node, up to 64 CPUs, bitmask on one word. See numa.h. */
#define _GNU_SOURCE
#include <sched.h>
#include <stdlib.h>
#include <string.h>
#include "numa.h"

int numa_available(void) { return -1; }
void *numa_alloc_onnode(size_t size, int node) { (void)node; return malloc(size); }
void numa_free(void *p, size_t size) { (void)size; free(p); }
int numa_run_on_node(int node) { (void)node; return 0; }
int numa_node_of_cpu(int cpu) { (void)cpu; return 0; }

static struct bitmask *mk(void)
{
	struct bitmask *m = calloc(1, sizeof(*m));

	m->size = 8 * sizeof(unsigned long);
	m->maskp = calloc(1, sizeof(unsigned long));
	return m;
}

struct bitmask *numa_allocate_cpumask(void) { return mk(); }
void numa_bitmask_free(struct bitmask *m) { if (m) { free(m->maskp); free(m); } }
struct bitmask *numa_bitmask_clearbit(struct bitmask *m, unsigned int n) { if (n < m->size) *m->maskp &= ~(1UL << n); return m; }
int numa_bitmask_isbitset(const struct bitmask *m, unsigned int n) { return n < m->size && (*m->maskp >> n) & 1; }
unsigned int numa_bitmask_weight(const struct bitmask *m) { return __builtin_popcountl(*m->maskp); }

struct bitmask *numa_parse_cpustring_all(const char *s)	/* "all", "N", "N-M", comma list */
{
	struct bitmask *m = mk();
	char *p, *e;
	long a, b;

	if (!strcmp(s, "all")) {
		*m->maskp = ~0UL;
		return m;
	}
	for (p = (char *)s; *p;) {
		a = b = strtol(p, &e, 10);
		if (e == p)
			goto bad;
		if (*e == '-')
			b = strtol(e + 1, &e, 10);
		for (; a <= b && a < (long)m->size; a++)
			*m->maskp |= 1UL << a;
		p = *e == ',' ? e + 1 : e;
		if (*e && *e != ',')
			goto bad;
	}
	return m;
bad:
	numa_bitmask_free(m);
	return NULL;
}

int numa_sched_getaffinity(pid_t pid, struct bitmask *m)
{
	cpu_set_t s;
	int i, r = sched_getaffinity(pid, sizeof(s), &s);

	*m->maskp = 0;
	for (i = 0; i < (int)m->size; i++)
		if (CPU_ISSET(i, &s))
			*m->maskp |= 1UL << i;
	return r;
}

int numa_sched_setaffinity(pid_t pid, struct bitmask *m)
{
	cpu_set_t s;
	int i;

	CPU_ZERO(&s);
	for (i = 0; i < (int)m->size; i++)
		if ((*m->maskp >> i) & 1)
			CPU_SET(i, &s);
	return sched_setaffinity(pid, sizeof(s), &s);
}
