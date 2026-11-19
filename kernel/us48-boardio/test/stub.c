// SPDX-License-Identifier: GPL-2.0
/* Host stubs: records every MSPI transfer with the select pin levels at that moment (scripts/mspi_frame_check.py). */
#include <stdarg.h>
#include "stub.h"

unsigned long jiffies = 1;
int temp_ret;			/* thermal_zone_get_temp result: 0 or -errno */
int temp_mc = 40000;
int spi_fail;			/* fail the next N spi_write calls with -EIO */
struct gpio_desc gp[2];		/* A = unit 0 GP0, B = unit 1 GP0 */

void logf_(const char *fmt, ...)
{
	va_list ap;

	va_start(ap, fmt);
	vfprintf(stderr, fmt, ap);
	va_end(ap);
}

int sysfs_emit(char *buf, const char *fmt, ...)
{
	va_list ap;
	int n;

	va_start(ap, fmt);
	n = vsnprintf(buf, 4096, fmt, ap);
	va_end(ap);
	return n;
}

int sysfs_emit_at(char *buf, int at, const char *fmt, ...)
{
	va_list ap;
	int n;

	va_start(ap, fmt);
	n = vsnprintf(buf + at, 4096 - at, fmt, ap);
	va_end(ap);
	return n;
}

bool sysfs_streq(const char *a, const char *b)
{
	size_t n = strlen(b);

	return !strncmp(a, b, n) && (a[n] == 0 || (a[n] == '\n' && a[n + 1] == 0));
}

void *devm_kzalloc(struct device *d, size_t n, int gfp)
{
	return calloc(1, n);
}

void *devm_kcalloc(struct device *d, size_t n, size_t s, int gfp)
{
	return calloc(n, s);
}

static struct { void (*fn)(void *); void *data; } acts[32];
static int nact;

int devm_add_action_or_reset(struct device *d, void (*fn)(void *), void *data)
{
	acts[nact].fn = fn;
	acts[nact++].data = data;
	return 0;
}

void run_devm(void)	/* unbind: reverse order */
{
	while (nact--)
		acts[nact].fn(acts[nact].data);
}

int of_property_count_strings(struct device_node *np, const char *name)
{
	return 2;
}

int of_property_read_string_array(struct device_node *np, const char *name, const char **out, size_t n)
{
	out[0] = "lm75-48";
	out[1] = "die0";
	return 2;
}

struct gpio_desc *devm_gpiod_get_index(struct device *d, const char *con, unsigned int idx, enum gpiod_flags f)
{
	gp[idx].val = f == GPIOD_OUT_HIGH;
	printf("gpio %c out %d\n", idx ? 'B' : 'A', gp[idx].val);
	return &gp[idx];
}

int gpiod_set_value_cansleep(struct gpio_desc *g, int v)
{
	if (!g->stuck)
		g->val = !!v;
	return 0;
}

int gpiod_get_value_cansleep(const struct gpio_desc *g)
{
	return g->val;
}

int spi_setup(struct spi_device *s)
{
	printf("spi_setup mode %u bits %u\n", s->mode, s->bits_per_word);
	return 0;
}

int spi_write(struct spi_device *s, const void *buf, size_t len)
{
	const u8 *b = buf;
	size_t i;

	if (spi_fail) {
		spi_fail--;
		printf("xfer A=%d B=%d len=%zu FAIL\n", gp[0].val, gp[1].val, len);
		return -EIO;
	}
	printf("xfer A=%d B=%d len=%zu:", gp[0].val, gp[1].val, len);
	for (i = 0; i < len; i++)
		printf(" %02x", b[i]);
	printf("\n");
	return 0;
}

static struct thermal_zone_device tzd;

struct thermal_zone_device *thermal_zone_get_zone_by_name(const char *n)
{
	return &tzd;
}

int thermal_zone_get_temp(struct thermal_zone_device *tz, int *t)
{
	*t = temp_mc;
	return temp_ret;
}

static struct thermal_cooling_device cdev;

struct thermal_cooling_device *devm_thermal_of_cooling_device_register(struct device *d, struct device_node *np,
	const char *type, void *devdata, const struct thermal_cooling_device_ops *ops)
{
	cdev.devdata = devdata;
	return &cdev;
}

static struct device hwdev;

struct device *devm_hwmon_device_register_with_info(struct device *d, const char *name, void *drvdata,
	const struct hwmon_chip_info *chip, const void *groups)
{
	hwdev.drvdata = drvdata;
	return &hwdev;
}

static struct led_classdev *leds[64];
static int nled;

int led_classdev_register(struct device *parent, struct led_classdev *c)
{
	leds[nled++] = c;
	return 0;
}

void led_classdev_unregister(struct led_classdev *c)
{
	c->brightness_set_blocking(c, LED_OFF);
}

int led_set(const char *name, int b)
{
	int i;

	for (i = 0; i < nled; i++)
		if (!strcmp(leds[i]->name, name))
			return leds[i]->brightness_set_blocking(leds[i],
				b > (int)leds[i]->max_brightness ? leds[i]->max_brightness : b);
	return -ENOENT;
}

static struct platform_device pdev;

struct platform_device *platform_device_register_data(struct device *parent, const char *name, int id,
	const void *data, size_t size)
{
	printf("platform device %s\n", name);
	return &pdev;
}

void platform_device_unregister(struct platform_device *p)
{
}
