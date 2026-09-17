#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pack_fw.py — TXW82x (TXW828, CK804DF/CK804D) 固件打包器（macOS/Linux 原生，替代 Windows 的
            BinScript.exe + makecode.exe）

镜像格式依据 SDK 开源头文件 sdk/include/lib/ota/fwinfo.h（芯片侧 ROM/AppLoader 解析的权威定义）：
  [boot header 32B][spi header 74B][firmware header 25B][encrypt header 6B]
  ... (pad 0xFF) ... [param @ ParamOffset] ... [core.bin @ 0][app @ app_offset] ...

CRC 算法依据对官方 makecode.exe（Go 程序）的二进制分析：
  - 头部各 crc16 字段 = CRC-16/MODBUS (poly 0x8005 反射, init 0xFFFF, xorout 0)
    （exe 内含 "CRC-16/MODBUS" 字符串）
  - code_crc32 = CRC-32 IEEE (Go hash/crc32, 多项式 0xEDB88320)

用法:
  python3 pack_fw.py selftest                 # CRC/格式自检
  python3 pack_fw.py sample-ini > fwpack.ini  # 生成示例配置（键名与官方 makecode.ini 对齐）
  python3 pack_fw.py pack -c fwpack.ini       # 按配置打包
  python3 pack_fw.py verify firmware.bin      # 解析并校验任意打包固件（含厂商产物，用于反验证格式）

仅依赖 Python 3 标准库。
"""
import argparse
import configparser
import datetime
import struct
import sys
from pathlib import Path

# ----------------------------------------------------------------------------
# CRC 算法
# ----------------------------------------------------------------------------

def _crc16_reflected(data: bytes, poly: int, init: int, xorout: int) -> int:
    """逐位反射型 CRC16（LSB first）"""
    crc = init
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ poly if crc & 1 else crc >> 1
    return crc ^ xorout


def crc16_modbus(data: bytes) -> int:
    """CRC-16/MODBUS: check("123456789") == 0x4B37"""
    return _crc16_reflected(data, 0xA001, 0xFFFF, 0x0000)


def crc32_ieee(data: bytes, crc: int = 0) -> int:
    """CRC-32/IEEE (zlib)，支持增量；check("123456789") == 0xCBF43926"""
    crc ^= 0xFFFFFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xEDB88320 if crc & 1 else crc >> 1
    return crc ^ 0xFFFFFFFF


# ----------------------------------------------------------------------------
# 固件头部链（fwinfo.h，__packed，小端）
#   boot       32 字节: 见字段注释的偏移
#   spi_info   74 字节: func(1)+size(1)+read_cfg(6)+spec_seq[64]+crc16(2)
#   fw_info    25 字节: func(1)+size(1)+sdk_ver(4)+svn(4)+date(4)+chip_id(2)
#                       +cpu_id(1)+code_crc32(4)+param_crc16(2)+crc16(2)
#   encrypt     6 字节: func(1)+size(1)+customer_id(2)+crc16(2)
# 合计 137 字节。
# 各 crc16 字段的覆盖范围 = 本头部中位于 crc 字段之前的全部字节（通用约定）。
# ----------------------------------------------------------------------------

FWINFO_BOOT_HDR = 0x5A69

def build_boot_header(c: dict, area_off: int, area_len: int, code_area: bytes) -> bytes:
    """构造 32 字节 boot header。

    area_off/area_len/code_area 均为"代码区"坐标（不含头部链的 buffer）；
    写入头部的 code_offset = area_off + 137（头部链总长 32+74+25+6）。
    """
    flash_size = c["spi_size"]
    flash_blk = (flash_size + 0xFFFF) // 0x10000 if flash_size else 0
    baud = c["spi_clk_mhz"]
    if not 0 <= baud <= 0x3FFF:
        raise ValueError(f"SPI_CLK_MHZ={baud:#x} 超出 14bit 字段")
    word0 = baud | ((c["driver_strength"] & 0x3) << 14)

    mode = (c["pll_src_mhz"] & 0xFF) \
        | ((1 if c["pll_en"] else 0) << 8) \
        | ((1 if c["debug_info_en"] else 0) << 9) \
        | ((1 if c["aes_enable"] else 0) << 10) \
        | ((1 if c["code_crc16_en"] else 0) << 11)

    # boot_data_crc 与 boot_from_flash_len 同区间：加载点起、整个加载长度
    load_len = area_len - area_off
    boot_crc = crc16_modbus(code_area[area_off:area_off + load_len])

    size = 32  # Link to Next Header
    h = struct.pack("<HBBIIIIHHHH",
                    FWINFO_BOOT_HDR, c["boot_version"], size,
                    c["code_load_sram_addr"], c["code_exe_addr"],
                    area_off + 137, load_len,
                    boot_crc, flash_blk, word0, mode)
    h += struct.pack("<H", 0)                    # reserved @28
    h += struct.pack("<H", crc16_modbus(h))      # head_crc16 覆盖前 30 字节
    assert len(h) == 32, len(h)
    return h


def build_spi_header(c: dict) -> bytes:
    """构造 74 字节 spi header（读命令/线宽/特殊时序序列）。"""
    # read_cfg: 6 字节
    b0 = c["read_cmd"]
    b1 = ((c["read_cmd_dummy"] & 0xF)
          | ((c["clock_mode"] & 0x3) << 4)
          | ((1 if c["spec_squence_en"] else 0) << 6)
          | ((1 if c["wire_mode4_en"] else 0) << 7))
    b2 = ((c["wire_mode_when_cmd"] & 0x3)
          | ((c["wire_mode_when_addr"] & 0x3) << 2)
          | ((c["wire_mode_when_data"] & 0x3) << 4)
          | ((c["wire_mode4_select"] & 0x3) << 6))
    read_cfg = bytes([b0, b1, b2, 0]) + struct.pack("<H", c["sample_delay"])

    # spec sequence: 每条 = cmd(1)+dummy(1)+dat_len(1)+dat(n)，序列化进 64 字节槽
    seqs = []
    for i in range(c["spec_squence_numbers"]):
        hexstr = c[f"spec_squence{i}"]
        seqs.append(bytes.fromhex(hexstr))
    blob = b"".join(seqs)
    if len(blob) > 64:
        raise ValueError(f"SpecSquence 总长 {len(blob)} 超过 64 字节槽")
    blob += b"\x00" * (64 - len(blob))

    size = 74
    h = bytes([0x1, size]) + read_cfg + blob
    h += struct.pack("<H", crc16_modbus(h))
    assert len(h) == 74, len(h)
    return h


def build_fw_header(c: dict, code_crc32: int, param_crc16: int) -> bytes:
    """构造 25 字节 firmware info header。"""
    size = 25
    h = struct.pack("<BBIIIHB", 0x2, size,
                    c["sdk_version"], c["svn_version"], c["build_date"],
                    c["chip_id"], c["cpu_id"])
    h += struct.pack("<IHH", code_crc32, param_crc16, 0)
    h = h[:-2] + struct.pack("<H", crc16_modbus(h[:-2]))
    assert len(h) == 25, len(h)
    return h


def build_encrypt_header(c: dict) -> bytes:
    """构造 6 字节 encrypt header（AES 授权信息；未加密也会写入 customer_id）。"""
    size = 6
    h = struct.pack("<BBH", 0x3, size, c["customer_id"])
    h += struct.pack("<H", crc16_modbus(h))
    assert len(h) == 6, len(h)
    return h


# ----------------------------------------------------------------------------
# 输入解析
# ----------------------------------------------------------------------------

def parse_ihex(path: Path) -> dict:
    """Intel HEX → {addr: bytes}（拼接连续段）。支持 I8HEX/I16HEX。"""
    segments, upper = {}, 0
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if not line.startswith(":"):
            raise ValueError(f"{path}:{lineno} 非 Intel HEX 行: {line[:20]}")
        rec = bytes.fromhex(line[1:])
        count, ahi, alo, rtype = rec[0], rec[1], rec[2], rec[3]
        data, cks = rec[4:4 + count], rec[4 + count]
        if (sum(rec) & 0xFF) != 0:
            raise ValueError(f"{path}:{lineno} 校验和错误")
        addr = upper + (ahi << 8) + alo
        if rtype == 0:
            segments.setdefault(addr, bytearray()).extend(data)
        elif rtype == 1:
            break
        elif rtype == 4:
            upper = ((data[0] << 8) | data[1]) << 16
        elif rtype == 2:
            upper = ((data[0] << 8) | data[1]) << 4
    # 合并相邻段
    merged = {}
    for a in sorted(segments):
        last = max(merged) if merged else None
        if last is not None and last + len(merged[last]) == a:
            merged[last].extend(segments[a])
        else:
            merged[a] = bytearray(segments[a])
    return merged


def load_payload(path: Path, want_base: int, window: int = 0, region_base: int = -1) -> tuple:
    """读 app/core 载荷，返回 (基址, 数据)。

    .hex: 解析段地址并合并（相邻空洞 0xFF 填充）。window>0 时启用加载区过滤，
    只保留 [region_base, region_base+window) 内的段：
      region_base>=0 用指定值；否则自动选"总字节数最大的段簇"为代码区
      （.text 远大于 .data，避免误选远端 RAM 小段）。
      丢弃窗口外段时打印告警。
    .bin: 原样读入，基址取 want_base。
    """
    if path.suffix.lower() not in (".hex", ".ihex"):
        return want_base, path.read_bytes()
    segs = parse_ihex(path)
    if not segs:
        raise ValueError(f"{path} 无数据段")

    def cluster_of(segments):
        """把段切成簇（间隔>64KB 视为不同簇），返回 [(start, total_bytes)]"""
        out = []
        for a in sorted(segments):
            if out and a - (out[-1][0] + out[-1][1]) < 0x10000:
                out[-1][1] += len(segments[a])
            else:
                out.append([a, len(segments[a])])
        return out

    if window > 0:
        clusters = cluster_of(segs)
        if region_base < 0 and clusters:
            region_base = max(clusters, key=lambda c: c[1])[0]
        kept = {a: d for a, d in segs.items() if region_base <= a < region_base + window}
        for a in sorted(segs):
            if a not in kept:
                print(f"[warn] {path.name}: 丢弃窗口外段 @{a:#010x}"
                      f" ({len(segs[a])} 字节) —— 若这是需要在 flash 中存放的"
                      f".data 段，请用 SDK 的链接脚本(gcc_csky.ld)把 LMA 指到 flash")
        segs = kept
    base = min(segs) if segs else 0
    data, end = bytearray(), base
    for a in sorted(segs):
        if a > end:
            data.extend(b"\xFF" * (a - end))
        data.extend(segs[a])
        end = a + len(segs[a])
    return base, bytes(data)


# ----------------------------------------------------------------------------
# 打包
# ----------------------------------------------------------------------------

def load_config(ini_path: Path) -> dict:
    cp = configparser.ConfigParser(inline_comment_prefixes=(";", "#"),
                                   comment_prefixes=(";", "#"))
    cp.optionxform = str
    read = cp.read(ini_path, encoding="utf-8")
    if not read:
        raise ValueError(f"无法读取配置 {ini_path}")
    def get(sec, key, default=None, cast=None):
        if sec not in cp or key not in cp[sec] or cp[sec][key].strip() == "":
            if default is None:
                raise ValueError(f"配置缺少 [{sec}] {key}")
            return default
        v = cp[sec][key].strip()
        return cast(v) if cast else v
    # 官方 makecode.ini 的数值一律按十六进制解析（如 SPI_SIZE=100000 即 1MB）；
    # 需要十进制时写 "0d" 前缀（如 AppOffset=0d4096）
    def ih(s):
        s = s.strip()
        return int(s[2:], 10) if s.lower().startswith("0d") else int(s, 16)

    c = dict(
        # [COMMON]
        code_file=get("COMMON", "CodeFile"),
        core_file=get("COMMON", "CoreFile", ""),
        param_file=get("COMMON", "ParamFile", ""),
        out_file=get("COMMON", "OutFile", "firmware.bin"),
        app_offset=get("COMMON", "AppOffset", 0x1000, ih),
        param_offset=get("COMMON", "ParamOffset", 0x230, ih),
        region_base=get("COMMON", "CodeRegionBase", -1, ih),
        mem_endian_le=get("COMMON", "MemEndianLE", 1, int),
        cpu_id=get("COMMON", "CPU_ID", 0, ih),
        chip_id=get("COMMON", "CHIP_ID", 0x8401, ih),
        customer_id=get("COMMON", "CustomerID", 1001, ih),
        sdk_version=get("COMMON", "SDKVersion", 0x0207, ih),
        svn_version=get("COMMON", "SVNVersion", 0, ih),
        build_date=get("COMMON", "BuildDate",
                       int(datetime.datetime.now().strftime("%Y%m%d"), 16), ih),
        # [SPI] —— 键名与官方 makecode.ini 对齐
        code_crc16_en=get("SPI", "CodeCRC16", 1, int),
        boot_version=get("SPI", "Version", 0x01, ih),
        code_load_sram_addr=get("SPI", "CodeLoadToSramAddr", 0x10000000, ih),
        code_exe_addr=get("SPI", "CodeExeAddr", 0x10000000, ih),
        code_load_len=get("SPI", "CodeLoadLen", 0x2000, ih),
        spi_size=get("SPI", "SPI_SIZE", 0x100000, ih),
        spi_clk_mhz=get("SPI", "SPI_CLK_MHZ", 0x3C, ih),
        driver_strength=get("SPI", "DriverStrength", 0, int),
        pll_src_mhz=get("SPI", "PLL_SRC_MHZ", 28, ih),
        pll_en=get("SPI", "PLL_EN", 1, int),
        debug_info_en=get("SPI", "DebugInfoEn", 0, int),
        aes_enable=get("SPI", "AesEnable", 0, int),
        read_cmd=get("SPI", "ReadCmd", 0xEB, ih),
        read_cmd_dummy=get("SPI", "ReadCmdDummy", 6, int),
        clock_mode=get("SPI", "ClockMode", 0, int),
        sample_delay=get("SPI", "SampleDelay", 0x55AA, ih),
        wire_mode_when_cmd=get("SPI", "WireModeWhenCmd", 1, int),
        wire_mode_when_addr=get("SPI", "WireModeWhenAddr", 4, int),
        wire_mode_when_data=get("SPI", "WireModeWhenData", 4, int),
        wire_mode4_select=get("SPI", "WireMode4Select", 0, int),
        wire_mode4_en=get("SPI", "WireMode4En", 1, int),
        spec_squence_en=get("SPI", "SpecSquenceEn", 1, int),
        spec_squence_numbers=get("SPI", "SpecSquenceNumbers", 6, int),
    )
    for i in range(c["spec_squence_numbers"]):
        c[f"spec_squence{i}"] = get("SPI", f"SpecSquence{i}", "00000000")
    return c


def cmd_pack(args):
    c = load_config(Path(args.config))
    here = Path(args.config).parent

    app_base, app = load_payload(here / c["code_file"], 0,
                                 window=c["spi_size"], region_base=c["region_base"])
    core = b""
    if c["core_file"]:
        _, core = load_payload(here / c["core_file"], 0)

    # ---- 组装代码区（BinScript 语义: core @0x0 + app @AppOffset, 空洞 0xFF）----
    code_end = max(len(core), c["app_offset"] + len(app))
    if code_end > c["spi_size"]:
        raise ValueError(f"代码区 {code_end:#x} 超过 SPI_SIZE={c['spi_size']:#x}")
    if core and c["app_offset"] < len(core):
        print(f"[warn] app @{c['app_offset']:#x} 与 core 区 [0,{len(core):#x}) 重叠，"
              f"app 将覆盖 core 对应字节 —— 请核对官方 BinScript.BinScript 的"
              f" remap 目标偏移后再烧写")
    image = bytearray(b"\xFF" * code_end)
    image[0:len(core)] = core
    image[c["app_offset"]:c["app_offset"] + len(app)] = app

    # ---- 参数区 ----
    param = b""
    if c["param_file"]:
        param = (here / c["param_file"]).read_bytes()
        pe = c["param_offset"] + len(param)
        if pe > c["app_offset"]:
            raise ValueError(f"参数区 [{c['param_offset']:#x},{pe:#x}) 与 app 区 "
                             f"(起点 {c['app_offset']:#x}) 重叠")
        image[c["param_offset"]:pe] = param

    # 代码区加载起点（对应 boot header 的 code_offset）：
    # 有 core.bin 时 ROM 从 0 加载整个映像；否则从 AppOffset 加载
    code_offset = 0 if core else c["app_offset"]

    # ---- 头部链（boot 最后生成，因为 boot_data_crc 覆盖代码区）----
    hdr = bytearray()
    hdr += build_spi_header(c)
    hdr += build_fw_header(c, crc32_ieee(bytes(image[code_offset:])),
                           crc16_modbus(param) if param else 0)
    hdr += build_encrypt_header(c)
    boot = build_boot_header(c, code_offset, len(image) - code_offset, bytes(image))
    image = bytes(boot) + bytes(hdr) + bytes(image)

    out = here / c["out_file"]
    out.write_bytes(image)
    print(f"[pack] app: {c['code_file']} @{app_base:#x} ({len(app)} 字节)")
    if core:
        print(f"[pack] core: {c['core_file']} @0x0 ({len(core)} 字节)")
    if param:
        print(f"[pack] param: {c['param_file']} {len(param)} 字节 @{c['param_offset']:#x}")
    print(f"[pack] code_offset={code_offset + 137:#x} "
          f"code_crc32={crc32_ieee(image[code_offset + 137:]):#010x}")
    print(f"[pack] 输出: {out} ({len(image)} 字节, {len(image):#x})")
    return 0


# ----------------------------------------------------------------------------
# 校验 / 解析
# ----------------------------------------------------------------------------

def cmd_verify(args):
    data = Path(args.file).read_bytes()
    ok = True

    def show(name, cond, detail=""):
        nonlocal ok
        print(f"  [{'OK' if cond else 'FAIL'}] {name} {detail}")
        ok = ok and cond

    if len(data) < 137:
        print("文件小于最小头部 137 字节"); return 1

    (flag, ver, bsize, to_sram, exe, code_off, from_len,
     bdata_crc, blk, word0, mode) = struct.unpack("<HBBIIIIHHHH", data[:28])
    boot_reserved, boot_crc = struct.unpack("<HH", data[28:32])
    show("boot_flag == 0x5A69", flag == FWINFO_BOOT_HDR, f"(got {flag:#06x})")
    show("boot head_crc16", crc16_modbus(data[:30]) == boot_crc,
         f"(存储 {boot_crc:#06x}, 计算 {crc16_modbus(data[:30]):#06x})")
    baud, drv = word0 & 0x3FFF, word0 >> 14
    print(f"  boot: ver={ver} next_hdr={bsize} load_to={to_sram:#x} exe={exe:#x}")
    print(f"        code_off={code_off:#x} load_len={from_len:#x} boot_data_crc={bdata_crc:#06x}")
    print(f"        flash_blk={blk}({blk * 64}KB) spi_clk={baud}MHz drv={drv}")
    print(f"        mode: pll_src={mode & 0xFF}MHz pll_en={(mode >> 8) & 1} "
          f"debug={(mode >> 9) & 1} aes={(mode >> 10) & 1} crc_en={(mode >> 11) & 1}")
    if code_off + from_len <= len(data):
        show("boot_data_crc", crc16_modbus(data[code_off:code_off + from_len]) == bdata_crc)

    # spi header
    s = 32
    func, ssize = data[s], data[s + 1]
    show("spi func_code == 1", func == 1)
    spi_crc = struct.unpack("<H", data[s + 72:s + 74])[0]
    show("spi header_crc16", crc16_modbus(data[s:s + 72]) == spi_crc)
    b0, b1, b2 = data[s + 2], data[s + 3], data[s + 4]
    sample_delay = struct.unpack("<H", data[s + 6:s + 8])[0]
    print(f"  spi: read_cmd={b0:#04x} dummy={b1 & 0xF} clock_mode={(b1 >> 4) & 3} "
          f"qspi_en={b1 >> 7} sample_delay={sample_delay:#06x}")

    # fw header
    s = 32 + 74
    func, fsize = data[s], data[s + 1]
    show("fwinfo func_code == 2", func == 2)
    sdk_ver, svn, bdate = struct.unpack("<III", data[s + 2:s + 14])
    chip_id, cpu_id = struct.unpack("<HB", data[s + 14:s + 17])
    code_crc32, param_crc16, fw_crc = struct.unpack("<IHH", data[s + 17:s + 25])
    show("fwinfo crc16", crc16_modbus(data[s:s + 23]) == fw_crc)
    show("code_crc32", crc32_ieee(data[code_off:]) == code_crc32,
         f"(存储 {code_crc32:#010x}, 计算 {crc32_ieee(data[code_off:]):#010x})")
    print(f"  fwinfo: sdk={sdk_ver:#x} svn={svn:#x} date={bdate:#x} "
          f"chip={chip_id:#x} cpu={cpu_id} param_crc16={param_crc16:#06x}")

    # encrypt header
    s = 32 + 74 + 25
    func, esize = data[s], data[s + 1]
    cust, ecrc = struct.unpack("<HH", data[s + 2:s + 6])
    show("encrypt func_code == 3", func == 3)
    show("encrypt crc16", crc16_modbus(data[s:s + 4]) == ecrc)
    print(f"  encrypt: customer_id={cust:#x}")

    print("\n总体:", "PASS ✔ 所有 CRC 与格式校验通过" if ok else "FAIL ✘ 存在不匹配项")
    return 0 if ok else 1


# ----------------------------------------------------------------------------
# 自检 / 示例配置
# ----------------------------------------------------------------------------

SAMPLE_INI = """\
; pack_fw.py 配置（键名与官方 makecode.ini 对齐，注释解释含义）
; 所有数值支持 0x 前缀/十进制/纯十六进制字符串（与官方 ini 一致）
[COMMON]
CodeFile=app.hex            ; 应用载荷: .hex(Intel HEX,自动取基址) 或 .bin
CoreFile=txw82xcore.bin     ; SDK 引导核(可空)。非空时置于镜像 0x0
ParamFile=                  ; 参数文件(可空)，嵌入到 ParamOffset
OutFile=firmware.bin        ; 输出文件名
AppOffset=1000              ; app 在代码区内的偏移(默认 0x1000，同 BinScript.BinScript)
ParamOffset=230             ; 参数区偏移(默认 0x230)
MemEndianLE=1
CPU_ID=0
CHIP_ID=8401                ; TXW82x 芯片 ID(官方 makecode.ini 默认)
CustomerID=1001
SDKVersion=207              ; 打包进 fwinfo 头，供 OTA 端比对版本
SVNVersion=0
;BuildDate=20260917         ; 不填则自动用当前日期

[SPI]
CodeCRC16=1
Version=1                   ; boot header version(version>1 时 flash_blk 单位 64KB)
CodeLoadToSramAddr=10000000 ; 代码加载进 SRAM 的地址
CodeExeAddr=10000000        ; 代码执行入口
CodeLoadLen=2000            ; ROM 首次搬运长度(用于 boot_data_crc)
SPI_SIZE=100000             ; flash 容量(字节)
SPI_CLK_MHZ=3c
DriverStrength=0
PLL_SRC_MHZ=28
PLL_EN=1
DebugInfoEn=0
AesEnable=0
ReadCmd=EB                  ; 03/0B/3B/6B/EB = 标准/快读/双线/四线/QPI
ReadCmdDummy=6
ClockMode=0
SampleDelay=55AA
WireModeWhenCmd=1
WireModeWhenAddr=4
WireModeWhenData=4
WireMode4Select=0
WireMode4En=1
SpecSquenceEn=1
SpecSquenceNumbers=6
SpecSquence0=50000000
SpecSquence1=0100020002
SpecSquence2=05800101
SpecSquence3=50000000
SpecSquence4=31000102
SpecSquence5=05800101
"""


def cmd_selftest(args):
    print("[selftest] CRC-16/MODBUS('123456789') =="
          f" {crc16_modbus(b'123456789'):#06x} (期望 0x4B37)",
          "OK" if crc16_modbus(b"123456789") == 0x4B37 else "FAIL")
    print("[selftest] CRC-32/IEEE('123456789') =="
          f" {crc32_ieee(b'123456789'):#010x} (期望 0xCBF43926)",
          "OK" if crc32_ieee(b"123456789") == 0xCBF43926 else "FAIL")
    # 打包→verify 闭环
    import tempfile, subprocess
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        app = bytes(range(256)) * 16
        (td / "app.bin").write_bytes(app)
        (td / "txw82xcore.bin").write_bytes(bytes(range(256)) * 4)
        (td / "fw.ini").write_text(SAMPLE_INI.replace("CodeFile=app.hex", "CodeFile=app.bin"))
        r = subprocess.run([sys.executable, __file__, "pack", "-c", str(td / "fw.ini")],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("[selftest] pack 失败:\n", r.stdout, r.stderr); return 1
        r2 = subprocess.run([sys.executable, __file__, "verify", str(td / "firmware.bin")],
                            capture_output=True, text=True)
        print(r2.stdout)
        if r2.stderr:
            print("[selftest] verify stderr:\n", r2.stderr)
        return r2.returncode


def main():
    ap = argparse.ArgumentParser(description="TXW82x 固件打包器 (fwinfo.h 格式)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("pack",):
        p = sub.add_parser(name)
        p.add_argument("-c", "--config", required=True, help="fwpack.ini 路径")
    for name in ("verify",):
        p = sub.add_parser(name)
        p.add_argument("file", help="打包固件 .bin")
    sub.add_parser("selftest")
    sub.add_parser("sample-ini")
    args = ap.parse_args()

    if args.cmd == "pack":
        return cmd_pack(args)
    if args.cmd == "verify":
        return cmd_verify(args)
    if args.cmd == "selftest":
        return cmd_selftest(args)
    if args.cmd == "sample-ini":
        sys.stdout.write(SAMPLE_INI)
        return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
