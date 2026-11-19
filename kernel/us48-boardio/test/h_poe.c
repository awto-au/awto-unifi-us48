// SPDX-License-Identifier: GPL-2.0
/* Harness access to the static sysfs handlers of us48_poe_led.c */
#include POE_C

int h_poe_pattern(const char *s)
{
	return poe_pattern_store(NULL, NULL, s, strlen(s));
}

void h_poe_show(char *buf)
{
	poe_pattern_show(NULL, NULL, buf);
}
