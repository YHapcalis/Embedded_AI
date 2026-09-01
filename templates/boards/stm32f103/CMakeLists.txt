# 新工程 CMakeLists 模板（STM32F103C8T6 示例）
# 用法：复制到新项目根目录，修改 PROJECT_NAME / MCU 相关配置

cmake_minimum_required(VERSION 3.16)
project(MY_NEW_PROJECT C ASM)

# ── MCU 参数（换芯片改这里）─────────────────────────────
set(MCU_FAMILY      stm32f103)
set(MCU_FLAGS       -mcpu=cortex-m3 -mthumb)
set(LINKER_SCRIPT   STM32F103C8TX_FLASH.ld)
set(STARTUP_FILE    startup_stm32f103xb.s)

# ── 编译选项 ────────────────────────────────────────────
set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
add_compile_options(${MCU_FLAGS} -Wall -Wextra -g -O2)

# ── 源文件 ──────────────────────────────────────────────
file(GLOB_RECURSE SOURCES
    Core/Src/*.c
    Drivers/*.c
    ${STARTUP_FILE}
)

add_executable(${PROJECT_NAME} ${SOURCES})

# ── 链接 ────────────────────────────────────────────────
target_link_options(${PROJECT_NAME} PRIVATE
    ${MCU_FLAGS} -T ${LINKER_SCRIPT} --specs=nano.specs -Wl,--gc-sections)

# ── 产物命名（供 foundation flash 使用）─────────────────
set_target_properties(${PROJECT_NAME} PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR})
