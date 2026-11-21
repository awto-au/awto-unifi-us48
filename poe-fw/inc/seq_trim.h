/* Offset trim, temperature, CM / VM scaling, detect classification (stock 0x1B03 / 0x37D2 / 0x34D0 / 0x3651 / 0x415C).
 * Spec: docs/poe-fw-seq/04-trim-measure.md. Compiled only with -DPOE_FW_SEQ; not wired into main. */
#ifndef SEQ_TRIM_H
#define SEQ_TRIM_H
#include "common.h"

/* hal_analog.c accessors added for this cluster */
void hal_ind_write(u8 idx, u16 w);
u8 hal_ae_status(void);
u16 hal_det_sample(u8 idx);
void hal_trim_sel(u8 sel);
u8 hal_trim_b6(void);
u8 hal_trim_b7(void);
u8 hal_trim_d7(void);
void hal_trim_f4_clear(void);
void hal_trim_ff_60(void);
void hal_trim_end(void);

/* State owned by other clusters, mirrored here at the stock address named in seq_trim.c. */
extern __xdata u8 seq_code[NCH];        /* 0x8077 + 0x12c: channel sequencer code, 0x0B = monitoring */
extern __xdata u8 seq_rec[NCH][7];      /* 0x8104 + 7c: detect / class record */
extern __xdata u8 seq_cfgb[NCH];        /* byte 0x80DE + 2c, bit 5 = class-4 power enabled (selects CM scale) */
extern __xdata u8 seq_stat[NCH];        /* IRAM 0x42 + p: detect nibble (low) */
extern __xdata u8 seq_remap_on;         /* IRAM 0x24 */
extern __xdata u8 seq_remap[NCH];       /* 0x8125 + c */

void seq_trim_calib(void);              /* 0x415C */
void seq_offset_trim(void);             /* 0x1B03 */
void seq_temp(void);                    /* 0x37D2 */
void seq_convert(void);                 /* 0x34D0 */
void seq_meas_step(void);               /* periodic ISR order 1B03, 37D2, 34D0 */
void seq_publish(u8 c);                 /* 0x2EBD..0x2ED3 + 0x442B */
void seq_publish_zero(u8 c);            /* 0x4538..0x453F */
u8 seq_detect_classify(u8 c, u8 nib);   /* 0x3651 */
extern __xdata u16 seq_vm[NCH], seq_cm[NCH];

#endif
