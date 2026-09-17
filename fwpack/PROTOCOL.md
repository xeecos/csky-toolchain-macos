# hgic USB-BOOT 下载协议规格（逆向自已开源的官方驱动）

来源：泰芯官方 Linux 驱动 **hgic_fmac**（GitHub 镜像
[TXW8301/TXW8301-FMAC-linux-driver](https://github.com/TXW8301/TXW8301-FMAC-linux-driver)，
对应官网 "taixin-fmac-linux-driver" 下载项）。该驱动在芯片处于 **ROM 下载态**
（空片 / 数据脚短接 GND 复位后，USB 枚举为 "hgic uboot"）时使用的就是这套 ROM 协议
——与 Windows 下 TXProgrammer 的 "USB-BOOT" 模式同源。

> 关键文件：`utils/fwdl.c`（状态机与下载流程）、`utils/fwdl.h`（命令集）、
> `hgic_def.h`（帧结构/VID/PID）、`hgic.h`（hgic_hdr/类型值）、`utils/if_usb.c`（USB 绑定）。
> 本目录 `usbboot.py` 按本规格实现。

## 1. USB 绑定

| 项 | 值 |
|---|---|
| VID | `0xA012` |
| PID | `0x4002` / `0x4104` / `0x8400`（TXW82x 家族为 0x8400 系） |
| 接口 | 单接口，bulk IN + bulk OUT 端点（自动探测） |
| 分片 | `bootdl_pktlen = 2048` 字节 |
| 校验模式 | `bootdl_cksum = HGIC_BUS_BOOTDL_CHECK_0XFD`（固定 0xFD） |
| 对齐 | USB 发送按 4 字节对齐（`ALIGN(len, 4)`） |

## 2. 帧格式（全部小端）

```
hgic_hdr (8 字节):
  +0  u16  magic  = 0x1A2B
  +2  u8   type   (7 = BOOTDL 命令, 15 = BOOTDL_DATA 数据)
  +3  u8   ifidx:4 | flags:4
  +4  u16  length （整帧长度，含本头）
  +6  u16  cookie （命令自增序号；数据帧为 0）

命令帧 (8 + 12 字节):
  +8  u8   cmd        （见命令集）
  +9  u8   cmd_len    = 12
  +10 u8   cmd_flag   = 校验模式 (0x02 = 0xFD 模式)
  +11 u32  addr       （目标地址 / 魔术字前 4 字节）
  +15 u32  len        （长度 / 标志位 / 魔术字后 4 字节）
  +19 u8   check      = checksum(cmd..len 共 11 字节; 0xFD 模式下恒 0xFD)

响应帧 (8 + 8 字节):
  +8  u8   cmd        （回显命令）
  +9  u8   rsp        （0=OK, 0xFF=已在固件态, 其他=错误码）
  +10 u32  rsp_data
  +14 u8   reserved
  +15 u8   check      （校验 &cmd 起 8 字节; 0xFD 模式下恒 0xFD）

数据帧:
  hgic_hdr(type=15, cookie=0) + 12 字节全零命令头 + 固件分片(≤2048B)
```

## 3. 命令集（ROM "hgic uboot"）

| 值 | 命令 | 语义 |
|---|---|---|
| 0x00 | ENTER | 握手。addr/len 两字段合放 8 字节魔术字 **`@huge-ic`** |
| 0x01 | GET_SC | 查询 |
| 0x02 | WRITE_MEM | 写内存。addr=目标地址；**USB 总线下 len=分片长度+1024**（SDIO 为裸长度）。应答 OK 后紧跟一个数据帧携带该分片 |
| 0x03 | READ_MEM | 读内存 |
| 0x04 | RUN | 跳转执行。addr=加载地址；len=固件长度 \| BIT31(AES解密) \| BIT30(CRC校验) \| BIT29(SP配置) |
| 0x05 | VERIFY_MEM | 校验内存（配合固件头的 local_crc32） |
| 0x06 | CHIP_RESET | 芯片复位 |
| 0x07/0x08 | WRITE_REG / READ_REG | 寄存器读写 |
| 0x09 | SPEED | 总线提速协商 |
| 0xFF | EXIT | 退出下载态 |

响应错误码：1=命令错 2=地址错 3=长度错 4=权限 5=命令校验错 6=数据校验错 8=超时 0xFF=已在固件态。

## 4. 固件下载流程（fwdl.c: hgic_bootdl_download）

```
1. 解析打包固件（fwinfo 头，即 pack_fw.py 产物）:
     write_addr = boot_to_sram_addr   （头偏移 4）
     hdr_len    = code_offset         （头偏移 12，我们的打包 = 137）
     fw_len     = 文件长度 - hdr_len
     aes_en/crc_en = mode 位域        （头偏移 26，BIT10/BIT11）
2. ENTER("@huge-ic") → rsp 0x00=下载态 / 0xFF=固件态
3. 循环（每片 ≤2048B）:
     WRITE_MEM(addr, frag_len+1024) → 应答
     数据帧(type=15, 12 零字节 + frag) → 应答 rsp==0
4. RUN(write_addr, fw_len|标志) → ROM 跳转执行，芯片重新枚举 USB
```

## 5. 与 TXProgrammer "USB-BOOT" 的关系（重要边界）

本协议把固件写进 **SRAM 并运行**（RAM 启动，适合开发调试）。
TXProgrammer 完整流程还要把镜像固化到 **SPI flash**，其做法大概率是：
用本协议 RAM 启动一个"烧录固件/AppLoader"，再由它通过同一 USB bulk 通道
接收完整镜像并写 flash——**RUN 之后的 flash 编程子协议未包含在开源驱动中**，
需一次 USB 抓包确认（Windows 虚拟机 + USBPcap + TXProgrammer，抓 VID 0xA012
的 bulk 传输，用本协议帧格式即可逐帧解码）。

另一个无需抓包的路径：SDK 的 OTA（`TXW82x_FPV/sdk/app/update/ota.c` + libota）在固件
运行态即可把新镜像写进 flash 备份区并由 AppLoader 完成切换——即先用
`usbboot.py ramboot` 启动一次带 OTA 功能的固件（如 SDK demo），之后全部
用 OTA 升级，绕开 flash 初烧问题。空片量产初烧仍建议 TXProgrammer。

## 6. 参考实现

macOS/Linux 原生工具：本目录 `usbboot.py`（`probe` / `ramboot` / `selftest`），
依赖 `pip3 install pyusb` + `brew install libusb`。
