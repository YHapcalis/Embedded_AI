#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
can_ota_send.py — CAN ISO-TP OTA 固件发送工具

通过串口 (COM3) 将固件 .bin 发送到 F103 网关，
F103 通过 CAN ISO-TP 转发给 F407 Bootloader 完成 OTA。

用法:
  python scripts/can_ota_send.py COM3 build/Debug/MY_OTA_GUI.bin

协议:
  F103 GATEWAY_MODE 从 UART2 接收裸字节，
  每积攒 1024 字节打包成 ISO-TP 帧发送。
  空闲 500ms 后发送剩余数据，再空闲 2s 后发结束标记。
"""

import sys
import time
import serial
from pathlib import Path


# ─── 配置 ─────────────────────────────────────────────────────
BAUDRATE = 115200
CHUNK_SIZE = 1024  # 与 F103 固件 CHUNK_SIZE 一致
END_MARKER = bytes([0xBE, 0xAD, 0xBE, 0xEF, 0x02])  # 结束标记


def send_firmware(port: str, bin_path: str) -> bool:
    """
    通过串口发送固件到 F103 网关。

    Args:
        port: 串口号，如 "COM3"
        bin_path: .bin 固件文件路径

    Returns:
        True 如果发送完成
    """
    bin_file = Path(bin_path)
    if not bin_file.exists():
        print(f"[ERROR] 固件文件不存在: {bin_file}")
        return False

    fw_size = bin_file.stat().st_size
    print(f"[SEND] 固件: {bin_file.name} ({fw_size:,} bytes)")
    print(f"[SEND] 串口: {port} @ {BAUDRATE} baud")
    print(f"[SEND] 块大小: {CHUNK_SIZE} bytes")
    estimated = fw_size / (BAUDRATE / 10)  # 8N1 = 10 bits/byte
    print(f"[SEND] 预计耗时: ~{estimated:.0f}s")

    try:
        ser = serial.Serial(
            port=port,
            baudrate=BAUDRATE,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=5,
            xonxoff=False,
            rtscts=False,
        )
    except serial.SerialException as e:
        print(f"[ERROR] 串口打开失败: {e}")
        print("  请检查:")
        print("  1. COM3 是否正确（查看设备管理器）")
        print("  2. F103 网关是否已上电")
        return False

    print(f"[SEND] 串口已打开: {port}")
    print(f"[SEND] 开始发送...")

    # 读取固件
    with open(bin_path, "rb") as f:
        fw_data = f.read()

    try:
        t_start = time.time()
        sent_total = 0

        # 逐块发送
        for offset in range(0, len(fw_data), CHUNK_SIZE):
            chunk = fw_data[offset:offset + CHUNK_SIZE]
            ser.write(chunk)
            ser.flush()
            sent_total += len(chunk)

            # 进度显示
            pct = sent_total * 100 // fw_size
            elapsed = time.time() - t_start
            speed = sent_total / elapsed if elapsed > 0 else 0
            eta = (fw_size - sent_total) / speed if speed > 0 else 0

            # 每 5% 或最后一块打印进度
            if pct % 5 == 0 or offset + CHUNK_SIZE >= fw_size:
                print(f"  [SEND] {pct:3d}% | {sent_total:>8,}/{fw_size:<,} bytes | "
                      f"{speed/1024:.1f} KB/s | ETA {eta:.0f}s")
                time.sleep(0.05)  # 给 F103 处理时间

        # 发送结束标记
        print(f"  [SEND] 固件发送完毕，发送结束标记...")
        time.sleep(0.6)  # 等 F103 超时触发 flush
        ser.write(END_MARKER)
        ser.flush()

        t_elapsed = time.time() - t_start
        avg_speed = fw_size / t_elapsed / 1024
        print(f"\n[SEND] [OK] 完成! {fw_size:,} bytes / {t_elapsed:.0f}s ({avg_speed:.1f} KB/s)")

    except serial.SerialException as e:
        print(f"[ERROR] 串口通信错误: {e}")
        return False
    except KeyboardInterrupt:
        print(f"\n[CANCEL] 用户中断")
        return False
    finally:
        ser.close()

    return True


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        print("用法: python can_ota_send.py <COM口> <固件.bin>")
        print("示例: python can_ota_send.py COM3 build/Debug/MY_OTA_GUI.bin")
        sys.exit(1)

    port = sys.argv[1]
    bin_path = sys.argv[2]

    ok = send_firmware(port, bin_path)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
