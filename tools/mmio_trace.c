// SPDX-License-Identifier: GPL-2.0+
/*
 * Direct-MMIO tracer for STOCK switchdrvr 7.5.15 (#46 option B): logs loads/stores to the /dev/mem mapping of the unit-0 CMIC
 * (phys 0x48000000) that bde_trace cannot see (the SDK uses the mapped pointer inline, no bde vtable call).
 * Method: wait (PTRACE_SYSCALL) until the mapping exists, mprotect(PROT_NONE) its pages except the kept ones, then handle
 * each SIGSEGV: decode the ARM load/store, log it, unprotect that page, run the one instruction (bkpt at pc+4), re-protect.
 * ARM mode only; Thumb or unknown encodings are logged as UNK and stepped. Other threads can slip through the open page.
 * Run on the switch before switchdrvr starts (same flow as bde_trace):
 *   mmio_trace -n switchdrvr -x OLDPID -o /tmp/mmio.log -R /var/run/fastpath/switchdrvr_ready [-k 0x31000,0x32000] [-p 0x48000000]
 * Line: <ms> <tid> MR|MW u0 <cmic offset> <value> pc=<pc>   (LDM/STM: one line per register, offset = first address + 4*i)
 * -k: CMIC page offsets left unprotected (default 0x31000: S-channel + MIIM + CMC0 DMA, too hot to trap). Build: scripts/build_bde_trace.py --mmio
 */
#include <dirent.h>
#include <errno.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/ptrace.h>
#include <sys/time.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include <linux/ptrace.h>

#ifndef __WALL
#define __WALL		0x40000000
#endif
#define ARM_BKPT	0xe7f001f0UL	/* undefined insn the ARM ptrace layer turns into SIGTRAP (BKPT would raise an unhandled debug abort) */
#define ARM_SVC		0xef000000UL
#define TRAMP		0x01d3e9c0UL	/* slack after the text LOAD of switchdrvr 7.5.15: never executed */
#define SYS_MPROTECT	125
#define PAGE		0x1000UL
#define MAXKEEP		16

struct regs { long u[18]; };	/* r0..r15, cpsr, orig_r0 */

static FILE *out;
static pid_t pid;
static long t0_ms;
static volatile sig_atomic_t stop_req;
static const char *stopfiles[4];
static int nstopfiles;
static uint32_t keep[MAXKEEP] = { 0x31000 };
static int nkeep = 1;
static uint32_t wonly[MAXKEEP];
static int nwonly;
static uint32_t quiet_lo, quiet_hi;	/* offsets in [lo,hi) are executed but not logged (S-channel regs) */
static unsigned long nquiet, nsch;	/* nsch = writes to CMIC 0x31000 (SCHAN_CTRL): S-channel op ordinal, to align with the bde_trace SCH log */
static uint32_t phys_base = 0x48000000;
static uint32_t map_lo, map_hi;	/* process vaddr of the CMIC mapping once found */
static int phase = 1;		/* 1 = PTRACE_SYSCALL until the mapping exists, 2 = protected */
static unsigned long nmr, nmw, nunk;
static int shutting, unprot;
static pid_t detached[256];
static int ndet;
static pid_t sc_tid[256];	/* threads currently inside a syscall (between its entry and exit stop) */

static int sc_toggle(pid_t t)	/* returns 1 if t is now at a syscall EXIT stop */
{
	int i, free = -1;

	for (i = 0; i < 256; i++) {
		if (sc_tid[i] == t) {
			sc_tid[i] = 0;
			return 1;
		}
		if (!sc_tid[i] && free < 0)
			free = i;
	}
	if (free >= 0)
		sc_tid[free] = t;
	return 0;
}

static long now_ms(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec * 1000L + ts.tv_nsec / 1000000L;
}

static void on_sig(int s)
{
	if (s != SIGALRM)
		stop_req = 1;
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
		usleep(2000);	/* new switchdrvr: attach long before SDK init (same as bde_trace) */
		if (stop_req)
			exit(1);
	}
}

static uint32_t peek(pid_t t, uint32_t a)
{
	return (uint32_t)ptrace(PTRACE_PEEKDATA, t, (void *)(uintptr_t)a, 0);
}

static void poke(pid_t t, uint32_t a, uint32_t v)
{
	ptrace(PTRACE_POKETEXT, t, (void *)(uintptr_t)a, (void *)(uintptr_t)v);
}

static int getregs(pid_t t, struct regs *r)
{
	return ptrace(PTRACE_GETREGS, t, 0, r);
}

static void setregs(pid_t t, struct regs *r)
{
	ptrace(PTRACE_SETREGS, t, 0, r);
}

static int resume(pid_t t, int sig)
{
	return ptrace(phase == 1 ? PTRACE_SYSCALL : PTRACE_CONT, t, 0, (void *)(long)sig);
}

/* Run `svc 0; bkpt` at TRAMP with the given syscall regs in thread t (stopped, user context); registers are restored. */
static long remote_syscall(pid_t t, long nr, long a0, long a1, long a2)
{
	struct regs saved, r;
	uint32_t w0 = peek(t, TRAMP), w1 = peek(t, TRAMP + 4);
	int st;
	long ret;

	getregs(t, &saved);
	r = saved;
	r.u[7] = nr;
	r.u[0] = a0;
	r.u[1] = a1;
	r.u[2] = a2;
	r.u[15] = TRAMP;
	r.u[16] &= ~0x20L;		/* ARM state */
	poke(t, TRAMP, ARM_SVC);
	poke(t, TRAMP + 4, ARM_BKPT);
	setregs(t, &r);
	ptrace(PTRACE_CONT, t, 0, 0);
	waitpid(t, &st, __WALL);
	getregs(t, &r);
	ret = r.u[0];
	poke(t, TRAMP, w0);
	poke(t, TRAMP + 4, w1);
	setregs(t, &saved);
	return ret;
}

static int is_wonly(uint32_t off)
{
	int i;

	for (i = 0; i < nwonly; i++)
		if ((off & ~(PAGE - 1)) == wonly[i])
			return 1;
	return 0;
}

static int kept(uint32_t off)
{
	int i;

	for (i = 0; i < nkeep; i++)
		if ((off & ~(PAGE - 1)) == keep[i])
			return 1;
	return 0;
}

/* /proc/PID/maps: a /dev/mem mapping whose file offset range contains phys_base. */
static int find_map(void)
{
	char p[64], ln[256];
	FILE *f;
	int ok = 0;

	snprintf(p, sizeof p, "/proc/%d/maps", pid);
	f = fopen(p, "r");
	while (f && fgets(ln, sizeof ln, f)) {
		unsigned long s, e, off;

		if (!strstr(ln, "/dev/mem") || sscanf(ln, "%lx-%lx %*s %lx", &s, &e, &off) != 3)
			continue;
		if (off == phys_base) {
			map_lo = s;
			map_hi = e;
			ok = 1;
			break;
		}
	}
	if (f)
		fclose(f);
	return ok;
}

static void protect(pid_t t)
{
	uint32_t a = map_lo, run = 0;
	int runprot = 0;

	for (; a <= map_hi; a += PAGE) {
		int k = a < map_hi && !kept(a - map_lo);
		int prot = a < map_hi && is_wonly(a - map_lo) ? 1 : 0;

		if (run && (!k || prot != runprot)) {
			long r = remote_syscall(t, SYS_MPROTECT, run, a - run, runprot);

			fprintf(out, "%ld %d PROT %08x-%08x prot=%d rc=%ld\n", now_ms() - t0_ms, t, run, a, runprot, r);
			run = 0;
		}
		if (k && !run) {
			run = a;
			runprot = prot;
		}
	}
}

static void handle_segv(pid_t t, siginfo_t *si)
{
	struct regs r;
	uint32_t ins, pc, va = (uint32_t)(uintptr_t)si->si_addr, page = va & ~(PAGE - 1), off = va - map_lo, mask;
	int load, nreg = 0, rt[16], i, isldm = 0, unk = 0;
	uint32_t next, nextw, pre[16];
	int st, pend = 0, quiet = 0;
	long ms = now_ms() - t0_ms;

	getregs(t, &r);
	pc = r.u[15];
	ins = peek(t, pc);
	load = (ins >> 20) & 1;
	if (r.u[16] & 0x20) {
		unk = 1;			/* Thumb: not decoded */
	} else if (((ins >> 26) & 3) == 1) {	/* ldr/str/ldrb/strb */
		rt[nreg++] = (ins >> 12) & 15;
	} else if (((ins >> 25) & 7) == 0 && (ins & 0x90) == 0x90 && (ins & 0x60)) {	/* ldrh/strh/ldrsb/ldrsh/ldrd/strd */
		int sh = (ins >> 5) & 3;

		rt[nreg++] = (ins >> 12) & 15;
		if (!load && (sh == 2 || sh == 3)) {
			load = sh == 2;		/* sh==2 && !L: ldrd, sh==3 && !L: strd */
			rt[nreg++] = ((ins >> 12) & 15) + 1;
		}
	} else if (((ins >> 25) & 7) == 4) {	/* ldm/stm */
		mask = ins & 0xffff;
		isldm = 1;
		for (i = 0; i < 16; i++)
			if (mask & (1u << i))
				rt[nreg++] = i;
	} else {
		unk = 1;
	}
	for (i = 0; i < nreg; i++)
		pre[i] = (uint32_t)r.u[rt[i]];
	if (!unk && !load && off == 0x31000)
		nsch++;
	if (!unk && off >= quiet_lo && off < quiet_hi) {
		nquiet++;
		quiet = 1;
	}
	if (!unk && !load && !quiet)
		for (i = 0; i < nreg; i++)
			fprintf(out, "%ld %d MW u0 %05x %08x pc=%08x%s s=%lu\n", ms, t, off + 4 * i, pre[i], pc, isldm ? " m" : "", nsch);
	/* step: open the page, run the one instruction, close it again */
	remote_syscall(t, SYS_MPROTECT, page, PAGE, 3);
	next = pc + 4;
	nextw = peek(t, next);
	poke(t, next, ARM_BKPT);
	getregs(t, &r);
	setregs(t, &r);
	for (;;) {	/* a signal for this thread arriving during the step is passed on, not lost */
		ptrace(PTRACE_CONT, t, 0, (void *)(long)pend);
		pend = 0;
		if (waitpid(t, &st, __WALL) < 0 || !WIFSTOPPED(st))
			break;
		if (WSTOPSIG(st) == SIGTRAP && !(st >> 16))
			break;
		pend = ((st >> 16) || WSTOPSIG(st) == SIGSTOP) ? 0 : WSTOPSIG(st);
	}
	poke(t, next, nextw);
	getregs(t, &r);
	if (!(WIFSTOPPED(st) && WSTOPSIG(st) == SIGTRAP && (uint32_t)r.u[15] == next))
		fprintf(out, "%ld %d STEPERR st=%x pc=%08lx\n", ms, t, st, r.u[15]);
	remote_syscall(t, SYS_MPROTECT, page, PAGE, is_wonly(off) ? 1 : 0);
	if (unk) {
		nunk++;
		fprintf(out, "%ld %d UNK u0 %05x ins=%08x pc=%08x\n", ms, t, off, ins, pc);
	} else if (quiet) {
		nmw += !load * nreg;
	} else if (load) {
		nmr += nreg;
		getregs(t, &r);
		for (i = 0; i < nreg; i++)
			fprintf(out, "%ld %d MR u0 %05x %08lx pc=%08x%s s=%lu\n", ms, t, off + 4 * i, r.u[rt[i]], pc, isldm ? " m" : "", nsch);
	} else {
		nmw += nreg;
	}
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

		if (t > 0 && ptrace(PTRACE_SEIZE, t, 0, (void *)(PTRACE_O_TRACECLONE | PTRACE_O_TRACESYSGOOD)) < 0)
			fprintf(stderr, "seize %d: %s\n", t, strerror(errno));
		else if (t > 0)
			ptrace(PTRACE_INTERRUPT, t, 0, 0);
	}
	if (d)
		closedir(d);
}

static int stopfile_ready(void)
{
	int i;

	for (i = 0; i < nstopfiles; i++)
		if (access(stopfiles[i], F_OK))
			return 0;
	return nstopfiles > 0;
}

static long shut_t0;

static void interrupt_all(void)
{
	char p[64];
	DIR *d;
	struct dirent *e;

	snprintf(p, sizeof p, "/proc/%d/task", pid);
	d = opendir(p);
	while (d && (e = readdir(d)))
		if (atoi(e->d_name) > 0)
			ptrace(PTRACE_INTERRUPT, atoi(e->d_name), 0, 0);
	if (d)
		closedir(d);
}

static int all_detached(void)
{
	char p[64];
	DIR *d;
	struct dirent *e;
	int left = 0, i, seen;

	snprintf(p, sizeof p, "/proc/%d/task", pid);
	d = opendir(p);
	while (d && (e = readdir(d))) {
		pid_t t = atoi(e->d_name);

		if (t <= 0)
			continue;
		for (seen = 0, i = 0; i < ndet; i++)
			seen |= detached[i] == t;
		left += !seen;
	}
	if (d)
		closedir(d);
	return !left;
}

int main(int argc, char **argv)
{
	const char *name = NULL, *path = "/tmp/mmio.log";
	pid_t old = 0;
	int o;
	struct sigaction sa = { .sa_handler = on_sig };
	struct itimerval it = { { 0, 200000 }, { 0, 200000 } };
	char *rf, *tok;

	const char *udp = NULL;

	while ((o = getopt(argc, argv, "p:n:x:o:R:k:P:u:w:q:")) != -1) {
		if (o == 'p') phys_base = strtoul(optarg, NULL, 0);
		else if (o == 'P') pid = atoi(optarg);
		else if (o == 'n') name = optarg;
		else if (o == 'x') old = atoi(optarg);
		else if (o == 'o') path = optarg;
		else if (o == 'w') {
			rf = strdup(optarg);
			for (tok = strtok(rf, ","); tok && nwonly < MAXKEEP; tok = strtok(NULL, ","))
				wonly[nwonly++] = strtoul(tok, NULL, 0) & ~(PAGE - 1);
		} else if (o == 'q') {
			quiet_lo = strtoul(optarg, &tok, 0);
			quiet_hi = strtoul(tok + 1, NULL, 0);
		} else if (o == 'u') udp = optarg;	/* ip:port: log lines as UDP datagrams (the switch's /tmp dies with a reboot) */
		else if (o == 'k') {
			nkeep = 0;
			rf = strdup(optarg);
			for (tok = strtok(rf, ","); tok && nkeep < MAXKEEP; tok = strtok(NULL, ","))
				keep[nkeep++] = strtoul(tok, NULL, 0) & ~(PAGE - 1);
		} else if (o == 'R') {
			rf = strdup(optarg);
			for (tok = strtok(rf, ","); tok && nstopfiles < 4; tok = strtok(NULL, ","))
				stopfiles[nstopfiles++] = tok;
		}
	}
	if (!pid && !name) {
		fprintf(stderr, "usage: -P PID | -n NAME [-x OLDPID] [-o file] [-R ready,ready] [-k pageoff,..] [-p physbase]\n");
		return 2;
	}
	if (udp) {
		struct sockaddr_in a = { .sin_family = AF_INET };
		char *c = strchr(udp, ':');
		int fd = socket(AF_INET, SOCK_DGRAM, 0);

		if (!c || fd < 0)
			return 1;
		*c = 0;
		a.sin_port = htons(atoi(c + 1));
		inet_pton(AF_INET, udp, &a.sin_addr);
		if (connect(fd, (struct sockaddr *)&a, sizeof a))
			return 1;
		out = fdopen(fd, "w");
	} else {
		out = fopen(path, "w");
	}
	if (!out)
		return 1;
	setvbuf(out, NULL, _IOLBF, 0);	/* the switch's /tmp is lost on a reboot: every line out at once */
	sigaction(SIGINT, &sa, NULL);
	sigaction(SIGTERM, &sa, NULL);
	sigaction(SIGALRM, &sa, NULL);
	if (!pid)
		pid = find_new(name, old);
	t0_ms = now_ms();
	fprintf(out, "0 0 START pid=%d keep=%d first=%05x\n", pid, nkeep, keep[0]);
	attach_all(pid);
	setitimer(ITIMER_REAL, &it, NULL);
	for (;;) {
		int st;
		pid_t t = waitpid(-1, &st, __WALL);
		int sig, ev;

		if (t < 0) {
			if (errno == EINTR) {
				if ((stop_req || stopfile_ready()) && !shutting) {
					shutting = 1;
					shut_t0 = now_ms();
					interrupt_all();
				}
				if (shutting && (all_detached() || now_ms() - shut_t0 > 3000))
					break;
				continue;
			}
			break;
		}
		if (WIFEXITED(st) || WIFSIGNALED(st)) {
			if (t == pid)
				break;
			continue;
		}
		sig = WSTOPSIG(st);
		ev = st >> 16;
		if (shutting) {		/* make the mapping accessible again before any thread is let go */
			if (phase == 2 && !unprot) {
				remote_syscall(t, SYS_MPROTECT, map_lo, map_hi - map_lo, 3);
				unprot = 1;
			}
			ptrace(PTRACE_DETACH, t, 0, (ev || sig == SIGSEGV || sig == SIGSTOP || sig == SIGTRAP || sig == (SIGTRAP | 0x80)) ? 0 : (void *)(long)sig);
			if (ndet < 256)
				detached[ndet++] = t;
			continue;
		}
		if (ev == PTRACE_EVENT_STOP || ev == PTRACE_EVENT_CLONE) {
			resume(t, 0);
		} else if (sig == (SIGTRAP | 0x80)) {		/* syscall stop (phase 1) */
			if (sc_toggle(t) && phase == 1) {	/* exit stop: user regs are safe to rewrite */
				struct regs r;

				if (getregs(t, &r) == 0 && (r.u[7] == 192 || r.u[7] == 90) && find_map()) {	/* mmap2 / mmap */
					fprintf(out, "%ld %d MAP %08x-%08x phys %08x\n", now_ms() - t0_ms, t, map_lo, map_hi, phys_base);
					protect(t);
					phase = 2;
				}
			}
			resume(t, 0);
		} else if (sig == SIGSEGV && phase == 2) {
			siginfo_t si;

			ptrace(PTRACE_GETSIGINFO, t, 0, &si);
			if ((uint32_t)(uintptr_t)si.si_addr >= map_lo && (uint32_t)(uintptr_t)si.si_addr < map_hi) {
				handle_segv(t, &si);
				resume(t, 0);
			} else {
				fprintf(out, "%ld %d SEGV outside map addr=%p\n", now_ms() - t0_ms, t, si.si_addr);
				resume(t, SIGSEGV);
			}
		} else {
			resume(t, sig == SIGSTOP || sig == SIGTRAP ? 0 : sig);
		}
	}
	fprintf(out, "%ld 0 SHUTDOWN quiet=%lu unprotected=%d detached=%d\n", now_ms() - t0_ms, nquiet, unprot, ndet);
	fprintf(out, "%ld 0 END MR=%lu MW=%lu UNK=%lu\n", now_ms() - t0_ms, nmr, nmw, nunk);
	fclose(out);
	return 0;
}
