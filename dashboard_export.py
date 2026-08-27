"""
dashboard_export.py — Harness 运行数据导出

将每轮迭代数据导出为标准 JSONL 格式，
兼容 AgentFlow Dashboard 等可视化工具。

用法:
    from dashboard_export import DashboardExporter
    exporter = DashboardExporter("./traces")
    exporter.export_observation(obs, scenario)
"""

import json
import time
from pathlib import Path
from typing import Optional

from feedback import Observation
from ai_backend import AIDecision


class DashboardExporter:
    """
    运行数据导出器。

    输出格式: JSONL (每行一条 JSON 记录)
    兼容 AgentFlow Dashboard: npx agentflow-dashboard --traces ./traces
    """

    def __init__(self, trace_dir: str = "./traces"):
        self.trace_dir = Path(trace_dir)
        self.trace_dir.mkdir(exist_ok=True)
        self._session_id = f"harness_{int(time.time())}"
        self._session_file = self.trace_dir / f"{self._session_id}.jsonl"
        self._round = 0

        # 写入 session 头
        self._write({
            "type": "session_start",
            "session_id": self._session_id,
            "timestamp": time.time(),
            "harness": "AI Embedded Harness v1",
        })

    def _write(self, data: dict):
        """写入一行 JSONL"""
        with open(self._session_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")

    def export_observation(self, obs: Observation,
                           scenario: str = ""):
        """导出一轮迭代的观测结果"""
        self._round += 1
        record = {
            "type": "observation",
            "session_id": self._session_id,
            "round": self._round,
            "timestamp": obs.timestamp or time.time(),
            "scenario": scenario[:60],

            # 编译
            "build": {
                "success": obs.build_success,
                "log_tail": obs.build_log[-500:] if obs.build_log else "",
            },

            # 烧录
            "flash": {
                "success": obs.flash_success,
                "log_tail": obs.flash_log[-500:] if obs.flash_log else "",
            },

            # 断言
            "checks": [
                {
                    "var": r.var,
                    "passed": r.passed,
                    "actual": str(r.actual) if r.actual else None,
                    "expected": str(r.expected) if r.expected else None,
                    "detail": r.detail[:100],
                }
                for r in obs.check_results
            ] if obs.check_results else [],

            # 采样摘要
            "samples": {
                var: {
                    "min": min(s["value"] for s in samples) if samples else None,
                    "max": max(s["value"] for s in samples) if samples else None,
                    "count": len(samples),
                }
                for var, samples in obs.samples.items()
            } if obs.samples else {},

            # 异常
            "errors": obs.errors[:5],

            # 总体
            "summary": obs.summary(),
            "all_passed": obs.all_passed(),
        }
        self._write(record)

    def export_decision(self, obs: Observation, decision: AIDecision,
                        scenario: str = ""):
        """导出 AI 决策结果"""
        record = {
            "type": "decision",
            "session_id": self._session_id,
            "round": self._round,
            "timestamp": time.time(),
            "scenario": scenario[:60],

            "goal_achieved": decision.goal_achieved,
            "reasoning": decision.reasoning[:300],

            "code_changes": [
                {
                    "file": c.get("file", ""),
                    "description": c.get("description", "")[:80],
                }
                for c in (decision.code_changes or [])
            ],

            "summary": obs.summary(),
        }
        self._write(record)

    def close(self, reason: str = "complete"):
        """关闭会话"""
        self._write({
            "type": "session_end",
            "session_id": self._session_id,
            "timestamp": time.time(),
            "reason": reason,
            "total_rounds": self._round,
        })
        print(f"\n[DASHBOARD] 运行记录已导出: {self._session_file}")
        print(f"[DASHBOARD] 用以下命令在浏览器查看:")
        print(f"  npx agentflow-dashboard --traces {self.trace_dir}")
