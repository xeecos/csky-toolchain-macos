# TXW828 macOS 开发环境（编译 → 打包 → USB 下载 → OTA 全链路）

在 macOS（Apple Silicon / Intel）上为 **泰芯 TXW828**（Wi-Fi SoC，双核 CK804DF + CK804D，
C-SKY 架构）搭建原生交叉开发环境，**不依赖 Docker / 虚拟机**（仅量产初烧除外）。
已在 macOS 26 + Apple Silicon（8 核 / 16GB）实测。

```
hello.c ──csky-elfabiv2-gcc──▶ hello.elf ──objcopy──▶ hello.hex
                                                      │
              fwpack/pack_fw.py（fwinfo 头部链 + txw82xcore.bin）
                                                      ▼
                                               firmware.bin ──┬── usbboot.py ramboot（RAM 启动）
                                                              └── OTA / TXProgrammer（写 flash）
```

## 完成度与验证状态

| 环节 | 状态 | 验证方式 |
|---|---|---|
| csky-elfabiv2 交叉工具链（GCC 15.3 + binutils 2.46.1 + newlib 4.6.0） | ✅ 构建并实测 | ck803f 硬浮点编译，反汇编确认 fpv2 指令（`fmacs` 等） |
| DSP 指令路径 | ✅ 汇编器实测 | `csky-elfabiv2-as -mcpu=e804df` 汇编 `abs.s32.s` 成功 |
| 固件打包器 pack_fw.py（替代 BinScript/makecode.exe） | ✅ 打包→回读全 CRC 自洽 | selftest + make pack 全 PASS |
| USB-BOOT 下载协议 | ✅ 协议逆向完成（源自官方开源驱动，非猜测） | 离线自检 PASS；**真机 ramboot 待硬件验证** |
| OTA | 📖 SDK 自带（ota.c/file_ota.c/libota），可基于此改造 | 未实测（需硬件） |
| flash 初烧（TXProgrammer 完整流程） | ⚠️ RUN 之后的 flash 编程子协议未开源 | 需抓包一次，或用 ramboot+OTA 绕开 |

## 快速开始

### 一次性：构建工具链（约 25–40 分钟）

```bash
./install-deps.sh          # Homebrew 依赖
./build-toolchain.sh       # 源码构建；支持断点续跑（已完成组件自动跳过）
source ./env.sh            # 加入 PATH（可写进 ~/.zshrc）
```

### 日常：编译 → 打包 → 下载

```bash
cd test
make            # 编译（-mcpu=ck803f -mfloat-abi=hard）
make dump       # 反汇编检查
make pack       # 打包为 ../fwpack/firmware.bin（自动回读校验）

# USB 下载（首次需: python3 -m venv .venv && .venv/bin/pip install pyusb，及 brew install libusb）
.venv/bin/python fwpack/usbboot.py probe                    # 找下载态设备
.venv/bin/python fwpack/usbboot.py ramboot fwpack/firmware.bin
```

### 进入芯片下载模式（硬件侧）

- 空片 / flash 无有效固件：插 USB 自动进入（设备管理器显示 "hgic uboot"，VID 0xA012）；
- 已烧录的板子：按 TXW828 数据手册 V1.3"表 1-7-1-2 烧录口说明"把指定数据脚短接 GND 后复位。

## 各组件说明

### 1. 交叉工具链（构建脚本）

- `install-deps.sh` / `build-toolchain.sh` / `env.sh`；安装于 `~/csky-toolchain/install`
  （`CSKY_ROOT` 可整体挪动；确认不再重建后可删 `src/`+`build/` 释放约 7GB）。
- 可调环境变量：`GCC_VERSION` / `BINUTILS_VERSION` / `NEWLIB_VERSION` / `LANGUAGES`（纯 C 设
  `c` 提速）/ `JOBS`（默认 min(核数,4)，见坑 6）/ `TRIM_MULTILIB`（默认 1 只编 ck803 系列
  multilib；设 0 保留全部，但 ck807/810/860 变体有单文件数小时的编译病理，16GB 机器慎用）。

### 2. 编译参数（TXW828 / CK804DF）

```makefile
CFLAGS = -mcpu=ck803f -mfloat-abi=hard -O2    # C/C++：主线 GCC 无 ck804，ck803 是其安全子集
```

- 主线 GCC `-mcpu` 有效值止于 ck803s 系（再上是 ck807/810/860），**ck803 指令集 ⊂ ck804**，
  代码可安全运行；`-mfloat-abi=hard` 生成 fpv2 硬件浮点（同 CK804DF FPU）。
- **DSP 热点**：写 `.S` 文件用 `csky-elfabiv2-as -mcpu=e804df` 汇编（支持 ck804 DSP 指令；
  注意 `-march=ck804` 不带 DSP 属性），与 ck803f 的 C 目标文件正常链接。
- 官方玄铁 GCC 6.3 的 C 级 DSP intrinsics 主线没有；SDK 预编译 `.a` 可直接链接。

### 3. 固件打包 fwpack/pack_fw.py

把编译产物打包为烧录/OTA 镜像（0x5A69 fwinfo 头部链 137B + core/app 代码区），替代
Windows 的 BinScript.exe + makecode.exe。配置键名与官方 makecode.ini 对齐，数值默认按
**十六进制**解析（十进制写 `0d` 前缀）。

```bash
python3 fwpack/pack_fw.py sample-ini > fwpack/fwpack.ini  # 生成/查看配置
python3 fwpack/pack_fw.py pack   -c fwpack/fwpack.ini
python3 fwpack/pack_fw.py verify fwpack/firmware.bin      # 全量 CRC 校验
python3 fwpack/pack_fw.py selftest
```

- 格式依据：头部布局=SDK 开源 `fwinfo.h`（芯片侧权威定义）；CRC 算法=反汇编官方
  makecode.exe（Go 程序）确认 CRC-16/MODBUS + CRC-32/IEEE；默认值=对照官方 makecode.ini。
- **首次烧录前建议反验证**：拿任何一份厂商打包固件跑 `verify`，若全 PASS 则格式完全对齐；
  若有 FAIL 会精确指出不匹配字段（README 底部"假设清单"也可对照 [fwpack/PROTOCOL.md](fwpack/PROTOCOL.md)）。
- `.hex` 输入自动选最大段簇为代码区并告警丢弃远端 RAM 段；`.data` 需进 flash 时用 SDK
  链接脚本 gcc_csky.ld 设 LMA。
- 加密（AesEnable=1）对应 TXProgrammer 的 USB-ENCRYPT 量产模式。

### 4. USB 下载 fwpack/usbboot.py

hgic USB-BOOT 协议的 macOS 原生实现，**逆向自泰芯官方开源 Linux 驱动 hgic_fmac**
（ROM 下载态同源协议），完整规格见 [fwpack/PROTOCOL.md](fwpack/PROTOCOL.md)。
支持 `probe` / `ramboot`（下载到 SRAM 运行，开发调试用）/ `selftest`。

边界：ROM 协议只写 RAM。**写 flash** 的完整 USB-BOOT（TXProgrammer）在 RUN 之后还有
flash 编程子协议（未开源），两条补齐路径：
1. Windows 虚拟机 + USBPcap 抓一次 TXProgrammer 流量，按 PROTOCOL.md 帧格式解码；
2. `ramboot` 启动带 OTA 的固件（SDK `TXW82x_FPV/sdk/app/update/ota.c`），之后升级全走
   OTA 写 flash。

### 5. OTA（SDK 自带，可改造）

SDK 内置三种升级途径，固件收包后经 `libota_write_fw()` 写 flash 备份区、设 0x5A69 标记、
复位，由 **AppLoader**（二级引导，官网单独下载）校验并应用：

| 途径 | 位置 | 说明 |
|---|---|---|
| 局域网 TCP | `TXW82x_FPV/sdk/app/update/ota.c` | 自定义 0xA0/0xA1 协议，流式收固件 |
| 本地文件 | `TXW82x_FPV/sdk/app/update/file_ota.c` | 检查 SD/U 盘 `ota/ota.bin` 后写入并复位 |
| hgic 以太网协议 | `TXW82x_FPV/sdk/include/lib/ota/libota.h` | 扫描/分包/CRC/复位，配上位机 |

TCP OTA 源码约 200 行明文，可改造为 HTTP 云端下载；断电安全由 AppLoader+libota 保证。

### 6. SDK 源码树 TXW82x_FPV/（已内置）

官方 TXW82x FPV SDK（Apache-2.0）原样存放在本仓库 `TXW82x_FPV/`，**不含**其 Windows 侧构建
产物；来源、校验与更新方式见 [TXW82x_FPV/PROVENANCE.md](TXW82x_FPV/PROVENANCE.md)——
标签 `TXW82x_FPV-v2.7.0.7-43482`、提交 `1cd7f11`，且其中 `project/txw82xcore.bin` 与
`fwpack/txw82xcore.bin` **逐字节相同**（md5 `e07954bacb6aa515d45eda8467cff3fd`），
说明打包输入与 SDK 同源。

- 用途：`libs/`（19 个预编译 `.a`）可直接链接；`sdk/include/lib/ota/fwinfo.h` 是固件头部
  格式的权威定义；`project/makecode.ini` 是 `fwpack/fwpack.ini` 的对齐基准；
  `project/` 内的 `makecode.exe`/`BinScript.exe`/`crc.exe` 正是 `pack_fw.py` 替代掉的那几个。
- 边界：上游要求用 **Windows + 玄铁 CDK** 构建 `project/txw82xApp.cdkproj`（该版本基于 CDS 的
  Linux 构建未验证），构建产物被上游 `.gitignore` 排除，故不在仓库内。macOS 侧的对应做法是
  **自建工具链编译自有代码 + 链接 `libs/*.a` + `fwpack/pack_fw.py` 打包**。

## 已踩过的坑（脚本已内置规避）

1. PATH 里 Homebrew GCC 被误选为宿主编译器 → `ld: library 'System' not found`；
   脚本固定 `CC=/usr/bin/cc` 并导出 `SDKROOT`。
2. GCC 内置老版 zlib 与新 macOS SDK 的 `fdopen` 宏冲突 → `--with-system-zlib`。
3. GCC driver 裸命令调用 `as`/`ld` 命中 Xcode clang 的 as → 安装后在
   `lib/gcc/csky-elfabiv2/<ver>/` 放 as/ld 符号链接（发行版惯例）。
4. macOS bash 3.2 对紧跟中文的 `$var` 解析错误 → 变量一律 `${var}`。
5. sourceware git 被重置 / ftp.gnu.org 极慢 → newlib 用发布包，GNU 走清华/阿里云镜像。
6. libgcc 单文件内存峰值 2GB+，并行过高导致 16GB 内存换页风暴（貌似"卡死"）→
   默认 `JOBS=min(核数,4)` + multilib 裁剪（44 → 7 个变体，全量构建需数小时，裁剪后约 25 分钟）。

## 打包格式待厂商镜像反验证的假设

各 crc16 字段覆盖"头部内位于该字段之前的全部字节"；`boot_data_crc` 与 `boot_from_flash_len`
同区间；size 字段=本头部长度；app 在代码区内偏移 0x1000（BinScript.BinScript 字面值）。
`pack_fw.py verify` 对厂商镜像报 FAIL 时会指出具体字段，据此微调即可。

## 文件清单

```
├── README.md               本文档
├── install-deps.sh         Homebrew 依赖
├── build-toolchain.sh      工具链构建（multilib 裁剪/as-ld 链接修复/断点续跑）
├── env.sh                  PATH 注入（source 生效）
├── test/
│   ├── hello.c             ck803f hard-float 冒烟测试
│   └── Makefile            make / make dump / make fpu / make pack
├── TXW82x_FPV/             官方 SDK 源码树（Apache-2.0，v2.7.0.7-43482；见其 PROVENANCE.md）
│   ├── project/            应用源码 + CDK 工程 + 引导核 txw82xcore.bin + 官方打包配置
│   ├── sdk/                芯片 SDK 源码/头文件/驱动/中间件（含 app/update 的 OTA）
│   ├── libs/               19 个预编译库（macOS 侧可直接链接）
│   ├── csky/               C-SKY/RISC-V 内核支持与 DSP 数学库
│   └── ohos/ tools/        OpenHarmony LiteOS-M 组件与附带工具
└── fwpack/
    ├── pack_fw.py          固件打包器（pack / verify / selftest / sample-ini）
    ├── usbboot.py          USB-BOOT 下载工具（probe / ramboot / selftest）
    ├── PROTOCOL.md         hgic USB-BOOT 协议完整规格（含来源引用）
    ├── fwpack.ini          打包配置示例
    └── txw82xcore.bin      引导核（打包输入；与 TXW82x_FPV/project/ 下同名文件逐字节相同）
```

## 参考来源

- 泰芯官网（产品/数据手册/下载，部分需登录）：taixin-semi.com · 开发者社区：dev.taixin-semi.com
- TXW82x_FPV SDK（GitHub，Apache-2.0）：github.com/Taixin-Semiconductor/TXW82x_FPV
  — 已内置于本仓库 `TXW82x_FPV/`（标签 `v2.7.0.7-43482`）
- hgic_fmac 官方 Linux 驱动（USB-BOOT 协议来源）：github.com/TXW8301/TXW8301-FMAC-linux-driver
- 玄铁工具/CDK：xrvm.cn · GNU 镜像：tuna/aliyun · newlib：sourceware.org

---
许可说明：本仓库脚本为原创工具；`TXW82x_FPV/` 为泰芯官方 SDK 原样存放（Apache-2.0，许可与
版权声明见其 `LICENSE` 与 `PROVENANCE.md`），`fwpack/txw82xcore.bin` 为该 SDK 产物，
协议格式描述逆向自官方开源代码，仅用于与自有硬件互联开发。
