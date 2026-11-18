// SPDX-License-Identifier: GPL-2.0+
/*
 * l2_burst: send COUNT frames back to back on one netdev, source MAC BASEMAC + i (low 16 bits), for the switchdev
 * L2 change-FIFO tests (#134, scripts/switchdev_test.py burst/fdb).
 * Usage: l2_burst IF COUNT BASEMAC DSTMAC    (COUNT 1-1024)
 * - Frames: 64 B, EtherType 0x88b5 (IEEE local experimental), payload = sequence number.
 * Output: "tx IF sent first T last T", T = local time (CLOCK_REALTIME, the clock `bridge -timestamp` stamps with).
 */
#include <arpa/inet.h>
#include <linux/if_packet.h>
#include <net/ethernet.h>
#include <net/if.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>

#define ETYPE 0x88b5
#define MAXN 1024

static int mac(const char *s, uint8_t *m)
{
	return sscanf(s, "%hhx:%hhx:%hhx:%hhx:%hhx:%hhx", &m[0], &m[1], &m[2], &m[3], &m[4], &m[5]) == 6 ? 0 : -1;
}

static void stamp(struct timespec *t)
{
	clock_gettime(CLOCK_REALTIME, t);
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
	unsigned int lo;

	if (argc != 5 || mac(argv[3], src) || mac(argv[4], dst))
		return 2;
	n = atoi(argv[2]);
	if (n < 1 || n > MAXN)
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
		if (!i)
			stamp(&t0);
		if (send(s, f, sizeof(f), 0) != sizeof(f))
			break;		/* TX error: report what went out */
		sent++;
		stamp(&t1);
	}
	printf("tx %s %d first ", argv[1], sent);
	pr_time(&t0);
	printf(" last ");
	pr_time(&t1);
	printf("\n");
	return sent == n ? 0 : 1;
}
