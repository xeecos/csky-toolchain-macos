# csky-elfabiv2 交叉编译冒烟测试
# 用法: make && make dump
CROSS   ?= csky-elfabiv2-
CC      := $(CROSS)gcc
OBJDUMP := $(CROSS)objdump
# TXW828 = CK804DF+CK804D。主线 GCC 无 ck804，ck803f 是其安全基线
# (ck803 指令集 ⊂ ck804)；-mfloat-abi=hard 使用 CK804DF 的 fpv2 硬件浮点。
MCPU    ?= ck803f
MABI    ?= hard
CFLAGS  ?= -mcpu=$(MCPU) -mfloat-abi=$(MABI) -O2 -Wall -Wextra
LDFLAGS ?=

all: hello.elf

hello.elf: hello.c
	$(CC) $(CFLAGS) $(LDFLAGS) $< -o $@

hello.hex: hello.elf
	$(CROSS)objcopy -O ihex $< $@

# 打包为可烧录/OTA 镜像（fwinfo 头部链 + txw82xcore.bin），产物在 ../fwpack/firmware.bin
pack: hello.hex
	cd ../fwpack && python3 pack_fw.py pack -c fwpack.ini
	cd ../fwpack && python3 pack_fw.py verify firmware.bin

dump: hello.elf
	$(OBJDUMP) -d $< | sed -n '1,60p'

fpu: hello.elf
	@$(OBJDUMP) -d $< | grep -m6 -E 'f(add|mul|sub|div|ld|st|mov)' || echo "未找到硬件浮点指令"

clean:
	rm -f hello.elf hello.hex

.PHONY: all dump fpu pack clean
