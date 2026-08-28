"""
session.py — 调试会话抽象（架构无关，SWD/JTAG 双协议）

MCP 风格调试会话（对齐 AixProbe 接口语义），通过 OpenOCD TCL 与目标硬件交互。
**架构无关设计**：
  - 不绑定具体 MCU（F407/F103/ESP32 通用）
  - 协议（SWD/JTAG）由 OpenOCD 配置决定，本模块不感知
  - OpenOCD 分支（ST/Espressif/主线）由 OpenOCDRegistry 自动选择

接口（对齐 skills/SKILL.md 的工具表）:
    connect()          建立会话（必须先调用，否则报 "no active session"）
    halt() / resume()  CPU 暂停/恢复
    register_read()    读寄存器（pc/lr/sp/xpsr/msp/psp/r0-r12/all）
    memory_read()      读内存（单字/块）
    memory_write()     写内存
    read_fault()       读 CFSR/HFSR/MMFAR/BFAR
    read_chip_id()     芯片探测（CPUID + DEV_ID + Flash 大小）
    backtrace()        8 字栈帧回溯
    analyze_exception() 综合异常分析

用法:
    from core.session import DebugSession
    sess = DebugSession()                # 自动发现 OpenOCD 分支
    info = sess.connect()
    sess.halt()
    regs = sess.register_read("all")
"""

import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# 允许直接运行时也能导入 core 包
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from core.openocd_registry import OpenOCDRegistry
except ImportError:
    from openocd_registry import OpenOCDRegistry


# ─── Cortex-M 内核外设寄存器地址（ARM 官方固定）──────────────

SCB_CFSR    = 0xE000ED28   # 可配置故障状态寄存器
SCB_HFSR    = 0xE000ED2C   # HardFault 状态寄存器
SCB_MMFAR   = 0xE000ED38   # MemManage 故障地址寄存器
SCB_BFAR    = 0xE000ED3C   # BusFault 故障地址寄存器
CPUID_ADDR  = 0xE000ED00   # CPUID 基础寄存器
DBGMCU_IDCODE_ADDR = 0xE0042000  # ST/GD 芯片 ID


# ─── OpenOCD 进程管理 ────────────────────────────────────────

class OpenOCDProcess:
    """启动/管理 OpenOCD 进程（按板卡 Profile 选分支）"""

    DEFAULT_TCL_PORT = 6666

    def __init__(self, board_profile: Optional[dict] = None,
                 tcl_port: int = DEFAULT_TCL_PORT):
        self.board_profile = board_profile or {}
        self.tcl_port = tcl_port
        self.process: Optional[subprocess.Popen] = None

    def is_running(self) -> bool:
        """检查 TCL 端口是否有 OpenOCD 在监听"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                s.connect(("127.0.0.1", self.tcl_port))
                return True
        except Exception:
            return False

    def find_openocd(self) -> Optional[str]:
        """通过 Registry 选择 OpenOCD 分支（架构无关的关键）"""
        reg = OpenOCDRegistry()
        entry = reg.resolve(self.board_profile)
        return entry.exe_path if entry else None

    def start(self) -> bool:
        """启动 OpenOCD（如果未运行）"""
        if self.is_running():
            return True
        exe = self.find_openocd()
        if not exe:
            return False
        # 基础命令：启动 TCL 端口（board cfg 由 Profile 提供，暂用默认）
        cmd = [exe, "-c", f"tcl_port {self.tcl_port}",
               "-c", "bindto 0.0.0.0"]
        # 如果有板卡配置，附加
        cfg = self.board_profile.get("openocd_cfg")
        if cfg and Path(cfg).exists():
            cmd = [exe, "-f", cfg, "-c", f"bindto 0.0.0.0"]
        try:
            self.process = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # 等待端口就绪
            for _ in range(20):
                time.sleep(0.5)
                if self.is_running():
                    return True
            return False
        except Exception:
            return False

    def stop(self):
        """停止 OpenOCD"""
        if self.process:
            self.process.terminate()
            self.process = None


# ─── OpenOCD TCL 客户端 ──────────────────────────────────────

class TCLClient:
    """OpenOCD TCL 协议客户端（内存/寄存器/CPU 控制的底层通信）"""

    def __init__(self, host: str = "127.0.0.1", port: int = 6666,
                 gdb_cmd: str = "arm-none-eabi-gdb"):
        self.host = host
        self.port = port
        self.gdb_cmd = gdb_cmd
        self._symbol_cache: dict[str, int] = {}

    def _tcl_send(self, command: str) -> Optional[str]:
        """发送 TCL 命令给 OpenOCD"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(5)
                s.connect((self.host, self.port))
                s.sendall((command + "\x1a").encode("utf-8"))
                data = b""
                while True:
                    try:
                        chunk = s.recv(4096)
                        if not chunk or b"\x1a" in chunk:
                            data += chunk
                            break
                        data += chunk
                    except socket.timeout:
                        break
                return data.decode("utf-8", errors="replace").replace("\x1a", "").strip()
        except Exception:
            return None

    def is_connected(self) -> bool:
        resp = self._tcl_send("target names")
        return resp is not None and len(resp.strip()) > 0

    def read_memory(self, address: int, width: int = 32) -> Optional[int]:
        """读单个内存字（width=8/16/32）"""
        cmd = {8: "mdb", 16: "mdh", 32: "mdw"}.get(width, "mdw")
        resp = self._tcl_send(f"{cmd} 0x{address:08X} 1")
        if resp is None:
            return None
        try:
            parts = resp.split(":")
            if len(parts) > 1:
                val = parts[1].strip().split()[0]
                return int(val, 16)
        except (ValueError, IndexError):
            pass
        return None

    def read_memory_block(self, address: int, size: int = 4, count: int = 8) -> list[int]:
        """连续读内存块（值可能无 0x 前缀，可能跨多行）"""
        cmd = {4: "mdw", 2: "mdh", 1: "mdb"}.get(size, "mdw")
        resp = self._tcl_send(f"{cmd} 0x{address:08X} {count}")
        if not resp:
            return []
        values: list[int] = []
        for line in resp.splitlines():
            line = line.strip()
            if ":" in line:
                line = line.split(":", 1)[1].strip()
            for token in line.split():
                try:
                    values.append(int(token.strip(), 16))
                except ValueError:
                    pass
        return values[:count]

    def write_memory(self, address: int, value: int, width: int = 32) -> bool:
        """写单个内存字"""
        cmd = {8: "mwb", 16: "mwh", 32: "mww"}.get(width, "mww")
        resp = self._tcl_send(f"{cmd} 0x{address:08X} 0x{value:X}")
        return resp is not None

    def resolve_address(self, elf_path: str, expression: str) -> Optional[int]:
        """用 gdb 解析符号地址"""
        elf_path = elf_path.replace("\\", "/")
        cache_key = f"{elf_path}:{expression}"
        if cache_key in self._symbol_cache:
            return self._symbol_cache[cache_key]
        cmd = [self.gdb_cmd, "--batch", "-ex", f'file "{elf_path}"',
               "-ex", f"print &({expression})"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            m = re.search(r"0x([0-9a-fA-F]+)", result.stdout)
            if m:
                addr = int(m.group(1), 16)
                self._symbol_cache[cache_key] = addr
                return addr
        except Exception:
            pass
        return None


# ─── 调试会话（MCP 风格）────────────────────────────────────

class DebugSession:
    """MCP 风格调试会话 — 架构无关的硬件操作接口"""

    REG_NAMES = ["r0", "r1", "r2", "r3", "r4", "r5", "r6", "r7",
                 "r8", "r9", "r10", "r11", "r12", "sp", "lr", "pc", "xpsr"]
    CORE_REGS = ["sp", "lr", "pc", "xpsr", "msp", "psp"]

    def __init__(self, ocd: Optional[TCLClient] = None,
                 board_profile: Optional[dict] = None):
        self.ocd = ocd or TCLClient()
        self.board_profile = board_profile or {}
        self.session_id: Optional[str] = None
        self.arch: Optional[str] = None
        self.target_name: Optional[str] = None
        self._connected = False

    # ── 会话管理 ──────────────────────────────────────────

    def connect(self) -> Optional[dict]:
        """建立调试会话（MCP 语义：所有操作的前置）"""
        if not self.ocd.is_connected():
            # 尝试自动启动 OpenOCD
            proc = OpenOCDProcess(self.board_profile)
            if not proc.start():
                return None
            time.sleep(0.5)
            if not self.ocd.is_connected():
                return None
        resp = self.ocd._tcl_send("target names")
        target = (resp or "").strip().split()[-1] if resp else "unknown"
        t_lower = target.lower()
        if "cortex" in t_lower or "stm32" in t_lower or "gd32" in t_lower:
            self.arch = "cortex_m"
        elif "riscv" in t_lower or "rv32" in t_lower:
            self.arch = "riscv"
        elif "xtensa" in t_lower or "esp" in t_lower:
            self.arch = "xtensa"
        else:
            self.arch = "cortex_m"
        self.target_name = target
        self.session_id = f"sess_{int(time.time())}"
        self._connected = True
        return {"session_id": self.session_id, "arch": self.arch,
                "target_name": self.target_name}

    def is_connected(self) -> bool:
        return self._connected and self.ocd.is_connected()

    # ── CPU 控制 ──────────────────────────────────────────

    def halt(self) -> bool:
        resp = self.ocd._tcl_send("halt")
        time.sleep(0.03)
        return resp is not None

    def resume(self, addr: Optional[int] = None) -> bool:
        cmd = f"resume 0x{addr:X}" if addr is not None else "resume"
        return self.ocd._tcl_send(cmd) is not None

    def reset_run(self) -> bool:
        return self.ocd._tcl_send("reset run") is not None

    # ── 寄存器读取 ────────────────────────────────────────

    def register_read(self, register: str = "all") -> dict[str, int]:
        result: dict[str, int] = {}
        names = self.CORE_REGS if register in ("all", "core") else [register]
        for name in names:
            val = self._read_single_reg(name)
            if val is not None:
                result[name] = val
        if register == "all":
            for name in self.REG_NAMES[:13]:
                val = self._read_single_reg(name)
                if val is not None:
                    result[name] = val
        return result

    def _read_single_reg(self, name: str) -> Optional[int]:
        resp = self.ocd._tcl_send(f"reg {name}")
        if not resp:
            return None
        try:
            m = re.search(r"0x([0-9a-fA-F]+)", resp)
            if m:
                return int(m.group(1), 16)
        except (ValueError, IndexError):
            pass
        return None

    # ── 内存读写 ──────────────────────────────────────────

    def read_memory(self, address: int, size: int = 4, width: int = 32) -> Optional[int]:
        w = {1: 8, 2: 16, 4: 32}.get(size, width)
        return self.ocd.read_memory(address, width=w)

    def read_memory_block(self, address: int, size: int = 4, count: int = 8) -> list[int]:
        return self.ocd.read_memory_block(address, size=size, count=count)

    def write_memory(self, address: int, value: int, width: int = 32) -> bool:
        return self.ocd.write_memory(address, value, width=width)

    # ── Fault 状态读取 ────────────────────────────────────

    def read_fault(self) -> dict[str, Optional[int]]:
        return {
            "cfsr": self.read_memory(SCB_CFSR, size=4),
            "hfsr": self.read_memory(SCB_HFSR, size=4),
            "mmfar": self.read_memory(SCB_MMFAR, size=4),
            "bfar": self.read_memory(SCB_BFAR, size=4),
            "cpuid": self.read_memory(CPUID_ADDR, size=4),
        }

    def read_chip_id(self) -> dict:
        """芯片探测（自动 halt 以确保可读）"""
        # running 态读寄存器会失败，先 halt（真机经验）
        was_halted = False
        try:
            self.halt()
            time.sleep(0.05)
        except Exception:
            pass
        cpuid = self.read_memory(CPUID_ADDR, size=4)
        dev_id_raw = self.read_memory(DBGMCU_IDCODE_ADDR, size=4)
        dev_id = (dev_id_raw & 0xFFF) if dev_id_raw is not None else None
        flash_size = None
        tn = (self.target_name or "").lower()
        if "stm32f1" in tn:
            fs = self.read_memory(0x1FFFF7E0, size=2)
            flash_size = fs
        elif "stm32f4" in tn:
            fs = self.read_memory(0x1FFF7A22, size=2)
            flash_size = fs
        return {"cpuid": cpuid, "dev_id": dev_id, "flash_size_kb": flash_size}

    # ── 栈回溯 ────────────────────────────────────────────

    def backtrace(self, sp: int, use_psp: bool = False) -> dict:
        frame = self.read_memory_block(sp, size=4, count=8)
        if len(frame) < 8:
            return {"frame": frame, "pc": None, "lr": None, "r0": None}
        return {"frame": frame, "r0": frame[0], "lr": frame[5],
                "pc": frame[6], "xpsr": frame[7], "sp_used": sp}

    def analyze_exception(self) -> dict:
        regs = self.register_read("all")
        pc = regs.get("pc")
        lr = regs.get("lr")
        xpsr = regs.get("xpsr")
        exc_number = (xpsr & 0xFF) if xpsr is not None else None
        fault = self.read_fault()
        result = {"registers": regs, "exception_number": exc_number,
                  "fault": fault, "pc": pc, "lr": lr, "xpsr": xpsr}
        use_psp = (lr is not None and (lr & 0xFFFFFFF0) == 0xFFFFFFF0
                   and (lr & 0x0F) == 0x0D)
        stack_ptr = regs.get("psp") if use_psp else regs.get("msp")
        if stack_ptr is not None:
            bt = self.backtrace(stack_ptr, use_psp=use_psp)
            result["backtrace"] = bt
            if bt.get("pc") is not None:
                result["crash_pc"] = bt["pc"]
            if bt.get("lr") is not None:
                result["caller_lr"] = bt["lr"]
        return result


# ─── CLI 演示 ────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== 调试会话演示 ===")
    sess = DebugSession()
    info = sess.connect()
    if info:
        print(f"连接成功: {info}")
        chip = sess.read_chip_id()
        print(f"芯片: CPUID=0x{chip['cpuid']:08X} DEV_ID=0x{chip['dev_id']:03X}"
              if chip['cpuid'] is not None and chip['dev_id'] is not None
              else f"芯片: {chip}")
        fault = sess.read_fault()
        print(f"Fault: CFSR=0x{fault['cfsr']:08X} HFSR=0x{fault['hfsr']:08X}"
              if fault['cfsr'] is not None else "Fault: 读取失败")
    else:
        print("连接失败：请确认 OpenOCD 已启动、板卡已连接")
