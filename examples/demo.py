#!/usr/bin/env python3
"""
demo.py — Harness 使用示例

展示了三种使用模式，基于你的 F407 MY_Car_GUI 工程。

用法:
  python examples/demo.py check-env     # 检查环境配置
  python examples/demo.py led-blink     # 交互模式：LED 闪烁调参
  python examples/demo.py can-comm      # 交互模式：CAN 通信验证
"""

import sys
import subprocess
from pathlib import Path

# 把 harness 根目录加入 path
_HERE = Path(__file__).parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from embed_harness import AIEmbedHarness, HarnessConfig


# ─── 你的工程路径（按实际修改） ─────────────────────────────────────

F407_DIR = "E:/ST/STM32/MY_workspace/Projects_F407/MY_Car_GUI"
F407_ELF = "E:/ST/STM32/MY_workspace/Projects_F407/MY_Car_GUI/build/Debug/MY_Car_GUI.elf"
F103_DIR = "E:/ST/STM32/MY_workspace/Projects_F103/MY_OTA_GUI_F103"
F103_ELF = "E:/ST/STM32/MY_workspace/Projects_F103/MY_OTA_GUI_F103/build/Debug/MY_OTA_GUI_F103.elf"


def check_environment():
    """检查 Harness 运行所需的外部依赖是否就绪"""
    print("=== 环境检查 ===\n")

    # 1. 工程目录
    for label, path in [("F407 工程", F407_DIR), ("F103 工程", F103_DIR)]:
        exists = Path(path).exists()
        print(f"  {label}: {'✅' if exists else '❌'} {path}")

    # 2. OpenOCD
    ocd = HarnessConfig.OPENOCD_EXE
    exists = Path(ocd).exists()
    print(f"  OpenOCD:  {'✅' if exists else '❌'} {ocd}")

    # 3. GDB
    try:
        r = subprocess.run(["arm-none-eabi-gdb", "--version"],
                          capture_output=True, text=True, timeout=5)
        print(f"  GDB:      ✅ {r.stdout.splitlines()[0]}")
    except FileNotFoundError:
        print(f"  GDB:      ❌ arm-none-eabi-gdb 不在 PATH 中")

    # 4. OpenOCD TCL 端口
    from monitor_client import OpenOCDClient
    client = OpenOCDClient()
    if client.is_connected():
        print(f"  OpenOCD TCL: ✅ 端口 6666 可连接")
    else:
        print(f"  OpenOCD TCL: [WARN] 端口 6666 未连接（F5 调试或启动 OpenOCD 后再试）")

    # 5. stm32_monitor.py
    script = Path(HarnessConfig.MONITOR_SCRIPT)
    print(f"  Monitor 脚本: {'✅' if script.exists() else '❌'} {script}")

    print("\n完成。")


def led_blink_demo():
    """LED 闪烁频率调参（交互模式）"""
    print("=== LED 闪烁调参 Demo ===\n")

    # 先快速检查环境
    client = __import__("monitor_client", fromlist=["OpenOCDClient"]).OpenOCDClient()
    if not client.is_connected():
        print("[WARN] OpenOCD 未连接。请先按 F5 启动调试（或独立启动 OpenOCD）。")
        return

    # 创建 Harness 实例
    harness = AIEmbedHarness(
        project_dir=F407_DIR,
        elf_path=F407_ELF,
    )

    # 执行交互式迭代
    harness.interactive_mode(
        variables=["GPIOA_ODR"],
        expectations=[
            {"var": "GPIOA_ODR", "check": "frequency",
             "params": {"target_hz": 2.0, "tolerance": 0.15}},
            {"var": "GPIOA_ODR", "check": "change_detected", "params": {}},
        ],
        scenario_desc="让 PA5 LED 以 2Hz 频率闪烁（周期 500ms）",
    )


def can_comm_demo():
    """CAN 通信验证（交互模式）"""
    print("=== CAN 通信验证 Demo ===\n")

    client = __import__("monitor_client", fromlist=["OpenOCDClient"]).OpenOCDClient()
    if not client.is_connected():
        print("[WARN] OpenOCD 未连接。请先按 F5 启动调试。")
        return

    harness = AIEmbedHarness(
        project_dir=F407_DIR,
        elf_path=F407_ELF,
    )

    harness.interactive_mode(
        variables=["CAN_RxMsg.Data[0]", "hcan1.ErrorCode", "hcan1.State"],
        expectations=[
            {"var": "CAN_RxMsg.Data[0]", "check": "change_detected", "params": {}},
            {"var": "hcan1.ErrorCode", "check": "range",
             "params": {"min": 0, "max": 0}},
        ],
        scenario_desc="验证 CAN 总线通信：有数据到达且无错误",
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python examples/demo.py [check-env|led-blink|can-comm]")
        sys.exit(1)

    command = sys.argv[1]
    if command == "check-env":
        check_environment()
    elif command == "led-blink":
        led_blink_demo()
    elif command == "can-comm":
        can_comm_demo()
    else:
        print(f"未知命令: {command}")
        print("可用: check-env, led-blink, can-comm")
