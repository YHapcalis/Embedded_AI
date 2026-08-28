"""
team_orchestrator.py — AI 团队编排（主理人 + 专业成员）

对应 标准流程12步 ④（拉起团队）和 ⑨（团队干活）。
把"主理人管理 + 专业成员执行 + 人类兜底"的协作结构封装成可编程模型。

核心概念:
    TeamOrchestrator — 团队总控
    ├── Lead            主理人（分配/审查/返工/对外）
    ├── TeamMember      专业成员（各负责模块，有文件白名单）
    ├── Task            任务（描述/依赖/负责人/验收项）
    └── SafetyGuard     安全横切面（白名单/操作上限/HardFault）

用法（AI 侧）:
    from core.team_orchestrator import TeamOrchestrator
    team = TeamOrchestrator(project_dir=".")
    team.add_member("driver", ["Core/Src/can.c", "Core/Inc/can.h"])
    team.add_member("ui", ["Core/Src/app_ui.c"])
    task = team.assign("实现 CAN 电量显示", owner="driver",
                       depends_on=[], acceptance=["编译通过", "电量变量 0-100"])
    output = team.collect(task)      # 收集成员产出
    verdict = team.review(task)      # 主理人审查 → 通过/返工
"""

import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional


# ─── 枚举 ────────────────────────────────────────────────────

class TaskStatus(Enum):
    PENDING = "pending"       # 待分配
    IN_PROGRESS = "in_progress"  # 执行中
    DONE = "done"             # 完成待审
    APPROVED = "approved"     # 主理人通过
    REJECTED = "rejected"     # 主理人打回
    BLOCKED = "blocked"       # 卡住，需人类介入


class ReviewVerdict(Enum):
    APPROVE = "approve"
    REJECT = "reject"
    ESCALATE = "escalate"     # 升级给人类


# ─── 数据结构 ────────────────────────────────────────────────

@dataclass
class TeamMember:
    """专业成员：有明确的职责边界和文件白名单"""
    role: str                          # 角色: driver/ui/comm/test...
    whitelist: list = field(default_factory=list)  # 可修改文件（相对路径）
    description: str = ""              # 能力描述
    active_tasks: list = field(default_factory=list)

    def can_modify(self, file_path: str) -> bool:
        """检查是否在白名单内（越界修改拦截）"""
        p = file_path.replace("\\", "/")
        return any(p == w or p.startswith(w.rstrip("/") + "/")
                   for w in self.whitelist)


@dataclass
class Task:
    """任务：描述 + 依赖 + 负责人 + 验收项"""
    id: str
    description: str
    owner: str                         # 成员角色名
    acceptance: list = field(default_factory=list)  # 可量化验收项
    depends_on: list = field(default_factory=list)  # 依赖任务 id
    status: TaskStatus = TaskStatus.PENDING
    output: str = ""                   # 成员产出的说明
    created_at: float = field(default_factory=time.time)


class SafetyGuard:
    """
    安全横切面（贯穿全程，非某一步骤）:
      S1 文件白名单   S2 硬件操作上限   S3 HardFault 检测
    """

    def __init__(self, op_limit: int = 15):
        self.op_limit = op_limit
        self.op_count = 0
        self.fault_detected = False

    def check_file(self, member: TeamMember, file_path: str) -> bool:
        """S1: 越界修改拦截"""
        return member.can_modify(file_path)

    def check_operation(self) -> bool:
        """S2: 硬件操作上限（防烧板）"""
        self.op_count += 1
        if self.op_count > self.op_limit:
            return False
        return True

    def report_fault(self):
        """S3: HardFault 检测"""
        self.fault_detected = True

    def summary(self) -> str:
        parts = []
        parts.append(f"操作计数: {self.op_count}/{self.op_limit}")
        parts.append(f"HardFault: {'检测到!' if self.fault_detected else '无'}")
        return " | ".join(parts)


# ─── 主类 ────────────────────────────────────────────────────

class TeamOrchestrator:
    """AI 团队编排器"""

    def __init__(self, project_dir: str = "."):
        self.project_dir = Path(project_dir)
        self.lead = None
        self.members: dict[str, TeamMember] = {}
        self.tasks: dict[str, Task] = {}
        self.guard = SafetyGuard()
        self._task_counter = 0

    # ─── 团队组建（④拉起团队）─────────────────────────────

    def set_lead(self, description: str = "主理人: 分配/审查/返工/对外沟通"):
        """设置主理人"""
        self.lead = {"description": description}
        return self

    def add_member(self, role: str, whitelist: list,
                   description: str = "") -> TeamMember:
        """添加专业成员（含文件白名单）"""
        member = TeamMember(role=role, whitelist=whitelist,
                            description=description)
        self.members[role] = member
        return member

    def team_summary(self) -> str:
        """团队清单（含能力边界）"""
        lines = ["=== AI 团队 ===", ""]
        if self.lead:
            lines.append(f"[主理人] {self.lead['description']}")
            lines.append("")
        for role, m in self.members.items():
            lines.append(f"[成员:{role}] {m.description or '未声明能力'}")
            lines.append(f"          可改文件: {', '.join(m.whitelist) if m.whitelist else '(无,只读)'}")
        return "\n".join(lines)

    # ─── 任务管理（⑨团队干活）─────────────────────────────

    def assign(self, description: str, owner: str,
               acceptance: list = None,
               depends_on: list = None) -> Optional[Task]:
        """分配任务给成员（校验依赖 + 成员存在）"""
        if owner not in self.members:
            print(f"[团队] 未知成员角色: {owner}，可用: {list(self.members.keys())}")
            return None
        if depends_on:
            for dep in depends_on:
                t = self.tasks.get(dep)
                if t and t.status not in (TaskStatus.APPROVED,):
                    print(f"[团队] 依赖任务未完成: {dep} ({t.status.value})")
                    return None

        self._task_counter += 1
        task = Task(
            id=f"T{self._task_counter}",
            description=description,
            owner=owner,
            acceptance=acceptance or [],
            depends_on=depends_on or [],
        )
        self.tasks[task.id] = task
        self.members[owner].active_tasks.append(task.id)
        task.status = TaskStatus.IN_PROGRESS
        print(f"[团队] 任务 {task.id} '{description}' → {owner}")
        return task

    def collect(self, task_id: str, output: str) -> bool:
        """收集成员产出（被打回的任务可重新提交）"""
        task = self.tasks.get(task_id)
        if not task:
            return False
        task.output = output
        task.status = TaskStatus.DONE
        tag = "（重新提交）" if task.output else ""
        print(f"[团队] 任务 {task_id} 产出已收集{tag}（待主理人审查）")
        return True

    def review(self, task_id: str,
               check_fn: Callable[[Task], ReviewVerdict] = None,
               files: dict = None) -> ReviewVerdict:
        """
        主理人审查任务产出。
        check_fn: 自定义审查函数（返回 APPROVE/REJECT/ESCALATE），
                  缺省用默认规则（有产出 + 有验收项即通过）。
        files: {文件路径: 源码内容} — 提供时自动执行宪法审查
               （embedded-engineering-rules：四层架构/ISR/内存/时序/CubeMX）
        """
        task = self.tasks.get(task_id)
        if not task or task.status != TaskStatus.DONE:
            print(f"[审查] 任务 {task_id} 无产出可审")
            return ReviewVerdict.ESCALATE

        # ★ 宪法审查（如果提供了文件）
        if files:
            try:
                from core.constitution_guard import ConstitutionGuard, Severity
                guard = ConstitutionGuard()
                critical = []
                for fpath, src in files.items():
                    r = guard.review_file(fpath, src)
                    for v in r.violations:
                        if v.severity == Severity.CRITICAL:
                            critical.append(f"  {fpath}: {v}")
                if critical:
                    task.status = TaskStatus.REJECTED
                    print(f"[审查] 任务 {task_id} ❌ 违反工程宪法（{len(critical)} 处违宪）：")
                    for c in critical[:5]:
                        print(c)
                    return ReviewVerdict.REJECT
            except ImportError:
                pass  # 宪法审查器不可用时退回默认规则

        if check_fn:
            verdict = check_fn(task)
        else:
            # 默认规则: 有产出 + 无未验收项
            verdict = (ReviewVerdict.APPROVE if task.output
                       else ReviewVerdict.REJECT)

        if verdict == ReviewVerdict.APPROVE:
            task.status = TaskStatus.APPROVED
            print(f"[审查] 任务 {task_id} ✅ 通过")
        elif verdict == ReviewVerdict.REJECT:
            task.status = TaskStatus.REJECTED
            print(f"[审查] 任务 {task_id} ❌ 打回返工")
        else:
            task.status = TaskStatus.BLOCKED
            print(f"[审查] 任务 {task_id} ⚠️ 升级人类决策")
        return verdict

    def escalate(self, task_id: str, reason: str) -> bool:
        """卡住时呼叫人类"""
        task = self.tasks.get(task_id)
        if not task:
            return False
        task.status = TaskStatus.BLOCKED
        print(f"[人类介入] 任务 {task_id} 需要人类决策: {reason}")
        print(f"  进展: {task.output or '无'}")
        print(f"  请人类提供: 方案取舍 / 范围变更 / 硬件操作许可")
        return True

    # ─── 安全横切面访问 ────────────────────────────────────

    def check_file_access(self, role: str, file_path: str) -> bool:
        """S1: 检查成员能否改某文件"""
        member = self.members.get(role)
        if not member:
            return False
        ok = self.guard.check_file(member, file_path)
        if not ok:
            print(f"[安全] 拦截: {role} 试图修改白名单外文件 {file_path}")
        return ok

    def guard_operation(self) -> bool:
        """S2: 硬件操作计数（超限返回 False）"""
        ok = self.guard.check_operation()
        if not ok:
            print(f"[安全] 硬件操作超限 ({self.guard.op_limit})，停止")
        return ok

    # ─── 状态查看 ──────────────────────────────────────────

    def status(self) -> str:
        lines = ["=== 团队状态 ===", ""]
        for tid, task in self.tasks.items():
            lines.append(f"  {tid}: [{task.status.value:10s}] {task.description}"
                         f" ({task.owner})")
        lines.append("")
        lines.append(f"[安全] {self.guard.summary()}")
        return "\n".join(lines)


# ─── CLI 演示 ────────────────────────────────────────────────

if __name__ == "__main__":
    # 演示: 组建团队 + 派发任务 + 审查
    team = TeamOrchestrator()
    team.set_lead()
    team.add_member("driver", ["Core/Src/can.c", "Core/Inc/can.h"],
                    "CAN 通信与驱动开发")
    team.add_member("ui", ["Core/Src/app_ui.c"], "LVGL 界面开发")

    print(team.team_summary())
    print()

    t1 = team.assign("实现 CAN 电量解析", "driver",
                     acceptance=["编译通过", "电量 0-100"])
    if t1:
        team.collect(t1.id, "can.c: 解析电量字段完成，编译通过")
        team.review(t1.id)

    # 演示白名单拦截
    print()
    print("=== 白名单拦截演示 ===")
    ok = team.check_file_access("driver", "Core/Src/fsmc.c")
    print(f"driver 改 fsmc.c: {'允许' if ok else '被拦截'} ✅")

    print()
    print(team.status())
