#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
usbboot.py — TXW82x/TXW81x (hgic) USB-BOOT 下载协议的 macOS/Linux 原生实现

协议来源（开源逆向，非猜测）：泰芯官方 Linux 驱动 hgic_fmac（GitHub 镜像
TXW8301/TXW8301-FMAC-linux-driver）的 utils/fwdl.c + hgic_def.h + hgic.h，
该驱动在芯片处于 ROM 下载态（USB 枚举名 "hgic uboot"）时使用同一套 ROM 协议。

协议规格（全部小端）：
  hgic_hdr      (8B):  magic u16=0x1A2B | type u8 | ifidx:4/flags:4 u8 | length u16 | cookie u16
  命令帧 (8+12B):      hgic_hdr(type=7) + cmd u8 + cmd_len u8(12) + cmd_flag u8
                        + addr u32 + len u32 + check u8
  响应帧 (8+8B):       hgic_hdr + cmd u8 + rsp u8 + rsp_data u32 + reserved u8 + check u8
  数据帧:              hgic_hdr(type=15, cookie=0) + 12 零字节 + 固件分片(≤2048B)
  USB: VID 0xA012，PID {0x4002, 0x4104, 0x8400}，bulk in/out，发送按 4 字节对齐

命令集（ROM "hgic uboot"）:
  0x00 ENTER   addr/len 8 字节 = "@huge-ic" 魔术字
  0x02 WRITE_MEM  addr=目标地址(如 SRAM 0x10000000)，len=分片长+1024(USB 总线)
                  命令应答后紧跟一个数据帧携带该分片
  0x04 RUN     addr=加载地址，len=固件长度|标志位(BIT31:AES解密 BIT30:CRC校验 BIT29:SP配置)
  其余: 0x01 GET_SC / 0x03 READ_MEM / 0x05 VERIFY_MEM / 0x06 CHIP_RESET /
        0x07 WRITE_REG / 0x08 READ_REG / 0x09 SPEED / 0xFF EXIT
  校验模式 cmd_flag: 0=累加和 1=CRC8 2=固定 0xFD（USB 总线默认 0xFD）

下载流程 = ENTER → [WRITE_MEM + 数据帧]×N → RUN → 芯片复位启动并重新枚举。

用法:
  python3 usbboot.py selftest               # 离线自检（构包/解析，无需硬件）
  python3 usbboot.py probe                  # 列出 USB 上的 hgic 设备
  python3 usbboot.py ramboot firmware.bin   # 把打包固件下载进 SRAM 并运行（开发调试用）

依赖: pip3 install pyusb (需要 libusb: brew install libusb)
注意: ROM 协议写的是 RAM（RAM 启动）。把固件固化到 SPI flash 需要 ROM 之上的
     AppLoader/烧录固件流程（TXProgrammer 的完整 USB-BOOT），其 RUN 之后的
     flash 编程子协议需抓包确认——见 PROTOCOL.md。
"""
import struct
import sys
import time

HGIC_VID = 0xA012
HGIC_PIDS = (0x4002, 0x4104, 0x8400)

HGIC_HDR_TX_MAGIC = 0x1A2B
HGIC_HDR_TYPE_BOOTDL = 7
HGIC_HDR_TYPE_BOOTDL_DATA = 15

CMD_ENTER = 0x00
CMD_GET_SC = 0x01
CMD_WRITE_MEM = 0x02
CMD_READ_MEM = 0x03
CMD_RUN = 0x04
CMD_VERIFY_MEM = 0x05
CMD_CHIP_RESET = 0x06
CMD_SPEED = 0x09
CMD_EXIT = 0xFF

CHECK_SUM = 0
CHECK_0XFD = 2
CMD_LEN = 12
FRAG_SIZE = 2048
BOOT_CMD_KEY = b"@huge-ic"
NORMAL_TMO = 100        # ms
WRITE_MEM_TMO = 1000    # ms

RUN_PREACT_AES_DEC = 1 << 31
RUN_PREACT_CRC_CHK = 1 << 30
RUN_PREACT_SP_CFG = 1 << 29

RSP_ERR_IN_FW = 0xFF    # 设备已在固件态（非下载态）


def checksum(mode: int, buf: bytes) -> int:
    if mode == CHECK_SUM:
        return sum(buf) & 0xFF
    if mode == CHECK_0XFD:
        return 0xFD
    raise NotImplementedError("CRC8 校验模式暂未实现（驱动中该分支亦为空实现）")


def hgic_hdr(type_: int, length: int, cookie: int = 0) -> bytes:
    return struct.pack("<HBBHH", HGIC_HDR_TX_MAGIC, type_, 0, length, cookie)


def cmd_frame(cmd: int, addr: int, length: int, mode: int = CHECK_0XFD,
              cookie: int = 0) -> bytes:
    """构造 20 字节命令帧（hgic_hdr + 12 字节命令体）。"""
    body = struct.pack("<BBBII", cmd, CMD_LEN, mode, addr, length)
    body += bytes([checksum(mode, body)])
    return hgic_hdr(HGIC_HDR_TYPE_BOOTDL, 8 + len(body), cookie) + body


def data_frame(frag: bytes) -> bytes:
    """构造数据帧：hdr(type=15) + 12 零字节命令头 + 分片。"""
    payload = b"\x00" * CMD_LEN + frag
    return hgic_hdr(HGIC_HDR_TYPE_BOOTDL_DATA, 8 + len(payload)) + payload


def parse_resp(frame: bytes, mode: int = CHECK_0XFD):
    """解析响应帧，返回 (cmd, rsp, rsp_data)。校验失败抛异常。"""
    if len(frame) < 16:
        raise ValueError(f"响应过短: {len(frame)} 字节")
    magic, typ, _, length, cookie = struct.unpack("<HBBHH", frame[:8])
    if magic != HGIC_HDR_TX_MAGIC:
        raise ValueError(f"响应 magic 错误: {magic:#06x}")
    cmd, rsp = frame[8], frame[9]
    rsp_data = struct.unpack("<I", frame[10:14])[0]
    check = frame[15]
    if check != checksum(mode, frame[8:16]):
        raise ValueError(f"响应校验错误: 期望 {checksum(mode, frame[8:16]):#04x}, 实际 {check:#04x}")
    return cmd, rsp, rsp_data


def parse_fwinfo(image: bytes) -> dict:
    """从 pack_fw.py 打包的固件解析下载参数（对应驱动 hgic_bootdl_parse_fw）。"""
    if len(image) < 137 or struct.unpack("<H", image[:2])[0] != 0x5A69:
        raise ValueError("不是 fwinfo 格式的打包固件（缺少 0x5A69 boot 头）")
    dl_addr, run_addr = struct.unpack("<II", image[4:12])
    code_off, load_len = struct.unpack("<II", image[12:20])
    mode = struct.unpack("<H", image[26:28])[0]
    return dict(
        write_addr=dl_addr,
        run_addr=run_addr,
        hdr_len=code_off,
        fw_len=len(image) - code_off,
        aes_en=(mode >> 10) & 1,
        crc_en=(mode >> 11) & 1,
    )


# ----------------------------------------------------------------------------
# USB 传输（需要 pyusb）
# ----------------------------------------------------------------------------

class HgicDevice:
    def __init__(self, dev):
        import usb.core
        self.dev = dev
        cfg = dev.get_active_configuration()
        intf = cfg[(0, 0)]
        self.ep_in = self.ep_out = None
        for ep in intf:
            if ep.bEndpointAddress & 0x80:
                self.ep_in = ep
            else:
                self.ep_out = ep
        if not self.ep_in or not self.ep_out:
            raise IOError("未找到 bulk in/out 端点")
        self.cookie = 1

    def _xfer(self, frame: bytes, timeout: int, want_resp: bool):
        import usb.core
        buf = frame + b"\x00" * (-len(frame) % 4)   # 4 字节对齐
        self.dev.write(self.ep_out.bEndpointAddress, buf, timeout)
        if not want_resp:
            return None
        raw = self.dev.read(self.ep_in.bEndpointAddress, 512, timeout)
        return bytes(raw)

    def cmd(self, cmd: int, addr: int, length: int, timeout: int = NORMAL_TMO):
        frame = cmd_frame(cmd, addr, length, cookie=self.cookie)
        self.cookie = (self.cookie + 1) & 0xFFFF or 1
        raw = self._xfer(frame, timeout, True)
        return parse_resp(raw)

    def write_mem(self, addr: int, frag: bytes) -> int:
        """WRITE_MEM 命令 + 数据帧（返回设备 rsp）。"""
        cmd, rsp, _ = self.cmd(CMD_WRITE_MEM, addr, len(frag) + 1024)
        if rsp != 0:
            return rsp
        raw = self._xfer(data_frame(frag), WRITE_MEM_TMO, True)
        _, drsp, _ = parse_resp(raw)
        return drsp

    def enter(self):
        """ENTER 命令。返回 'boot'（下载态）或 'fw'（固件态）。"""
        frame = cmd_frame(CMD_ENTER,
                          struct.unpack("<I", BOOT_CMD_KEY[:4])[0],
                          struct.unpack("<I", BOOT_CMD_KEY[4:])[0],
                          cookie=self.cookie)
        self.cookie = (self.cookie + 1) & 0xFFFF or 1
        raw = self._xfer(frame, NORMAL_TMO, True)
        _, rsp, _ = parse_resp(raw)
        return "fw" if rsp == RSP_ERR_IN_FW else "boot"

    def run(self, write_addr: int, fw_len: int, aes_en: bool, crc_en: bool):
        flags = fw_len
        if aes_en:
            flags |= RUN_PREACT_AES_DEC
        if crc_en:
            flags |= RUN_PREACT_CRC_CHK
        flags |= RUN_PREACT_SP_CFG
        return self.cmd(CMD_RUN, write_addr, flags)


def find_device():
    import usb.core
    for pid in HGIC_PIDS:
        dev = usb.core.find(idVendor=HGIC_VID, idProduct=pid)
        if dev is not None:
            return dev
    devs = usb.core.find(find_all=True, idVendor=HGIC_VID)
    lst = list(devs)
    if len(lst) == 1:
        return lst[0]
    if not lst:
        return None
    raise IOError(f"发现多个 hgic 设备，请指定 PID: {[hex(d.idProduct) for d in lst]}")


# ----------------------------------------------------------------------------
# 命令行
# ----------------------------------------------------------------------------

def cmd_probe(args):
    import usb.core
    found = False
    for d in usb.core.find(find_all=True, idVendor=HGIC_VID):
        found = True
        try:
            prod = d.product or ""
        except Exception:
            prod = "?"
        print(f"hgic 设备: bus {d.bus} dev {d.address} "
              f"VID:{d.idVendor:#06x} PID:{d.idProduct:#06x} {prod}")
        print("  （PID 0x8400 家族 = 下载态/双态，TXW82x 应为此类）")
    if not found:
        print(f"未发现 VID {HGIC_VID:#06x} 设备。请让芯片进入下载模式：")
        print("  空片自动进入；或按数据手册把指定数据脚短接 GND 后上电/复位")
    return 0


def cmd_ramboot(args):
    image = open(args.file, "rb").read()
    info = parse_fwinfo(image)
    print(f"固件: {args.file} ({len(image)} 字节)")
    print(f"  下载地址 {info['write_addr']:#010x}  代码区偏移 {info['hdr_len']:#x}"
          f"  长度 {info['fw_len']}  AES={info['aes_en']} CRC={info['crc_en']}")

    dev = find_device()
    if dev is None:
        print("错误: 未找到 hgic USB 设备（VID 0xA012）。先进入下载模式再运行。")
        return 1
    if dev.is_kernel_driver_active(0):
        dev.detach_kernel_driver(0)   # macOS 通常无需
    d = HgicDevice(dev)

    state = d.enter()
    print(f"ENTER -> {state} 态")
    if state != "boot":
        print("设备已在固件态运行（芯片 flash 有程序且未进下载模式）。"
              "如需强制下载态，请按手册短接数据脚到 GND 后复位。")
        return 2

    payload = image[info["hdr_len"]:]
    addr = info["write_addr"]
    sent = 0
    t0 = time.time()
    for off in range(0, len(payload), FRAG_SIZE):
        frag = payload[off:off + FRAG_SIZE]
        rsp = d.write_mem(addr + off, frag)
        if rsp != 0:
            print(f"\n错误: WRITE_MEM @ {addr + off:#x} 失败, rsp={rsp}")
            return 3
        sent += len(frag)
        pct = sent * 100 // len(payload)
        print(f"\r下载中 {pct:3d}%  ({sent}/{len(payload)} 字节)", end="")
    dt = time.time() - t0
    print(f"\n下载完成 ({dt:.1f}s, {sent / dt / 1024:.0f} KB/s)")

    print("RUN ...")
    _, rsp, _ = d.run(info["write_addr"], info["fw_len"],
                      bool(info["aes_en"]), bool(info["crc_en"]))
    print(f"RUN rsp={rsp} —— 芯片应已启动固件并重新枚举 USB")
    return 0


def cmd_selftest(args):
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(f"  [{'OK' if cond else 'FAIL'}] {name} {detail}")
        ok = ok and cond

    f = cmd_frame(CMD_ENTER,
                  struct.unpack("<I", BOOT_CMD_KEY[:4])[0],
                  struct.unpack("<I", BOOT_CMD_KEY[4:])[0])
    check("命令帧长度 = 20", len(f) == 20, f"({len(f)})")
    check("magic = 0x1A2B", struct.unpack("<H", f[:2])[0] == 0x1A2B)
    check("type = BOOTDL(7)", f[2] == 7)
    check("length 字段 = 20", struct.unpack("<H", f[4:6])[0] == 20)
    check("ENTER 含 @huge-ic", b"@huge-ic" in f)
    check("check = 0xFD", f[-1] == 0xFD)

    df = data_frame(b"\xAA" * 16)
    check("数据帧 type = BOOTDL_DATA(15)", df[2] == 15)
    check("数据帧 = 8+12+16 字节", len(df) == 8 + 12 + 16)

    # 构造一个模拟响应并解析
    body = struct.pack("<BBIBB", 0x00, 0x00, 0x8401, 0, 0xFD)
    resp = hgic_hdr(HGIC_HDR_TYPE_BOOTDL, 16, 1) + body
    cmd, rsp, data = parse_resp(resp)
    check("响应解析 cmd/rsp/chipid", (cmd, rsp, data) == (0, 0, 0x8401))

    # 打包固件参数提取（用 pack_fw 生成一个真实镜像）
    import tempfile, subprocess
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from pack_fw import SAMPLE_INI
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "app.bin").write_bytes(bytes(range(256)) * 16)
        (td / "txw82xcore.bin").write_bytes(b"\x90" * 4096)
        (td / "fw.ini").write_text(SAMPLE_INI.replace("CodeFile=app.hex", "CodeFile=app.bin"))
        subprocess.run([sys.executable, str(Path(__file__).parent / "pack_fw.py"),
                        "pack", "-c", str(td / "fw.ini")],
                       capture_output=True, check=True)
        img = (td / "firmware.bin").read_bytes()
        info = parse_fwinfo(img)
        check("fwinfo 提取 write_addr=0x10000000", info["write_addr"] == 0x10000000,
              f"({info['write_addr']:#x})")
        check("fwinfo 提取 hdr_len=137", info["hdr_len"] == 137)
        check("fwinfo 提取 fw_len = 文件-137", info["fw_len"] == len(img) - 137)

    print("\n自检:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main():
    import argparse
    ap = argparse.ArgumentParser(description="hgic USB-BOOT 下载工具 (macOS/Linux)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe")
    sub.add_parser("selftest")
    p = sub.add_parser("ramboot")
    p.add_argument("file", help="pack_fw.py 打包的固件 .bin")
    args = ap.parse_args()
    return {"probe": cmd_probe, "ramboot": cmd_ramboot,
            "selftest": cmd_selftest}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main() or 0)
