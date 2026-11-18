// SPDX-License-Identifier: GPL-2.0+
/*
 * fwd_pkt: send N test frames on one netdev and count them on others (ASIC forwarding test, #57, scripts/asic_fwd_test.py).
 * Usage: fwd_pkt TXIF COUNT RATE SRCMAC DSTMAC TAIL_MS RXIF...
 * - Frames: 64 B, EtherType 0x88b5 (IEEE local experimental), payload = sequence number. RATE <= 100 frames/s.
 * - RX: AF_PACKET per RXIF, opened before the first TX; counts frames with EtherType 0x88b5 (plain or after one 802.1Q tag)
 *   and source SRCMAC, outgoing copies skipped. Counts until TAIL_MS after the last TX.
 * Output: "tx IF sent" and "rx IF count dup_or_reorder".
 */
#include <arpa/inet.h>
#include <linux/if_packet.h>
#include <net/ethernet.h>
#include <net/if.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define ETYPE 0x88b5
#define MAXRX 4

static double now(void)
{
	struct timespec t;

	clock_gettime(CLOCK_MONOTONIC, &t);
	return t.tv_sec + t.tv_nsec / 1e9;
}

static int mac(const char *s, uint8_t *m)
{
	return sscanf(s, "%hhx:%hhx:%hhx:%hhx:%hhx:%hhx", &m[0], &m[1], &m[2], &m[3], &m[4], &m[5]) == 6 ? 0 : -1;
}

static int sock_on(const char *ifn)
{
	struct sockaddr_ll a = { .sll_family = AF_PACKET, .sll_protocol = htons(ETH_P_ALL) };
	int s = socket(AF_PACKET, SOCK_RAW, htons(ETH_P_ALL));

	a.sll_ifindex = if_nametoindex(ifn);
	if (s < 0 || !a.sll_ifindex || bind(s, (struct sockaddr *)&a, sizeof(a))) {
		fprintf(stderr, "fwd_pkt: cannot open %s\n", ifn);
		exit(1);
	}
	return s;
}

int main(int argc, char **argv)
{
	uint8_t src[6], dst[6], f[64] = { 0 }, b[2048];
	int i, n, rate, tail_ms, nrx = argc - 7, rx[MAXRX], cnt[MAXRX] = { 0 }, bad[MAXRX] = { 0 }, last[MAXRX];
	struct pollfd pf[MAXRX];
	double t0, next, end = 0;
	int tx, sent = 0;

	if (argc < 8 || nrx > MAXRX || mac(argv[4], src) || mac(argv[5], dst))
		return 2;
	n = atoi(argv[2]);
	rate = atoi(argv[3]);
	tail_ms = atoi(argv[6]);
	if (rate < 1 || rate > 100 || n < 1 || n > 1000)
		return 2;
	for (i = 0; i < nrx; i++) {
		rx[i] = sock_on(argv[7 + i]);
		pf[i] = (struct pollfd){ .fd = rx[i], .events = POLLIN };
		last[i] = -1;
	}
	tx = sock_on(argv[1]);
	memcpy(f, dst, 6);
	memcpy(f + 6, src, 6);
	f[12] = ETYPE >> 8;
	f[13] = ETYPE & 0xff;
	t0 = next = now();
	for (;;) {
		double t = now();
		int wait_ms;

		if (sent < n && t >= next) {
			f[14] = sent >> 8;
			f[15] = sent & 0xff;
			if (send(tx, f, sizeof(f), 0) == sizeof(f))
				sent++;
			else
				n = sent;	/* TX error: stop sending, report what went out */
			next += 1.0 / rate;
			if (sent == n)
				end = now() + tail_ms / 1000.0;
		}
		if (sent == n && now() >= end)
			break;
		wait_ms = (int)(((sent < n ? next : end) - now()) * 1000) + 1;
		if (poll(pf, nrx, wait_ms < 0 ? 0 : wait_ms) <= 0)
			continue;
		for (i = 0; i < nrx; i++) {
			struct sockaddr_ll a;
			socklen_t al = sizeof(a);
			ssize_t l;
			int off = 12, seq;

			if (!(pf[i].revents & POLLIN))
				continue;
			l = recvfrom(rx[i], b, sizeof(b), MSG_DONTWAIT, (struct sockaddr *)&a, &al);
			if (l < 18 || a.sll_pkttype == PACKET_OUTGOING || memcmp(b + 6, src, 6))
				continue;
			if (b[12] == 0x81 && b[13] == 0x00)
				off = 16;
			if (b[off] != ETYPE >> 8 || b[off + 1] != (ETYPE & 0xff))
				continue;
			seq = b[off + 2] << 8 | b[off + 3];
			if (seq <= last[i])
				bad[i]++;
			last[i] = seq;
			cnt[i]++;
		}
	}
	printf("tx %s %d in %.2f s\n", argv[1], sent, now() - t0);
	for (i = 0; i < nrx; i++)
		printf("rx %s %d %d\n", argv[7 + i], cnt[i], bad[i]);
	return 0;
}
