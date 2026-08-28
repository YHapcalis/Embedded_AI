"""
constitution_guard.py — 工程宪法审查器

把 embedded-engineering-rules.md 的关键约束变成可执行检查，
供 TeamOrchestrator.review() 在审查代码产出时自动调用。

检查项（对应宪法章节）:
    R1 四层架构: APPLICATION 层禁止直接调 HAL / 操作寄存器
    R2 ISR 铁律: ISR 内禁止阻塞调用 / HAL_Delay / printf / HAL_UART_Transmit
    R3 共享变量: ISR 与主循环共享变量必须 volatile
    R4 内存策略: 默认禁止 malloc/free/new/delete
    R5 时序约束: 禁止 HAL_Delay 做业务延时
    R6 CubeMX 边界: 非 USER CODE 区改动需标注（默认只改 BEGIN/END 之间）

用法:
    from core.constitution_guard import ConstitutionGuard, ReviewVerdict
    guard = ConstitutionGuard()
    verdict = guard.review_file("Core/Src/app_ui.c", source_code)
    verdict = guard.review_source(source_code, filename="xxx.c")
"""

import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

# 允许直接运行时也能导入 core 包
sys.path.insert(0, str(Path(__file__).parent.parent))


# ─── 枚举 ────────────────────────────────────────────────────

class Severity(Enum):
    CRITICAL = "CRITICAL"   # 违宪，必须拦截
    WARNING = "WARNING"     # 违反建议，提示
    INFO = "INFO"           # 提示


# ─── 数据结构 ────────────────────────────────────────────────

@dataclass
class RuleViolation:
    """单条违规记录"""
    rule: str                # 规则编号 R1-R6
    severity: Severity
    message: str
    line: Optional[int] = None

    def __str__(self):
        loc = f" (行 {self.line})" if self.line else ""
        return f"[{self.severity.value}] {self.rule}: {self.message}{loc}"


@dataclass
class ReviewResult:
    """审查结果"""
    filename: str = ""
    violations: list = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """无 CRITICAL 违规 = 通过"""
        return not any(v.severity == Severity.CRITICAL for v in self.violations)

    @property
    def critical_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == Severity.CRITICAL)

    @property
    def warning_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == Severity.WARNING)

    def summary(self) -> str:
        status = "✅ 通过" if self.passed else f"❌ 拦截（{self.critical_count} 违宪）"
        extra = f" + {self.warning_count} 警告" if self.warning_count else ""
        return f"{self.filename}: {status}{extra}"


# ─── 宪法审查器 ──────────────────────────────────────────────

class ConstitutionGuard:
    """工程宪法审查器：按 embedded-engineering-rules 检查代码"""

    # 四层架构的文件名特征（用于 R1 层归属判断）
    LAYER_PATTERNS = {
        "APPLICATION": [r"app_", r"ui_", r"^main\.c$", r"freertos\.c$"],
        "SERVICE":     [r"sha256", r"lfs_", r"delay\.c$", r"inter_flash_cfg"],
        "DRIVER":      [r"nt35510", r"ov7670", r"en25q128", r"canif", r"sccb"],
        "HAL":         [r"stm32f\dxx_hal", r"cmsis_os", r"startup_", r"system_stm32f"],
    }

    # ISR 内的禁止调用（R2）
    ISR_BLOCKING_CALLS = [
        r"HAL_Delay",
        r"HAL_UART_Transmit",
        r"HAL_UART_Receive",
        r"printf\s*\(",
        r"osDelay\s*\(",
        r"vTaskDelay\s*\(",
        r"while\s*\(\s*.*HAL_GetTick",   # 忙等待
    ]

    # 动态内存（R4）
    DYNAMIC_MEMORY = [
        r"\bmalloc\s*\(", r"\bfree\s*\(", r"\bnew\b", r"\bdelete\b",
        r"pvPortMalloc\s*\(", r"vPortFree\s*\(",
    ]

    def __init__(self, rules_file: Optional[str] = None):
        """
        rules_file: 宪法文件路径（用于记录来源，不强制读取全文）
        """
        self.rules_file = rules_file or (
            str(Path(__file__).parent.parent / "templates"
                / "embedded-engineering-rules.md"))

    # ── 主入口 ──────────────────────────────────────────────

    def review_file(self, filename: str, source: str) -> ReviewResult:
        """审查单个文件（按文件名判断层归属）"""
        result = ReviewResult(filename=filename)
        lines = source.splitlines()

        # 按文件名判断层归属
        layer = self._detect_layer(filename)

        # R1: 层依赖违规
        if layer == "APPLICATION":
            self._check_app_hal_violation(lines, result)
        elif layer == "DRIVER":
            self._check_driver_hal_violation(lines, result)

        # R2/R3: ISR 铁律（检测 ISR 函数体）
        self._check_isr_rules(lines, result)

        # R4: 内存策略
        self._check_memory_rules(lines, result)

        # R5: 时序约束
        self._check_timing_rules(lines, result)

        # R6: CubeMX 边界（检测是否有 USER CODE 区外改动迹象）
        self._check_cubemx_boundary(filename, result)

        return result

    def review_source(self, source: str, filename: str = "unknown.c") -> ReviewResult:
        """审查源码片段（不区分层，只查通用规则）"""
        return self.review_file(filename, source)

    # ── 各规则实现 ──────────────────────────────────────────

    def _detect_layer(self, filename: str) -> Optional[str]:
        """按文件名特征判断四层归属"""
        fname = Path(filename).name
        for layer, patterns in self.LAYER_PATTERNS.items():
            for pat in patterns:
                if re.search(pat, fname):
                    return layer
        return None

    def _check_app_hal_violation(self, lines: list[str], result: ReviewResult):
        """R1: APPLICATION 层禁止直接调 HAL 或操作寄存器"""
        hal_calls = [
            r"HAL_[A-Z]",
            r"\b__HAL_",
            r"->(SR|DR|CR|ODR|IDR|MODER|AFR)\b",  # 直接寄存器访问
            r"GPIO[ABCDEFG]->",
        ]
        for i, line in enumerate(lines, 1):
            if line.strip().startswith("//") or line.strip().startswith("*"):
                continue
            for pat in hal_calls:
                if re.search(pat, line):
                    result.violations.append(RuleViolation(
                        "R1", Severity.CRITICAL,
                        f"APPLICATION 层直接调用 HAL/寄存器: {line.strip()[:60]}", i))
                    break

    def _check_driver_hal_violation(self, lines: list[str], result: ReviewResult):
        """R1: DRIVER 层不应反向依赖上层（引用 app_/ui_ 符号）"""
        for i, line in enumerate(lines, 1):
            if re.search(r"#include\s*[\"<](app_|ui_|freertos)", line):
                result.violations.append(RuleViolation(
                    "R1", Severity.WARNING,
                    f"DRIVER 层反向引用上层头文件: {line.strip()}", i))

    def _check_isr_rules(self, lines: list[str], result: ReviewResult):
        """R2/R3: ISR 铁律"""
        in_isr = False
        isr_name = ""
        for i, line in enumerate(lines, 1):
            # 进入 ISR（xxx_IRQHandler）
            m = re.search(r"void\s+(\w+_IRQHandler)\s*\(", line)
            if m:
                in_isr = True
                isr_name = m.group(1)
                continue
            # 退出 ISR（下一个函数或文件尾）
            if in_isr and re.match(r"^[a-zA-Z_].*\(", line) and "_IRQHandler" not in line:
                in_isr = False

            if in_isr:
                stripped = line.strip()
                # R2: 阻塞调用
                for pat in self.ISR_BLOCKING_CALLS:
                    if re.search(pat, line):
                        result.violations.append(RuleViolation(
                            "R2", Severity.CRITICAL,
                            f"ISR({isr_name}) 内阻塞调用: {stripped[:50]}", i))
                        break
                # R3: ISR 内定义的共享变量未加 volatile
                m2 = re.search(r"\b(static\s+)?(?:uint\d+_t|int\d+_t|char|float)\s+"
                               r"(g_\w+)\s*[=;]", line)
                if m2 and "volatile" not in line:
                    result.violations.append(RuleViolation(
                        "R3", Severity.WARNING,
                        f"ISR({isr_name}) 共享变量 {m2.group(2)} 缺 volatile", i))

    def _check_memory_rules(self, lines: list[str], result: ReviewResult):
        """R4: 默认禁止动态内存"""
        for i, line in enumerate(lines, 1):
            if line.strip().startswith("//") or line.strip().startswith("*"):
                continue
            for pat in self.DYNAMIC_MEMORY:
                if re.search(pat, line):
                    result.violations.append(RuleViolation(
                        "R4", Severity.CRITICAL,
                        f"动态内存分配: {line.strip()[:60]}"
                        f"（默认禁止，改用静态分配/内存池）", i))
                    break

    def _check_timing_rules(self, lines: list[str], result: ReviewResult):
        """R5: 禁止 HAL_Delay 做业务延时（非 ISR 场景）"""
        for i, line in enumerate(lines, 1):
            if re.search(r"HAL_Delay\s*\(", line):
                result.violations.append(RuleViolation(
                    "R5", Severity.WARNING,
                    f"HAL_Delay 业务延时（改用定时器/差值比较）: {line.strip()[:50]}", i))

    def _check_cubemx_boundary(self, filename: str, result: ReviewResult):
        """R6: CubeMX 边界提示"""
        if re.search(r"(stm32f\dxx_hal_|gpio\.c$|main\.c$|freertos\.c$)", filename):
            result.violations.append(RuleViolation(
                "R6", Severity.INFO,
                "CubeMX 生成文件：默认只改 USER CODE BEGIN/END 区域，"
                "修改非 USER CODE 区需二次确认"))


# ─── CLI ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="工程宪法审查器")
    parser.add_argument("file", help="要审查的 C 文件")
    parser.add_argument("--rules", default="", help="宪法文件路径")
    args = parser.parse_args()

    guard = ConstitutionGuard(args.rules or None)
    source = Path(args.file).read_text(encoding="utf-8", errors="replace")
    result = guard.review_file(args.file, source)
    print(f"=== {result.summary()} ===")
    for v in result.violations:
        print(f"  {v}")
