# Harness AI 模拟人工操作硬件的方法

> 对应工程: MY_OTA_GUI (STM32F407ZGT6)
> 参考文档: harness_ai_toolchain.json / .yaml / .md（工程 AI 生成，已实机验证）

## 核心原理

通过 **OpenOCD TCL → SWD 协议 → STM32 调试端口**，直接读写芯片内部的寄存器和内存，模拟人工的操作效果。

```
人工操作                    Harness AI 模拟
──────────                 ──────────────────
手指按按钮                  SWD 写内存变量 (g_pressed)
手指转编码器                SWD 写内存变量 (g_can_sensor.knob)
手指触摸屏幕                SWD 写触摸坐标 + 调用 touch_poll()
眼睛看屏幕                  SWD 读内存变量 (传感器数据)
大脑判断数据是否正常         AI 分析采样结果
```

---

## 函数调用方式（实机验证）

**必须使用 `halt → resume [map_addr]`**。严禁使用 `reg pc + resume`。

| 方式 | 结果 |
|:-----|:-----|
| ❌ `halt → reg pc 0xaddr → resume` | HardFault（reg pc 被 resume 忽略） |
| ✅ `halt → resume [map_addr]` | 正常执行，函数返回自动回到 RTOS |

### 调用序列

```
无参函数: halt → resume [link_address] → sleep(N) → halt
有参函数: halt → reg r0 val → resume [link_address] → sleep(N) → halt
读返回值: halt → resume [addr] → sleep(N) → halt → reg r0
```

### 关键参数

| 参数 | 说明 |
|------|------|
| link_address | `.map` 文件中的链接地址（不加 1，`resume` 自动设 Thumb 位） |
| LR | 由 `resume` 保留原值，函数返回自动回到 RTOS，**不需要手动设置** |
| R0~R3 | 前 4 个参数，halt 后写入，resume 后生效 |
| R0 | 返回值，halt 后读取 |
| float | IEEE 754 单精度，`struct.pack('>f', 25.5).hex()` → `41CC0000` |

### 页面切换函数地址

所有地址使用 `.map` 文件的 link_address（已验证通过）：

| 函数 | map 地址 | 效果 |
|------|---------|------|
| `mode_ui_show()` | **0x08043088** | 当前页 → Mode 信息页 |
| `mode_ui_hide()` | **0x080430CC** | Mode 页 → 主页 |
| `settings_ui_show()` | **0x080434C0** | 当前页 → 设置页 |
| `settings_ui_hide()` | **0x080434E8** | 设置页 → 主页 |
| `clock_ui_show()` | **0x08044648** | 当前页 → 时钟日历页 |
| `clock_ui_hide()` | **0x080445B4** | 时钟页 → 主页 |
| `camera_ui_show()` | **0x080448E8** | 当前页 → 摄像头页 |
| `camera_ui_hide()` | **0x0804499C** | 摄像头页 → 主页 |

### 有参数函数

| 函数 | map 地址 | R0 | R1 | R2 | R3 |
|------|---------|:--:|:--:|:--:|:--:|
| `app_ui_update_sensor()` | **0x08042370** | temp(float) | hum(float) | knob | key_id |
| `NT35510_DrawPixel()` | **0x08040D3A** | x | y | color | — |
| `EN25Q128_ReadID()` | **0x080407E0** | — | — | — | — |

完整地址表见工程的 `harness_ai_toolchain.md` 附录。

---

## 页面切换规则

```
✅ 正确: main → mode → main → settings → main → clock → main → camera → main
❌ 错误: mode → settings（子页间直接跳转会导致系统卡死）
❌ 错误: reg pc + resume（导致 HardFault）
```

每次切换后等待 **≥5 秒**让 LVGL 完成渲染和动画（SPI Flash 加载图片 + FADE_IN 200ms 动画）。

---

## 三种操作层级

### 层级一：内存变量直接读写

通过 OpenOCD TCL 直接读写 STM32 SRAM。

**系统状态变量：**

| 变量 | 地址 | 大小 | 权限 | 说明 |
|------|------|:----:|:----:|------|
| `g_ota_pending` | 0x20017C17 | 1B | RW | 写1触发OTA流程 |
| `g_camera_active` | 0x20017D90 | 1B | RW | 摄像头激活标志 |
| `g_theme_id` | 0x20017C14 | 1B | RW | 主题ID: 0暗黑/1明亮/2护眼 |
| `g_pressed` | 0x20017B80 | 1B | RW | 触摸按下: 0释放/1按下 |
| `g_last_x` | 0x20017B7E | 2B | RW | 最后触摸X坐标(0~799) |
| `g_last_y` | 0x20017B7C | 2B | RW | 最后触摸Y坐标(0~479) |
| `g_width` | 0x200000C2 | 2B | R | LCD宽度 |
| `g_height` | 0x200000C0 | 2B | R | LCD高度 |

**LVGL 屏幕对象指针（判断当前页面）：**

| 变量 | 地址 | 大小 |
|------|------|:----:|
| `ui_scr_main` | 0x20017C50 | 4B |
| `ui_scr_mode` | 0x20017CB8 | 4B |
| `ui_scr_settings` | 0x20017CCC | 4B |
| `ui_scr_clock` | 0x20017D8C | 4B |
| `ui_scr_camera` | 0x20017D94 | 4B |

**OpenOCD 操作示例：**

```tcl
# 读当前主题
mdb 0x20017C14 1
# 切换主题为"明亮"
mwb 0x20017C14 1
# 模拟触摸按下 (x=400, y=200)
mwh 0x20017B7E 400
mwh 0x20017B7C 200
mwb 0x20017B80 1
# 模拟触摸释放
mwb 0x20017B80 0
```

### 层级二：函数调用（已验证）

```tcl
# 切换到 Mode 页
halt
resume 0x08043088      # mode_ui_show (.map 地址，不加1)
sleep 5000
halt                    # 检查 uwTick 是否递增

# 返回主页
halt
resume 0x080430CC      # mode_ui_hide
sleep 5000
halt

# 注入传感器数据 (temp=25.5, hum=60.0)
halt
reg r0 0x41CC0000      # float 25.5
reg r1 0x42700000      # float 60.0
reg r2 512             # knob
reg r3 1               # key_id
resume 0x08042370      # app_ui_update_sensor
sleep 2000
halt
```

### 层级三：AI 工具封装

`SWDInputSimulator` 类封装了以上所有操作，供 AI 调用：

```python
sim = SWDInputSimulator(ocd_client)

# 页面导航（自动处理 main 回退逻辑）
sim.switch_page("mode")      # 进 Mode 页（含 5s LVGL 渲染等待）
sim.switch_page("main")      # 回主页
sim.switch_page("settings")  # 进设置页
sim.switch_page("main")      # 回主页

# 触摸模拟
sim.touch_press(400, 200)    # 按下
sim.touch_release()          # 释放
sim.touch_click(400, 200)    # 点击（按下+释放+轮询）

# 主题切换
sim.set_theme(1)             # 0=暗黑 1=明亮 2=护眼

# 函数调用（底层）
sim.call_func("mode_ui_show", wait_ms=5000)
sim.call_func("app_ui_update_sensor", wait_ms=2000,
              r0=0x41CC0000, r1=0x42700000, r2=512, r3=1)
```

---

## 安全机制

| 保护 | 措施 |
|------|------|
| HardFault 检测 | `call_func` 调用后 `halt` 检查 `reg pc`，检测到 HardFault 则返回失败 |
| 连续操作上限 | 单次 AI 决策最多 15 次导航/旋钮操作 |
| LR 保护 | `resume [addr]` 保留原 LR，函数返回自然回到 RTOS |
| 子代理隔离 | 子代理没有 navigate_page 和 knob_action 工具 |
| 翻页顺序保护 | `switch_page` 自动维护 `_current_page`，禁止子页间直接跳转 |

---

## 和人工操作的区别

| 方面 | 人工操作 | AI 模拟操作 |
|------|---------|------------|
| 速度 | 慢（秒级） | 快（<1s 完成翻页，但需等 5s 渲染） |
| 精度 | 可能按错 | 精确到内存地址 |
| 耐久 | 会累 | 7×24 不间断 |
| 感知 | 看到屏幕画面 | 读到内存中的全部变量 |
| 范围 | 只能操作物理按键 | 可直接调用任何固件函数 |
