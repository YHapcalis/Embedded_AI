# 编码规范补充（coding-standard）

> **定位：这是 `embedded-engineering-rules.md`（工程宪法）的补充，不是替代。**
> 工程宪法的内容（四层架构/六模式/ISR 铁律/内存策略/时序约束/CubeMX 边界）
> 以 `embedded-engineering-rules.md` 为准，本文件只补充它未覆盖的细节。
> 模板版本：v2.0 ｜ 变更：瘦身为"rules 补充"（2026-08-29）

---

## 0. 优先级声明

**规则优先级（从高到低）**：
1. `embedded-engineering-rules.md`（工程宪法：架构/模式/ISR/内存/时序/CubeMX）
2. 本文件（补充：命名/文件头/commit/工作流安全横切面）
3. 项目自己的约定（如 README/工程目标）

冲突时以高优先级为准。

---

## 1. 命名规则（rules 未覆盖的补充）

| 类别 | 规则 | 示例 |
|------|------|------|
| 函数 | 模块名_动作（小写下划线） | `app_ui_update_sensor()` |
| 全局变量 | g_前缀 + 模块名_名称 | `g_can_sensor` |
| 局部变量 | 小写驼峰或下划线 | `temp_val` |
| 宏/常量 | 全大写 + 下划线 | `#define BUF_PIXELS` |
| 类型 | 模块名_类型名（PascalCase） | `typedef struct {...} AppUi_t;` |
| 文件 | 模块名（小写） | `app_ui.c / app_ui.h` |

## 2. 文件头模板

```c
/**
 * @file    module_name.c
 * @brief   模块一句话说明
 * @author  AI Team / <作者>
 * @date    YYYY-MM-DD
 * @note    修改记录: 日期-作者-说明
 */
```

## 3. 错误处理约定

- 函数返回值：`0=成功，非0=错误码`（int 返回）
- 外设操作失败：返回错误码 + 可选日志，**不静默吞错**
- 断言：仅用于"不可能发生"的 invariants，不用于常规错误
- （ISR 内的错误处理铁律见 embedded-engineering-rules.md）

## 4. 工作流安全横切面（AI 团队必须遵守）

1. **文件白名单**：只能修改任务允许的文件，越界修改会被拦截
2. **硬件操作上限**：单会话 15 次硬件操作（halt/resume/写寄存器），防烧板
3. **HardFault 检测**：每次硬件操作后检查，异常立即停止并诊断
4. **编译通过才提交**：不提交编译不过的代码
5. **诚实原则**：工具不可用如实报告，不假装编译/烧录/测试成功

## 5. 提交规范

- commit message 格式：`type(scope): 描述`
  - type: feat / fix / docs / refactor / test / chore
  - 示例：`feat(ota): 增加固件签名校验`
- 每次功能完成一个 commit，不堆积

---

*本文件仅补充 rules 未覆盖的细节。核心约束以 embedded-engineering-rules.md 为准。*
