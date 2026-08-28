# EMBEDDED_AI_WORKFLOW.md — 嵌入式 AI 开发工作流 · 自引导蓝图

> **本文件是一份"自引导蓝图"（Self-Bootstrapping Blueprint）**
> 用法：将此文件直接交给任意 AI 编程工具（Claude Code / Trae / Cursor / CodeBuddy / WorkBuddy 等），
> AI 应能独立解读本文件并开始搭建完整的嵌入式 AI 开发工作流，无需额外对话上下文。
>
> 版本：v1.0 ｜ 创建：2026-08-28 ｜ 作者：Ryumeido Mei（余豪）

---

## 0. 给 AI 的第一指令（请先读这段）

你是嵌入式 AI 开发工作流的**搭建者**。本文件描述了要搭建的完整系统。

**你的任务**：严格按照本文件的「四、系统规格」和「五、实施步骤」，
在当前工作区搭建一套可运行的嵌入式 AI 开发工作流。

**执行原则**：
1. 先完整读本文件，再动手；不要跳过任何章节。
2. 每完成一个阶段，验证验收标准通过后再进入下一阶段。
3. 所有产物写入当前工作区，遵循「三、目录结构」。
4. 遇到环境差异（Windows/Linux/macOS、不同调试器），按「六、跨环境适配」自动处理。
5. 用户（开发者）是唯一决策者：涉及硬件操作、烧录、真实板卡时，先向用户确认。

---

## 一、项目愿景

让嵌入式开发（STM32/ARM MCU 为主）具备 **AI 全自动闭环能力**：

```
需求 → 架构 → 骨架 → 编码 → 编译烧录 → 运行验证 → 诊断修复 → 交付
        ↑____________________ 失败自动迭代 ____________________↓
```

核心能力：AI 通过调试接口（SWD/JTAG）**直接监控和操作真实硬件**，
并在失败时自动诊断根因、修复代码、重新验证，形成闭环。

---

## 二、核心方法论（三层结构）

```
┌─────────────────────────────────────────────┐
│ 接入层：任意 AI 编程工具                       │
│ Claude Code ｜ Trae ｜ Cursor ｜ CodeBuddy ... │
└──────────┬──────────────┬──────────────────┘
           │ MCP 接入      │ 技能包加载
┌──────────▼────────┐  ┌──▼───────────────┐
│ 第1层 MCP 服务     │  │ 第3层 技能包知识   │
│ 标准工具接口       │  │ AI 诊断知识库     │
└──────────┬────────┘  └──┬───────────────┘
┌──────────▼──────────────▼───────────────┐
│ 第2层 标准脚本（唯一真逻辑，跨平台）        │
│ harness CLI：build/flash/test/diagnose    │
└─────────────────────────────────────────┘
```

- **第 2 层（标准脚本）**：唯一的业务逻辑，Python 实现，跨平台。
- **第 1 层（MCP 服务）**：把第 2 层能力暴露为标准工具，任意 AI 工具可接入。
- **第 3 层（技能包）**：把"老工程师的排查经验"写成 Markdown，指导 AI 诊断。

---

## 三、目录结构（目标形态）

```
<workflow_root>/                  ← 当前工作区
├── EMBEDDED_AI_WORKFLOW.md       ← 本文件（蓝图）
├── STANDARD_PROCESS.md           ← 标准流程（8 步，AI 执行依据）
├── harness/                      ← 第 2 层：标准脚本（核心）
│   ├── harness_cli.py            ← 统一 CLI 入口
│   ├── core/
│   │   ├── env_probe.py          ← 环境探测（openocd/gcc/stlink）
│   │   ├── config.py             ← 配置管理（.harness/config.json）
│   │   ├── builder.py            ← 编译封装
│   │   ├── flasher.py            ← 烧录封装（4 段 verify）
│   │   ├── tester.py             ← 测试场景执行
│   │   ├── diagnostics.py        ← 诊断引擎（HardFault/挂死/内存）
│   │   └── swd_session.py        ← SWD 调试会话（MCP 风格接口）
│   ├── mcp_server.py             ← 第 1 层：MCP 服务
│   ├── requirements.txt
│   └── skills/                   ← 第 3 层：技能包
│       ├── SKILL.md              ← 总入口：问题→路由
│       ├── probe-workflow.md     ← 芯片探测流程
│       ├── diagnosis-hardfault.md
│       ├── diagnosis-hang.md
│       ├── diagnosis-memory.md
│       ├── diagnosis-peripheral.md
│       ├── chip-reference.md     ← 芯片 ID 速查
│       ├── register-decode.md
│       └── svd/                  ← SVD 芯片描述（按厂商分目录）
├── templates/                    ← 新工程模板（harness init 用）
│   └── boards/
│       ├── stm32f407/            ← 板级支持包模板
│       └── stm32f103/
└── .harness/
    └── config.json               ← 本地配置（工具链路径/默认板卡）
```

---

## 四、系统规格

### 4.1 harness CLI（第 2 层核心）

```
harness env check                # 环境自检：openocd/gcc/stlink/python 版本
harness init <project>           # 从 templates/ 复制新工程骨架
harness build <board>            # 编译（cmake/make 封装）
harness flash <board>            # 烧录（openocd 4 段 verify：BL+APP+签名+参数）
harness test <scenario>          # 跑测试场景（YAML 断言）
harness diagnose <problem>       # 硬件诊断（hardfault/hang/memory）
```

### 4.2 MCP 工具接口（第 1 层）

| 工具名 | 功能 | 底层实现 |
|--------|------|---------|
| `connect()` | 建立调试会话，返回 arch/target | OpenOCD TCL |
| `halt()` / `resume()` | CPU 暂停/恢复 | OpenOCD |
| `memory_read()` / `write()` | 内存读写 | mdw/mww |
| `register_read()` | 读寄存器（PC/LR/SP/xPSR） | reg |
| `chip_identify()` | 芯片探测（CPUID+DEV_ID） | 0xE000ED00 |
| `diagnose(problem)` | 硬件诊断 | diagnostics.py |
| `build(board)` / `flash(board)` / `test(scenario)` | 开发链路 | harness CLI |

**强制前置**：所有硬件操作必须先 `connect()`，未连接返回 `"no active session"`。

### 4.3 诊断引擎（第 3 层核心，diagnostics.py）

| 诊断 | 能力 |
|------|------|
| HardFaultDiagnosis | CFSR/HFSR/MMFAR/BFAR 位域解码 + 8 字栈帧回溯 + ELF 源码映射 |
| HangDiagnosis | 5 次 PC 采样判断死循环/跑飞/中断风暴 |
| MemoryDiagnosis | SP 栈使用率 + 向量表 + FreeRTOS 堆 + canary 扫描 |

### 4.4 断言引擎（tester.py）

支持 8 种检查：`frequency / range / monotonic / rate / change_detected / pattern / state_machine / stable_after`，场景由 YAML 定义。

---

## 五、实施步骤（AI 按此顺序执行）

### 阶段 1：环境基线
- [ ] 确认 Python 3.10+、OpenOCD、arm-none-eabi-gcc、Git 可用
- [ ] `harness env check` 输出全部通过

### 阶段 2：harness CLI 骨架
- [ ] 实现 harness_cli.py（argparse，子命令：env/init/build/flash/test/diagnose）
- [ ] 实现 env_probe.py（探测 openocd 路径、gcc、stlink 连接）
- [ ] 实现 config.py（.harness/config.json 读写）
- [ ] **验收**：`python harness_cli.py env check` 正常输出环境信息

### 阶段 3：核心能力
- [ ] swd_session.py：connect/halt/resume/register_read/memory_read/backtrace
- [ ] diagnostics.py：HardFault/挂死/内存三类诊断 + 结构化报告
- [ ] flasher.py：openocd 4 段烧录 + verify 校验
- [ ] tester.py：YAML 场景加载 + 断言执行
- [ ] **验收**：能对真实板卡（F407）完成 芯片探测 → 诊断 → 烧录

### 阶段 4：MCP 服务
- [ ] mcp_server.py：暴露第 4.2 节全部工具
- [ ] **验收**：Claude Code / Trae 等一条命令注册成功，AI 可调用 connect()

### 阶段 5：技能包
- [ ] SKILL.md 路由（问题类型→诊断流程映射）
- [ ] 3 个核心诊断技能（hardfault/hang/memory）
- [ ] 芯片参考 + SVD 目录
- [ ] **验收**：新 AI 读 SKILL.md 后能按流程引导诊断

### 阶段 6：模板与流程
- [ ] templates/boards/ 新工程模板（多 MCU 适配）
- [ ] STANDARD_PROCESS.md（8 步标准流程）
- [ ] **验收**：`harness init demo` 生成可用工程骨架

---

## 六、跨环境 / 跨工具适配

### 环境差异（Windows/Linux/macOS）
- 工具链探测：Windows 查 STM32CubeIDE 插件目录；Linux/macOS 查 PATH 中 openocd/arm-none-eabi-gcc
- 路径统一 `Path.as_posix()`（Windows 反斜杠会坑 OpenOCD）
- 调试器：ST-Link/J-Link/CMSIS-DAP 通过 openocd.cfg 隔离，harness 不关心

### AI 工具接入
```bash
# Claude Code
claude mcp add harness-ai -- python harness/mcp_server.py
# Trae / Cursor 等：在 MCP 配置界面添加同一条命令
```
技能包复制：`.claude/skills/` ｜ `.trae/skills/` ｜ `.cursor/rules/`

### 换工具/换环境时不变的东西
- 第 2 层脚本（唯一逻辑）
- 项目记忆（可进 git，随仓库走）
- 标准流程（STANDARD_PROCESS.md）

---

## 七、标准流程（STANDARD_PROCESS.md 摘要）

AI 开发新功能时按此 8 步执行：

```
① 需求定义（人，10min）
② 架构方案（AI 出 + 人审，30min）
③ 工程骨架（harness init，5min）
④ 开发（AI 编码 + 文件白名单，1-2h）
⑤ 编译烧录（harness build + flash，2min）
⑥ 运行验证（harness test 断言，按场景）
⑦ 判定+诊断（通过→交付；失败→harness diagnose→AI 修→回⑤）
⑧ 交付输出（固件+文档+测试报告+记忆沉淀）
```

---

## 八、安全规则（AI 必须遵守）

1. **硬件操作安全阀**：烧录/复位/写寄存器前先确认，操作次数设上限（默认 15 次/会话）
2. **文件白名单**：AI 只能修改场景 YAML 允许的文件，禁止改启动文件/链接脚本
3. **HardFault 检测**：每次函数调用后检查，异常立即停止并诊断
4. **诚实原则**：工具不可用时如实报告，不假装执行成功

---

## 九、验收总标准

以下全部满足视为"工作流搭建完成"：
- [ ] `harness env check` 通过
- [ ] 能对真实板卡完成：连接 → 芯片探测 → 健康检查
- [ ] 能完整烧录固件（4 段 verify 全过）
- [ ] 能诊断真实 HardFault 并输出结构化报告
- [ ] MCP 服务可被 AI 工具注册并调用
- [ ] 新 AI 读本蓝图 + SKILL.md 后能独立执行标准流程

---

*本蓝图随项目演进持续更新。任何 AI 搭建完成后，应在 STANDARD_PROCESS.md 中记录实际偏差与改进。*
