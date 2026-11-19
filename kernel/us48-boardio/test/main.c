// SPDX-License-Identifier: GPL-2.0
/*
 * Scenario for scripts/mspi_frame_check.py: the same fan sequence against the old us48_fan.c (own pins + spi_write)
 * and the new one (us48_boardio client); -DNEW adds the PoE LED steps. stdout = transfers + return codes.
 */
#include "stub.h"

extern int temp_ret, temp_mc, spi_fail;
extern struct gpio_desc gp[2];
void run_devm(void);
int h_fan_probe(void);
void h_watch(void);
int h_set_state(unsigned long s);
int h_hwmon_write(int attr, long v);
void h_params(bool q, unsigned int l);
int h_poe_pattern(const char *s);
void h_poe_show(char *buf);

#define STEP(name, call) do { printf("# %s\n", name); printf("ret %s = %d\n", name, (int)(call)); } while (0)

static void fan_steps(void)
{
	static const unsigned long states[] = { 3, 1, 2, 0, 3, 0 };
	static const long pwm[] = { 127, 179, 230, 255, 0, 200 };
	char name[64];
	int i;

	h_params(true, 0);	/* quiet rule defaults */
	STEP("probe", h_fan_probe());
	STEP("watch ok", (h_watch(), 0));
	for (i = 0; i < 6; i++) {
		snprintf(name, sizeof(name), "state %lu", states[i]);
		STEP(name, h_set_state(states[i]));
	}
	STEP("state 4 invalid", h_set_state(4));
	STEP("manual on", h_hwmon_write(hwmon_pwm_enable, 1));
	for (i = 0; i < 6; i++) {
		snprintf(name, sizeof(name), "pwm %ld", pwm[i]);
		STEP(name, h_hwmon_write(hwmon_pwm_input, pwm[i]));
	}
	STEP("manual off", h_hwmon_write(hwmon_pwm_enable, 2));
	STEP("state 2", h_set_state(2));
	temp_ret = -EIO;
	STEP("sensor fail quiet", (h_watch(), 0));
	h_params(false, 0);
	STEP("sensor fail loud", (h_watch(), 0));
	temp_ret = 0;
	STEP("sensor ok loud", (h_watch(), 0));
	temp_mc = 200000;
	STEP("sensor bogus loud", (h_watch(), 0));
	temp_mc = 40000;
	STEP("sensor ok again", (h_watch(), 0));
	STEP("state 0 loud", h_set_state(0));
	h_params(false, 2);
	STEP("floor 2", (h_watch(), 0));
	spi_fail = 1;
	STEP("state 3 one write error", h_set_state(3));
	STEP("watch after error", (h_watch(), 0));
	spi_fail = 3;
	STEP("state 1 three write errors", h_set_state(1));
	STEP("watch after errors", (h_watch(), 0));
	h_params(true, 0);
	STEP("quiet again", h_set_state(0));
	STEP("watch quiet", (h_watch(), 0));
}

#ifdef NEW
static int led(const char *n, int b)
{
	return led_set(n, b);
}

static void poe_steps(void)
{
	char pat[64], buf[128];

	STEP("poe led ge1 1", led("ge1::poe", 1));
	STEP("poe led ge24 1", led("ge24::poe", 1));
	STEP("poe led ge1 0", led("ge1::poe", 0));
	STEP("poe led ge24 0", led("ge24::poe", 0));
	STEP("poe led ge25 1", led("ge25::poe", 1));
	STEP("poe led ge48 1", led("ge48::poe", 1));
	STEP("poe led ge48 1 unchanged", led("ge48::poe", 1));
	STEP("poe led ge48 brightness 255 clamps to 1", led("ge48::poe", 255));
	memset(pat, '2', 48);
	pat[48] = 0;
	STEP("poe pattern all 2", h_poe_pattern(pat));
	pat[10] = '3';
	STEP("poe pattern with 3", h_poe_pattern(pat));
	pat[10] = '2';
	pat[47] = 0;
	STEP("poe pattern 47 digits", h_poe_pattern(pat));
	memset(pat, '0', 48);
	pat[0] = '2';
	pat[48] = 0;
	STEP("poe pattern front 1 = 2", h_poe_pattern(pat));
	STEP("poe pattern off", h_poe_pattern("off\n"));
	h_poe_show(buf);
	printf("poe show %s", buf);
	STEP("poe fan frame between", h_set_state(3));
	STEP("poe led ge2 1 after fan", led("ge2::poe", 1));
	STEP("poe fan frame before stuck A", h_set_state(0));
	gp[0].stuck = 1;
	STEP("poe stuck A led ge3 1", led("ge3::poe", 1));
	gp[0].stuck = 0;
	STEP("poe unstuck A led ge3 1", led("ge3::poe", 1));
	gp[1].stuck = 1;
	STEP("poe stuck B led ge26 1", led("ge26::poe", 1));
	gp[1].stuck = 0;
	STEP("poe unstuck B led ge26 1", led("ge26::poe", 1));
}
#endif

int main(void)
{
	fan_steps();
#ifdef NEW
	poe_steps();
#endif
	printf("# unbind\n");
	run_devm();
	return 0;
}
