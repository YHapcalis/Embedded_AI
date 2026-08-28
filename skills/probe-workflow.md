# 芯片自动探测流程

> 来源：改编自 AixProbe 技能包（CC BY-NC-SA 4.0，非商用）
> 适配：foundation 工具接口（MCP 风格）

## 前置要求

**在执行本流程之前，必须先调用 `connect()` 建立调试会话。**
如果操作过程中收到 `"no active session"` 错误，需要重新 `connect()`。

## 总览

```
connect()
  ├─ arch: "cortex_m"  → ARM Cortex-M 探测流程
  ├─ arch: "cortex_a"  → ARM Cortex-A 探测流程
  ├─ arch: "riscv"     → RISC-V 探测流程
  └─ arch: "xtensa"    → Xtensa 探测流程（ESP32 系）
```

## 第 1 步：连接并获取架构

```
→ connect()
← {"session_id": "sess_123", "arch": "cortex_m", "target_name": "stm32f4x.cpu"}
```

`arch` 字段直接告诉你目标架构。`target_name` 通常包含芯片系列信息。

## 第 2 步：按架构读取芯片 ID

### ARM Cortex-M

**必读寄存器：CPUID（所有 Cortex-M 通用）**

```
→ halt()
→ memory_read(0xE000ED00, 1)
← {"data": "0x411FC231"}
```

CPUID 解码：

| 位域 | 位置 | 含义 |
|------|------|------|
| Implementer | [31:24] | 厂商：0x41=ARM |
| Architecture | [19:16] | 0xF=ARMv7-M |
| PartNo | [15:4] | 内核型号（查 chip-reference） |

**厂商特定 ID 寄存器（按 target_name 判断）：**

target_name 包含 `stm32` 或 `gd32`:
```
→ memory_read(0xE0042000, 1)   // DBGMCU_IDCODE
→ memory_read(0x40015800, 1)   // 备用地址（新系列）
```

### RISC-V

```
→ halt()
→ register_read("all")   // 查找 mvendorid / marchid / mimpid
```

### Xtensa（ESP32 系）

通过 Espressif OpenOCD 分支的 target 配置识别，无需读 ID 寄存器。

## 第 3 步：匹配芯片型号

将读到的 ID 值与 [chip-reference.md](chip-reference.md) 表格对比，确定具体型号。

## 第 4 步：加载 SVD

确定芯片型号后，在 `skills/svd/` 目录下递归搜索匹配的 SVD 文件
（搜索方法见 chip-reference.md 的"SVD 文件查找"节）。

## 完整示例：识别 STM32F407

```
→ connect()
← {"arch": "cortex_m", "target_name": "stm32f4x.cpu"}

→ halt()

→ memory_read(0xE000ED00, 1)
← 0x410FC241 → PartNo=0xC24 → Cortex-M4

→ memory_read(0xE0042000, 1)
← 0x20036413 → DEV_ID=0x413 → STM32F405/407

→ Glob("**/*STM32F407*.svd")
← skills/svd/arm/STM32F407xx.svd
```
