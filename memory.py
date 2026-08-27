"""
memory.py — Harness 的分层记忆系统

对应 Harness Engineering 文章中的记忆分层与 Token 转化流水线。

三层记忆:
  工作记忆 (working)    → 当前会话的 Observation 列表 (在 embed_harness.py 的 self.history 中)
  短期记忆 (short-term) → 近 24 小时的迭代记录 (JSONL 文件)
  长期记忆 (long-term)  → 跨场景的知识沉淀 (SQLite)

用法:
    memory = HarnessMemory(project_dir="E:/.../MY_Car_GUI")
    memory.short_term.append(obs, "LED 闪烁")
    memory.long_term.record(obs, changes, "LED 闪烁")
    context = memory.build_context("LED")
"""

import json
import sqlite3
import time
from pathlib import Path
from typing import Optional


# ── 记忆存储路径 ───────────────────────────────────────────────

_HERE = Path(__file__).parent
MEMORY_DIR = _HERE / ".harness_memory"


def _ensure_dir():
    """确保记忆存储目录存在"""
    MEMORY_DIR.mkdir(exist_ok=True)


# ── 短期记忆（JSONL 文件） ─────────────────────────────────────


class ShortTermMemory:
    """
    短期记忆：以 JSONL 文件形式保留每一轮迭代的完整记录。

    文件名按日期分片: .harness_memory/2026-07-09.jsonl

    每行一条记录，包含编译状态、断言结果、采样摘要、修改内容。
    不保留完整采样数据（只保留统计量）以节省空间。
    """

    def __init__(self, base_dir: Optional[Path] = None):
        _ensure_dir()
        self.base_dir = Path(base_dir) / ".harness_memory" if base_dir else MEMORY_DIR
        self.base_dir.mkdir(exist_ok=True)
        self._today = time.strftime("%Y-%m-%d")
        self._db_path = self.base_dir / f"{self._today}.jsonl"

    def append(self, obs: "Observation", scenario: str,
               changes: list[dict] = None):
        """追加一条迭代记录到当天文件"""
        entry = {
            "timestamp": obs.timestamp,
            "scenario": scenario[:40],
            "iteration": obs.iteration,
            "build_ok": obs.build_success,
            "flash_ok": obs.flash_success,
            "checks_passed": obs.summary(),
            "all_passed": obs.all_passed(),
            "errors": obs.errors[:3],  # 只保留最多 3 个错误
            "changes": [
                {"file": c.get("file", "?"),
                 "description": c.get("description", "")[:80]}
                for c in (changes or [])
            ],
            "sample_stats": {
                var: {
                    "min": min(s["value"] for s in samples) if samples else None,
                    "max": max(s["value"] for s in samples) if samples else None,
                    "count": len(samples),
                }
                for var, samples in obs.samples.items()
            },
        }
        with open(self._db_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def query_recent(self, hours: float = 2,
                     max_entries: int = 10) -> list[dict]:
        """查询最近 N 小时内的记录"""
        if not self._db_path.exists():
            return []
        cutoff = time.time() - hours * 3600
        entries = []
        try:
            with open(self._db_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    e = json.loads(line)
                    if e["timestamp"] >= cutoff:
                        entries.append(e)
        except (json.JSONDecodeError, FileNotFoundError):
            pass
        return entries[-max_entries:]

    def summarize(self, scenario: str = "") -> str:
        """
        生成短期记忆摘要文本（用于注入到给 AI 的提示中）。

        包含:
          - 最近几轮的状态摘要
          - 同场景的模式（如果有）
        """
        recent = self.query_recent(hours=2, max_entries=10)
        if not recent:
            return ""

        lines = ["\n── 短期记忆（近 2 小时） ──"]
        for e in recent[-5:]:
            status = "OK" if e["all_passed"] else "FAIL"
            changes = ", ".join(
                c["file"].split("/")[-1] for c in e.get("changes", [])
            ) or "无修改"
            lines.append(
                f"  第{e['iteration']}轮 [{status}]: "
                f"编译{'OK' if e['build_ok'] else 'FAIL'} "
                f"断言{e['checks_passed']} "
                f"修改={changes}"
            )
        return "\n".join(lines)


# ── 长期记忆（SQLite） ─────────────────────────────────────────


class LongTermMemory:
    """
    长期记忆：基于 SQLite 的经验沉淀。

    记录每个场景每次迭代的"修改了什么 + 结果如何"，
    让 AI 能在未来相似场景中借鉴历史经验。

    表结构:
      iterations:   每次迭代的完整记录
      patterns:     从历史中提取的模式（TODO: 自动挖掘）
    """

    def __init__(self, base_dir: Optional[Path] = None):
        _ensure_dir()
        db_dir = Path(base_dir) / ".harness_memory" if base_dir else MEMORY_DIR
        db_dir.mkdir(exist_ok=True)
        self.conn = sqlite3.connect(str(db_dir / "longterm.db"))
        self._init_db()

    def _init_db(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS iterations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                scenario TEXT NOT NULL,
                iteration INTEGER NOT NULL,
                build_ok INTEGER NOT NULL,
                flash_ok INTEGER NOT NULL,
                all_passed INTEGER NOT NULL,
                errors TEXT,
                changes TEXT,
                sample_stats TEXT
            )
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_scenario
            ON iterations(scenario)
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_timestamp
            ON iterations(timestamp)
        """)
        self.conn.commit()

    def record(self, obs: "Observation", changes: list[dict],
               scenario: str):
        """记录一次迭代到长期记忆"""
        self.conn.execute(
            """INSERT INTO iterations
               (timestamp, scenario, iteration, build_ok, flash_ok,
                all_passed, errors, changes, sample_stats)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                obs.timestamp,
                scenario[:80],
                obs.iteration,
                int(obs.build_success),
                int(obs.flash_success),
                int(obs.all_passed()),
                json.dumps(obs.errors[:5], ensure_ascii=False),
                json.dumps(
                    [{"file": c.get("file"),
                      "description": c.get("description", "")[:80]}
                     for c in (changes or [])],
                    ensure_ascii=False,
                ),
                json.dumps({
                    var: len(samples) for var, samples in obs.samples.items()
                }),
            ),
        )
        self.conn.commit()

    def query_similar(self, scenario: str, limit: int = 5) -> list[dict]:
        """查找同场景/相似场景的历史记录"""
        # 先用精确匹配，再用模糊匹配
        rows = self.conn.execute(
            """SELECT * FROM iterations
               WHERE scenario = ? OR scenario LIKE ?
               ORDER BY timestamp DESC LIMIT ?""",
            (scenario, f"%{scenario[:20]}%", limit),
        ).fetchall()

        if not rows:
            # 按关键词匹配
            words = scenario.lower().replace("_", " ").split()
            conditions = " OR ".join(
                f"scenario LIKE ?" for _ in words[:3]
            )
            if conditions:
                params = [f"%{w}%" for w in words[:3]]
                rows = self.conn.execute(
                    f"SELECT * FROM iterations WHERE {conditions} "
                    f"ORDER BY timestamp DESC LIMIT ?",
                    params + [limit],
                ).fetchall()

        return [
            {
                "scenario": r[2],
                "iteration": r[3],
                "build_ok": bool(r[4]),
                "all_passed": bool(r[6]),
                "errors": json.loads(r[8]) if r[8] else [],
                "changes": json.loads(r[9]) if r[9] else [],
            }
            for r in rows
        ]

    def summarize(self, scenario: str = "") -> str:
        """生成长期记忆摘要（用于提示注入）"""
        similar = self.query_similar(scenario, limit=3)
        if not similar:
            return ""

        lines = ["\n── 长期记忆（同场景历史） ──"]
        for s in similar:
            status = "OK" if s["all_passed"] else "FAIL"
            changes_data = s.get("changes", [])
            if isinstance(changes_data, list) and changes_data:
                if isinstance(changes_data[0], dict):
                    changes = ", ".join(
                        c.get("file", "?").split("/")[-1]
                        for c in changes_data
                    )
                else:
                    changes = str(changes_data[0])[:40]
            else:
                changes = "无"
            lines.append(
                f"  场景[{s['scenario'][:20]}] "
                f"第{s['iteration']}轮 [{status}] "
                f"修改={changes}"
            )
        return "\n".join(lines)

    def close(self):
        self.conn.close()


# ── 统一记忆入口 ───────────────────────────────────────────────


class HarnessMemory:
    """
    统一记忆入口 — 同时管理短期和长期记忆。

    封装了:
      - 短期记忆的写入/查询
      - 长期记忆的写入/查询
      - 给 AI 的上下文构建（合并短期+长期）
    """

    def __init__(self, project_dir: str = "."):
        base = Path(project_dir).resolve()
        self.short_term = ShortTermMemory(base_dir=base)
        self.long_term = LongTermMemory(base_dir=base)

    def record(self, obs: "Observation", scenario: str = "",
               changes: list[dict] = None):
        """同时记录到短期和长期记忆"""
        self.short_term.append(obs, scenario, changes)
        self.long_term.record(obs, changes or [], scenario)

    def record_decision(self, obs: "Observation", decision: "AIDecision",
                        scenario: str = ""):
        """
        记录 AI 决策结果到记忆（在 auto_mode 中调用）。

        与 record() 不同，此方法专门记录"决策+修改"信息，
        供后续迭代参考。
        """
        changes = [
            {"file": c.get("file"),
             "description": c.get("description", "")[:80]}
            for c in (decision.code_changes or [])
        ]
        self.long_term.record(obs, changes, scenario)
        # 同时更新短期记忆的最后一条记录（添加 change 信息）
        self.short_term.append(obs, scenario, changes)

    def build_context(self, scenario: str = "") -> str:
        """
        构建给 AI 的完整历史上下文。

        合并短期记忆和长期记忆，按时间倒序排列。
        空结果返回空字符串（不注入）。
        """
        short = self.short_term.summarize(scenario)
        long_ = self.long_term.summarize(scenario)

        parts = []
        if short:
            parts.append(short)
        if long_:
            parts.append(long_)

        return "\n".join(parts)

    def close(self):
        self.long_term.close()
