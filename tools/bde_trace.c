// SPDX-License-Identifier: GPL-2.0+
/*
 * Register-access tracer for STOCK switchdrvr 7.5.15 on stock firmware (issue #66).
 * Breakpoints on the user-space BDE vtable functions (vtable 0x01e52d1c) + the S-channel send.
 * Build: scripts/build_bde_trace.py. Run on the switch (before switchdrvr starts):
 *   bde_trace -n switchdrvr -x OLDPID -o /tmp/bde.log -R /var/run/fastpath/switchdrvr_ready,/var/run/fastpath/.poe_ready
 * Line: <ms> <tid> <op> u<unit> <addr> <val>
 *   R/W = reg read/write (read value at return), IR/IW = iproc, SPR/SPW = spi (val = rc:bytes / bytes),
 *   SCH = S-channel request (addr = word0, val = remaining words), DCB = CMCx DESC write (val = phys:words).
 * Returns are caught by swapping lr for a trampoline breakpoint (no PTRACE_SINGLESTEP on ARM Linux 3.6).
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
#define ARM_BKPT	0xe7f001f0UL
#define TRAMP		0x01d3e9c0UL	/* slack after text LOAD (ends 0x1d3e9a8, page-mapped): never executed */
#define DCB_WORDS	16
#define SPI_BYTES	32

enum { K_SCH, K_RD, K_WR, K_IRD, K_IWR, K_SPR, K_SPW, NK };
static const struct bp {
	unsigned long addr;
	int kind;
} bps[] = {
	/* addr = the `push {..}` insn of each function (see docs); args: r0 unit, r1 addr, r2 val / buf, r3 len */
	{ 0x0127a59cUL, K_SCH },	/* _soc_schan_op_cmicm(unit, words, n) */
	{ 0x00f5e338UL, K_RD },		/* bde read  @0xf5e330: movw/movt r3 precede the push */
	{ 0x00f5e1a0UL, K_WR },		/* bde write @0xf5e198 */
	{ 0x00f5e740UL, K_IRD },	/* iproc read  @0xf5e738 */
	{ 0x00f5e818UL, K_IWR },	/* iproc write @0xf5e810 */
	{ 0x00f5dc84UL, K_SPR },	/* spi read  (unit, addr, buf, len) */
	{ 0x00f5dc04UL, K_SPW },	/* spi write (unit, addr, buf, len) */
};
#define NBP (sizeof bps / sizeof bps[0])
static const char *opname[NK] = { "SCH", "R", "W", "IR", "IW", "SPR", "SPW" };

struct regs { long u[18]; };
struct frame {
	pid_t tid;
	uint32_t lr, unit, addr, buf, len;
	int kind;
};

static FILE *out;
static long orig_insn[NBP], tramp_orig;
static pid_t pid;
static long t0_ms;
static volatile sig_atomic_t stop_req;
static struct frame frames[256];
static int nframes;
static const char *stopfiles[4];
static int nstopfiles;
static unsigned long counts[NK], ndcb;

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
		/* New switchdrvr starts SDK init >1 s after exec; 2 ms poll attaches long before that. */
		usleep(2000);
		if (stop_req)
			exit(1);
	}
}

static uint32_t peek(pid_t t, uint32_t a)
{
	return (uint32_t)ptrace(PTRACE_PEEKDATA, t, (void *)(uintptr_t)a, 0);
}

static int getregs(pid_t t, struct regs *r)
{
	return ptrace(PTRACE_GETREGS, t, 0, r);
}

/* Emulate the displaced `push {reglist}` (0xe92dXXXX) from insn `ins`; r14 already holds the value to push. */
static void emulate_push(pid_t t, struct regs *r, uint32_t ins)
{
	uint32_t mask = ins & 0xffff, a;
	int i, n = __builtin_popcount(mask);

	r->u[13] -= 4 * n;
	a = r->u[13];
	for (i = 0; i < 16; i++)
		if (mask & (1u << i)) {
			ptrace(PTRACE_POKEDATA, t, (void *)(uintptr_t)a, (void *)r->u[i]);
			a += 4;
		}
	r->u[15] += 4;
	ptrace(PTRACE_SETREGS, t, 0, r);
}

static void push_frame(const struct frame *f)
{
	if (nframes < 256)
		frames[nframes++] = *f;
}

static int pop_frame(pid_t t, struct frame *f)
{
	int i;

	for (i = nframes - 1; i >= 0; i--)
		if (frames[i].tid == t) {
			*f = frames[i];
			memmove(&frames[i], &frames[i + 1], (nframes - i - 1) * sizeof frames[0]);
			nframes--;
			return 1;
		}
	return 0;
}

static void put_bytes(pid_t t, uint32_t a, uint32_t len)
{
	uint32_t i, w = 0;

	if (len > SPI_BYTES)
		len = SPI_BYTES;
	for (i = 0; i < len; i++) {
		if (!(i & 3))
			w = peek(t, a + i);
		fprintf(out, "%02x", (w >> (8 * (i & 3))) & 0xff);
	}
}

/* DESC regs of CMC0..2: <0x31000|0x32000|0x33000> + 0x158..0x164. DCB phys -> process vaddr via /dev/mem maps. */
static int is_desc_reg(uint32_t a)
{
	uint32_t b = a & 0xfffff000, o = a & 0xfff;

	return b >= 0x31000 && b <= 0x33000 && o >= 0x158 && o <= 0x164;
}

static int phys_to_virt(uint32_t phys, uint32_t *va)
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
		if (phys >= off && phys - off < e - s) {
			*va = s + (phys - off);
			ok = 1;
			break;
		}
	}
	if (f)
		fclose(f);
	return ok;
}

static void log_dcb(pid_t t, uint32_t unit, uint32_t reg, uint32_t phys)
{
	uint32_t va;
	int i;

	ndcb++;
	fprintf(out, "%ld %d DCB u%u %08x %08x:", now_ms() - t0_ms, t, unit, reg, phys);
	if (!phys || !phys_to_virt(phys, &va)) {
		fputs("nomap\n", out);
		return;
	}
	for (i = 0; i < DCB_WORDS; i++)
		fprintf(out, " %08x", peek(t, va + 4 * i));
	fputc('\n', out);
}

static void hit_entry(pid_t t, struct regs *r, int bi)
{
	int kind = bps[bi].kind;
	long ms = now_ms() - t0_ms;
	uint32_t ins = (uint32_t)orig_insn[bi];

	counts[kind]++;
	if (kind == K_SCH) {
		uint32_t n = r->u[2] > 22 ? 22 : (uint32_t)r->u[2], i;

		fprintf(out, "%ld %d SCH u%ld", ms, t, r->u[0]);
		for (i = 0; i < n; i++)
			fprintf(out, " %08x", peek(t, r->u[1] + 4 * i));
		fputc('\n', out);
	} else if (kind == K_WR || kind == K_IWR) {
		fprintf(out, "%ld %d %s u%ld %08lx %08lx\n", ms, t, opname[kind], r->u[0], r->u[1], r->u[2]);
		if (kind == K_WR && is_desc_reg(r->u[1]) && r->u[2])
			log_dcb(t, r->u[0], r->u[1], r->u[2]);
	} else if (kind == K_SPW) {
		fprintf(out, "%ld %d SPW u%ld %08lx ", ms, t, r->u[0], r->u[1]);
		put_bytes(t, r->u[2], r->u[3]);
		fputc('\n', out);
	} else {		/* reads: log at return */
		struct frame f = { t, (uint32_t)r->u[14], (uint32_t)r->u[0], (uint32_t)r->u[1],
				   (uint32_t)r->u[2], (uint32_t)r->u[3], kind };

		push_frame(&f);
		r->u[14] = TRAMP;
	}
	emulate_push(t, r, ins);
}

static void hit_tramp(pid_t t, struct regs *r)
{
	struct frame f;
	long ms = now_ms() - t0_ms;

	if (!pop_frame(t, &f)) {
		fprintf(out, "%ld %d ERR trampoline without frame\n", ms, t);
		return;
	}
	if (f.kind == K_SPR) {
		fprintf(out, "%ld %d SPR u%u %08x rc%ld:", ms, t, f.unit, f.addr, r->u[0]);
		put_bytes(t, f.buf, f.len);
		fputc('\n', out);
	} else {
		fprintf(out, "%ld %d %s u%u %08x %08lx\n", ms, t, opname[f.kind], f.unit, f.addr, r->u[0]);
	}
	if (f.lr & 1) {
		r->u[16] |= 0x20;
		r->u[15] = f.lr & ~1u;
	} else {
		r->u[15] = f.lr;
	}
	ptrace(PTRACE_SETREGS, t, 0, r);
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

static void poke_bps(pid_t t, int insert)
{
	size_t i;

	for (i = 0; i < NBP; i++)
		ptrace(PTRACE_POKETEXT, t, (void *)bps[i].addr, (void *)(insert ? ARM_BKPT : (unsigned long)orig_insn[i]));
	ptrace(PTRACE_POKETEXT, t, (void *)TRAMP, (void *)(insert ? ARM_BKPT : (unsigned long)tramp_orig));
}

static int stopfile_ready(void)
{
	int i;

	for (i = 0; i < nstopfiles; i++)
		if (access(stopfiles[i], F_OK))
			return 0;
	return nstopfiles > 0;
}

static void all_tids(pid_t p, pid_t *v, int *n, int max)
{
	char path[64];
	DIR *d;
	struct dirent *e;

	snprintf(path, sizeof path, "/proc/%d/task", p);
	*n = 0;
	d = opendir(path);
	while (d && (e = readdir(d)))
		if (atoi(e->d_name) > 0 && *n < max)
			v[(*n)++] = atoi(e->d_name);
	if (d)
		closedir(d);
}

/* Stop all threads, drop breakpoints once no return is pending (<= 1 s), detach. */
static void shutdown_trace(pid_t any)
{
	pid_t v[128];
	int n, i, st, waited = 0;
	long deadline = now_ms() + 1000;

	all_tids(pid, v, &n, 128);
	for (i = 0; i < n; i++)
		ptrace(PTRACE_INTERRUPT, v[i], 0, 0);
	/* entry bps first: a thread stopped at one just re-executes the original insn after the restore */
	{
		size_t b;

		for (b = 0; b < NBP; b++)
			ptrace(PTRACE_POKETEXT, any, (void *)bps[b].addr, (void *)(unsigned long)orig_insn[b]);
	}
	while (nframes && now_ms() < deadline) {
		pid_t t = waitpid(-1, &st, __WALL | WNOHANG);
		struct regs r;

		if (t <= 0) {
			usleep(1000);
			continue;
		}
		if (WIFEXITED(st) || WIFSIGNALED(st))
			continue;
		if (WSTOPSIG(st) == SIGTRAP && getregs(t, &r) == 0 && (unsigned long)r.u[15] == TRAMP)
			hit_tramp(t, &r);
		ptrace(PTRACE_CONT, t, 0, 0);
		waited++;
	}
	ptrace(PTRACE_POKETEXT, any, (void *)TRAMP, (void *)(unsigned long)tramp_orig);
	all_tids(pid, v, &n, 128);
	for (i = 0; i < n; i++)
		ptrace(PTRACE_DETACH, v[i], 0, 0);
}

int main(int argc, char **argv)
{
	const char *name = NULL, *path = "/tmp/bde.log";
	pid_t old = 0;
	int o, first = 1, done = 0;
	struct sigaction sa = { .sa_handler = on_sig };
	struct itimerval it = { { 0, 200000 }, { 0, 200000 } };
	char *rf, *tok;
	pid_t last = 0;

	while ((o = getopt(argc, argv, "p:n:x:o:R:")) != -1) {
		if (o == 'p') pid = atoi(optarg);
		else if (o == 'n') name = optarg;
		else if (o == 'x') old = atoi(optarg);
		else if (o == 'o') path = optarg;
		else if (o == 'R') {
			rf = strdup(optarg);
			for (tok = strtok(rf, ","); tok && nstopfiles < 4; tok = strtok(NULL, ","))
				stopfiles[nstopfiles++] = tok;
		}
	}
	if (!pid && !name) {
		fprintf(stderr, "usage: -p PID | -n NAME [-x OLDPID]  [-o file] [-R readyfile,readyfile]\n");
		return 2;
	}
	out = fopen(path, "w");
	if (!out)
		return 1;
	setvbuf(out, NULL, _IOFBF, 1 << 20);	/* flushed on exit/signal only: keeps tracing fast */
	sigaction(SIGINT, &sa, NULL);
	sigaction(SIGTERM, &sa, NULL);
	sigaction(SIGALRM, &sa, NULL);
	if (!pid)
		pid = find_new(name, old);
	t0_ms = now_ms();
	attach_all(pid);
	setitimer(ITIMER_REAL, &it, NULL);	/* 200 ms: wake waitpid to poll stop files / signals */
	while (!done) {
		int st;
		pid_t t = waitpid(-1, &st, __WALL);
		int sig, ev;

		if (t < 0) {
			if (errno == EINTR) {
				if (stop_req || stopfile_ready())
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
		last = t;
		if (first) {	/* insert once the first thread has stopped */
			size_t i;

			errno = 0;
			for (i = 0; i < NBP; i++)
				orig_insn[i] = ptrace(PTRACE_PEEKTEXT, t, (void *)bps[i].addr, 0);
			tramp_orig = ptrace(PTRACE_PEEKTEXT, t, (void *)TRAMP, 0);
			poke_bps(t, 1);
			first = 0;
		}
		sig = WSTOPSIG(st);
		ev = st >> 16;
		if (ev == PTRACE_EVENT_STOP || ev == PTRACE_EVENT_CLONE) {
			ptrace(PTRACE_CONT, t, 0, 0);
		} else if (sig == SIGTRAP) {
			struct regs r;
			size_t i;

			if (getregs(t, &r) == 0) {
				if ((unsigned long)r.u[15] == TRAMP) {
					hit_tramp(t, &r);
				} else {
					for (i = 0; i < NBP; i++)
						if ((unsigned long)r.u[15] == bps[i].addr) {
							hit_entry(t, &r, i);
							break;
						}
				}
			}
			ptrace(PTRACE_CONT, t, 0, 0);
		} else {
			ptrace(PTRACE_CONT, t, 0, sig == SIGSTOP ? 0 : sig);
		}
	}
	if (!first && last && !kill(pid, 0))
		shutdown_trace(last);
	fprintf(out, "%ld 0 END counts R=%lu W=%lu IR=%lu IW=%lu SPR=%lu SPW=%lu SCH=%lu DCB=%lu\n", now_ms() - t0_ms,
		counts[K_RD], counts[K_WR], counts[K_IRD], counts[K_IWR], counts[K_SPR], counts[K_SPW], counts[K_SCH], ndcb);
	fclose(out);
	return 0;
}
