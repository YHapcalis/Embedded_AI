# Embedded AI Harness — STM32 全自动 AI 闭环验证框架

面向 STM32 固件的 AI 自主迭代闭环框架。每一轮迭代自动执行 **编译 → 烧录 → 监控 → 断言 → AI 分析 → 修改代码 → 再验证**，直到断言全部通过。通过 SWD/OpenOCD 直接读写真硬件寄存器与内存，可模拟人工按键、旋钮、触摸等操作，替代手工测试。

> 对应目标硬件: STM32F407ZGT6 (MY_OTA_GUI / MY_Car_GUI / Bate_Camera)

## 核心特性

| 特性 | 说明 |
|------|------|
| 🔁 全自动闭环 | AI 分析硬件反馈并修改固件代码，断言全部通过才停止 |
| 🔌 真硬件 SWD | OpenOCD TCL 协议读写芯片寄存器/内存，无需物理操作硬件 |
| 🖐 模拟人工操作 | `halt → resume [addr]` 调用固件函数、翻页、模拟触摸/旋钮/OTA |
| 📏 8 种断言检查 | `frequency` / `range` / `monotonic` / `rate` / `change_detected` / `pattern` / `state_machine` / `stable_after` |
| 📜 场景驱动 | YAML 定义监控目标、断言与可修改文件白名单，按场景隔离 AI 权限 |
| 🖥 双后端混合 | Renode 仿真 LCD + SWD 真硬件 CAN，带三级熔断自动降级 |
| 🤖 AI 子代理 | 主模型决策、子代理并行执行多文件修改，完全隔离 |
| 🛡 策略门控 | 文件白名单 / 内容安全 / ELF 符号校验，拦截 AI 破坏性修改 |
| 🧠 分层记忆 | 短期 JSONL + 长期 SQLite，跨场景经验自动注入 AI 上下文 |
| 🔧 OTA 双区支持 | 自动检测 bootloader，BL + APP + 签名 + 参数 4 段烧录 |

## 环境要求

- Python 3.10+
- OpenOCD（STM32CubeIDE 自带，或独立安装）
- `arm-none-eabi-gdb`
- AI 决策后端：`pip install anthropic`，设置 `ANTHROPIC_API_KEY` 环境变量
- （可选）Renode 仿真器，用于 LCD 像素级仿真后端

## 快速开始

```bash
# 1. 环境自检（检查工程路径 / OpenOCD / GDB / TCL 端口）
python examples/demo.py check-env

# 2. 交互模式：手动给 AI 反馈（LED 2Hz 调参示例）
python examples/demo.py led-blink

# 3. 自动闭环：从场景 YAML 定义监控目标与断言，AI 全自动迭代
python embed_harness.py --backend swd \
    --dir E:/.../MY_OTA_GUI \
    --elf build/Debug/MY_OTA_GUI.elf \
    --scenario scenarios/full_monitor.yaml \
    --iterations 10 --skip-build

# 4. 纯监控模式（不修改代码、不调用 AI）
python embed_harness.py --backend swd --dir ... \
    --scenario scenarios/full_monitor.yaml \
    --monitor-only --no-ai --export-dashboard ./traces

# 5. 启动 Web Dashboard 查看轨迹
python -m streamlit run harness_dashboard.py
```

`--skip-build` / `--monitor-only` 可跳过编译/烧录，直接连已有的 OpenOCD 采样——非常适合调试阶段反复验证。

## 目录结构

```
harness_ai/
├── embed_harness.py        # 主编排器: 编译→烧录→监控→断言→AI→策略门控→记忆
├── ai_backend.py           # AI 决策后端 (Claude, 子代理, 思考模式)
├── monitor_client.py       # SWD/OpenOCD 客户端 + SWDInputSimulator (翻页/触摸/函数调用)
├── expectations.py         # 断言引擎 (8 种检查)
├── policy.py               # 策略门控 (文件白名单, 内容安全, ELF 检查)
├── memory.py               # 分层记忆 (短期 JSONL + 长期 SQLite)
├── feedback.py             # 结构化反馈 + TokenBudget 压缩
├── hybrid_backend.py       # 混合后端 (Renode + SWD) + CircuitBreaker 熔断
├── ui.py / harness_dashboard.py  # 终端 UI / Streamlit Web 面板
├── scenarios/              # 9 个场景 YAML (led_blink / can_comm / pid_tuning ...)
├── renode/                 # Renode 外设模型 (NT35510.cs) 与客户端
├── scripts/can_ota_send.py # CAN ISO-TP OTA 固件发送工具
├── examples/demo.py        # 三种使用模式示例
└── docs/                   # 技术文档
```

## 文档

- [docs/技术使用指南.md](docs/技术使用指南.md) — 完整技术使用指南（架构 / 场景 / 断言 / AI / SWD / 门控 / CLI 全参考）
- [docs/AI模拟人工操作硬件原理.md](docs/AI模拟人工操作硬件原理.md) — SWD 模拟人工操作原理（函数调用协议、翻页、触摸）
- [docs/工程推进记录.md](docs/工程推进记录.md) — 架构总览与踩坑记录

## 快速上手写一个场景

在 `scenarios/` 下新建 YAML，定义监控目标与断言：

```yaml
name: "LED 闪烁频率调优"
targets:
  - variable: "GPIOA_ODR"      # GDB 符号或内存变量
    check: frequency
    params: { target_hz: 2.0, tolerance: 0.15 }
  - variable: "GPIOA_ODR"
    check: change_detected
source_files: ["Core/Src/main.c"]   # AI 仅可修改白名单内的文件
```

然后 `python embed_harness.py --scenario scenarios/xxx.yaml ...` 即可启动闭环。
