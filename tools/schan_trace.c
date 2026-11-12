// SPDX-License-Identifier: GPL-2.0+
/*
 * Log every S-channel request stock switchdrvr sends (_soc_schan_op_cmicm @ 0x127a59c, 7.5.15).
 * Build: scripts/build_schan_trace.py. Run on the switch:
 *   schan_trace -n switchdrvr -x OLDPID -o /tmp/schan.log   (wait for a NEW switchdrvr, attach, trace)
 *   schan_trace -p PID -o /tmp/schan.log                    (attach to a running one)
 * Line: <ms> <tid> u<unit> <word0> <word1> ... (request words only; MIIM/DMA register writes are not S-channel).
 * Docs: docs/stock-debug-cli.md
 */
#include <errno.h>
#include <fcntl.h>
#include <dirent.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/ptrace.h>
#include <sys/types.h>
#include <sys/user.h>
#include <sys/wait.h>
#include <linux/ptrace.h>

#define BP_ADDR		0x0127a59cUL
#define ARM_BKPT	0xe7f001f0UL	/* undefined-instruction breakpoint, as gdb uses on ARM Linux */
#define MAX_WORDS	22		/* SCHAN_MESSAGE[22] */
#ifndef __WALL
#define __WALL		0x40000000
#endif

static FILE *out;
static long orig_insn;
static pid_t pid;
static long t0_ms;

static long now_ms(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec * 1000L + ts.tv_nsec / 1000000L;
}

static pid_t find_new(const char *name, pid_t old)
{
	for (;;) {
		DIR *d = opendir("/proc");
		struct dirent *e;

		while (d && (e = readdir(d))) {
			char p[64], comm[64] = "";
			FILE *f;
			pid_t n = atoi(e->d_name);

			if (n <= 0 || n == old)
				continue;
			snprintf(p, sizeof p, "/proc/%d/comm", n);
			f = fopen(p, "r");
			if (f) {
				if (fgets(comm, sizeof comm, f))
					comm[strcspn(comm, "\n")] = 0;
				fclose(f);
			}
			if (!strcmp(comm, name)) {
				closedir(d);
				return n;
			}
		}
		if (d)
			closedir(d);
		/* New switchdrvr starts SDK init >1 s after exec; 2 ms poll attaches long before that. */
		usleep(2000);
	}
}

static void log_request(pid_t tid)
{
	struct user_regs_struct_arm { long uregs[18]; } r;
	uint32_t w[MAX_WORDS];
	int i, n;

	if (ptrace(PTRACE_GETREGS, tid, 0, &r) < 0)
		return;
	n = r.uregs[2] > MAX_WORDS ? MAX_WORDS : (int)r.uregs[2];
	for (i = 0; i < n; i++) {
		errno = 0;
		w[i] = ptrace(PTRACE_PEEKDATA, tid, (void *)(r.uregs[1] + 4 * i), 0);
	}
	fprintf(out, "%ld %d u%ld", now_ms() - t0_ms, tid, r.uregs[0]);
	for (i = 0; i < n; i++)
		fprintf(out, " %08x", w[i]);
	fputc('\n', out);
	fflush(out);
}

/* Entry insn is `push {r4-r11,lr}` (0xe92d4ff0). ARM Linux 3.6 here has no PTRACE_SINGLESTEP, so emulate it. */
static void emulate_push(pid_t tid)
{
	struct { long u[18]; } r;
	int i;
	static const int regs[9] = { 4, 5, 6, 7, 8, 9, 10, 11, 14 };

	ptrace(PTRACE_GETREGS, tid, 0, &r);
	r.u[13] -= 36;
	for (i = 0; i < 9; i++)
		ptrace(PTRACE_POKEDATA, tid, (void *)(r.u[13] + 4 * i), (void *)r.u[regs[i]]);
	r.u[15] += 4;
	ptrace(PTRACE_SETREGS, tid, 0, &r);
}

static void attach_all(pid_t p)
{
	char path[64];
	DIR *d;
	struct dirent *e;

	snprintf(path, sizeof path, "/proc/%d/task", p);
	d = opendir(path);
	while (d && (e = readdir(d))) {
		pid_t t = atoi(e->d_name);

		if (t > 0 && ptrace(PTRACE_SEIZE, t, 0, (void *)(PTRACE_O_TRACECLONE)) < 0)
			fprintf(stderr, "seize %d: %s\n", t, strerror(errno));
		else if (t > 0)
			ptrace(PTRACE_INTERRUPT, t, 0, 0);
	}
	if (d)
		closedir(d);
}

int main(int argc, char **argv)
{
	const char *name = NULL, *path = "/tmp/schan.log";
	pid_t old = 0;
	int o, first = 1;

	while ((o = getopt(argc, argv, "p:n:x:o:")) != -1) {
		if (o == 'p') pid = atoi(optarg);
		else if (o == 'n') name = optarg;
		else if (o == 'x') old = atoi(optarg);
		else if (o == 'o') path = optarg;
	}
	if (!pid && !name) {
		fprintf(stderr, "usage: -p PID | -n NAME [-x OLDPID]  [-o file]\n");
		return 2;
	}
	out = fopen(path, "w");
	if (!out)
		return 1;
	if (!pid)
		pid = find_new(name, old);
	t0_ms = now_ms();
	attach_all(pid);
	for (;;) {
		int st;
		pid_t t = waitpid(-1, &st, __WALL);

		if (t < 0)
			break;
		if (WIFEXITED(st) || WIFSIGNALED(st)) {
			if (t == pid)
				break;
			continue;
		}
		if (first) {	/* insert once the first thread has stopped */
			errno = 0;
			orig_insn = ptrace(PTRACE_PEEKTEXT, t, (void *)BP_ADDR, 0);
			ptrace(PTRACE_POKETEXT, t, (void *)BP_ADDR, (void *)ARM_BKPT);
			first = 0;
		}
		int sig = WSTOPSIG(st), ev = st >> 16;

		if (ev == PTRACE_EVENT_STOP || ev == PTRACE_EVENT_CLONE) {
			ptrace(PTRACE_CONT, t, 0, 0);
		} else if (sig == SIGTRAP) {
			struct { long u[18]; } r;

			ptrace(PTRACE_GETREGS, t, 0, &r);
			if ((unsigned long)r.u[15] == BP_ADDR) {
				log_request(t);
				emulate_push(t);
			}
			ptrace(PTRACE_CONT, t, 0, 0);
		} else {
			ptrace(PTRACE_CONT, t, 0, sig == SIGSTOP ? 0 : sig);
		}
	}
	fclose(out);
	return 0;
}
