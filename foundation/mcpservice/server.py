"""
mcp/server.py — foundation MCP 服务（第 1 层：跨工具标准接口）

把 foundation 引擎能力暴露为 MCP（Model Context Protocol）标准工具，
任意支持 MCP 的 AI 工具（Claude Code / Trae / Cursor / CodeBuddy 等）
一条命令即可接入并直接驱动硬件调试。

对应 STANDARD_PROCESS 准备阶段②（接入 MCP 服务）。

用法:
    # 直接启动 (stdio)
    python -m foundation.mcp.server

    # Claude Code 接入
    claude mcp add foundation-ai -- python E:/嵌入式AI工作流/foundation/mcp/server.py

    # Trae / Cursor 等在 MCP 配置界面添加同一条命令

工具清单:
    chip_identify   — 芯片探测 (CPUID + DEV_ID)
    env_check       — 环境自检
    openocd_list    — OpenOCD 多分支查看
    memory_read     — 读内存
    register_read   — 读寄存器
    halt / resume   — CPU 控制
    diagnose        — 硬件诊断（hardfault/hang/memory）
"""

import sys
from pathlib import Path

# 保证能导入 core 包
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.env_probe import EnvironmentProbe
from core.openocd_registry import OpenOCDRegistry

try:
    from mcp.server.fastmcp import FastMCP
    HAS_MCP = True
    MCP_CLASS = FastMCP
except ImportError:
    try:
        from mcp.server.mcpserver import MCPServer  # mcp 2.x
        HAS_MCP = True
        MCP_CLASS = MCPServer
    except ImportError:
        HAS_MCP = False
        MCP_CLASS = None


# ─── 调试会话封装（轻量，MCP 工具用） ────────────────────────

class _DebugSession:
    """简单的 OpenOCD TCL 调试会话（供 MCP 工具使用）"""

    def __init__(self):
        self.ocd = None
        self._connected = False

    def _ensure_client(self):
        """延迟创建 OpenOCD 客户端（避免 import 循环）"""
        if self.ocd is None:
            try:
                from core.swd_session import SWDDebugSession
                from core.openocd_proc import OpenOCDProcess
                # 启动 OpenOCD（如果未运行）并连接
                proc = OpenOCDProcess()
                if not proc.is_running():
                    proc.start()
                self.ocd = SWDDebugSession()
            except ImportError:
                # 回退：直接 TCL 连接
                from core.tcl_client import TCLClient
                self.ocd = TCLClient()
        return self.ocd

    def connect(self) -> dict:
        client = self._ensure_client()
        if hasattr(client, "connect"):
            return client.connect()
        return {"ok": True}


# 全局调试会话
_session = _DebugSession()


# ─── MCP 服务定义 ────────────────────────────────────────────

def create_server():
    mcp = MCP_CLASS(
        "foundation-ai",
        instructions=(
            "嵌入式 AI 开发工作流工具。"
            "所有硬件操作前必须先调用 chip_identify 或 env_check 确认连接。"
            "支持: 芯片探测、环境检查、OpenOCD 管理、内存/寄存器读写、硬件诊断。"
        ),
    )

    # ── 环境与芯片 ──────────────────────────────────────────

    @mcp.tool()
    def env_check() -> str:
        """环境自检：探测 OpenOCD/编译器/调试器/板卡是否就绪"""
        probe = EnvironmentProbe()
        return probe.summary_text()

    @mcp.tool()
    def openocd_list() -> str:
        """查看已发现的 OpenOCD 多分支（st/esp32/mainline）"""
        reg = OpenOCDRegistry()
        return reg.summary_text()

    @mcp.tool()
    def chip_identify() -> str:
        """
        芯片探测：识别当前连接的芯片（CPUID + DEV_ID）。
        所有硬件操作的第一步，确认连接与芯片型号。
        """
        try:
            client = _session._ensure_client()
            if hasattr(client, "read_chip_id"):
                info = client.read_chip_id()
                cpuid = info.get("cpuid")
                dev_id = info.get("dev_id")
                return (
                    f"CPUID=0x{cpuid:08X} DEV_ID=0x{dev_id:03X}"
                    if cpuid is not None and dev_id is not None
                    else f"芯片信息: {info}"
                )
            # 回退：TCL 直读
            resp = client._tcl_send("mdw 0xE000ED00 1")
            return f"CPUID 原始值: {resp}"
        except Exception as e:
            return f"芯片探测失败: {e}（请确认 OpenOCD 已启动、板卡已连接）"

    # ── 调试操作 ────────────────────────────────────────────

    @mcp.tool()
    def halt() -> str:
        """暂停 CPU（读寄存器/内存前先调用）"""
        client = _session._ensure_client()
        if hasattr(client, "halt"):
            ok = client.halt()
            return "CPU 已暂停" if ok else "暂停失败"
        resp = client._tcl_send("halt")
        return f"halt: {resp or 'ok'}"

    @mcp.tool()
    def resume() -> str:
        """恢复 CPU 运行"""
        client = _session._ensure_client()
        if hasattr(client, "resume"):
            ok = client.resume()
            return "CPU 已恢复" if ok else "恢复失败"
        resp = client._tcl_send("resume")
        return f"resume: {resp or 'ok'}"

    @mcp.tool()
    def register_read(register: str = "all") -> str:
        """读寄存器。register 可选: all/pc/lr/sp/xpsr/msp/psp"""
        client = _session._ensure_client()
        if hasattr(client, "register_read"):
            regs = client.register_read(register)
            return "\n".join(f"{k}=0x{v:X}" for k, v in regs.items())
        resp = client._tcl_send(f"reg {register}")
        return f"{register}: {resp}"

    @mcp.tool()
    def memory_read(address: int, count: int = 1) -> str:
        """读内存。address: 十六进制地址(如 0x20000000), count: 字数"""
        client = _session._ensure_client()
        if hasattr(client, "read_memory_block"):
            values = client.read_memory_block(address, size=4, count=count)
            return " ".join(f"0x{v:08X}" for v in values) if values else "读取失败"
        resp = client._tcl_send(f"mdw 0x{address:08X} {count}")
        return resp or "读取失败"

    # ── 诊断 ────────────────────────────────────────────────

    @mcp.tool()
    def diagnose(problem: str = "hardfault", elf_path: str = "") -> str:
        """
        硬件诊断。problem: hardfault / hang / memory / all
        elf_path: ELF 文件路径（用于崩溃源码定位，可选）
        """
        try:
            from core.diagnostics import DiagnosisEngine
            client = _session._ensure_client()
            engine = DiagnosisEngine(client, elf_path=elf_path)
            if problem == "all":
                reports = engine.diagnose_all()
                return "\n\n".join(f"=== {name} ===\n{r.to_markdown()}"
                                   for name, r in reports.items())
            report = engine.diagnose(problem)
            return report.to_markdown()
        except ImportError as e:
            return f"诊断引擎未就绪: {e}"
        except Exception as e:
            return f"诊断失败: {e}"

    return mcp


# ─── 入口 ────────────────────────────────────────────────────

if __name__ == "__main__":
    if not HAS_MCP:
        print("[mcp_server] 未安装 mcp 库，请执行: pip install 'mcp[cli]'",
              file=sys.stderr)
        sys.exit(1)
    mcp = create_server()
    mcp.run()  # stdio 模式
