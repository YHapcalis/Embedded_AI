"""
diagnostics.py — AI 硬件诊断引擎（架构无关）

从 harness_ai 的 diagnosis.py 提炼，适配 foundation 的 session 接口。
三类诊断 + 自动路由 + 结构化 Markdown 报告：

    1. HardFaultDiagnosis  — CFSR/HFSR/MMFAR/BFAR 位域 + 栈回溯 + ELF 源码映射
    2. HangDiagnosis       — 5 次 PC 采样判断死循环/跑飞/中断风暴
    3. MemoryDiagnosis     — SP 栈使用率 + 向量表 + FreeRTOS 堆 + canary

知识表（CFSR/HFSR 位域、异常号、EXC_RETURN、内存布局）与诊断逻辑分离，
换芯片只加 chip-reference 数据，不改诊断流程（对齐技能包方法论）。

用法:
    from core.diagnostics import DiagnosisEngine
    engine = DiagnosisEngine(session, elf_path="build/Debug/xxx.elf")
    report = engine.diagnose("hardfault")   # 或 "设备崩溃了"
    print(report.to_markdown())
"""

import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# 允许直接运行时也能导入 core 包
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from core.session import DebugSession
except ImportError:
    from session import DebugSession


# ─── 知识表（与诊断逻辑分离）────────────────────────────────

EXCEPTION_NAMES = {
    0: "Thread 模式（正常运行）", 3: "HardFault", 4: "MemManage (MPU 违规)",
    5: "BusFault (总线错误)", 6: "UsageFault (用法错误)",
    11: "SVCall", 14: "PendSV (RTOS 上下文切换)", 15: "SysTick",
}

EXC_RETURN_TABLE = {
    0xFFFFFFF1: "Handler 模式 + MSP（嵌套异常）",
    0xFFFFFFF9: "Thread 模式 + MSP（无 OS 场景）",
    0xFFFFFFFD: "Thread 模式 + PSP（RTOS 场景）",
}

CFSR_BITS = [
    (0, "IACCVIOL", "取指访问违规"), (1, "DACCVIOL", "数据访问违规"),
    (3, "MUNSTKERR", "退栈 MemManage 错误"), (4, "MSTKERR", "入栈 MemManage 错误"),
    (7, "MMARVALID", "MMFAR 地址有效"),
    (8, "IBUSERR", "取指总线错误"), (9, "PRECISERR", "精确数据总线错误 (BFAR 有效)"),
    (10, "IMPRECISERR", "非精确总线错误"), (11, "UNSTKERR", "退栈总线错误"),
    (12, "STKERR", "入栈总线错误"), (15, "BFARVALID", "BFAR 地址有效"),
    (16, "UNDEFINSTR", "未定义指令"), (17, "INVSTATE", "无效状态 (Thumb 位错误)"),
    (18, "INVPC", "无效 PC 加载"), (19, "NOCP", "协处理器不存在"),
    (24, "UNALIGNED", "非对齐访问"), (25, "DIVBYZERO", "除零"),
]

HFSR_BITS = [
    (1, "VECTTBL", "向量表读取错误"),
    (30, "FORCED", "强制 HardFault（低优先级 Fault 升级）"),
    (31, "DEBUGEVT", "调试事件触发"),
]

CFSR_CAUSES = {
    "PRECISERR": ("访问了不存在的地址", "检查 BFAR 指向的地址是否越界"),
    "IMPRECISERR": ("非精确总线错误（写缓冲延迟上报）", "检查崩溃点附近的存储访问"),
    "INVSTATE": ("跳转到 ARM 模式（非 Thumb）", "函数指针最低位未置 1"),
    "UNDEFINSTR": ("执行到数据区/垃圾指令", "栈溢出覆盖返回地址 → 检查栈大小"),
    "DIVBYZERO": ("除法除数为 0", "检查崩溃点附近的除法运算"),
    "UNALIGNED": ("非对齐访问", "检查指针强转 / packed 结构体"),
    "IACCVIOL": ("取指访问违规", "PC 跳转到不可执行区域（函数指针错误）"),
    "DACCVIOL": ("数据访问违规", "MPU 配置或非法地址访问"),
    "STKERR": ("入栈失败", "栈溢出！硬件无法压栈现场"),
    "UNSTKERR": ("退栈失败", "栈指针被破坏 / 栈溢出"),
    "IBUSERR": ("取指总线错误", "PC 跳转到不存在的 Flash 地址"),
}

# 内存布局（可扩展：换芯片加条目即可）
FLASH_RANGES = {
    "stm32f4": (0x08000000, 0x08100000),
    "stm32f1": (0x08000000, 0x08020000),
    "stm32f407": (0x08000000, 0x08100000),
    "stm32f103": (0x08000000, 0x08020000),
}
SRAM_RANGES = {
    "stm32f4": (0x20000000, 0x20030000),
    "stm32f1": (0x20000000, 0x20005000),
    "stm32f407": (0x20000000, 0x20030000),
    "stm32f103": (0x20000000, 0x20005000),
}


# ─── ELF 工具 ────────────────────────────────────────────────

class ELFTools:
    """arm-none-eabi 工具链封装：符号解析 + 地址→源码行映射"""

    def __init__(self, elf_path: str = "", tool_prefix: str = "arm-none-eabi-"):
        self.elf_path = Path(elf_path) if elf_path else None
        self.prefix = tool_prefix

    def is_available(self) -> bool:
        if not self.elf_path or not self.elf_path.exists():
            return False
        try:
            r = subprocess.run([f"{self.prefix}nm", "--version"],
                               capture_output=True, timeout=5)
            return r.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def addr2line(self, address: int) -> str:
        if not self.is_available():
            return hex(address)
        try:
            r = subprocess.run(
                [f"{self.prefix}addr2line", "-e", str(self.elf_path),
                 "-f", "-C", f"0x{address:X}"],
                capture_output=True, text=True, timeout=10)
            lines = r.stdout.strip().splitlines()
            if len(lines) >= 2:
                func = lines[0].strip()
                src = lines[1].strip()
                if src != "??:?":
                    return f"{func} @ {src}"
                return f"{func} @ {hex(address)}"
            return hex(address)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return hex(address)

    def format_addr(self, address: Optional[int]) -> str:
        if address is None:
            return "N/A"
        src = self.addr2line(address)
        return f"0x{address:08X} ({src})"


# ─── 诊断报告 ────────────────────────────────────────────────

class DiagnosisReport:
    """结构化诊断报告：Markdown 输出 + 一行摘要"""

    def __init__(self, title: str, conclusion: str = ""):
        self.title = title
        self.conclusion = conclusion
        self.sections: list[tuple[str, str]] = []

    def add(self, section: str, content: str) -> None:
        self.sections.append((section, content))

    def to_markdown(self) -> str:
        lines = [f"## {self.title}", ""]
        for title, content in self.sections:
            lines.append(f"### {title}")
            lines.append("")
            lines.append(content)
            lines.append("")
        if self.conclusion:
            lines.append("### 诊断结论")
            lines.append("")
            lines.append(self.conclusion)
        return "\n".join(lines)

    def summary(self) -> str:
        return f"[{self.title}] {self.conclusion}"


# ─── 诊断 1：HardFault ───────────────────────────────────────

class HardFaultDiagnosis:
    def __init__(self, session: DebugSession, elf_path: str = "",
                 target: str = "stm32f4"):
        self.session = session
        self.elf = ELFTools(elf_path) if elf_path else None
        self.target = target.lower()

    def run(self) -> DiagnosisReport:
        report = DiagnosisReport("HardFault 诊断报告")
        if not self.session.is_connected():
            if not self.session.connect():
                return DiagnosisReport("HardFault 诊断报告",
                                       conclusion="❌ 无法连接 OpenOCD，请检查调试器与目标板")
        self.session.halt()
        time.sleep(0.05)

        regs = self.session.register_read("all")
        pc = regs.get("pc")
        lr = regs.get("lr")
        sp = regs.get("sp")
        xpsr = regs.get("xpsr")
        exc_num = (xpsr & 0xFF) if xpsr is not None else None
        exc_name = EXCEPTION_NAMES.get(exc_num, f"未知异常({exc_num})")

        exc_text = f"- 异常号: {exc_num} → **{exc_name}**\n"
        exc_text += f"- 当前 PC: {self._fmt(pc)}\n"
        exc_text += f"- SP: {self._fmt(sp)}\n"
        exc_text += f"- LR: {self._fmt(lr)}"
        if lr is not None:
            ret = EXC_RETURN_TABLE.get(lr & 0xFFFFFFF0 | (lr & 0x0F))
            if ret:
                exc_text += f"（{ret}）"
        report.add("异常上下文", exc_text)

        fault = self.session.read_fault()
        cfsr = fault.get("cfsr")
        hfsr = fault.get("hfsr")
        mmfar = fault.get("mmfar")
        bfar = fault.get("bfar")
        fault_lines = [f"- CFSR = {self._hex(cfsr)}",
                       f"- HFSR = {self._hex(hfsr)}",
                       f"- MMFAR = {self._hex(mmfar)}",
                       f"- BFAR = {self._hex(bfar)}"]
        report.add("Fault 状态寄存器", "\n".join(fault_lines))

        if cfsr is not None:
            set_bits = [f"- **{name}** [bit{bit}]: {desc}"
                        for bit, name, desc in CFSR_BITS if cfsr & (1 << bit)]
            report.add("CFSR 位域解码",
                       "\n".join(set_bits) if set_bits else
                       "- 无 MemManage/Bus/Usage 位置位（可能为强制 HardFault）")

        if hfsr is not None:
            hf_lines = [f"- **{name}** [bit{bit}]: {desc}"
                        for bit, name, desc in HFSR_BITS if hfsr & (1 << bit)]
            if hf_lines:
                report.add("HFSR 位域解码", "\n".join(hf_lines))

        report.add("栈回溯", self._do_backtrace(regs))
        report.add("根因分析", self._analyze_root_cause(cfsr, hfsr, pc, sp))
        report.conclusion = self._build_conclusion(cfsr, hfsr, pc, sp)
        return report

    def _do_backtrace(self, regs: dict) -> str:
        lr = regs.get("lr")
        sp = regs.get("sp")
        if lr is None or sp is None:
            return "- 缺少 LR/SP 寄存器，无法回溯"
        lr_low = lr & 0x0F
        if lr_low == 0x0D:
            stack, mode = regs.get("psp", sp), "PSP（RTOS 场景）"
        elif lr_low == 0x09:
            stack, mode = regs.get("msp", sp), "MSP（无 OS 场景）"
        elif lr_low == 0x01:
            stack, mode = regs.get("msp", sp), "MSP（Handler 嵌套）"
        else:
            stack, mode = sp, f"未知 EXC_RETURN（LR={hex(lr)}），用当前 SP"

        bt = self.session.backtrace(stack)
        lines = [f"- 栈指针来源: {mode}（SP={self._fmt(stack)}）"]
        if bt.get("pc") is None:
            for alt_name in ["msp", "psp", "sp"]:
                alt = regs.get(alt_name)
                if alt is None or alt == stack:
                    continue
                alt_bt = self.session.backtrace(alt)
                if alt_bt.get("pc") is not None:
                    lines.append(f"- 回退成功（用 {alt_name}=0x{alt:08X}）")
                    bt = alt_bt
                    break
            else:
                lines.append("- ❌ 所有栈指针均无法读取栈帧（硬件现场可能已丢失）")
                return "\n".join(lines)

        lines.append(f"- **崩溃点 PC**（栈帧+0x18）: {self._fmt(bt.get('pc'))}")
        lines.append(f"- **调用者 LR**（栈帧+0x14）: {self._fmt(bt.get('lr'))}")
        lines.append(f"- R0（第一参数）: {self._hex(bt.get('r0'))}")
        crash_pc = bt.get("pc")
        if crash_pc is not None:
            flash_lo, flash_hi = FLASH_RANGES.get(self.target, FLASH_RANGES["stm32f4"])
            if not (flash_lo <= crash_pc < flash_hi):
                lines.append(f"- ⚠️ 崩溃 PC 不在 Flash 范围 ({hex(flash_lo)}-{hex(flash_hi)})"
                             f" → **程序跑飞**（函数指针错误 / 栈溢出覆盖返回地址）")
        return "\n".join(lines)

    def _analyze_root_cause(self, cfsr, hfsr, pc, sp) -> str:
        causes = []
        if cfsr:
            for bit, name, desc in CFSR_BITS:
                if cfsr & (1 << bit) and name in CFSR_CAUSES:
                    hint, direction = CFSR_CAUSES[name]
                    causes.append(f"- **{name}**: {hint} → {direction}")
        if hfsr and (hfsr & (1 << 30)):
            causes.append("- **FORCED=1**: 低优先级 Fault 升级为 HardFault，真正原因见 CFSR")
        if pc is not None:
            flash_lo, flash_hi = FLASH_RANGES.get(self.target, FLASH_RANGES["stm32f4"])
            sram_lo, sram_hi = SRAM_RANGES.get(self.target, SRAM_RANGES["stm32f4"])
            if not (flash_lo <= pc < flash_hi):
                if sram_lo <= pc < sram_hi:
                    causes.append("- **PC 落在 SRAM 区** → 跳转到数据区执行，"
                                  "常见于栈溢出覆盖返回地址或函数指针被破坏")
                else:
                    causes.append(f"- **PC={hex(pc)} 在合法范围外** → 严重跑飞，检查中断向量表")
        if sp is not None:
            sram_lo, _ = SRAM_RANGES.get(self.target, SRAM_RANGES["stm32f4"])
            if sp < sram_lo + 0x100:
                causes.append(f"- **SP={hex(sp)} 接近 SRAM 底部** → 栈几乎耗尽 / 已溢出")
        return "\n".join(causes) if causes else "- 未识别到明确根因，建议结合反汇编进一步分析"

    def _build_conclusion(self, cfsr, hfsr, pc, sp) -> str:
        parts = []
        if cfsr and (cfsr & 0xFF):
            parts.append("MemManage Fault（内存访问违规）")
        elif cfsr and (cfsr & 0xFF00):
            parts.append("BusFault（总线访问错误）")
        elif cfsr and (cfsr & 0xFFFF0000):
            parts.append("UsageFault（用法错误）")
        elif hfsr and (hfsr & (1 << 30)):
            parts.append("强制 HardFault（低优先级 Fault 升级）")
        else:
            parts.append("HardFault")
        detail = []
        if pc is not None:
            flash_lo, flash_hi = FLASH_RANGES.get(self.target, FLASH_RANGES["stm32f4"])
            if not (flash_lo <= pc < flash_hi):
                detail.append("程序跑飞（PC 非法）")
        if sp is not None:
            sram_lo, _ = SRAM_RANGES.get(self.target, SRAM_RANGES["stm32f4"])
            if sp < sram_lo + 0x100:
                detail.append("栈溢出风险")
        return "；".join(parts + detail) if detail else "；".join(parts)

    def _fmt(self, addr: Optional[int]) -> str:
        if addr is None:
            return "N/A"
        if self.elf and self.elf.is_available():
            return f"0x{addr:08X} ({self.elf.addr2line(addr)})"
        return f"0x{addr:08X}"

    @staticmethod
    def _hex(val: Optional[int]) -> str:
        return f"0x{val:08X}" if val is not None else "N/A"


# ─── 诊断 2：挂死 / 跑飞 ─────────────────────────────────────

class HangDiagnosis:
    PC_SAMPLES = 5
    SAMPLE_GAP = 0.2

    def __init__(self, session: DebugSession, elf_path: str = "",
                 target: str = "stm32f4", samples: int = PC_SAMPLES):
        self.session = session
        self.elf = ELFTools(elf_path) if elf_path else None
        self.target = target.lower()
        self.samples = samples

    def run(self) -> DiagnosisReport:
        report = DiagnosisReport("挂死 / 跑飞诊断报告")
        if not self.session.is_connected():
            if not self.session.connect():
                return DiagnosisReport("挂死 / 跑飞诊断报告",
                                       conclusion="❌ 无法连接 OpenOCD")

        pcs = []
        for i in range(self.samples):
            self.session.halt()
            regs = self.session.register_read("pc")
            pcs.append(regs.get("pc") if isinstance(regs, dict) else regs)
            if i < self.samples - 1:
                self.session.resume()
                time.sleep(self.SAMPLE_GAP)
        self.session.halt()

        rows = ["| 采样 | PC | 区域 |", "|------|------|------|"]
        for i, pc in enumerate(pcs, 1):
            rows.append(f"| {i} | {self._fmt(pc)} | {self._region_of(pc)} |")
        report.add("PC 采样结果", "\n".join(rows))

        valid = [pc for pc in pcs if pc is not None]
        if not valid:
            report.conclusion = "❌ 无法读取 PC（目标可能已断开）"
            return report
        conclusion, analysis = self._analyze_distribution(valid)
        report.add("分布模式分析", analysis)
        report.conclusion = conclusion
        return report

    def _analyze_distribution(self, pcs: list[int]) -> tuple[str, str]:
        flash_lo, flash_hi = FLASH_RANGES.get(self.target, FLASH_RANGES["stm32f4"])
        illegal = [pc for pc in pcs if not (flash_lo <= pc < flash_hi)]
        if illegal:
            return ("程序跑飞（PC 不在 Flash 范围）",
                    f"- {len(illegal)}/{len(pcs)} 次采样 PC 不在 Flash 范围\n"
                    f"- 首次非法 PC: {self._fmt(illegal[0])}\n"
                    f"- 可能原因: 栈溢出覆盖返回地址 / 函数指针被破坏 / 中断向量表损坏")
        lo, hi = min(pcs), max(pcs)
        spread = hi - lo
        if spread <= 0x80:
            return ("死循环（PC 高度集中）",
                    f"- {len(pcs)} 次采样 PC 集中在 ±{spread} 字节内\n"
                    f"- 停留位置: {self._fmt(pcs[0])}\n"
                    f"- 常见死循环: while(FLAG==0) 等待中断 / 忙等外设 / while(1) 误入")
        if spread <= 0x200:
            return ("疑似循环交替（两个地址间切换）",
                    f"- PC 在 ±{spread} 字节内交替（{len(set(pcs))} 个不同地址）\n"
                    f"- 可能: 两个函数互相调用 / 中断反复触发")
        return ("程序可能正常运行（PC 分散）",
                f"- {len(set(pcs))} 个不同 PC 且分布分散（±{spread} 字节）\n"
                f"- 问题可能不是挂死，而是: 功能路径未执行 / 通信问题 / 外设无输出")

    def _region_of(self, pc: Optional[int]) -> str:
        if pc is None:
            return "读取失败"
        flash_lo, flash_hi = FLASH_RANGES.get(self.target, FLASH_RANGES["stm32f4"])
        sram_lo, sram_hi = SRAM_RANGES.get(self.target, SRAM_RANGES["stm32f4"])
        if flash_lo <= pc < flash_hi:
            return "Flash"
        if sram_lo <= pc < sram_hi:
            return "SRAM"
        if 0xE0000000 <= pc <= 0xE00FFFFF:
            return "Cortex 系统区"
        return "非法"

    def _fmt(self, addr: Optional[int]) -> str:
        if addr is None:
            return "N/A"
        if self.elf and self.elf.is_available():
            return f"0x{addr:08X} ({self.elf.addr2line(addr)})"
        return f"0x{addr:08X}"


# ─── 诊断 3：内存问题 ────────────────────────────────────────

class MemoryDiagnosis:
    def __init__(self, session: DebugSession, elf_path: str = "",
                 target: str = "stm32f4",
                 free_rtos_heap: Optional[int] = None):
        self.session = session
        self.elf = ELFTools(elf_path) if elf_path else None
        self.target = target.lower()
        self.free_rtos_heap = free_rtos_heap

    def run(self) -> DiagnosisReport:
        report = DiagnosisReport("内存诊断报告")
        if not self.session.is_connected():
            if not self.session.connect():
                return DiagnosisReport("内存诊断报告", conclusion="❌ 无法连接 OpenOCD")
        self.session.halt()

        sram_lo, sram_hi = SRAM_RANGES.get(self.target, SRAM_RANGES["stm32f4"])
        regs = self.session.register_read("sp")
        sp = regs.get("sp")
        if sp is not None:
            used = sram_hi - sp
            pct = min(100.0, max(0.0, (used / (sram_hi - sram_lo)) * 100))
            status = "安全" if pct < 70 else ("警告" if pct < 90 else "⚠️ 溢出风险")
            report.add("栈使用状态",
                       f"- SRAM 范围: {hex(sram_lo)} - {hex(sram_hi)}\n"
                       f"- 当前 SP: {self._fmt(sp)}\n"
                       f"- 已用空间: {used} 字节（{pct:.1f}%）\n"
                       f"- 状态: **{status}**")
        else:
            report.add("栈使用状态", "- SP 读取失败")

        vtor = self.session.read_memory(0xE000ED08, size=4)
        if vtor is not None:
            report.add("向量表检查", f"- VTOR = {hex(vtor)}")

        if self.free_rtos_heap:
            val = self.session.read_memory(self.free_rtos_heap, size=4)
            if val is not None:
                report.add("FreeRTOS 堆", f"- 剩余堆空间: {val} 字节")

        canary_addr = sram_lo + 0x100
        canary = self.session.read_memory_block(canary_addr, size=1, count=64)
        if canary:
            a5_count = sum(1 for b in canary if b == 0xA5)
            report.add("栈 canary 扫描",
                       f"- 扫描 {hex(canary_addr)} 起 64 字节\n"
                       f"- 0xA5 填充: {a5_count}/64"
                       f"（FreeRTOS 任务栈剩余水印）")

        report.conclusion = self._conclude(sp, sram_lo, sram_hi)
        return report

    def _conclude(self, sp: Optional[int], sram_lo: int, sram_hi: int) -> str:
        if sp is None:
            return "SP 读取失败，无法判断"
        if sp < sram_lo + 0x100:
            return "⚠️ 栈已溢出到全局变量区（SP 极低）→ 增大栈空间 / 检查递归与深局部数组"
        pct = (sram_hi - sp) / (sram_hi - sram_lo) * 100
        if pct > 90:
            return "⚠️ 栈使用率超 90%，溢出风险高 → 建议增大栈空间"
        return f"栈使用正常（{pct:.1f}%）"

    def _fmt(self, addr: Optional[int]) -> str:
        if addr is None:
            return "N/A"
        if self.elf and self.elf.is_available():
            return f"0x{addr:08X} ({self.elf.addr2line(addr)})"
        return f"0x{addr:08X}"


# ─── 统一入口 ────────────────────────────────────────────────

class DiagnosisEngine:
    """按问题描述路由到对应诊断（对齐 SKILL.md 路由思想）"""

    DIAGNOSIS_TYPES = {
        "hardfault": HardFaultDiagnosis, "crash": HardFaultDiagnosis,
        "崩溃": HardFaultDiagnosis,
        "hang": HangDiagnosis, "死机": HangDiagnosis, "挂死": HangDiagnosis,
        "跑飞": HangDiagnosis,
        "memory": MemoryDiagnosis, "内存": MemoryDiagnosis, "栈": MemoryDiagnosis,
    }

    def __init__(self, session: DebugSession, elf_path: str = "",
                 target: str = "stm32f4"):
        self.session = session
        self.elf_path = elf_path
        self.target = target

    def diagnose(self, problem: str) -> DiagnosisReport:
        p = problem.lower()
        cls = None
        for key, c in self.DIAGNOSIS_TYPES.items():
            if key in p:
                cls = c
                break
        if cls is None:
            cls = HardFaultDiagnosis
        return cls(self.session, self.elf_path, self.target).run()

    def diagnose_all(self) -> dict[str, DiagnosisReport]:
        results = {}
        for name, cls in [("hardfault", HardFaultDiagnosis),
                          ("hang", HangDiagnosis),
                          ("memory", MemoryDiagnosis)]:
            try:
                results[name] = cls(self.session, self.elf_path, self.target).run()
            except Exception as e:
                results[name] = DiagnosisReport(f"{name} 诊断",
                                                conclusion=f"❌ 诊断异常: {e}")
        return results


# ─── CLI ─────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="AI 硬件诊断引擎（foundation）")
    parser.add_argument("problem", nargs="?", default="hardfault",
                        help="诊断类型: hardfault / hang / memory / all")
    parser.add_argument("--elf", default="", help="ELF 文件路径（源码定位用）")
    parser.add_argument("--target", default="stm32f4",
                        help="目标芯片: stm32f4 / stm32f1")
    parser.add_argument("--markdown", action="store_true",
                        help="输出完整 Markdown 报告")
    args = parser.parse_args()

    sess = DebugSession()
    engine = DiagnosisEngine(sess, elf_path=args.elf, target=args.target)
    if args.problem == "all":
        for name, report in engine.diagnose_all().items():
            print(f"\n{'='*60}\n"
                  f"{report.to_markdown() if args.markdown else report.summary()}")
    else:
        report = engine.diagnose(args.problem)
        print(report.to_markdown() if args.markdown else report.summary())


if __name__ == "__main__":
    main()
