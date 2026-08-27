"""
policy.py — 策略门控（Policy Gateway）

Harness Engineering 中的安全层，在 AI 的代码修改被应用之前，
以及编译产物的烧录之前，执行策略检查。

职责:
  1. 文件白名单 — 每个场景只允许修改特定的源文件
  2. 内容安全 — 禁止危险代码模式（修改时钟、Flash 配置等）
  3. ELF 安全 — 编译后检查 ELF 符号是否包含危险配置
  4. 历史审计 — 追踪每次策略拒绝，防止 AI 反复尝试同一违规操作

用法:
    from policy import PolicyGateway

    violations = PolicyGateway.check_code_changes(changes, scenario)
    if violations:
        print(f"策略违规: {violations}")
        # 不应用修改
"""

import re
import subprocess
import time
from pathlib import Path
from typing import Optional


# ── 策略规则库 ────────────────────────────────────────────────


class PolicyRules:
    """
    所有策略规则的定义集。
    规则按场景分组，可以按需扩展。
    """

    # ── 文件白名单 ──
    # 每个场景只允许修改哪些文件（相对项目根目录）
    # key = 场景标识（匹配场景 name 或 description 的子串）
    FILE_WHITELIST: dict[str, list[str]] = {
        "led_blink": [
            "Core/Src/main.c",
            "Core/Src/gpio.c",
            "Core/Inc/main.h",
            "Core/Inc/gpio.h",
        ],
        "pid_tuning": [
            "Core/Src/pid.c",
            "Core/Src/main.c",
            "Core/Inc/pid.h",
        ],
        "can_comm": [
            "Core/Src/can.c",
            "Core/Src/main.c",
            "Core/Inc/can.h",
        ],
        "my_ota_can": [
            "Core/Src/can.c",
            "Core/Src/main.c",
            "Core/Inc/can.h",
        ],
        "heartbeat": [
            "Core/Src/main.c",
            "Core/Src/gpio.c",
        ],
        "sensor_i2c": [
            "Core/Src/i2c.c",
            "Core/Src/main.c",
            "Core/Inc/i2c.h",
        ],
        "bate_camera": [
            "Core/Src/camera.c",
            "Core/Src/main.c",
            "Core/Inc/camera.h",
        ],
        # 纯监控场景 — 不允许修改任何文件
        "full_monitor": [],
        "全链路监控": [],
    }

    # ── 默认白名单（场景未匹配时使用的宽松规则） ──
    DEFAULT_WHITELIST = [
        "Core/Src/main.c",
    ]

    # ── 危险代码模式 ──
    # 每项: (模式描述, 正则表达式, 适用场景例外)
    DANGEROUS_PATTERNS: list[tuple[str, str, list[str]]] = [
        (
            "RCC 时钟配置修改",
            r"HAL_RCC_ClockConfig\s*\(|RCC->CR\s*=|RCC_OscInit\s*\(",
            ["pid_tuning"],  # 调 PID 可能要改时钟？通常不需要，但留个口子
        ),
        (
            "Flash 等待周期修改",
            r"FLASH->ACR\s*=|__HAL_FLASH_SET_LATENCY",
            [],
        ),
        (
            "PWR 电压调节",
            r"HAL_PWR_Config\s*\(|PWR->CR1\s*=",
            [],
        ),
        (
            "GPIO 复用功能重映射",
            r"GPIOA->AFR|GPIOB->AFR|GPIOC->AFR",
            [],
        ),
        (
            "NVIC 优先级分组修改",
            r"HAL_NVIC_SetPriorityGrouping",
            [],
        ),
        (
            "MPU 配置（如果有）",
            r"HAL_MPU_Config|MPU->RNR\s*=",
            [],
        ),
        (
            "system_stm32 文件修改",
            r".*system_stm32.*\.c",
            [],  # 永远不应修改
        ),
        (
            "汇编启动文件修改",
            r".*startup_stm32.*\.s|.*startup_stm32.*\.S",
            [],
        ),
        (
            "链接脚本修改",
            r".*STM32.*\.ld|.*\.lds",
            [],
        ),
        (
            "Debug 配置修改（可能影响 SWD）",
            r"DBGMCU->CR\s*=|HAL_DBGMCU_",
            [],
        ),
    ]

    # ── 编译后 ELF 检查 ──
    # 检查 ELF 中的符号值是否在安全范围内
    ELF_SYMBOL_CHECKS: dict[str, tuple[Optional[int], Optional[int]]] = {
        # 符号名: (min, max) — None 表示不检查该边界
        "HSE_VALUE": (8_000_000, 16_000_000),     # 8-16 MHz 外部晶振
        "PLL_M": (4, 16),                          # PLL 分频系数合理范围
    }


class PolicyGateway:
    """
    策略门控 — 在 AI 代码修改前和编译后执行安全检查。

    本类不抛出异常，所有违规以列表形式返回。
    调用方决定如何处理（跳过修改 / 报告给 AI / 终止迭代）。
    """

    def __init__(self, project_dir: str = "."):
        self.project_dir = Path(project_dir).resolve()
        # 审计日志: [(timestamp, 场景, 违规类型, 详情)]
        self.audit_log: list[tuple[float, str, str, str]] = []
        # 连续违规计数器（同一类型违规连续触发 N 次则升级处理）
        self._consecutive_violations: dict[str, int] = {}

    # ── 检查一：文件白名单 ──────────────────────────────────────

    def check_file_whitelist(self, changes: list[dict],
                             scenario: str = "") -> list[str]:
        """
        检查 AI 要修改的文件是否在场景对应的白名单内。

        匹配逻辑: 场景名包含白名单 key 的子串即匹配。
        如 "LED 闪烁频率调优" 匹配 key "led_blink"。
        """
        matched_key = self._match_scenario(scenario)
        whitelist = PolicyRules.FILE_WHITELIST.get(
            matched_key, PolicyRules.DEFAULT_WHITELIST
        )

        violations = []
        for change in changes:
            file_rel = change.get("file", "")
            if not file_rel:
                continue

            # 检查是否在白名单内
            allowed = any(
                file_rel.endswith(w) or file_rel == w
                for w in whitelist
            )
            if not allowed:
                msg = f"文件 [{file_rel}] 不在场景 [{scenario}] 的白名单内"
                violations.append(msg)

            # 检查是否属于绝对不允许修改的文件
            for desc, pattern, _ in PolicyRules.DANGEROUS_PATTERNS:
                if re.match(pattern, file_rel):
                    msg = f"文件 [{file_rel}] 属于禁止修改类别: {desc}"
                    violations.append(msg)

        return violations

    # ── 检查二：内容安全 ────────────────────────────────────────

    def check_content_safety(self, changes: list[dict],
                             scenario: str = "") -> list[str]:
        """
        检查 AI 要写入的代码内容是否包含危险模式。
        """
        violations = []
        for change in changes:
            file_rel = change.get("file", "")
            content = change.get("content", "")
            if not content:
                continue

            for desc, pattern, exceptions in PolicyRules.DANGEROUS_PATTERNS:
                # 检查是否有场景例外
                if any(e in scenario.lower() for e in exceptions):
                    continue

                # 文件路径模式匹配
                if pattern.startswith(".*") and pattern.endswith(".*"):
                    # 路径模式
                    if re.match(pattern, file_rel):
                        violations.append(
                            f"[{file_rel}] 包含危险操作: {desc}"
                        )
                elif re.search(pattern, content):
                    violations.append(
                        f"[{file_rel}] 包含危险操作: {desc}"
                    )

        return violations

    # ── 检查三：ELF 安全 ────────────────────────────────────────

    def check_elf_symbols(self, elf_path: str) -> list[str]:
        """
        编译后检查 ELF 中的关键符号值是否在安全范围内。

        需要 arm-none-eabi-gdb。
        """
        warnings = []
        elf = Path(elf_path)
        if not elf.exists():
            return [f"ELF 文件不存在: {elf_path}"]

        for sym, (min_val, max_val) in PolicyRules.ELF_SYMBOL_CHECKS.items():
            try:
                r = subprocess.run(
                    ["arm-none-eabi-gdb", "--batch",
                     "-ex", f'file "{elf}"',
                     "-ex", f"print {sym}"],
                    capture_output=True, text=True, timeout=10,
                )
                match = re.search(r"\$\d+\s*=\s*(\d+)", r.stdout)
                if match:
                    val = int(match.group(1))
                    if min_val is not None and val < min_val:
                        warnings.append(
                            f"{sym}={val} < 最小值 {min_val}"
                        )
                    if max_val is not None and val > max_val:
                        warnings.append(
                            f"{sym}={val} > 最大值 {max_val}"
                        )
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass  # GDB 不可用时不阻塞

        return warnings

    # ── 组合检查 ────────────────────────────────────────────────

    def check_all(self, changes: list[dict], scenario: str = "",
                  elf_path: Optional[str] = None) -> list[str]:
        """
        执行所有策略检查，返回所有违规列表。

        参数:
            changes: AI 的代码修改列表
            scenario: 当前场景描述
            elf_path: 编译后的 ELF 路径（可选，仅构建后检查）

        返回:
            violations: 违规描述列表（空列表 = 全部通过）
        """
        all_violations = []

        # 1. 文件白名单
        all_violations.extend(
            self.check_file_whitelist(changes, scenario)
        )

        # 2. 内容安全
        all_violations.extend(
            self.check_content_safety(changes, scenario)
        )

        # 3. ELF 符号检查
        if elf_path:
            all_violations.extend(
                self.check_elf_symbols(elf_path)
            )

        # 记录审计日志
        timestamp = time.time()
        for v in all_violations:
            self.audit_log.append((timestamp, scenario, "violation", v))

        # 更新连续违规计数器
        if all_violations:
            for v in all_violations:
                key = v.split(":")[0] if ":" in v else v[:40]
                self._consecutive_violations[key] = \
                    self._consecutive_violations.get(key, 0) + 1
        else:
            # 本轮无违规，重置计数器
            self._consecutive_violations.clear()

        return all_violations

    # ── 审计 ─────────────────────────────────────────────────────

    @property
    def has_repeated_violations(self) -> bool:
        """是否有连续重复违规（可能 AI 在绕过策略）"""
        return any(count >= 3 for count in self._consecutive_violations.values())

    def get_audit_summary(self, limit: int = 10) -> str:
        """返回最近的审计日志摘要"""
        if not self.audit_log:
            return "无策略审计记录"
        lines = ["── 策略审计日志（最近）──"]
        for ts, scenario, vtype, detail in self.audit_log[-limit:]:
            t = time.strftime("%H:%M:%S", time.localtime(ts))
            lines.append(f"  [{t}] [{scenario}] {vtype}: {detail}")
        return "\n".join(lines)

    # ── 内部方法 ────────────────────────────────────────────────

    def _match_scenario(self, scenario: str) -> Optional[str]:
        """将场景描述匹配到白名单 key"""
        scenario_lower = scenario.lower()
        for key in PolicyRules.FILE_WHITELIST:
            if key in scenario_lower:
                return key
        # 尝试模糊匹配
        for key in PolicyRules.FILE_WHITELIST:
            words = key.split("_")
            if any(w in scenario_lower for w in words):
                return key
        return None

    def reset(self):
        """重置审计状态（开始新场景时调用）"""
        self.audit_log.clear()
        self._consecutive_violations.clear()
