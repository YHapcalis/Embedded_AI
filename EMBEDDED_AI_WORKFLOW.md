# EMBEDDED_AI_WORKFLOW.md — 嵌入式 AI 开发工作流 · 自引导蓝图

> **本文件是一份"自引导蓝图"（Self-Bootstrapping Blueprint）**
> 用法：将此文件连同整个工作流包（foundation/ + skills/ + 模板）交给任意 AI 编程工具，
> AI 应能独立解读并**继续完善/使用**这套嵌入式 AI 开发工作流，无需额外对话上下文。
>
> 版本：v2.0 ｜ 更新：2026-08-28 ｜ 作者：Ryumeido Mei（余豪）
> v2.0 变更：对齐当前实现（foundation 目录 / 12 步流程 / 技能包 / 团队编排）

---

## 0. 给 AI 的第一指令（请先读这段）

你是嵌入式 AI 开发工作流的**使用者/维护者**。这套工作流已经搭建完成（v2.0），
你的任务是理解它、按流程使用它、并在必要时完善它。

**你的任务**：
1. 通读本文件 + `STANDARD_PROCESS.md`（12 步流程，执行的唯一依据）
2. 查看 `实现计划.md` 了解目标与任务状态
3. 使用 `foundation/` 的 CLI 和 MCP 服务操作开发板
4. 按 `skills/` 技能包执行硬件诊断
5. 如需新建项目，用 `foundation/templates/` 生成骨架

**执行原则**：
1. 涉及硬件操作/烧录/真实板卡时，先向用户确认
2. 每个任务完成前，先读项目的 `PROJECT_CONTEXT.md`（若有）
3. 任务完成后，按 `delivery-report.md` 模板交付，并更新上下文文档
4. 诚实原则：工具不可用如实报告，不假装成功

---

## 一、项目愿景

让嵌入式开发（STM32/ARM MCU 为主，可扩展 RISC-V/ESP32）具备 **AI 全自动闭环能力**：

```
需求 → 架构 → 骨架 → 编码 → 编译烧录 → 运行验证 → 诊断修复 → 交付
        ↑____________________ 失败自动迭代 ____________________↓
```

核心能力：AI 通过调试接口（SWD/JTAG）**直接监控和操作真实硬件**，
并在失败时自动诊断根因、修复代码、重新验证，形成闭环。

---

## 二、标准流程（12 步，详见 STANDARD_PROCESS.md）

```
准备（AI 独立）：
  ① 确认目标芯片 + 环境检查（验收门）
  ② 技能包 + MCP + 编码规范
  ③ 项目上下文 + 原理图
  ④ 拉起 AI 团队（主理人 + 专业成员）
规划（人类敲定）：
  ⑤ 理解需求
  ⑥ 风险与约束识别
  ⑦ 目标定义 + 验收标准（★人敲定）
  ⑧ 任务拆解（映射验收标准）
执行（AI 团队 + 人类兜底）：
  ⑨ 团队干活（主理人管理）
  ⑩ 自主验收（断言可量化 + 终止条件）
交付：
  ⑪ 交付文档
  ⑫ 更新交接文档（闭环回③）

安全横切面（贯穿全程）：
  S1 文件白名单 ｜ S2 硬件操作上限(15次) ｜ S3 HardFault 检测 ｜ S4 诚实原则
```

---

## 三、目录结构（当前实现）

```
<workflow_root>/                     ← 工作流包根目录
├── STANDARD_PROCESS.md              ← 12 步流程（执行依据）✅
├── 实现计划.md                      ← 目标 + 任务拆解 + 状态 ✅
├── EMBEDDED_AI_WORKFLOW.md          ← 本文件（蓝图）
├── foundation/                      ← 引擎（"工作流地基"）
│   ├── __main__.py                  ← python -m foundation
│   ├── cli/main.py                  ← CLI 入口（env/openocd/chip-confirm）
│   ├── mcpservice/server.py         ← MCP 服务（8 工具，AI 工具接入）
│   ├── core/
│   │   ├── env_probe.py             ← 环境探测（跨平台）
│   │   ├── openocd_registry.py      ← OpenOCD 多分支管理
│   │   ├── team_orchestrator.py     ← AI 团队编排（主理人+成员）
│   │   └── （待建）session.py / diagnostics.py / flasher.py / tester.py
│   └── templates/
│       ├── project-context.md       ← 项目上下文模板（③⑫闭环）
│       ├── coding-standard.md       ← 编码规范模板
│       └── delivery-report.md       ← 交付文档模板（⑪）
├── skills/                          ← 技能包知识库（内置分发）
│   ├── SKILL.md                     ← 路由入口
│   ├── diagnosis-*.md               ← 诊断技能（hardfault/hang/memory/peripheral/clock）
│   ├── probe-workflow.md            ← 芯片探测
│   ├── register-decode.md           ← 寄存器解读
│   ├── elf-workflow.md              ← ELF 源码定位
│   ├── chip-reference.md            ← 芯片 ID 速查
│   └── svd/                         ← SVD 描述文件（arm/STM32F407xx.svd）
└── .gitignore
```

---

## 四、核心能力

### 4.1 CLI（foundation）

```
python -m foundation env check            # 环境自检（openocd/gcc/stlink/python/git）
python -m foundation openocd list         # OpenOCD 多分支查看（st/esp32/mainline）
python -m foundation chip-confirm --mcu stm32f407 --arch cortex_m4   # 确认芯片+映射环境
```

### 4.2 MCP 服务（跨工具接入）

```bash
claude mcp add foundation-ai -- python E:/嵌入式AI工作流/foundation/mcpservice/server.py
```

8 个工具：`env_check / openocd_list / chip_identify / halt / resume /
register_read / memory_read / diagnose`

### 4.3 AI 团队编排（team_orchestrator.py）

```python
from core.team_orchestrator import TeamOrchestrator
team = TeamOrchestrator()
team.set_lead()
team.add_member("driver", ["Core/Src/can.c"], "CAN 驱动")
task = team.assign("实现 CAN 电量解析", "driver", acceptance=["编译通过"])
team.collect(task.id, "完成，编译通过")
team.review(task.id)   # 主理人审查
```

### 4.4 多 MCU 适配（OpenOCD Registry + 双协议）

- OpenOCD 多分支共存：STM32 用 ST 分支，ESP32 用 Espressif 分支，通用用主线
- 接口不变，协议（SWD/JTAG）由板卡 Profile 决定 → 换芯片只加适配文件

---

## 五、使用指南

### 5.1 新项目流程

1. 复制 `foundation/templates/project-context.md` 为 `PROJECT_CONTEXT.md` 并填写
2. 按 STANDARD_PROCESS ② 复制 coding-standard.md 为 `CODING_STANDARD.md`
3. 确认目标芯片：`python -m foundation chip-confirm`
4. 环境检查：`python -m foundation env check`
5. 按 12 步流程执行开发

### 5.2 接入新 AI 工具

1. 注册 MCP（见 4.2）
2. 把 `skills/SKILL.md` 复制到工具的 skills 目录
3. 告诉 AI "按 STANDARD_PROCESS.md 执行"

### 5.3 换 MCU（新增板卡）

1. 新增 `boards/<name>/profile.json`（mcu/arch/toolchain/transport/openocd/flash_sections）
2. 提供对应 openocd.cfg
3. 引擎零改动（S9 验收项）

---

## 六、安全规则（AI 必须遵守）

1. **硬件操作安全阀**：烧录/复位/写寄存器前先确认；操作次数上限 15 次/会话
2. **文件白名单**：只能修改任务允许的文件（team_orchestrator 强制）
3. **HardFault 检测**：每次硬件操作后检查，异常立即停止并诊断
4. **诚实原则**：工具不可用时如实报告，不假装执行成功
5. **协议合规**：技能包改编自 AixProbe（CC BY-NC-SA 4.0），不可商用

---

## 七、当前状态与待完善

| 组件 | 状态 |
|------|------|
| 12 步流程文档 | ✅ v2.0 |
| CLI（env/openocd/chip-confirm） | ✅ |
| MCP 服务（8 工具） | ✅ |
| 技能包知识库（10 文件 + SVD） | ✅ |
| 团队编排 | ✅ |
| 模板（上下文/编码规范/交付） | ✅ |
| 调试会话/诊断引擎/烧录/断言 | ⬜ 待建（Phase 1） |
| 全量验收 S1-S10 | ⬜ 待建（Phase 6） |

---

*本蓝图随项目演进持续更新。任何变更须同步修订 STANDARD_PROCESS.md 与实现计划.md。*
