/**
 * @file    main.c
 * @brief   新工程主程序入口模板（STM32F103C8T6 裸机 LED 闪烁）
 * @author  AI Team / <作者>
 * @date    YYYY-MM-DD
 *
 * 四层架构归属（embedded-engineering-rules）：
 *   - 本文件: APPLICATION 层（业务流程编排），禁止直接操作寄存器
 *   - HAL 层: CubeMX 生成的 HAL 初始化（HAL_Init / MX_GPIO_Init 等）
 *   - 后续按需拆出 SERVICE（延时服务/参数管理）与 DRIVER（外设芯片驱动）
 */

#include "main.h"

int main(void)
{
    HAL_Init();
    SystemClock_Config();

    /* 外设初始化（CubeMX 生成，按项目补充）*/
    MX_GPIO_Init();

    /* 主循环：LED 闪烁。
       业务延时后续应替换为 HAL_GetTick() + 差值比较的非阻塞模式
       （embedded-engineering-rules: 禁止用 HAL_Delay 做业务延时）*/
    for (;;) {
        HAL_GPIO_TogglePin(LED_GPIO_Port, LED_Pin);
        HAL_Delay(500);
    }
}
