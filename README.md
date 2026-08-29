# 嵌入式 AI 开发工作流（Embedded AI Workflow）

> **把"嵌入式开发全流程"打包成一个 AI 可直接执行的工作流包。**
> 任何支持 AI 工具 / MCP 的会话，拿到本仓库即可按标准流程完成
> **需求 → 编码 → 编译烧录 → 验证 → 诊断修复 → 交付** 的闭环，
> 不绑定特定 MCU、不绑定特定开发环境、不绑定特定 AI 工具。

```
一句话：让 AI 成为可迁移的嵌入式开发工程师，
本仓库是可携带的"岗位说明书 + 工具箱"。
```

---

## 这是什么

嵌入式开发中，AI 最难的一环是**让 AI 能看见并操作硬件**——灯不亮、串口无输出、死机，
现象模糊，AI 无法反推代码错在哪。本仓库填补的正是这个断裂带：

- **12 步标准流程**（`标准流程12步.md`）：准备 → 规划 → 执行 → 交付，闭环可复用
- **引擎层**（`foundation/`）：OpenOCD 多分支管理、SWD/JTAG 调试会话、多段烧录、
  HardFault/挂死/内存三类诊断、测试断言、AI 团队编排、工程宪法审查
- **知识层**（`skills/`）：诊断技能路由、芯片参考、SVD 寄存器库（渐进式披露，防上下文爆炸）
- **安全横切面**：文件白名单、硬件操作上限、HardFault 检测、诚实原则——AI 搞不坏板子

真机验收：STM32F407ZGT6（SWD）+ ESP32-C3（JTAG）双架构驱动同一套引擎。

## 目录结构

```
嵌入式AI工作流/
├── 工作流启动引导.md          ← AI 入口（只读这一页，其余按需加载）
├── 标准流程12步.md            ← 完整执行流程（开始干活才读）
├── docs/
│   └── 设计决策与约束.md      ← 设计决策 + 协议约束（维护者用）
├── foundation/                ← 引擎（CLI / MCP 服务 / core 模块 / 模板）
├── boards/                    ← 板卡 Profile（stm32f407 / esp32c3）
├── templates/boards/          ← 新工程骨架模板
├── skills/                    ← 技能包（SKILL.md 路由 + 诊断技能 + SVD）
└── 验收记录_S1-S10.md         ← 验收证据（含 2026-08-29 独立复现附录）
```

## 快速开始

```bash
# 环境自检（OpenOCD / 编译器 / 调试器 / Python / Git）
python -m foundation env check

# 查看 OpenOCD 多分支注册表（ST / Espressif / 主线）
python -m foundation openocd list

# 确认目标芯片（决定所需开发环境）
python -m foundation chip-confirm --mcu stm32f407 --arch cortex_m4 --transport swd
```

接入 AI 工具：`foundation/mcpservice/server.py` 暴露标准 MCP 服务
（connect / halt / memory_read / chip_identify / diagnose / build / flash / test）。

## 验收状态

| # | 验收项 | 状态 |
|---|--------|------|
| S1 | 环境自检通过 | ✅ 真机 |
| S2 | 芯片探测（CPUID/DEV_ID/Flash） | ✅ 真机 |
| S3 | 健康检查（异常号=0, CFSR=0） | ✅ 真机 |
| S4 | 4 段完整烧录 + verify | ✅ 真机 |
| S5 | HardFault 诊断（位域+回溯+源码定位） | ✅ 真机 |
| S6 | 挂死诊断（5 次 PC 采样） | ✅ 真机 |
| S7 | MCP 标准协议接入 | ✅ |
| S8 | 技能包路由 | ✅ |
| S9 | 抽象层通用性（双板卡零引擎改动） | ✅ |
| S10 | 自引导完整性（新 AI 仅凭仓库独立执行） | ✅ |

> **2026-08-29 独立复现**：S1-S10 全部由独立会话在真机复现通过
> （含 4 段烧录 ×2、HardFault 注入+断点钉现场、PC 采样源码映射）。
> 复现过程发现并修复的问题详见 `验收记录_S1-S10.md` 复现附录。

## 安全设计（AI 操作硬件的四道锁）

| 编号 | 机制 | 说明 |
|------|------|------|
| S1 | 文件白名单 | AI 只能修改任务允许的文件，禁止改启动文件/链接脚本 |
| S2 | 硬件操作上限 | 默认 15 次/会话，防 AI 反复操作烧坏板子 |
| S3 | HardFault 检测 | 每次硬件操作后检查，异常立即停止并诊断 |
| S4 | 诚实原则 | 工具不可用如实报告，不假装编译/烧录/测试成功 |

## 开源协议与致谢

- 本仓库遵循 **CC BY-NC-SA 4.0**（署名-非商用-相同方式共享）
- `skills/` 诊断技能包（diagnosis-*.md / chip-reference / probe-workflow 等）
  **改编自 [AixProbe 开源 AI 远程调试器](https://oshwhub.com/guiwu1/project_rsowazwu)**
  （作者：玩转嵌入式linux；CC BY-NC-SA 4.0，非商用）：
  方法论可借鉴、不得冒充、不得商用
- `foundation/` 引擎与其余文档为作者原创
- 作者：Ryumeido Mei（余豪）

> 商用或其他授权需求，请联系作者。
