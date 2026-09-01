/**
 * @file    main.c
 * @brief   新工程主程序入口模板（ESP32-S3 GPIO LED 闪烁）
 * @author  AI Team / <作者>
 * @date    YYYY-MM-DD
 *
 * 四层架构归属（embedded-engineering-rules）：
 *   - 本文件: APPLICATION 层（业务流程编排）
 *   - IDF driver 组件（gpio 等）: 视为 HAL/DRIVER 层，直接调用
 *   - 后续按需拆出 SERVICE（延时/参数管理）与 DRIVER（外设芯片驱动）
 *   - 注意: ESP32 用 FreeRTOS 任务，ISR 铁律同样适用（ISR 内禁止阻塞调用）
 */

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"

#define LED_GPIO GPIO_NUM_2  /* 按板卡实际引脚修改 */

void app_main(void)
{
    gpio_config_t io_conf = {
        .pin_bit_mask = (1ULL << LED_GPIO),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = 0,
        .pull_down_en = 0,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&io_conf);

    for (;;) {
        gpio_set_level(LED_GPIO, 1);
        vTaskDelay(pdMS_TO_TICKS(500));
        gpio_set_level(LED_GPIO, 0);
        vTaskDelay(pdMS_TO_TICKS(500));
    }
}
