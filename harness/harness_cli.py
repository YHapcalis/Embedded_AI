"""
harness_cli.py — harness 统一 CLI 入口

封装嵌入式 AI 工作流的全部命令，供人类或 AI 调用。
对应 STANDARD_PROCESS 准备阶段①（确认芯片 + 环境检查）。

用法:
    python -m harness env check          # 环境自检
    python -m harness openocd list       # 查看 OpenOCD 多分支
    python -m harness chip-confirm       # 确认目标芯片类型
    python -m harness --help             # 全部命令

设计原则:
    - 每个子命令 = 流程中的一步（映射 STANDARD_PROCESS）
    - 输出人类可读 + 结构化（JSON 可选，供 AI 解析）
    - 失败不假装成功（诚实原则）
"""

import argparse
import json
import sys
from pathlib import Path

# 允许直接运行脚本时也能找到 core 包
sys.path.insert(0, str(Path(__file__).parent))

from core.env_probe import EnvironmentProbe
from core.openocd_registry import OpenOCDRegistry


# ─── 子命令实现 ─────────────────────────────────────────────

def cmd_env_check(args):
    """环境自检（对应流程①第 2-4 步）"""
    probe = EnvironmentProbe()
    results = probe.check_all()

    if args.json:
        out = {name: {"found": r.found, "version": r.version,
                      "path": r.path, "details": r.details}
               for name, r in results.items()}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if all(r.found for r in results.values()) else 1

    print(probe.summary_text())
    ok = all(r.found for r in results.values())
    print("")
    print("=== 结论 ===")
    print(f"环境检查: {'全部通过 ✅' if ok else '存在缺失 ⚠️'}")
    if not ok:
        print("提示: 缺失组件需补全后才能进入下一步（不假装成功）")
    return 0 if ok else 1


def cmd_openocd_list(args):
    """查看 OpenOCD 多分支注册表（对应流程①第 3 步）"""
    reg = OpenOCDRegistry()
    if args.json:
        out = [{"name": e.name, "path": e.exe_path, "version": e.version,
                "tags": e.tags, "source": e.source}
               for e in reg.list()]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out else 1
    print(reg.summary_text())
    return 0 if reg.entries else 1


def cmd_chip_confirm(args):
    """
    确认目标芯片类型（对应流程①第 1 步）。

    交互式确认芯片型号/架构/调试接口，并映射所需开发环境。
    非交互模式: --mcu stm32f407 --arch cortex_m4 --transport swd
    """
    if args.mcu:
        # 非交互模式：直接按参数构建
        chip = {"mcu": args.mcu, "arch": args.arch,
                "transport": args.transport or _guess_transport(args.arch)}
    else:
        # 交互模式：询问
        chip = {}
        print("=== 确认目标芯片 ===")
        chip["mcu"] = input("  MCU/SoC 型号 (如 stm32f407zgt6): ").strip()
        chip["arch"] = input("  内核架构 (cortex_m4/riscv/xtensa): ").strip().lower()
        chip["transport"] = input(f"  调试接口 (回车默认 {_guess_transport(chip['arch'])}): ").strip() \
            or _guess_transport(chip["arch"])

    # 映射所需开发环境
    env_map = _map_environment(chip)
    chip["required_env"] = env_map

    if args.json:
        print(json.dumps(chip, ensure_ascii=False, indent=2))
        return 0

    print("")
    print("=== 目标芯片确认 ===")
    print(f"  MCU:       {chip['mcu']}")
    print(f"  架构:      {chip['arch']}")
    print(f"  调试接口:  {chip['transport']}")
    print("")
    print("=== 所需开发环境 ===")
    for k, v in env_map.items():
        print(f"  {k}: {v}")
    print("")
    print("下一步: 检查这些组件是否已就位 → harness env check")
    return 0


# ─── 辅助函数 ────────────────────────────────────────────────

def _guess_transport(arch: str) -> str:
    """按架构猜测调试接口"""
    arch = (arch or "").lower()
    if "cortex" in arch or "arm" in arch:
        return "swd"
    return "jtag"  # riscv / xtensa / 其他 → JTAG


def _map_environment(chip: dict) -> dict:
    """芯片类型 → 所需开发环境映射（对应 STANDARD_PROCESS ① 映射表）"""
    arch = (chip.get("arch") or "").lower()
    mcu = (chip.get("mcu") or "").lower()

    if "esp32" in mcu and "c" not in mcu and "h" not in mcu and "p" not in mcu:
        # ESP32/S2/S3 → Xtensa
        return {
            "openocd_branch": "esp32 (Espressif 分支)",
            "compiler": "xtensa-esp32-elf-gcc",
            "protocol": "jtag",
        }
    if "esp32" in mcu or "esp" in mcu:
        # ESP32-C/H/P → RISC-V
        return {
            "openocd_branch": "esp32 (Espressif 分支)",
            "compiler": "riscv32-esp-elf-gcc",
            "protocol": "jtag",
        }
    if "riscv" in arch or "rv32" in arch or "ch32v" in mcu:
        return {
            "openocd_branch": "mainline (官方主线)",
            "compiler": "riscv64-unknown-elf-gcc",
            "protocol": "jtag",
        }
    if "xtensa" in arch:
        return {
            "openocd_branch": "esp32 (Espressif 分支)",
            "compiler": "xtensa-esp32-elf-gcc",
            "protocol": "jtag",
        }
    # 默认: ARM Cortex
    return {
        "openocd_branch": "st (STM32CubeIDE 分支) 或 mainline",
        "compiler": "arm-none-eabi-gcc",
        "protocol": chip.get("transport") or "swd",
    }


# ─── 主入口 ─────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness",
        description="嵌入式 AI 开发工作流 — 统一命令行入口",
        epilog="示例: python -m harness env check | python -m harness chip-confirm --mcu stm32f407 --arch cortex_m4",
    )
    sub = parser.add_subparsers(dest="command", help="子命令")

    # env check
    p_env = sub.add_parser("env", help="环境检查")
    p_env.add_argument("check", nargs="?", default="check", help="子操作: check")
    p_env.add_argument("--json", action="store_true", help="JSON 输出")
    p_env.set_defaults(func=cmd_env_check)

    # openocd list
    p_ocd = sub.add_parser("openocd", help="OpenOCD 管理")
    p_ocd.add_argument("list", nargs="?", default="list", help="子操作: list")
    p_ocd.add_argument("--json", action="store_true", help="JSON 输出")
    p_ocd.set_defaults(func=cmd_openocd_list)

    # chip-confirm
    p_chip = sub.add_parser("chip-confirm", help="确认目标芯片类型")
    p_chip.add_argument("--mcu", help="MCU 型号 (非交互模式)")
    p_chip.add_argument("--arch", default="cortex_m4", help="内核架构")
    p_chip.add_argument("--transport", help="调试接口 (swd/jtag)")
    p_chip.add_argument("--json", action="store_true", help="JSON 输出")
    p_chip.set_defaults(func=cmd_chip_confirm)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if not hasattr(args, "func"):
        parser.print_help()
        return 0

    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n[harness] 已中断")
        return 130
    except Exception as e:
        print(f"[harness] 错误: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
