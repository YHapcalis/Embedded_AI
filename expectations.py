"""
expectations.py — 嵌入式硬件断言检查

将原始采样数据转换成结构化 pass/fail 判决，供 AI 直接消费。

断言类型:
  - monotonic:    变量单调递增/递减
  - range:        变量值在期望区间内
  - frequency:    变量翻转频率接近目标值（用于 LED 闪烁/PWM）
  - pattern:      采样序列匹配期望模式
  - state_machine: 状态机按预期顺序跳转
  - stable_after:  一段时间后变量趋于稳定（用于 PID 收敛）
  - change_detected: 变量在观察期内发生改变
"""

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CheckResult:
    """一次断言检查的结果"""
    passed: bool
    var: str
    detail: str = ""
    actual: Any = None
    expected: Any = None

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "variable": self.var,
            "detail": self.detail,
            "actual": str(self.actual) if self.actual is not None else None,
            "expected": str(self.expected) if self.expected is not None else None,
        }


# ─── 检查器 ───────────────────────────────────────────────────────────


def check_monotonic(values: list[float], direction: str = "increasing",
                    var: str = "") -> CheckResult:
    """检查变量是否单调（递增/递减/非严格）"""
    if len(values) < 2:
        return CheckResult(True, var, "不足 2 个样本，默认通过")

    if direction == "increasing":
        ok = all(values[i] <= values[i + 1] for i in range(len(values) - 1))
        desc = "单调递增"
    elif direction == "decreasing":
        ok = all(values[i] >= values[i + 1] for i in range(len(values) - 1))
        desc = "单调递减"
    elif direction == "strict_increasing":
        ok = all(values[i] < values[i + 1] for i in range(len(values) - 1))
        desc = "严格单调递增"
    elif direction == "strict_decreasing":
        ok = all(values[i] > values[i + 1] for i in range(len(values) - 1))
        desc = "严格单调递减"
    else:
        return CheckResult(False, var, f"未知方向: {direction}")

    return CheckResult(
        passed=ok, var=var,
        detail=f"{'[OK]' if ok else '[FAIL]'} {desc}（首={values[0]:.4f}, 末={values[-1]:.4f}）",
        actual=f"{values[0]:.4f} → {values[-1]:.4f}",
        expected=direction,
    )


def check_range(values: list[float], min_v: float, max_v: float,
                var: str = "") -> CheckResult:
    """检查变量值是否在 [min, max] 区间内"""
    if not values:
        return CheckResult(False, var, "无采样数据")

    out_of_range = [v for v in values if v < min_v or v > max_v]
    ok = len(out_of_range) == 0
    actual_min, actual_max = min(values), max(values)

    return CheckResult(
        passed=ok, var=var,
        detail=f"{'[OK]' if ok else '[FAIL]'} 期望 [{min_v}, {max_v}]，"
               f"实际 [{actual_min:.4f}, {actual_max:.4f}]"
               f"{'（有 ' + str(len(out_of_range)) + ' 个越界点）' if not ok else ''}",
        actual=f"[{actual_min:.4f}, {actual_max:.4f}]",
        expected=f"[{min_v}, {max_v}]",
    )


def check_frequency(values: list[float], target_hz: float,
                    tolerance: float = 0.1, sample_interval: float = 0.5,
                    var: str = "") -> CheckResult:
    """
    检查变量翻转频率是否接近目标值。

    适用于 LED 闪烁、PWM 信号等场景。
    通过统计采样值的变化次数来计算频率。
    """
    if len(values) < 3:
        return CheckResult(False, var, f"样本不足（{len(values)}），至少需要 3 个")

    # 统计跳变次数
    transitions = 0
    for i in range(1, len(values)):
        if abs(values[i] - values[i - 1]) > 1e-6:  # 数值变化视为一次跳变
            transitions += 1

    total_time = len(values) * sample_interval
    # 一个完整周期 = 两次跳变（上升+下降）
    actual_hz = transitions / (2 * total_time) if total_time > 0 else 0

    if target_hz == 0:
        ok = actual_hz == 0
    else:
        error = abs(actual_hz - target_hz) / target_hz
        ok = error < tolerance

    return CheckResult(
        passed=ok, var=var,
        detail=f"{'[OK]' if ok else '[FAIL]'} 目标 {target_hz}Hz，"
               f"实际 {actual_hz:.3f}Hz（{transitions} 次跳变 / {total_time:.1f}s）"
               f"{'' if ok else f'，误差 {error*100:.1f}% > 容忍 {tolerance*100:.0f}%'}",
        actual=f"{actual_hz:.3f} Hz",
        expected=f"{target_hz} Hz (±{tolerance*100:.0f}%)",
    )


def check_pattern(values: list[Any], expected: list[Any],
                  var: str = "") -> CheckResult:
    """检查采样值是否匹配期望序列（前 N 个匹配即可）"""
    if not values:
        return CheckResult(False, var, "无采样数据")

    n = min(len(values), len(expected))
    matches = sum(1 for i in range(n) if str(values[i]) == str(expected[i]))
    ok = matches == len(expected) or (len(expected) > 0 and matches >= len(expected) * 0.8)

    return CheckResult(
        passed=ok, var=var,
        detail=f"{'[OK]' if ok else '[FAIL]'} 匹配 {matches}/{len(expected)} 个期望值"
               f"{'（80% 模糊匹配）' if not ok and matches >= len(expected) * 0.8 else ''}",
        actual=values[:len(expected)],
        expected=expected,
    )


def check_state_machine(values: list[int], expected_states: list[int],
                        var: str = "") -> CheckResult:
    """
    检查状态机是否按预期序列跳转。

    提取采样中的唯一状态序列（去重相邻重复值），与期望序列比较。
    """
    if not values:
        return CheckResult(False, var, "无采样数据")

    # 提取实际状态序列（去重相邻重复）
    actual_states = [values[0]]
    for v in values[1:]:
        if v != actual_states[-1]:
            actual_states.append(v)

    # 比较前缀
    min_len = min(len(actual_states), len(expected_states))
    ok = actual_states[:min_len] == expected_states[:min_len]
    complete = len(actual_states) >= len(expected_states)

    return CheckResult(
        passed=ok and complete, var=var,
        detail=f"{'[OK]' if ok and complete else '[FAIL]'} "
               f"期望跳转 {expected_states}，实际跳转 {actual_states}"
               f"{'（未完成所有跳转）' if ok and not complete else ''}"
               f"{'（跳转顺序错误）' if not ok else ''}",
        actual=actual_states,
        expected=expected_states,
    )


def check_change_detected(values: list, var: str = "") -> CheckResult:
    """检查变量在观察期内是否发生了改变"""
    if len(values) < 2:
        return CheckResult(False, var, "不足 2 个样本")

    changed = any(v != values[0] for v in values[1:])
    return CheckResult(
        passed=changed, var=var,
        detail=f"{'[OK]' if changed else '[FAIL]'} 变量{'已' if changed else '未'}变化"
               f"（{len(values)} 次采样，值 {values[0]} → {values[-1]}）",
        actual=f"{values[0]} → {values[-1]}",
        expected="发生变化",
    )


def check_stable_after(values: list[float], threshold: float = 0.05,
                        min_stable_samples: int = 3, var: str = "") -> CheckResult:
    """
    检查变量是否在观察期后半段趋于稳定。

    用于 PID 调参：检查收敛后的稳态误差。
    """
    if len(values) < min_stable_samples * 2:
        return CheckResult(False, var, f"样本不足（{len(values)}）")

    # 取后半段
    half = len(values) // 2
    tail = values[half:]

    if not tail:
        return CheckResult(True, var, "无后半段数据")

    avg = sum(tail) / len(tail)
    max_dev = max(abs(v - avg) for v in tail)
    ok = max_dev < threshold

    return CheckResult(
        passed=ok, var=var,
        detail=f"{'[OK]' if ok else '[FAIL]'} 稳态均值 {avg:.4f}，最大偏差 {max_dev:.4f}"
               f"{'' if ok else f' > 阈值 {threshold}'}",
        actual=f"偏差 {max_dev:.4f}",
        expected=f"偏差 < {threshold}",
    )


def check_rate(values: list[float], target_min: float | None = None,
                target_max: float | None = None,
                sample_interval: float = 0.5, var: str = "") -> CheckResult:
    """
    检查变量的变化率（单位时间增量）。

    用于: 帧率（ov_frame 增量/秒）、计数器增速、累计值等。

    变化率 = (末值 - 首值) / 总时间
    """
    if len(values) < 2:
        return CheckResult(False, var, f"不足 2 个样本（{len(values)}）")

    duration = len(values) * sample_interval
    delta = values[-1] - values[0]
    rate = delta / duration if duration > 0 else 0

    ok = True
    desc_parts = [f"变化率 {rate:.2f}/s"]
    if target_min is not None:
        ok = ok and rate >= target_min
        desc_parts.append(f">= {target_min}")
    if target_max is not None:
        ok = ok and rate <= target_max
        desc_parts.append(f"<= {target_max}")

    return CheckResult(
        passed=ok, var=var,
        detail=f"{'[OK]' if ok else '[FAIL]'} {' '.join(desc_parts)}"
               f"（{values[0]} -> {values[-1]}, {duration:.1f}s）",
        actual=f"{rate:.2f}/s",
        expected=f"{target_min or '-'}~{target_max or '-'}/s",
    )


# ─── 检查调度中枢 ────────────────────────────────────────────────────


def run_checks(samples: dict[str, list[dict]], expectations: list[dict],
               sample_interval: float = 0.5) -> list[CheckResult]:
    """
    对采样数据执行所有断言检查。

    参数:
      samples: {变量名: [{timestamp, value, raw}, ...], ...}
      expectations: 期望列表，每一项格式:
          {"var": str, "check": str, "params": dict}
      sample_interval: 采样间隔（秒），用于频率计算

    返回:
      [CheckResult, ...]
    """
    results = []

    for exp in expectations:
        var = exp["var"]
        check_type = exp["check"]
        params = exp.get("params", {})

        if var not in samples or not samples[var]:
            results.append(CheckResult(False, var, f"变量 '{var}' 无采样数据"))
            continue

        # 提取数值
        raw_values = [s["value"] for s in samples[var]]

        # 类型转换: type: float → IEEE754 32-bit 转 Python float
        var_type = exp.get("type", "")
        if var_type == "float":
            import struct
            values = []
            for v in raw_values:
                try:
                    # raw int → 4 bytes → IEEE754 float
                    packed = struct.pack("<I", int(v) & 0xFFFFFFFF)
                    values.append(struct.unpack("<f", packed)[0])
                except (ValueError, struct.error):
                    values.append(float(v))
        else:
            # 默认: 直接转 float
            values = []
            for v in raw_values:
                try:
                    values.append(float(v) if not isinstance(v, (int, float)) else float(v))
                except (ValueError, TypeError):
                    values.append(float(int(str(v), 16)) if isinstance(v, str) and v.startswith("0x") else 0)

        # 分发检查器
        if check_type == "monotonic":
            r = check_monotonic(values, params.get("direction", "increasing"), var)
        elif check_type == "range":
            r = check_range(values, params["min"], params["max"], var)
        elif check_type == "frequency":
            r = check_frequency(values, params["target_hz"], params.get("tolerance", 0.1), sample_interval, var)
        elif check_type == "pattern":
            r = check_pattern(raw_values, params["expected"], var)
        elif check_type == "state_machine":
            r = check_state_machine(values, params["expected_states"], var)
        elif check_type == "change_detected":
            r = check_change_detected(values, var)
        elif check_type == "stable_after":
            r = check_stable_after(values, params.get("threshold", 0.05),
                                    params.get("min_stable_samples", 3), var)
        elif check_type == "rate":
            r = check_rate(values, params.get("target_min"), params.get("target_max"),
                            sample_interval, var)
        else:
            r = CheckResult(False, var, f"未知检查类型: {check_type}")

        results.append(r)

    return results
