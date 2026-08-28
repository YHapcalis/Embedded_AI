/**
 * @file    main.c
 * @brief   新工程主程序入口模板（FreeRTOS + 基础任务）
 * @author  AI Team / <作者>
 * @date    YYYY-MM-DD
 */

#include "main.h"
#include "cmsis_os.h"

/* 示例任务：LED 闪烁 */
void LedTask(void *argument)
{
    for (;;) {
        HAL_GPIO_TogglePin(LED_GPIO_Port, LED_Pin);
        osDelay(500);
    }
}

/* 任务句柄与定义 */
osThreadId_t ledTaskHandle;
const osThreadAttr_t ledTask_attributes = {
    .name = "ledTask",
    .stack_size = 512,
    .priority = (osPriority_t)osPriorityNormal,
};

int main(void)
{
    HAL_Init();
    SystemClock_Config();

    /* 外设初始化（按项目补充）*/
    MX_GPIO_Init();

    /* 创建任务 */
    ledTaskHandle = osThreadNew(LedTask, NULL, &ledTask_attributes);

    /* 启动调度器 */
    osKernelStart();

    for (;;) {
    }
}
