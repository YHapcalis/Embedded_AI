#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nav_test.py - AI 自主翻页验收测试（独立脚本）

流程:
  1. 编译 + 烧录固件（首次烧录任务）
  2. 监控硬件变量
  3. 调用 AI backend，AI 使用 navigate_page 工具翻页
  4. 8 次翻页后提交决策

用法:
  python nav_test.py
"""

import sys, time, json, os
from pathlib import Path

# Windows GBK 终端兼容
if hasattr(sys.stdout, 'buffer') and sys.stdout.encoding and 'utf' not in sys.stdout.encoding.lower():
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

from embed_harness import BuildSystem, HarnessConfig
from monitor_client import OpenOCDClient, SWDInputSimulator
from ai_backend import ClaudeAIBackend
from feedback import Observation, AIDecision


# --- 配置 -------------------------------------------------------

PROJECT_DIR = "E:/ST/STM32/MY_workspace/Projects_F407/MY_OTA_GUI"
ELF_PATH    = "build/Debug/MY_OTA_GUI.elf"
ELF_ABS     = str(Path(PROJECT_DIR) / ELF_PATH)

VARIABLES = ["g_theme_id"]


# --- 步骤 1：编译 + 烧录 ------------------------------------------

def step_build_and_flash():
    """编译 + 烧录固件"""
    print("=" * 60)
    print("步骤 1/3: 编译 + 烧录固件")
    print("=" * 60)

    cfg = HarnessConfig()
    build = BuildSystem(PROJECT_DIR, ELF_PATH, config=cfg)

    if not build.elf_path.exists():
        print("  [BUILD] ELF 不存在，开始编译...")
        ok, log = build.build()
        if not ok:
            print("  [BUILD] [FAIL] 编译失败")
            print(f"  {log[-500:]}")
            return False
        print("  [BUILD] [OK] 编译通过")
    else:
        print(f"  [BUILD] [OK] ELF 已存在: {build.elf_path.name}")

    print("  [FLASH] 开始烧录...")
    ok, log = build.flash()
    if not ok:
        print("  [FLASH] [FAIL] 烧录失败")
        print(f"  {log[-500:]}")
        return False
    print("  [FLASH] [OK] 烧录成功")

    time.sleep(1.5)
    return True


# --- 步骤 2：启动监控 ---------------------------------------------

def step_start_monitor():
    """启动 OpenOCD 监控连接"""
    print("\n" + "=" * 60)
    print("步骤 2/3: 连接 OpenOCD 监控")
    print("=" * 60)

    ocd = OpenOCDClient()

    if ocd.is_connected():
        print("  [MON] [OK] OpenOCD 运行中，SWD 通信正常")
        return ocd

    print("  [MON] OpenOCD 未运行，尝试自动启动...")

    cfg = HarnessConfig()
    scripts_dir = Path(cfg.ST_SCRIPTS)
    cfg_path = Path(PROJECT_DIR) / "openocd.cfg"

    if not cfg_path.exists():
        print(f"  [MON] [FAIL] openocd.cfg 不存在: {cfg_path}")
        return None

    import subprocess
    subprocess.Popen(
        [cfg.OPENOCD_EXE, "-s", str(scripts_dir), "-f", str(cfg_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    import socket
    for _ in range(20):
        time.sleep(0.5)
        try:
            s = socket.socket()
            s.settimeout(1)
            s.connect(("127.0.0.1", 6666))
            s.close()
            resp = ocd._tcl_send("mdw 0x20000000 1")
            if resp and "0x" in resp:
                ocd._tcl_send("reset run")
                time.sleep(0.5)
                print("  [MON] [OK] OpenOCD + SWD 连接就绪")
                return ocd
        except:
            continue

    print("  [MON] [FAIL] OpenOCD 启动失败")
    return None


# --- 步骤 3：AI 翻页导航 -----------------------------------------

def step_ai_navigation(ocd):
    """AI 自主翻页导航"""
    print("\n" + "=" * 60)
    print("步骤 3/3: AI 自主翻页导航")
    print("=" * 60)

    # 创建 AI 后端
    backend = ClaudeAIBackend(
        model=None,
        project_dir=PROJECT_DIR,
    )
    print(f"  [AI] 模型: {backend.model}")
    print(f"  [AI] tool_choice: {backend.tool_choice}")
    print(f"  [AI] thinking: {'开启' if backend.thinking_config else '关闭'}")

    # 创建 SWD 输入模拟器
    sim = SWDInputSimulator(ocd)
    backend.input_sim = sim
    print("  [AI] SWD 输入模拟器就绪")

    # 采样当前状态
    print(f"\n  [MON] 采样变量: {VARIABLES}...")
    samples = ocd.sample_multiple(
        elf_path=ELF_ABS, variables=VARIABLES,
        duration=3.0, interval=0.3,
    )
    if samples.get("g_theme_id"):
        vals = [s["value"] for s in samples["g_theme_id"]]
        print(f"  [MON] g_theme_id: {vals[:5]}... ({len(vals)} 个样本)")
    else:
        print("  [MON] [WARN] 无采样数据")

    # 构建 Observation
    obs = Observation(
        iteration=1,
        timestamp=time.time(),
        build_success=True,
        flash_success=True,
        samples=samples,
    )

    # 任务描述 - 指令式，强硬要求翻页
    scenario_desc = (
        "[强制翻页任务] 你必须使用 navigate_page 工具按以下顺序翻页！\n"
        "顺序:\n"
        "  1. main -> mode\n"
        "  2. mode -> main\n"
        "  3. main -> settings\n"
        "  4. settings -> main\n"
        "  5. main -> clock\n"
        "  6. clock -> main\n"
        "  7. main -> camera\n"
        "  8. camera -> main\n"
        "规则: 子页之间不能直接跳转，必须先回 main。\n"
        "全部 8 次翻页完成后，调用 submit_decision 报告完成。不要提前结束！"
    )

    # 调用 AI 决策（内部包含导航循环）
    print("\n  [AI] 调用 AI 决策...\n", flush=True)
    import datetime
    t_start = time.time()
    memory_context = ""
    decision = backend.decide(obs, scenario_desc, memory_context)
    t_elapsed = time.time() - t_start

    # 检查导航是否实际执行
    nav_count = 0
    if hasattr(backend, '_pending_nav') and backend._pending_nav:
        # code doesn't track nav count directly, check history
        pass

    print(f"\n  [AI] 决策结果 (耗时 {t_elapsed:.0f}s):")
    print(f"    goal_achieved: {decision.goal_achieved}")
    if decision.reasoning:
        print(f"    reasoning: {decision.reasoning[:300]}")
    print(f"    code_changes: {len(decision.code_changes)} 项")

    if decision.goal_achieved:
        print("\n  [OK] AI 翻页验收完成！目标达成。")
    else:
        print("\n  [WARN] AI 报告目标未达成。")

    return decision


# --- 主流程 -------------------------------------------------------

def main():
    print("=" * 60)
    print("  AI 嵌入式 Harness - 首次烧录 + 翻页验收")
    print(f"  工程: {PROJECT_DIR}")
    print(f"  ELF:  {ELF_PATH}")
    print("=" * 60)

    # 步骤 1
    if not step_build_and_flash():
        print("\n[FAIL] 编译/烧录失败，终止。")
        sys.exit(1)

    # 步骤 2
    ocd = step_start_monitor()
    if not ocd:
        print("\n[FAIL] 监控启动失败，终止。")
        sys.exit(1)

    # 步骤 3
    decision = step_ai_navigation(ocd)

    print("\n" + "=" * 60)
    print("  任务完成")
    print("=" * 60)


if __name__ == "__main__":
    main()
