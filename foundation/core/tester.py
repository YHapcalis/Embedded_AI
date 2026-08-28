"""
tester.py — 测试断言引擎（YAML 场景驱动）

从 harness_ai 的 expectations.py 提炼，适配 foundation。
8 种断言检查 + YAML 场景加载。

支持断言:
    frequency / range / monotonic / rate / change_detected /
    pattern / state_machine / stable_after

用法:
    from core.tester import run_scenario
    results = run_scenario(session, "scenarios/led_blink.yaml")
"""

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# 允许直接运行时也能导入 core 包
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from core.session import DebugSession
except ImportError:
    from session import DebugSession


# ─── 数据结构 ────────────────────────────────────────────────

@dataclass
class CheckResult:
    """单条断言结果"""
    variable: str
    check_type: str
    passed: bool = False
    detail: str = ""
    samples: list = field(default_factory=list)

    def __str__(self):
        return f"[{'✅' if self.passed else '❌'}] {self.variable} {self.check_type}: {self.detail}"


# ─── 断言函数 ────────────────────────────────────────────────

def check_range(values: list[float], min_v: float, max_v: float) -> CheckResult:
    """值在 [min, max] 区间"""
    passed = all(min_v <= v <= max_v for v in values if v is not None)
    bad = [v for v in values if v is not None and not (min_v <= v <= max_v)]
    detail = (f"全部在 [{min_v}, {max_v}]" if passed
              else f"越界值: {bad[:3]}")
    return CheckResult("", "range", passed, detail, values)


def check_frequency(values: list[float], target_hz: float,
                    tolerance: float = 0.2) -> CheckResult:
    """信号翻转频率"""
    if len(values) < 4:
        return CheckResult("", "frequency", False, "样本不足", values)
    transitions = sum(1 for i in range(1, len(values))
                      if values[i] != values[i-1])
    duration = len(values) * 0.1  # 假设 100ms 采样间隔
    hz = transitions / max(duration, 0.001)
    passed = abs(hz - target_hz) <= tolerance
    detail = f"实测 {hz:.2f}Hz vs 目标 {target_hz}Hz"
    return CheckResult("", "frequency", passed, detail, values)


def check_monotonic(values: list[float], direction: str = "increasing",
                    ) -> CheckResult:
    """单调递增/递减"""
    passed = True
    for i in range(1, len(values)):
        if values[i] is None or values[i-1] is None:
            continue
        if direction == "increasing" and values[i] < values[i-1]:
            passed = False
            break
        if direction == "decreasing" and values[i] > values[i-1]:
            passed = False
            break
    detail = f"{direction} {'成立' if passed else '被破坏'}"
    return CheckResult("", "monotonic", passed, detail, values)


def check_change_detected(values: list) -> CheckResult:
    """变量在观察期内变化"""
    distinct = set(v for v in values if v is not None)
    passed = len(distinct) > 1
    detail = f"{len(distinct)} 个不同值" if passed else "始终不变"
    return CheckResult("", "change_detected", passed, detail, values)


def check_stable_after(values: list[float], threshold: float = 0.05,
                       ) -> CheckResult:
    """后期趋于稳定（PID 收敛）"""
    if len(values) < 5:
        return CheckResult("", "stable_after", False, "样本不足", values)
    tail = values[-5:]
    valid = [v for v in tail if v is not None]
    if not valid:
        return CheckResult("", "stable_after", False, "尾部无有效值", values)
    spread = max(valid) - min(valid)
    passed = spread <= threshold
    detail = f"尾部波动 {spread:.4f} (阈值 {threshold})"
    return CheckResult("", "stable_after", passed, detail, values)


# ─── 场景执行器 ──────────────────────────────────────────────

def _load_yaml(path: str) -> dict:
    """加载 YAML 场景（pyyaml 可选）"""
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        raise RuntimeError("需要 pyyaml: pip install pyyaml")


def run_scenario(session: DebugSession, scenario_path: str,
                 elf_path: str = "", duration: float = 3.0,
                 interval: float = 0.3) -> list[CheckResult]:
    """
    执行场景中的所有断言。

    场景 YAML 结构:
        targets:
          - variable: "g_can_sensor.valid"
            check: range
            width: 8
            params: {min: 1, max: 1}
    """
    scenario = _load_yaml(scenario_path)
    targets = scenario.get("targets", [])
    results: list[CheckResult] = []

    if not session.is_connected():
        if not session.connect():
            return [CheckResult("", "connect", False, "无法连接 OpenOCD")]

    for t in targets:
        var = t.get("variable", "")
        check_type = t.get("check", "range")
        params = t.get("params", {})
        width = t.get("width", 32)
        ftype = t.get("type", "")  # "float" 时按 IEEE754 解释

        # 采样
        samples = []
        if elf_path:
            addr = session.ocd.resolve_address(elf_path, var)
        else:
            addr = None
        start = time.time()
        while time.time() - start < duration:
            if addr is not None:
                raw = session.ocd.read_memory(addr, width=width)
            else:
                raw = None
            if raw is not None:
                val = _interpret(raw, ftype)
                samples.append(val)
            time.sleep(interval)

        # 执行断言
        if check_type == "range":
            r = check_range(samples, params.get("min", 0), params.get("max", 1))
        elif check_type == "frequency":
            r = check_frequency(samples, params.get("target_hz", 1),
                                params.get("tolerance", 0.2))
        elif check_type == "monotonic":
            r = check_monotonic(samples, params.get("direction", "increasing"))
        elif check_type == "change_detected":
            r = check_change_detected(samples)
        elif check_type == "stable_after":
            r = check_stable_after(samples, params.get("threshold", 0.05))
        else:
            r = CheckResult(var, check_type, False, f"未知断言类型: {check_type}")
        r.variable = var
        r.check_type = check_type
        r.samples = samples
        results.append(r)

    return results


def _interpret(raw: int, ftype: str) -> Any:
    """解释原始值（float 时 IEEE754 转换）"""
    if ftype == "float":
        import struct
        return struct.unpack("<f", (raw & 0xFFFFFFFF).to_bytes(4, "little"))[0]
    return raw


# ─── CLI ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="foundation 测试断言引擎")
    parser.add_argument("scenario", help="场景 YAML 路径")
    parser.add_argument("--elf", default="", help="ELF 路径（符号解析）")
    parser.add_argument("--duration", type=float, default=3.0)
    args = parser.parse_args()

    sess = DebugSession()
    results = run_scenario(sess, args.scenario, elf_path=args.elf,
                           duration=args.duration)
    print(f"=== 测试结果（{len(results)} 项）===")
    passed = 0
    for r in results:
        print(f"  {r}")
        passed += 1 if r.passed else 0
    print(f"\n通过: {passed}/{len(results)}")
    sys.exit(0 if passed == len(results) else 1)
