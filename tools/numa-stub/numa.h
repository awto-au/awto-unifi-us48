/* SPDX-License-Identifier: GPL-2.0-or-later OR MIT */
/* Minimal libnuma stand-in for a static, UP, no-NUMA cyclictest (rt-tests 2.x includes <numa.h> unconditionally). */
#ifndef US48_NUMA_STUB_H
#define US48_NUMA_STUB_H
#include <stddef.h>
#include <sys/types.h>

struct bitmask {
	unsigned long size;	/* bits */
	unsigned long *maskp;
};

int numa_available(void);
void *numa_alloc_onnode(size_t size, int node);
void numa_free(void *p, size_t size);
int numa_run_on_node(int node);
int numa_node_of_cpu(int cpu);
struct bitmask *numa_allocate_cpumask(void);
void numa_bitmask_free(struct bitmask *m);
struct bitmask *numa_bitmask_clearbit(struct bitmask *m, unsigned int n);
int numa_bitmask_isbitset(const struct bitmask *m, unsigned int n);
unsigned int numa_bitmask_weight(const struct bitmask *m);
struct bitmask *numa_parse_cpustring_all(const char *s);
int numa_sched_getaffinity(pid_t pid, struct bitmask *m);
int numa_sched_setaffinity(pid_t pid, struct bitmask *m);
#endif
