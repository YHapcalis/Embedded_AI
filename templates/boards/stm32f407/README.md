# 新工程模板说明

> 这是 `harness init` 生成新工程的蓝本模板。
> 复制 `templates/boards/stm32f407/` 到新项目根目录，替换占位符即可。

## 模板结构

```
templates/boards/stm32f407/
├── CMakeLists.txt        # 顶层构建（含 MCU 参数）
├── Core/
│   ├── Inc/              # 头文件目录
│   └── Src/              # 源码目录
│       ├── main.c        # 主程序入口（FreeRTOS + 示例任务）
│       └── ...
├── boards/               # 板级支持
│   └── stm32f407/        # （由 foundation boards/ 提供 profile.json）
└── openocd.cfg           # 调试配置（按实际修改）
```

## 使用步骤

1. **复制模板**：`cp -r templates/boards/stm32f407/ 新项目/`
2. **改 MCU 名**：CMakeLists.txt 中的 `STM32F407` 相关配置
3. **配置 Profile**：确认 `boards/<mcu>/profile.json` 存在（或用 foundation ProfileManager 创建）
4. **构建**：`python -m foundation build <mcu>`
5. **烧录**：`python -m foundation flash <mcu>`

## 生成 VSCode 配置

```bash
python -m foundation.core.vscode_gen <mcu> --project 新项目/
```

> 详细用法见 `标准流程12步.md` 与 `docs/设计决策与约束.md`
