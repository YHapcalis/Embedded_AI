# HardFault / 崩溃诊断

> ⚠️ 渐进式披露：仅在 SKILL.md 路由到本技能时读取（按需单读）。

> 来源：改编自 AixProbe 技能包（CC BY-NC-SA 4.0，非商用）
> 适配：foundation 工具接口（MCP 风格）

## 适用场景

- 设备 HardFault / MemManage / BusFault / UsageFault
- 设备突然卡死无响应
- LED 停止闪烁 / 看门狗超时
- 串口日志中断

## 诊断流程

```
connect → halt → 读寄存器 → 判断异常类型
→ 读 Fault 状态寄存器 → 栈回溯 → 输出报告
```

## 第 0 步：建立连接

**必须先 `connect()` 建立调试会话。** 会话不存在时，所有操作返回 `"no active session"`。

```
→ connect()
← {"session_id": "sess_xxx", "arch": "cortex_m", "target_name": "stm32f4x.cpu"}
```

## 第 1 步：获取 CPU 状态

```
→ halt()
← {pc: "0x08001234"}

→ register_read("all")
← {r0: ..., r1: ..., sp: ..., lr: ..., pc: ..., xpsr: ...}
```

## 第 2 步：判断是否在异常上下文

**检查 xpsr 低 8 位（异常号）：**

| 异常号 | 名称 | 常见原因 |
|--------|------|---------|
| 0 | Thread 模式 | 正常运行，不在异常中 |
| 3 | HardFault | 非法访问、未对齐、除零、escalation |
| 4 | MemManage | MPU 违规、执行不可执行区域 |
| 5 | BusFault | 访问不存在的地址、总线超时 |
| 6 | UsageFault | 未定义指令、非法状态切换、除零 |
| 11 | SVCall | SVC 指令触发（一般正常） |
| 14 | PendSV | RTOS 上下文切换（一般正常） |
| 15 | SysTick | 系统滴答定时器 |

**检查 lr 值（EXC_RETURN）：**

| lr 值 | 含义 |
|------|------|
| 0xFFFFFFF1 | 返回 Handler 模式，使用 MSP（嵌套异常） |
| 0xFFFFFFF9 | 返回 Thread 模式，使用 MSP（无 OS 场景） |
| 0xFFFFFFFD | 返回 Thread 模式，使用 PSP（RTOS 场景） |

## 第 3 步：读 Fault 状态寄存器

```
→ memory_read(0xE000ED28, 1)   // CFSR
→ memory_read(0xE000ED2C, 1)   // HFSR
→ memory_read(0xE000ED38, 1)   // MMFAR
→ memory_read(0xE000ED3C, 1)   // BFAR
```

### CFSR 位域解析（0xE000ED28）

**MemManage Fault [7:0]：**

| 位 | 名称 | 含义 |
|----|------|------|
| [0] | IACCVIOL | 取指访问违规 |
| [1] | DACCVIOL | 数据访问违规 |
| [3] | MUNSTKERR | 退栈 MemManage 错误 |
| [4] | MSTKERR | 入栈 MemManage 错误 |
| [7] | MMARVALID | MMFAR 地址有效 |

**BusFault [15:8]：**

| 位 | 名称 | 含义 |
|----|------|------|
| [8] | IBUSERR | 取指总线错误 |
| [9] | PRECISERR | 精确数据总线错误（BFAR 有效） |
| [10] | IMPRECISERR | 非精确总线错误 |
| [11] | UNSTKERR | 退栈总线错误 |
| [12] | STKERR | 入栈总线错误 |
| [15] | BFARVALID | BFAR 地址有效 |

**UsageFault [31:16]：**

| 位 | 名称 | 含义 |
|----|------|------|
| [16] | UNDEFINSTR | 未定义指令 |
| [17] | INVSTATE | 无效状态（Thumb 位错误） |
| [18] | INVPC | 无效 PC 加载 |
| [19] | NOCP | 协处理器不存在 |
| [24] | UNALIGNED | 非对齐访问 |
| [25] | DIVBYZERO | 除零 |

### HFSR 位域解析（0xE000ED2C）

| 位 | 名称 | 含义 |
|----|------|------|
| [1] | VECTTBL | 向量表读取错误 |
| [30] | FORCED | 强制 HardFault（低优先级 Fault 升级） |
| [31] | DEBUGEVT | 调试事件触发 |

> FORCED=1 时，真正原因在 CFSR 中。

## 第 4 步：栈回溯

异常入口时硬件自动压栈 8 字，栈帧布局：

```
SP+0x00: R0    SP+0x04: R1    SP+0x08: R2    SP+0x0C: R3
SP+0x10: R12   SP+0x14: LR    SP+0x18: PC ← 崩溃点!   SP+0x1C: xPSR
```

根据 lr 判断使用 MSP 还是 PSP：
- lr=0xFFFFFFF9 → 用 MSP
- lr=0xFFFFFFFD → 用 PSP

```
→ register_read("msp")   // 或 psp
← {value: "0x20004FE0"}

→ memory_read(0x20004FE0, 8)   // 读 8 字栈帧
```

解析：
- 偏移 0x18 = 崩溃时真正 PC
- 偏移 0x14 = 调用者返回地址
- 偏移 0x00 = R0（第一个参数）

## 第 5 步：地址合法性判断

参考 chip-reference.md 确认合法范围：

| 区域 | 典型范围 (F407) | 含义 |
|------|----------------|------|
| Flash | 0x08000000-0x08100000 | 代码区 |
| SRAM | 0x20000000-0x20030000 | 数据区 |
| 外设 | 0x40000000-0x5FFFFFFF | 外设寄存器 |
| Cortex 系统 | 0xE0000000-0xE00FFFFF | NVIC/SCB/Debug |

PC 不在 Flash 范围 → 程序跑飞（函数指针错误 / 栈溢出覆盖返回地址）
SP 接近 SRAM 底部 → 栈溢出风险

## 输出诊断报告模板

```
## HardFault 诊断报告

### 环境
- 芯片: [根据 target_name 和 chip_identify 结果]
- 架构: cortex_m

### 异常信息
- 异常类型: [HardFault/BusFault/...]（xpsr 异常号=X）
- 当前 PC: 0xXXXXXXXX [Flash区/SRAM区/非法]
- 当前 SP: 0xXXXXXXXX [栈剩余 XXX 字节]
- LR: 0xXXXXXXXX [EXC_RETURN 含义]

### Fault 寄存器
- CFSR = 0xXXXXXXXX → [具体错误位]
- HFSR = 0xXXXXXXXX → [FORCED=X]
- BFAR = 0xXXXXXXXX [如果 BFARVALID]
- MMFAR = 0xXXXXXXXX [如果 MMARVALID]

### 栈回溯
- 崩溃点 PC(栈帧): 0xXXXXXXXX
- 调用者 LR(栈帧): 0xXXXXXXXX

### 根因分析
[根据 CFSR 位域+地址范围+栈信息综合判断]

### 常见原因对照
| CFSR 标志 | 可能原因 | 检查方向 |
|----------|---------|---------|
| PRECISERR | 访问了不存在的地址 | 检查 BFAR 指向的地址 |
| INVSTATE | 跳转到 ARM 模式(非 Thumb) | 函数指针最低位未置 1 |
| UNDEFINSTR | 执行到数据区 | 栈溢出覆盖返回地址 |
| DIVBYZERO | 除法除数为 0 | 检查崩溃点附近的除法 |
| UNALIGNED | 非对齐访问 | 检查指针强转 |

### 建议
1. [具体修复建议]
```
