# 时钟系统诊断

> 来源：改编自 AixProbe 技能包（CC BY-NC-SA 4.0，非商用）
> 适配：foundation 工具接口（MCP 风格）

## 适用场景

- 串口波特率不对（乱码）
- 定时器频率不正确
- 外设工作异常但配置看起来正确
- 系统运行速度异常（太快/太慢）
- PLL 未锁定

## 核心思路

MCU 的所有时钟由时钟树生成。一个错误的时钟源配置会导致**所有**
依赖它的外设出问题。

```
HSI/HSE → PLL 倍频 → SYSCLK → AHB 分频 → APB1/APB2 分频
                                              ↓
                                    外设时钟（UART/SPI/TIM...）
```

## ARM Cortex-M：RCC 时钟检查

### 第 1 步：读 RCC 寄存器

以 STM32F407 为例（RCC 基地址 = 0x40023800）：

```
→ halt()
→ memory_read(0x40023800, 1)   // RCC_CR
→ memory_read(0x40023808, 1)   // RCC_CFGR
```

### 第 2 步：分析 RCC_CR

| 位 | 名称 | 含义 |
|----|------|------|
| [0] | HSION | HSI(16MHz 内部 RC)开启 |
| [1] | HSIRDY | HSI 就绪 |
| [16] | HSEON | HSE(外部晶振)开启 |
| [17] | HSERDY | HSE 就绪 |
| [24] | PLLON | PLL 开启 |
| [25] | PLLRDY | PLL 锁定 |

**常见问题**：
- HSEON=1 但 HSERDY=0 → 外部晶振未起振（硬件问题：晶振虚焊/负载电容不对）
- PLLON=1 但 PLLRDY=0 → PLL 未锁定（PLL 输入源有问题）

### 第 3 步：分析 RCC_CFGR

| 位 | 名称 | 含义 |
|----|------|------|
| [1:0] | SW | 系统时钟源选择：00=HSI, 01=HSE, 10=PLL |
| [3:2] | SWS | 系统时钟源状态（实际值） |
| [7:4] | HPRE | AHB 预分频 |
| [10:8] | PPRE1 | APB1 预分频（低速，最高 42MHz） |
| [13:11] | PPRE2 | APB2 预分频（高速，最高 84MHz） |

### 第 4 步：时钟树计算

```
SYSCLK = HSI/HSE × PLLM/PLLN/PLLP（由 RCC_PLLCFGR 决定）

AHB 时钟  = SYSCLK / HPRE
APB1 时钟 = AHB / PPRE1（外设定时器时钟 ×2 如果分频>1）
APB2 时钟 = AHB / PPRE2
```

**验证**：算出外设期望频率（如 UART 波特率、TIM 频率），与读到的配置对比。

## 输出诊断报告模板

```
## 时钟诊断报告

### 时钟源状态
- HSI: [开启/关闭/就绪]
- HSE: [开启/关闭/就绪/未起振]
- PLL: [开启/关闭/锁定]

### 时钟树
| 节点 | 期望 | 实际计算 |
|------|------|---------|
| SYSCLK | 168MHz | ... |
| AHB | 168MHz | ... |
| APB1 | 42MHz | ... |
| APB2 | 84MHz | ... |

### 诊断结论
[晶振未起振/PLL 未锁定/分频配置错/正常]

### 建议
1. [修复建议]
```
