# 寄存器解读流程

> 来源：改编自 AixProbe 技能包（CC BY-NC-SA 4.0，非商用）
> 适配：foundation 工具接口（MCP 风格）

## 前提

- **必须先 `connect()` 建立调试会话**
- 已通过 probe-workflow.md 识别芯片型号
- 已读取对应 SVD 文件获得外设寄存器知识

## 解读流程

### 第 1 步：在 SVD 中查找目标寄存器

SVD XML 结构：

```xml
<peripheral>
  <name>RCC</name>
  <baseAddress>0x40023800</baseAddress>
  <registers>
    <register>
      <name>CR</name>
      <addressOffset>0x0</addressOffset>
      <fields>
        <field><name>HSION</name><bitOffset>0</bitOffset><bitWidth>1</bitWidth></field>
      </fields>
    </register>
  </registers>
</peripheral>
```

计算绝对地址：`baseAddress + addressOffset`

### 第 2 步：读取硬件寄存器

```
→ halt()
→ memory_read(0x40023800, 1)   // RCC_CR
```

### 第 3 步：按位域解码

从 SVD 的 `<fields>` 获取每个位域的名称/偏移/宽度，然后：

```
field_value = (raw_value >> bit_offset) & ((1 << bit_width) - 1)
```

### 第 4 步：输出解读结果

以表格形式呈现：位域名 | 位置 | 值 | 含义

## SVD 位域格式兼容（3 种都要能处理）

**格式 1: bitOffset + bitWidth（最常见）**
```xml
<field><name>HSION</name><bitOffset>0</bitOffset><bitWidth>1</bitWidth></field>
```

**格式 2: lsb + msb**
```xml
<field><name>SW</name><lsb>0</lsb><msb>1</msb></field>
```
转换: bitOffset = lsb, bitWidth = msb - lsb + 1

**格式 3: bitRange**
```xml
<field><name>SWS</name><bitRange>[3:2]</bitRange></field>
```
转换: bitOffset = 2, bitWidth = 3 - 2 + 1 = 2

## derivedFrom 处理

```xml
<peripheral derivedFrom="USART1">
  <name>USART2</name>
  <baseAddress>0x40004400</baseAddress>
</peripheral>
```
遇到 `derivedFrom` 时，去源外设查找寄存器定义。

## 写寄存器（读-改-写）

```
1. 读: memory_read(addr, 1) → 0x0300FF83
2. 改: 设置 HSEON[16]=0 → new = 0x0300FF83 & ~(1<<16)
3. 写: memory_write(addr, new)
4. 验: memory_read(addr, 1) → 确认生效
```
