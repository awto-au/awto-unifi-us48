# Out of tree: make O=<kernel build dir> M=$PWD modules (scripts/build_mainline.py --module-only)
obj-m := us48_a2.o
us48_a2-y := us48_a2_core.o us48_a2_dcc.o
ccflags-y := -I$(src)/../us48-pci -I$(src)/../../hwdb/gen -I$(srctree)/drivers/remoteproc -I$(src)/../../a2fw
