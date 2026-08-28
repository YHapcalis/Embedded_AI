# 芯片 ID 速查表

> 来源：改编自 AixProbe 技能包（CC BY-NC-SA 4.0，非商用）
> 覆盖：F407 / F103 / ESP32-C3 基础表（可扩展）

## ARM Cortex-M 通用

### CPUID @ 0xE000ED00（所有 Cortex-M 通用）

| CPUID 值 | PartNo | 内核 |
|---------|--------|------|
| 0x410CC200 | 0xC20 | Cortex-M0 |
| 0x410CC210 | 0xC21 | Cortex-M1 |
| 0x411FC231 | 0xC23 | Cortex-M3 r1p1 |
| 0x410FC241 | 0xC24 | Cortex-M4 r0p1 |
| 0x411FC271 | 0xC27 | Cortex-M7 r1p1 |
| 0x410FD200 | 0xD20 | Cortex-M23 |
| 0x410FD210 | 0xD21 | Cortex-M33 |

解码方法：`PartNo = (CPUID >> 4) & 0xFFF`

---

## STMicroelectronics（ST）

### DBGMCU_IDCODE 地址
- 主地址：`0xE0042000`
- 备用地址：`0x40015800`（L0/L4/G0/G4/WB/WL 等新系列）

### DEV_ID 速查（IDCODE 低 12 位）

| DEV_ID | 芯片系列 | 内核 | SVD 文件 |
|--------|---------|------|---------|
| 0x410 | STM32F103 中密度 | Cortex-M3 | arm/STM32F103xx.svd |
| 0x412 | STM32F103 低密度 | Cortex-M3 | arm/STM32F103xx.svd |
| 0x414 | STM32F103 高密度 | Cortex-M3 | arm/STM32F103xx.svd |
| **0x413** | **STM32F405/407/415/417** | **Cortex-M4F** | **arm/STM32F407xx.svd** |
| 0x419 | STM32F42x/43x | Cortex-M4F | arm/STM32F427xx.svd |
| 0x421 | STM32F446 | Cortex-M4F | arm/STM32F446xx.svd |
| 0x431 | STM32F411 | Cortex-M4F | arm/STM32F411xx.svd |
| 0x441 | STM32F412 | Cortex-M4F | arm/STM32F412xx.svd |
| 0x449 | STM32F74x/75x | Cortex-M7 | arm/STM32F750xx.svd |
| 0x451 | STM32F76x/77x | Cortex-M7 | arm/STM32F767xx.svd |
| 0x450 | STM32H743/745/747/750/753/755/757 | Cortex-M7 | arm/STM32H743xx.svd |

### Flash 大小寄存器

| 芯片系列 | 地址 | 单位 |
|---------|------|------|
| STM32F1/F3 | 0x1FFFF7E0 | KB (16bit) |
| **STM32F2/F4** | **0x1FFF7A22** | **KB (16bit)** |
| STM32F7 | 0x1FF0F442 | KB (16bit) |
| STM32H7 | 0x1FF1E880 | KB (16bit) |
| STM32L0/L1 | 0x1FF8004C | KB (16bit) |
| STM32L4 | 0x1FFF75E0 | KB (16bit) |

### F407 / F103 内存布局

| 区域 | F407ZGT6 | F103C8 |
|------|----------|--------|
| Flash | 0x08000000-0x08100000 (1MB) | 0x08000000-0x08020000 (128KB) |
| SRAM | 0x20000000-0x20030000 (192KB) | 0x20000000-0x20005000 (20KB) |
| 外设 | 0x40000000-0x5FFFFFFF | 0x40000000-0x5FFFFFFF |
| Cortex 系统 | 0xE0000000-0xE00FFFFF | 0xE0000000-0xE00FFFFF |

---

## Espressif（ESP32 系列）

### 调试方式：JTAG（Espressif 专用 OpenOCD 分支）

| 芯片 | 架构 | 调试接口 | OpenOCD 配置 |
|------|------|---------|-------------|
| ESP32 | Xtensa LX6 双核 | JTAG | board/esp32-wrover-kit.cfg |
| ESP32-S2 | Xtensa LX7 单核 | JTAG | board/esp32s2-kaluga-1.cfg |
| ESP32-S3 | Xtensa LX7 双核 | JTAG | board/esp32s3-builtin.cfg |
| ESP32-C3 | RISC-V 单核 | JTAG（板载 USB-JTAG） | board/esp32c3-builtin.cfg |
| ESP32-C6 | RISC-V 单核 | JTAG（板载 USB-JTAG） | board/esp32c6-builtin.cfg |

### 芯片识别

- Xtensa 系：通过 OpenOCD target 探测（ESP32_ONLYCPU 等变量）
- RISC-V 系（C3/C6）：`marchid` / `mvendorid` CSR

---

## 识别流程决策树

```
connect() → arch?
├── cortex_m
│   ├── 读 CPUID(0xE000ED00) → 确认内核
│   ├── target_name 含 "stm32"?
│   │   └── 读 DBGMCU_IDCODE(0xE0042000) → 查 DEV_ID 表
│   └── 未知 → 用 CPUID 确定内核，提示用户指定 SVD
│
├── riscv
│   ├── target_name 含 "esp32c"?
│   │   └── ESP32-C3/C6（Espressif 分支）
│   ├── target_name 含 "ch32"?
│   │   └── 按 marchid 查 CH32V 表
│   └── 未知 → 提示用户指定芯片和 SVD
│
└── xtensa
    ├── target_name 含 "esp32"?
    │   └── ESP32/S2/S3（Espressif 分支）
    └── 未知 → 提示用户指定芯片和 SVD
```

---

## SVD 文件查找（3 级递进）

**第 1 级：精确匹配文件名**
```
→ Glob("**/*STM32F407*.svd")
← skills/svd/arm/STM32F407xx.svd  ← 命中！
```

**第 2 级：忽略大小写搜索**
```
→ Grep("stm32f103", glob="*.svd", "-i")
← 命中！
```

**第 3 级：列出所有 SVD，AI 选最接近的**
```
→ Glob("**/*.svd")
```

> SVD 文件来源：cmsis-svd-data 仓库 / 厂商 SDK / CMSIS Pack
