# Out of tree: make O=<kernel build dir> M=$PWD modules (scripts/build_mainline.py --module-only)
obj-m := us48_pvtmon.o
ccflags-y := -I$(src)/../us48-pci -I$(src)/../../common/bcm-cmic -I$(src)/../../hwdb/gen -I$(srctree)/drivers/thermal
