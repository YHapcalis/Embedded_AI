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


# Cortex-M CPUID 地址（链路健康判据：能读到此寄存器 = SWD/JTAG 链路真实可用）
CORTEX_M_CPUID = 0xE000ED00


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

    def find_openocd(self) -> Optional[object]:
        """通过 Registry 选择 OpenOCD 分支。

        返回完整 OpenOCDEntry（含 exe_path 和 script_dir）。
        修复：旧版只返回 exe_path，丢失 script_dir 导致冷启动时
        OpenOCD 找不到 interface/stlink-dap.cfg（2026-08-29 真机复现时发现）。
        """
        reg = OpenOCDRegistry()
        entry = reg.resolve(self.board_profile)
        return entry if entry else None

    @staticmethod
    def _link_healthy(client, retries: int = 5, delay: float = 0.3) -> bool:
        """链路健康判据（诚实原则：TCL 端口通 ≠ 目标链路通）。

        Cortex-M 目标：必须能通过调试链路读到 CPUID。
        RISC-V/Xtensa 目标：无 CPUID，能应答 target names 即视为健康。
        带重试窗口：connect_assert_srst 配置下，init 后芯片仍在复位释放期，
        立即读会失败（2026-08-29 冷启动验证实测）。
        """
        for i in range(retries):
            if not client.is_connected():
                return False
            names = (client._tcl_send("target names") or "").lower()
            if "riscv" in names or "esp" in names or "xtensa" in names:
                return True
            if client.read_memory(CORTEX_M_CPUID, width=32) is not None:
                return True
            if i < retries - 1:
                time.sleep(delay)
        return False

    def _terminate_stale(self):
        """清理遗留的坏 openocd 进程（仅当端口属主确认为 openocd 镜像）。

        背景：验收复现时发现遗留坏实例占住 TCL 端口、SWD 链路已断，
        新会话拿到的是缓存寄存器值。只杀镜像名为 openocd 的进程，避免误伤。
        """
        try:
            if sys.platform == "win32":
                out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                                     capture_output=True, text=True,
                                     timeout=10).stdout or ""
                for line in out.splitlines():
                    if f":{self.tcl_port} " in line and "LISTENING" in line:
                        pid = line.split()[-1]
                        img = subprocess.run(
                            ["tasklist", "/FI", f"PID eq {pid}"],
                            capture_output=True, text=True, timeout=10).stdout or ""
                        if "openocd" in img.lower():
                            subprocess.run(["taskkill", "/PID", pid, "/F"],
                                           capture_output=True, timeout=10)
                            # 强杀会中断 SWD 事务，ST-Link 固件需要恢复窗口
                            # （实测：杀完立即重拉 → 新实例 CTRL/STAT 全失败）
                            time.sleep(2.0)
            else:
                # Unix：只匹配带 tcl_port 参数的 openocd，保守清理
                subprocess.run(["pkill", "-f", "openocd.*tcl_port"],
                               capture_output=True, timeout=5)
                time.sleep(0.5)
        except Exception:
            pass  # 清理失败不假装成功，后续启动会如实报错

    def start(self) -> bool:
        """启动 OpenOCD（如果未运行）。

        端口已占用时先做链路健康检查：
        - 健康（能读到 CPUID）→ 直接复用现有实例
        - 不健康（坏实例占坑）→ 清理后自行启动，防止拿到缓存值
        """
        if self.is_running():
            probe = TCLClient(port=self.tcl_port)
            if self._link_healthy(probe):
                return True
            print("[session] TCL 端口被占用但目标链路不健康，清理遗留实例...",
                  file=sys.stderr)
            self._terminate_stale()
            if self.is_running():
                print("[session] 遗留实例无法清理，启动失败", file=sys.stderr)
                return False

        entry = self.find_openocd()
        if not entry:
            return False

        # 脚本搜索路径：Profile 显式指定 > Registry 记录的 script_dir
        scripts = (self.board_profile.get("openocd_scripts")
                   or getattr(entry, "script_dir", "") or "")

        cfg = self.board_profile.get("openocd_cfg")
        if cfg and Path(cfg).exists():
            cmd = [entry.exe_path, "-f", cfg,
                   "-c", f"tcl_port {self.tcl_port}", "-c", "bindto 0.0.0.0"]
        else:
            cmd = [entry.exe_path,
                   "-c", f"tcl_port {self.tcl_port}", "-c", "bindto 0.0.0.0"]
        # 修复核心：注入 -s，否则 ST 分支找不到 interface/stlink-dap.cfg
        if scripts and Path(scripts).exists():
            cmd += ["-s", scripts]

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
        info = {"session_id": self.session_id, "arch": self.arch,
                "target_name": self.target_name}
        # 链路健康探测（诚实原则）：端口通但读不到 CPUID = 链路故障/坏实例
        info["link_healthy"] = OpenOCDProcess._link_healthy(self.ocd)
        if not info["link_healthy"]:
            print("[session] 警告: TCL 端口已连接但目标内存不可读"
                  "（SWD 链路可能故障），后续内存操作预计失败",
                  file=sys.stderr)
        return info

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

    # 看门狗调试冻结位（halt 期间冻结 WWDG/IWDG）
    # F1/F4 通用：DBGMCU_APB1FZ(0xE0042008) bit11=WWDG_STOP, bit12=IWDG_STOP
    WDG_FREEZE_ADDR = 0xE0042008
    WDG_FREEZE_BITS = (1 << 11) | (1 << 12)

    def freeze_watchdogs(self) -> bool:
        """冻结看门狗（halt 期间），防止诊断现场被 IWDG 复位清除。

        真机经验（2026-08-29 F407 + MY_OTA_GUI 实测）：
        fault 后 CPU 停止喂狗，IWDG 在"fault → 诊断 halt"的运行窗口内
        复位芯片 → CFSR 被清零 + OpenOCD 状态失步（targets 卡 reset，
        之后 halt 永远超时）。诊断前必须先冻结。
        """
        cur = self.read_memory(self.WDG_FREEZE_ADDR, size=4)
        if cur is None:
            return False
        if cur & self.WDG_FREEZE_BITS == self.WDG_FREEZE_BITS:
            return True  # 已冻结
        return self.write_memory(self.WDG_FREEZE_ADDR,
                                 cur | self.WDG_FREEZE_BITS, width=32)

    def read_fault(self) -> dict[str, Optional[int]]:
        return {
            "cfsr": self.read_memory(SCB_CFSR, size=4),
            "hfsr": self.read_memory(SCB_HFSR, size=4),
            "mmfar": self.read_memory(SCB_MMFAR, size=4),
            "bfar": self.read_memory(SCB_BFAR, size=4),
            "cpuid": self.read_memory(CPUID_ADDR, size=4),
        }

    def read_chip_id(self) -> dict:
        """芯片探测（自动 halt 以确保可读）。

        真机经验（2026-08-29 F407 实测）：Flash 大小寄存器（0x1FFF7A22）
        在未经复位的会话中读到 0x0000，必须先 reset run 再 halt 才能读出
        正确值（F407ZGT6 = 0x0400 = 1024KB）。
        """
        # 先复位运行再 halt，保证系统存储区（Flash 大小/DEV_ID）可读
        self.reset_run()
        time.sleep(0.5)
        self.halt()
        time.sleep(0.05)
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
