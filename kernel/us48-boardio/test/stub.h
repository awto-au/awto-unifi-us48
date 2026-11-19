/* Host stubs of the kernel API used by us48_fan.c / us48_boardio_core.c / us48_poe_led.c (scripts/mspi_frame_check.py). */
#ifndef STUB_H
#define STUB_H
#include <ctype.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef uint8_t u8;
typedef uint32_t u32;
typedef uint64_t u64;
typedef unsigned int uint;
typedef unsigned short umode_t;
typedef long ssize_t;

#define __init
#define __exit
#define THIS_MODULE NULL
#define module_param(n, t, p)
#define MODULE_PARM_DESC(n, d)
#define MODULE_DESCRIPTION(d)
#define MODULE_LICENSE(l)
#define MODULE_DEVICE_TABLE(t, n)
#define EXPORT_SYMBOL_GPL(s)
#define module_spi_driver(d)
#define READ_ONCE(x) (x)
#define container_of(p, t, m) ((t *)((char *)(p) - offsetof(t, m)))
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define min_t(t, a, b) ((t)(a) < (t)(b) ? (t)(a) : (t)(b))
#define max(a, b) ((a) > (b) ? (a) : (b))
#define DIV_ROUND_CLOSEST(x, d) (((x) + (d) / 2) / (d))
#define IS_ERR(p) ((uintptr_t)(p) >= (uintptr_t)-4095)
#define PTR_ERR(p) ((long)(intptr_t)(p))
#define ERR_PTR(e) ((void *)(intptr_t)(e))
#define lockdep_assert_held(l) do { if (!(l)->held) { fprintf(stderr, "lock not held\n"); abort(); } } while (0)

struct mutex { int held; };
#define DEFINE_MUTEX(n) struct mutex n = { 0 }
static inline void mutex_init(struct mutex *m) { m->held = 0; }
static inline void mutex_lock(struct mutex *m) { m->held = 1; }
static inline void mutex_unlock(struct mutex *m) { m->held = 0; }

struct device_node { int dummy; };
struct device { struct device_node *of_node; void *drvdata; };
struct attribute { const char *name; };
struct device_attribute { struct attribute attr; };
struct attribute_group { struct attribute **attrs; };
#define DEVICE_ATTR_RO(n) struct device_attribute dev_attr_##n = { { #n } }
#define DEVICE_ATTR_RW(n) struct device_attribute dev_attr_##n = { { #n } }
#define ATTRIBUTE_GROUPS(n) static const struct attribute_group *n##_groups[] = { NULL }
static inline void *dev_get_drvdata(struct device *d) { return d->drvdata; }

void logf_(const char *fmt, ...);
#define dev_err_ratelimited(d, ...) logf_("dev_err: " __VA_ARGS__)
#define pr_err_ratelimited(...) logf_("pr_err: " __VA_ARGS__)
#define dev_warn(d, ...) logf_("dev_warn: " __VA_ARGS__)
#define dev_info(d, ...) logf_("dev_info: " __VA_ARGS__)
#define dev_err_probe(d, e, ...) (logf_("dev_err_probe: " __VA_ARGS__), (int)(e))
int sysfs_emit(char *buf, const char *fmt, ...);
int sysfs_emit_at(char *buf, int at, const char *fmt, ...);
bool sysfs_streq(const char *a, const char *b);

extern unsigned long jiffies;
#define time_after(a, b) ((long)((b) - (a)) < 0)
static inline unsigned long msecs_to_jiffies(unsigned int m) { return m; }

struct work_struct { int dummy; };
struct delayed_work { struct work_struct work; void (*fn)(struct work_struct *); };
#define INIT_DELAYED_WORK(w, f) ((w)->fn = (f))
static inline bool schedule_delayed_work(struct delayed_work *w, unsigned long d) { return true; }
static inline bool cancel_delayed_work_sync(struct delayed_work *w) { return false; }

void *devm_kzalloc(struct device *d, size_t n, int gfp);
void *devm_kcalloc(struct device *d, size_t n, size_t s, int gfp);
#define GFP_KERNEL 0
#define kcalloc(n, s, g) calloc(n, s)
#define kfree free
int devm_add_action_or_reset(struct device *d, void (*fn)(void *), void *data);

struct of_device_id { const char *compatible; };
int of_property_count_strings(struct device_node *np, const char *name);
int of_property_read_string_array(struct device_node *np, const char *name, const char **out, size_t n);

/* gpio: two descriptors (A, B); recording in stub.c */
struct gpio_desc { int val; int stuck; };
enum gpiod_flags { GPIOD_OUT_LOW, GPIOD_OUT_HIGH };
struct gpio_desc *devm_gpiod_get_index(struct device *d, const char *con, unsigned int idx, enum gpiod_flags f);
int gpiod_set_value_cansleep(struct gpio_desc *g, int v);
int gpiod_get_value_cansleep(const struct gpio_desc *g);

struct spi_device { struct device dev; u32 mode; u8 bits_per_word; };
struct spi_device_id { const char *name; };
struct spi_driver { struct { const char *name; const struct of_device_id *of_match_table;
	const struct attribute_group **dev_groups; } driver; const struct spi_device_id *id_table;
	int (*probe)(struct spi_device *); };
#define SPI_MODE_3 3
#define to_spi_device(d) container_of(d, struct spi_device, dev)
static inline void *spi_get_drvdata(struct spi_device *s) { return s->dev.drvdata; }
static inline void spi_set_drvdata(struct spi_device *s, void *p) { s->dev.drvdata = p; }
int spi_setup(struct spi_device *s);
int spi_write(struct spi_device *s, const void *buf, size_t len);

struct thermal_zone_device { int dummy; };
struct thermal_cooling_device { void *devdata; };
struct thermal_cooling_device_ops {
	int (*get_max_state)(struct thermal_cooling_device *, unsigned long *);
	int (*get_cur_state)(struct thermal_cooling_device *, unsigned long *);
	int (*set_cur_state)(struct thermal_cooling_device *, unsigned long);
};
struct thermal_zone_device *thermal_zone_get_zone_by_name(const char *n);
int thermal_zone_get_temp(struct thermal_zone_device *tz, int *t);
struct thermal_cooling_device *devm_thermal_of_cooling_device_register(struct device *d, struct device_node *np,
	const char *type, void *devdata, const struct thermal_cooling_device_ops *ops);

enum hwmon_sensor_types { hwmon_temp, hwmon_pwm };
enum { hwmon_pwm_input, hwmon_pwm_enable };
#define HWMON_PWM_INPUT 1
#define HWMON_PWM_ENABLE 2
struct hwmon_channel_info { int type; };
#define HWMON_CHANNEL_INFO(t, ...) (&(const struct hwmon_channel_info){ hwmon_##t })
struct hwmon_ops {
	umode_t (*is_visible)(const void *, enum hwmon_sensor_types, u32, int);
	int (*read)(struct device *, enum hwmon_sensor_types, u32, int, long *);
	int (*write)(struct device *, enum hwmon_sensor_types, u32, int, long);
};
struct hwmon_chip_info { const struct hwmon_ops *ops; const struct hwmon_channel_info * const *info; };
struct device *devm_hwmon_device_register_with_info(struct device *d, const char *name, void *drvdata,
	const struct hwmon_chip_info *chip, const void *groups);

/* LED class: register/unregister mimic the core (unregister sets brightness 0) */
enum led_brightness { LED_OFF = 0, LED_ON = 1 };
struct led_classdev {
	const char *name; unsigned int max_brightness; const char *default_trigger;
	int (*brightness_set_blocking)(struct led_classdev *, enum led_brightness);
	enum led_brightness (*brightness_get)(struct led_classdev *);
};
int led_classdev_register(struct device *parent, struct led_classdev *c);
void led_classdev_unregister(struct led_classdev *c);
int led_set(const char *name, int b);	/* harness: write brightness of a registered LED by name */

struct platform_device { struct device dev; };
#define PLATFORM_DEVID_NONE (-1)
struct platform_device *platform_device_register_data(struct device *parent, const char *name, int id,
	const void *data, size_t size);
void platform_device_unregister(struct platform_device *p);
static inline int device_add_groups(struct device *d, const struct attribute_group **g) { return 0; }
static inline void device_remove_groups(struct device *d, const struct attribute_group **g) { }
#endif
