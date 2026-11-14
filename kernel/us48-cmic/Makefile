# us48_cmic.ko = driver core + the shared bcm-cmic core (common/bcm-cmic), copied next to the driver at build.
# us48_cmic_xe.ko only uses the exported us48_* symbols.
BCM := $(src)/../../common/bcm-cmic
BCM_FILES := bcm_cmic_schan.c bcm_cmic_miim.c bcm_cmic_hr2.c bcm_cmic.h

obj-m := us48_cmic.o us48_cmic_xe.o
us48_cmic-y := us48_cmic_core.o bcm_cmic_schan.o bcm_cmic_miim.o bcm_cmic_hr2.o
ccflags-y += -I$(src)

$(addprefix $(obj)/,$(BCM_FILES)): $(obj)/%: $(BCM)/%
	$(Q)cp $< $@
$(addprefix $(obj)/,us48_cmic_core.o bcm_cmic_schan.o bcm_cmic_miim.o bcm_cmic_hr2.o): $(obj)/bcm_cmic.h
