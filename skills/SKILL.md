---
name: foundation-diagnostics
description: "嵌入式硬件诊断技能包。芯片探测、寄存器解读与调试诊断：HardFault 崩溃分析、程序挂死/跑飞定位、内存问题排查。触发词：'识别芯片'、'探测'、'HardFault'、'崩溃'、'死机'、'跑飞'、'内存'、'栈溢出'、'diagnose'、'crash'、'hang'、'memory'。"
argument-hint: "[problem-type] [chip-family]"
---

# foundation 诊断技能包 — 芯片探测与硬件诊断

基于 foundation 调试接口（MCP 风格：connect/halt/memory_read/register_read），
连接目标 MCU 后自动识别芯片，并执行各类硬件诊断流程。

> 方法论改编自 AixProbe 开源项目（CC BY-NC-SA 4.0，非商用）。
> 工具接口对齐 foundation 的 SWDDebugSession。

## ⚠️ 前置要求：必须先 connect

**所有硬件操作（读寄存器、读内存、halt、诊断等）都依赖一个活动的调试会话。**

在执行任何操作之前，**必须先调用 `connect()`** 建立连接。
如果会话已断开，后续操作会返回 `"no active session"` 错误。

```
❌ 错误流程：register_read("pc") → 报错 "no active session"
✅ 正确流程：connect() → halt() → register_read("pc") → 成功
```

**判断是否需要 connect：**
- 收到 `"no active session"` 错误 → 需要重新 `connect()`
- 每次新对话或长时间未操作后 → 应先 `connect()` 确认连接
- `connect()` 成功返回 `session_id` / `arch` / `target_name`

## 核心流程

```
connect() → 获取 arch/target_name（必须的第一步）
    ↓
halt() → 暂停 CPU（读寄存器/内存前通常需要）
    ↓
读取芯片 ID 寄存器（memory_read）
    ↓
匹配芯片型号 → 加载芯片参考/SVD 知识
    ↓
AI 可解读外设寄存器 / 执行诊断流程
```

## 工作流路由

### 芯片探测与寄存器

| 场景 | 参考文档 | 说明 |
|------|---------|------|
| 连接后识别芯片 | [probe-workflow.md](probe-workflow.md) | CPUID + DEV_ID 探测型号 |
| 芯片 ID 速查 | [chip-reference.md](chip-reference.md) | F407/F103/ESP32-C3 等 ID 表 |
| 寄存器解读 | [register-decode.md](register-decode.md) | 配合 SVD 解析位域 |
| ELF 源码定位 | [elf-workflow.md](elf-workflow.md) | 崩溃地址 → 源码行 |

### 调试诊断

| 现象 | 诊断流程 | 核心策略 |
|------|---------|---------|
| HardFault / 崩溃 / 异常 | [diagnosis-hardfault.md](diagnosis-hardfault.md) | 读 CFSR + 栈回溯 + Fault 地址 |
| 程序挂死 / 跑飞 / 不响应 | [diagnosis-hang.md](diagnosis-hang.md) | 多次 PC 采样 + 死循环定位 |
| 栈溢出 / 内存踩踏 | [diagnosis-memory.md](diagnosis-memory.md) | SP 检查 + 栈 canary + 填充对比 |
| 外设不工作 | [diagnosis-peripheral.md](diagnosis-peripheral.md) | 时钟→GPIO→外设逐层排查 |
| 时钟 / 波特率异常 | [diagnosis-clock.md](diagnosis-clock.md) | RCC 时钟树分析 |

## 工具接口（foundation MCP 风格）

| 接口 | 语义 |
|------|------|
| `connect()` | 建立调试会话（必须先调用） |
| `halt()` / `resume()` | 暂停 / 恢复 CPU |
| `register_read(name)` | 读寄存器（pc/lr/sp/xpsr/msp/psp/r0-r12/all） |
| `memory_read(addr, count)` | 读内存（32 位字） |
| `read_fault()` | 读 CFSR/HFSR/MMFAR/BFAR |
| `read_chip_id()` | 芯片探测（CPUID + DEV_ID + Flash 大小） |
| `backtrace(sp)` | 8 字栈帧回溯（崩溃点 PC/LR） |

## 快速开始

用户说"设备 HardFault 了"、"芯片识别一下"、"程序死机"等时，按以下步骤：

1. **第一步必须 `connect()`** — 建立调试会话
2. 需要读寄存器/内存 → 先 `halt()`
3. 查看 `arch` 字段判断架构
4. 按 [probe-workflow.md](probe-workflow.md) 读取芯片 ID → 匹配 [chip-reference.md](chip-reference.md)
5. 按问题类型路由到对应 [diagnosis-*.md](#工作流路由)
6. 执行诊断流程 → 输出结构化报告

> **重要提醒**：任何操作返回 `"no active session"`，立即重新 `connect()` 后再继续。
