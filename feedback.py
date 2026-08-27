"""
feedback.py — 硬件观测结果 → AI 结构化反馈

将编译日志、烧录结果、变量采样、断言检查打包成 AI Agent
可以直接解析的反馈格式。
"""

import json
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional
from pathlib import Path

from expectations import CheckResult, run_checks


# ─── Token 预算分配器 ──────────────────────────────────────────
# 对应文章中 Token 转化流水线的"预算分配"步骤


class TokenBudget:
    """
    Token 预算分配器 — 为不同信息类型分配字符额度。

    模拟文章中"预算分配"的概念：
    在有限的 Token 窗口内，为不同类别的信息分配合理空间，
    确保关键信息不被低价值信息淹没。

    用法:
        budget = TokenBudget()
        compressed = budget.compress("很长的文本...", "errors")
    """

    # 各信息段的最大字符数
    ALLOCATION = {
        "status": 300,       # 编译/烧录/断言状态摘要
        "build_log": 600,    # 编译错误日志
        "flash_log": 300,    # 烧录日志
        "assertions": 600,   # 断言检查结果
        "samples": 400,      # 采样数据摘要
        "errors": 400,       # 异常信息
        "memory": 500,       # 历史记忆
    }

    @classmethod
    def compress(cls, text: str, section: str) -> str:
        """
        按预算智能截断文本。

        策略:
          - 首尾保留法：保留头部（关键状态）和尾部（最新错误），
            省略中间（常规过程信息）
          - 断行对齐：截断时以换行符为界，避免截断单词
        """
        budget = cls.ALLOCATION.get(section, 300)
        if len(text) <= budget:
            return text

        # 智能截断：保留头部和尾部
        head_lines = text.split("\n")
        tail_text = head_lines[-1] if len(head_lines) > 1 else ""

        head_budget = budget - len(tail_text) - 30
        if head_budget < 50:
            # 预算太少，只保留尾部
            return f"...（省略 {len(text) - budget} 字符）\n{tail_text[-budget:]}"

        head = ""
        for line in head_lines:
            if len(head) + len(line) + 3 > head_budget:
                break
            head += line + "\n"

        omitted = len(text) - len(head) - len(tail_text)
        return f"{head}...（省略 {omitted} 字符）\n{tail_text}"


@dataclass
class Observation:
    """
    一次完整的硬件观测结果。

    包含从编译到硬件采样到断言判决的全链路信息，
    是 AI 判断"当前状态"的唯一依据。
    """
    # 元信息
    iteration: int = 0
    timestamp: float = 0.0

    # 编译阶段
    build_success: bool = False
    build_log: str = ""

    # 烧录阶段
    flash_success: bool = False
    flash_log: str = ""

    # 采样原始数据: {变量名: [{timestamp, value, raw}, ...]}
    samples: dict[str, list[dict]] = field(default_factory=dict)

    # 断言检查结果
    check_results: list[CheckResult] = field(default_factory=list)

    # 异常信息
    errors: list[str] = field(default_factory=list)

    def to_ai_prompt(self, scenario_desc: str = "",
                     memory_context: str = "",
                     compact: bool = False) -> str:
        """
        生成给 AI Agent 的结构化报告。

        这是 Harness 与 AI 之间的核心接口——AI 通过阅读
        这个字符串来理解硬件当前的状态。

        参数:
            scenario_desc: 场景描述
            memory_context: 历史记忆上下文（来自 HarnessMemory）
            compact: 是否启用 Token 预算压缩（长文本时启用）
        """
        budget = TokenBudget if compact else None
        lines = []

        # ── 标题栏（不压缩） ──
        lines.append("=" * 60)
        lines.append(f"【硬件反馈】第 {self.iteration} 轮")
        lines.append("=" * 60)

        if scenario_desc:
            lines.append(f"\n目标: {scenario_desc}")

        # ── 状态摘要行（一行看清全局） ──
        status_parts = [
            f"{'[OK]' if self.build_success else '[FAIL]'}编译",
            f"{'[OK]' if self.flash_success else '[FAIL]'}烧录",
            self.summary(),
        ]
        status_line = " | ".join(status_parts)
        lines.append(f"\n状态: {status_line}")
        lines.append(f"时间: {time.strftime('%H:%M:%S', time.localtime(self.timestamp))}")

        # ── 记忆上下文（可选注入） ──
        if memory_context:
            if budget:
                lines.append(budget.compress(memory_context, "memory"))
            else:
                lines.append(memory_context)

        # ── 阶段一：编译 ──
        if not self.build_success:
            log_text = f"  ```\n{self._truncate(self.build_log, 800)}\n  ```"
            if budget:
                log_text = budget.compress(log_text, "build_log")
            lines.append(f"\n── 编译 ──")
            lines.append("  [FAIL] 编译失败")
            lines.append(log_text)
        else:
            lines.append(f"\n── 编译 ── 通过")

        # ── 阶段二：烧录 ──
        if not self.flash_success and self.flash_log:
            log_text = f"  ```\n{self._truncate(self.flash_log, 400)}\n  ```"
            if budget:
                log_text = budget.compress(log_text, "flash_log")
            lines.append(f"\n── 烧录 ──")
            lines.append("  [FAIL] 烧录失败")
            lines.append(log_text)
        else:
            pass  # 烧录成功不单独占行（已在状态行显示）

        # ── 阶段三：断言检查（核心信息，全文保留） ──
        if self.check_results:
            lines.append(f"\n── 断言检查 ({self.summary()}) ──")
            for r in self.check_results:
                lines.append(f"  {r.detail}")

        # ── 阶段四：采样统计摘要 ──
        if self.samples:
            lines.append(f"\n── 采样 ──")
            for var_name, var_samples in self.samples.items():
                values = [s["value"] for s in var_samples]
                if not values:
                    continue
                if compact and len(values) > 3:
                    # 压缩模式：只输出统计量
                    stats = {
                        "min": min(values), "max": max(values),
                        "avg": sum(values) / len(values),
                    }
                    lines.append(
                        f"  {var_name}: [{stats['min']} ~ {stats['max']}] "
                        f"平均 {stats['avg']:.1f} ({len(values)} 个样本)"
                    )
                else:
                    # 完整模式：输出前 10 个值
                    show = values[:10]
                    suffix = f" ...（共 {len(values)} 个）" if len(values) > 10 else ""
                    lines.append(f"  {var_name}: {show}{suffix}")

        # ── 异常 ──
        if self.errors:
            errors_text = "\n".join(f"  [WARN] {e}" for e in self.errors)
            if budget:
                errors_text = budget.compress(errors_text, "errors")
            lines.append(f"\n── 异常 ──")
            lines.append(errors_text)

        # ── 诊断建议（基于断言失败的根因推测） ──
        failed_checks = [r for r in self.check_results if not r.passed]
        if failed_checks:
            diagnoses = []
            for r in failed_checks:
                detail = r.detail.lower()
                if "frequency" in detail:
                    diagnoses.append(
                        f"  [建议] {r.var} 频率未达标，调整延时参数或定时器 PSC/ARR"
                    )
                elif "range" in detail and "越界" in detail:
                    diagnoses.append(
                        f"  [建议] {r.var} 越界，检查限幅逻辑或传感器值范围"
                    )
                elif "monotonic" in detail:
                    direction = "递增" if "increasing" in detail else "递减"
                    diagnoses.append(
                        f"  [建议] {r.var} 应单调{direction}但未满足，检查数据处理逻辑"
                    )
                elif "change_detected" in detail and "未变化" in detail:
                    diagnoses.append(
                        f"  [建议] {r.var} 未变化，检查外设初始化/中断使能/时钟配置"
                    )
                elif "stable_after" in detail:
                    diagnoses.append(
                        f"  [建议] {r.var} 未收敛到稳态，调整 PID 参数"
                    )
            if diagnoses:
                lines.append(f"\n── 诊断建议 ──")
                lines.extend(diagnoses)

        # ── 结论 ──
        passed = sum(1 for r in self.check_results if r.passed)
        total = len(self.check_results)
        if total > 0:
            lines.append(f"\n── 结论 ──")
            if passed == total:
                lines.append("[OK] 所有检查通过！目标可能已达成。")
            else:
                lines.append(f"[FAIL] 通过 {passed}/{total} 项检查，需要继续修改。")
                # 指出具体哪个没通过
                for r in failed_checks:
                    lines.append(f"  └─ {r.var}: {r.expected} (实际: {r.actual})")

        lines.append("=" * 60)
        return "\n".join(lines)

    def summary(self) -> str:
        """简短摘要"""
        passed = sum(1 for r in self.check_results if r.passed)
        total = len(self.check_results)
        if total == 0:
            return "无断言检查"
        return f"{passed}/{total} 通过"

    def all_passed(self) -> bool:
        """所有断言检查是否通过"""
        return len(self.check_results) > 0 and all(r.passed for r in self.check_results)

    def to_json(self) -> str:
        """导出 JSON 供其他工具消费"""
        data = {
            "iteration": self.iteration,
            "timestamp": self.timestamp,
            "build_success": self.build_success,
            "flash_success": self.flash_success,
            "errors": self.errors,
            "check_results": [r.to_dict() for r in self.check_results],
            "summary": self.summary(),
            "all_passed": self.all_passed(),
        }
        return json.dumps(data, indent=2, ensure_ascii=False)

    @staticmethod
    def _truncate(text: str, max_len: int) -> str:
        if len(text) <= max_len:
            return text
        return text[:max_len] + f"\n...（共 {len(text)} 字符，截断显示 {max_len}）"


@dataclass
class AIDecision:
    """
    AI Agent 对反馈的决策结果。

    包含: 目标是否达成、要修改什么、怎么改。
    """
    goal_achieved: bool = False
    reasoning: str = ""
    code_changes: list[dict] = field(default_factory=list)
    # code_changes: [{"file": "Core/Src/main.c", "description": "修改 HAL_Delay 参数", "content": "..."}, ...]

    def to_dict(self) -> dict:
        return {
            "goal_achieved": self.goal_achieved,
            "reasoning": self.reasoning,
            "code_changes": [
                {"file": c["file"], "description": c.get("description", "")}
                for c in self.code_changes
            ],
        }


def parse_check_results(checks: list[dict]) -> list[CheckResult]:
    """从 dict 列表恢复 CheckResult 对象"""
    return [
        CheckResult(
            passed=c.get("passed", False),
            var=c.get("variable", c.get("var", "?")),
            detail=c.get("detail", ""),
            actual=c.get("actual"),
            expected=c.get("expected"),
        )
        for c in checks
    ]
