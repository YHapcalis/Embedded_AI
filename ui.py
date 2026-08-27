"""
ui.py — Harness 终端 UI（基于 Rich）

将纯文本输出升级为彩色面板、进度条、波形预览。

用法:
    from ui import HarnessUI
    ui = HarnessUI()
    ui.show_flash_success(4, 22.1)
"""

import time
import sys
import io
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    Progress, SpinnerColumn, TextColumn,
    BarColumn, TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text
from rich import box

from expectations import CheckResult
from feedback import Observation

# ── Windows GBK 终端兼容 ────────────────────────────────────
# Rich 在 Windows 上默认使用系统编码（GBK），导致 Unicode 字符崩溃
# 方案：用 textwrap 包装 stdout 为 UTF-8
def _safe_stdout():
    """返回一个安全的 stdout 包装器，避免 GBK 编码崩溃"""
    if hasattr(sys.stdout, 'encoding') and sys.stdout.encoding:
        if 'utf' not in sys.stdout.encoding.lower():
            # GBK 终端：用 errors=replace 包装
            buffer = getattr(sys.stdout, 'buffer', None)
            if buffer:
                return io.TextIOWrapper(buffer, encoding='utf-8',
                                       errors='replace')
    return sys.stdout


class HarnessUI:
    """Harness 终端 UI — 基于 Rich 的彩色输出引擎"""

    def __init__(self, plain: bool = False):
        # 使用安全的 stdout（避免 GBK 编码崩溃）
        self.console = Console(file=_safe_stdout(), highlight=False,
                              force_terminal=True, color_system="auto")
        self.plain = plain  # plain=True 时回退到纯文本

    # ── 状态符号 ──────────────────────────────────────────────

    @staticmethod
    def ok(text: str = ""):
        return f"[bold green]OK[/] {text}" if text else "[bold green]OK[/]"

    @staticmethod
    def fail(text: str = ""):
        return f"[bold red]FAIL[/] {text}" if text else "[bold red]FAIL[/]"

    @staticmethod
    def warn(text: str = ""):
        return f"[bold yellow]WARN[/] {text}" if text else "[bold yellow]WARN[/]"

    # ── 编译 ──────────────────────────────────────────────────

    def show_compile(self, success: bool, elapsed: float = 0,
                     log: str = ""):
        """编译结果面板"""
        if success:
            self.console.log(f"[green]编译通过[/] ({elapsed:.1f}s)")
        else:
            panel = Panel(
                f"[red]{log[-600:]}[/red]",
                title="[bold red]编译失败[/]",
                border_style="red",
                width=80,
            )
            self.console.print(panel)

    # ── 烧录 ──────────────────────────────────────────────────

    def show_flash_start(self, segments: list, timeout: int):
        """烧录开始 —— 显示段列表"""
        table = Table(
            title=f"烧录计划 (超时 {timeout}s)",
            box=box.SIMPLE,
            show_header=False,
            width=70,
        )
        total_kb = 0
        for path, _ in segments:
            size = path.stat().st_size if path.exists() else 0
            total_kb += size
            table.add_row(
                f"[cyan]{path.name:30s}[/]",
                f"[dim]{size // 1024}KB[/]",
            )
        self.console.print(table)

    def show_flash_progress(self, segments: list):
        """烧录进度条"""
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeElapsedColumn(),
            console=self.console,
            transient=False,
        ) as progress:
            for path, _ in segments:
                task = progress.add_task(
                    f"[cyan]{path.name}[/]", total=100)
                # 模拟进度（实际无法获取 real-time progress）
                for _ in range(10):
                    progress.update(task, advance=10)
                    time.sleep(0.05)

    def show_flash_success(self, count: int, elapsed: float):
        """烧录成功"""
        self.console.log(
            f"[green]烧录成功[/] — {count} 段, {elapsed:.1f}s"
        )

    def show_flash_fail(self, diagnosis: dict):
        """烧录失败诊断面板"""
        lines = [
            f"[red]{diagnosis['summary']}[/]",
        ]
        for d in diagnosis.get("details", []):
            lines.append(f"  [dim]{d}[/]")
        if diagnosis.get("suggestion"):
            lines.append("")
            lines.append(f"[yellow]建议:[/] {diagnosis['suggestion']}")

        panel = Panel(
            "\n".join(lines),
            title="[bold red]烧录失败诊断[/]",
            border_style="red",
            width=80,
        )
        self.console.print(panel)

    # ── 监控与断言 ────────────────────────────────────────────

    def show_assertions(self, results: list[CheckResult]):
        """断言检查结果表格"""
        table = Table(
            title="断言检查",
            box=box.ROUNDED,
            show_header=True,
            header_style="bold",
            width=80,
        )
        table.add_column("变量", style="cyan")
        table.add_column("结果", justify="center")
        table.add_column("详情")

        for r in results:
            result_str = self.ok() if r.passed else self.fail()
            table.add_row(r.var, result_str, r.detail[:50])

        self.console.print(table)

    def show_samples(self, samples: dict[str, list[dict]]):
        """采样数据波形摘要"""
        for var_name, var_samples in samples.items():
            values = [s["value"] for s in var_samples]
            if not values:
                continue

            # 生成简易 ASCII 波形
            min_v, max_v = min(values), max(values)
            avg_v = sum(values) / len(values)
            range_v = max_v - min_v if max_v != min_v else 1

            bars = []
            for v in values:
                height = int((v - min_v) / range_v * 8)
                bars.append("▁▂▃▄▅▆▇█"[min(height, 7)])

            waveform = "".join(bars)
            self.console.log(
                f"  [cyan]{var_name:20s}[/] {waveform}"
                f"  [dim][{min_v} ~ {max_v} / avg {avg_v:.1f}][/]"
            )

    # ── AI 决策 ──────────────────────────────────────────────

    def show_ai_decision(self, iteration: int, decision: "AIDecision",
                         note: str = ""):
        """AI 决策面板"""
        status = "[green]目标达成[/]" if decision.goal_achieved else "[yellow]继续修改[/]"
        lines = [
            f"分析: {decision.reasoning[:200]}",
        ]
        if decision.code_changes:
            lines.append("")
            lines.append(f"修改 {len(decision.code_changes)} 个文件:")
            for c in decision.code_changes:
                fname = c.get("file", "?")
                desc = c.get("description", "")
                lines.append(f"  - [cyan]{fname}[/] — {desc}")

        panel = Panel(
            "\n".join(lines),
            title=f"[bold]第 {iteration} 轮 AI 决策 — {status}[/]{note}",
            border_style="green" if decision.goal_achieved else "yellow",
            width=80,
        )
        self.console.print(panel)

    # ── 迭代汇总 ──────────────────────────────────────────────

    def show_iteration_header(self, iteration: int, total: int,
                               scenario: str = ""):
        """每轮开始的标题栏"""
        self.console.rule(
            f"[bold]第 {iteration}/{total} 轮[/]"
            + (f" — {scenario[:40]}" if scenario else ""),
            style="blue",
        )

    def show_iteration_summary(self, obs: "Observation",
                                elapsed: float = 0):
        """本轮摘要行"""
        checks = obs.summary()
        status_parts = [
            f"[green]编译OK[/]" if obs.build_success else "[red]编译FAIL[/]",
            f"[green]烧录OK[/]" if obs.flash_success else "[red]烧录FAIL[/]",
            f"[green]{checks}[/]" if obs.all_passed() else f"[red]{checks}[/]",
        ]
        self.console.log(
            f"{' | '.join(status_parts)}  "
            f"[dim]({elapsed:.1f}s)[/]"
        )

    def show_goal(self, iteration: int):
        """目标达成"""
        panel = Panel(
            f"[bold green]第 {iteration} 轮达成目标！[/]",
            border_style="green",
            width=40,
        )
        self.console.print(panel)

    def show_halt(self, reason: str):
        """停止迭代"""
        panel = Panel(
            f"[red]{reason}[/]",
            title="迭代终止",
            border_style="red",
            width=80,
        )
        self.console.print(panel)

    # ── 辅助 ──────────────────────────────────────────────────

    def log(self, msg: str, style: str = "dim"):
        """普通日志"""
        self.console.log(f"[{style}]{msg}[/]")
