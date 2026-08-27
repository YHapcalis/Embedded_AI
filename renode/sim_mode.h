/**
 * sim_mode.h — 仿真模式编译开关
 *
 * 用法:
 *   CMake 中加 target_compile_definitions(PUBLIC SIMULATION)
 *   或在编译器参数加 -DSIMULATION
 *
 * 效果:
 *   - 自旋延时(NOP) → HAL_Delay (让 Renode 可 TimeSkip)
 *   - 可选: 跳过耗时长循环 (如 LCD 色条填充)
 *   - 可选: 注入测试数据
 *
 * 在 Renode 仿真时生效，真实硬件不受影响。
 */

#ifndef __SIM_MODE_H
#define __SIM_MODE_H

#include "main.h"   /* HAL_GetTick, HAL_Delay */

#ifdef SIMULATION

/* ================================================================
 * 仿真模式：所有延时改为 SysTick 基类
 * Renode 在 WFI/HAL_Delay 时可 TimeSkip 跳过空闲时间
 * ================================================================ */

/* 短延时：直接用 HAL_Delay 代替 NOP 自旋
 * 注意：HAL_Delay 最小精度 1ms，短于 1ms 的会向上取整 */
#define SIM_DELAY_US(us)       do { if ((us) >= 1000) HAL_Delay((us) / 1000); } while(0)
#define SIM_DELAY_MS(ms)       HAL_Delay(ms)

/* 大循环跳过：当 count * iter_time_ms > threshold 时跳过
 * 用于跳过 LCD 色条填充等耗时操作 */
#define SIM_SKIP_LARGE_LOOP(count, label) \
    do { \
        printf("[SIM] Skip large loop: %s (%u iterations)\\r\\n", label, (unsigned)(count)); \
        HAL_Delay(10); \
    } while(0)

/* 测试图案注入（当需要已知数据时使用）*/
#define SIM_INJECT_VALUE(var, val)  do { (var) = (val); } while(0)

#else /* !SIMULATION */

/* ================================================================
 * 真实硬件模式：保留原始行为
 * ================================================================ */

#define SIM_DELAY_US(us)       /* 无操作 — 使用原来自旋延时 */
#define SIM_DELAY_MS(ms)       /* 无操作 — 使用原来自旋延时 */
#define SIM_SKIP_LARGE_LOOP(count, label)  /* 无操作 */
#define SIM_INJECT_VALUE(var, val)         /* 无操作 */

#endif /* SIMULATION */

#endif /* __SIM_MODE_H */
