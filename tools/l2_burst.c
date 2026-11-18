// SPDX-License-Identifier: GPL-2.0+
/*
 * l2_burst: send COUNT frames on one netdev, source MAC BASEMAC + i (low 16 bits), for the switchdev
 * L2 change-FIFO tests (#134, #135, scripts/switchdev_test.py burst/rate/fdb).
 * Usage: l2_burst IF COUNT BASEMAC DSTMAC [RATE]    (COUNT 1-16384; RATE frames/s, 0 or absent = back to back)
 * - Frames: 64 B, EtherType 0x88b5 (IEEE local experimental), payload = sequence number.
 * - RATE: frame i leaves at first + i / RATE (busy-wait on CLOCK_MONOTONIC; the sender's CPU time is printed).
 * Output: "tx IF sent first T last T cpu_ms C", T = local time (CLOCK_REALTIME, the clock `bridge -timestamp` stamps with).
 */
#include <arpa/inet.h>
#include <linux/if_packet.h>
#include <net/ethernet.h>
#include <net/if.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <time.h>

#define ETYPE 0x88b5
#define MAXN 16384

static int mac(const char *s, uint8_t *m)
{
	return sscanf(s, "%hhx:%hhx:%hhx:%hhx:%hhx:%hhx", &m[0], &m[1], &m[2], &m[3], &m[4], &m[5]) == 6 ? 0 : -1;
}

static void stamp(struct timespec *t)
{
	clock_gettime(CLOCK_REALTIME, t);
}

static int64_t mono_ns(void)
{
	struct timespec t;

	clock_gettime(CLOCK_MONOTONIC, &t);
	return (int64_t)t.tv_sec * 1000000000 + t.tv_nsec;
}

/* Local time, %Y-%m-%dT%H:%M:%S.usec */
static void pr_time(const struct timespec *t)
{
	char b[40];

	strftime(b, sizeof(b), "%Y-%m-%dT%H:%M:%S", localtime(&t->tv_sec));
	printf("%s.%06ld", b, t->tv_nsec / 1000);
}

int main(int argc, char **argv)
{
	struct sockaddr_ll a = { .sll_family = AF_PACKET, .sll_protocol = htons(ETH_P_ALL) };
	uint8_t src[6], dst[6], f[64] = { 0 };
	struct timespec t0 = { 0 }, t1 = { 0 };
	int s, i, n, sent = 0;
	int64_t m0 = 0, rate = 0;
	unsigned int lo;
	struct rusage ru;

	if ((argc != 5 && argc != 6) || mac(argv[3], src) || mac(argv[4], dst))
		return 2;
	n = atoi(argv[2]);
	if (argc == 6)
		rate = atoll(argv[5]);
	if (n < 1 || n > MAXN || rate < 0)
		return 2;
	s = socket(AF_PACKET, SOCK_RAW, htons(ETH_P_ALL));
	a.sll_ifindex = if_nametoindex(argv[1]);
	if (s < 0 || !a.sll_ifindex || bind(s, (struct sockaddr *)&a, sizeof(a))) {
		fprintf(stderr, "l2_burst: cannot open %s\n", argv[1]);
		return 1;
	}
	memcpy(f, dst, 6);
	f[12] = ETYPE >> 8;
	f[13] = ETYPE & 0xff;
	lo = src[4] << 8 | src[5];
	for (i = 0; i < n; i++) {
		memcpy(f + 6, src, 4);
		f[10] = (lo + i) >> 8 & 0xff;
		f[11] = (lo + i) & 0xff;
		f[14] = i >> 8;
		f[15] = i & 0xff;
		if (!i) {
			stamp(&t0);
			m0 = mono_ns();
		} else if (rate) {
			while (mono_ns() - m0 < (int64_t)i * 1000000000 / rate)
				;
		}
		if (send(s, f, sizeof(f), 0) != sizeof(f))
			break;		/* TX error: report what went out */
		sent++;
		stamp(&t1);
	}
	getrusage(RUSAGE_SELF, &ru);
	printf("tx %s %d first ", argv[1], sent);
	pr_time(&t0);
	printf(" last ");
	pr_time(&t1);
	printf(" cpu_ms %lld\n", (long long)(ru.ru_utime.tv_sec + ru.ru_stime.tv_sec) * 1000 +
	       (ru.ru_utime.tv_usec + ru.ru_stime.tv_usec) / 1000);
	return sent == n ? 0 : 1;
}
