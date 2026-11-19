// SPDX-License-Identifier: GPL-2.0
/* Harness access to the static functions of us48_fan.c (FAN_C = old or new source, scripts/mspi_frame_check.py) */
#include FAN_C

static struct spi_device spi;

int h_fan_probe(void)
{
	return fan_probe(&spi);
}

void h_watch(void)
{
	struct fan *f = spi_get_drvdata(&spi);

	fan_watch(&f->watch.work);
}

int h_set_state(unsigned long s)
{
	struct thermal_cooling_device c = { spi_get_drvdata(&spi) };

	return fan_set_cur_state(&c, s);
}

int h_hwmon_write(int attr, long v)
{
	return fan_write(&spi.dev, hwmon_pwm, attr, 0, v);
}

void h_params(bool q, unsigned int l)
{
	quiet = q;
	level = l;
}
