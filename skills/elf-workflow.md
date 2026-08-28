# ELF 符号解析与源码级调试

> 来源：改编自 AixProbe 技能包（CC BY-NC-SA 4.0，非商用）
> 工具：arm-none-eabi-* 交叉工具链（foundation env check 已探测）

## 适用场景

- 用户提供了编译产物（.elf 文件）
- 需要按函数名设断点
- 崩溃时需要定位到源码行
- 需要按变量名读写内存

## 前提

PC 上需要交叉编译工具链（GCC 安装时自带）：
- ARM: `arm-none-eabi-readelf / nm / addr2line / objdump`

## 第 1 步：获取 ELF 基本信息

```bash
arm-none-eabi-readelf -h firmware.elf
```

关键字段：Machine（架构）、Entry point address（入口）、Type

## 第 2 步：获取段信息（内存布局）

```bash
arm-none-eabi-readelf -S firmware.elf
```

| 段名 | 含义 | 调试用途 |
|------|------|---------|
| .text | 代码段 | PC 应在此范围内 |
| .rodata | 只读数据 | 常量字符串 |
| .data | 初始化全局变量 | 变量读写 |
| .bss | 零初始化全局变量 | 变量读写 |

## 第 3 步：提取符号表（核心）

```bash
# 所有函数符号
arm-none-eabi-nm -nS firmware.elf | grep " [Tt] "

# 全局变量
arm-none-eabi-nm -nS firmware.elf | grep " [BbDdGg] "
```

## 第 4 步：地址 → 源码行映射

```bash
# 单个地址
arm-none-eabi-addr2line -e firmware.elf -f -C 0x08001456
# 输出: main / src/main.c:87

# 批量（栈回溯用）
arm-none-eabi-addr2line -e firmware.elf -f -C 0x08001456 0x08001234
```

## 第 5 步：反汇编特定函数

```bash
# 只反汇编特定函数
arm-none-eabi-objdump -d firmware.elf | sed -n '/<main>:/,/^$/p'
```

## 实战场景

### 场景 1：崩溃地址 → 源码定位

```
用户: "设备 HardFault 了"

→ halt()
→ register_read("all")
← {pc: "0x08002456", xpsr: "0x01000003", lr: "0xFFFFFFFD"}

// xpsr 异常号=3 → HardFault
// 从栈帧读崩溃点
→ memory_read(0x20004FE0, 8)
// 偏移 0x18 = 真正崩溃 PC, 偏移 0x14 = 调用者 LR

→ addr2line -e firmware.elf -f -C 0x080014B2
→ USART1_Send / src/usart.c:42   ← 源码定位完成
```

### 场景 2：按变量名读内存

```
用户: "读 SystemCoreClock 的值"

→ arm-none-eabi-nm firmware.elf | grep SystemCoreClock
→ 20000000 D SystemCoreClock

→ memory_read(0x20000000, 1)
← 0x044AA200 = 72000000 = 72MHz
```

## 注意事项

1. **ELF 必须含符号表**：编译用 `-g` 保留调试信息，不要 `-s` strip
2. **addr2line 需要 DWARF**：`-g` 选项生成，否则只有函数名无行号
3. **地址一致性**：ELF 地址必须与实际烧录地址一致（bootloader 偏移时注意）
4. **Windows 路径**：工具链路径含空格需引号包裹；路径用正斜杠
