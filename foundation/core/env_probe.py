"""
env_probe.py — 环境探测（跨平台）

自动探测嵌入式开发所需的全部工具链组件：
  - OpenOCD（多位置扫描：STM32CubeIDE 内置 / 独立安装 / Espressif 分支）
  - 交叉编译器（arm-none-eabi-gcc / riscv / xtensa）
  - 调试器连接（ST-Link / J-Link / CMSIS-DAP）
  - Python / Git 版本
  - 平台识别（Windows / Linux / macOS）

设计原则（来自环境依赖审计）：
  - 高依赖集中在"工具链定位" → 全部收敛到本模块，业务逻辑零环境耦合
  - 路径统一 Path.as_posix()（Windows 反斜杠会坑 OpenOCD）
  - 探测失败不报错崩溃，返回结构化状态供上层决策

用法：
    from core.env_probe import EnvironmentProbe
    probe = EnvironmentProbe()
    result = probe.check_all()   # {component: {found, version, path, ...}}
"""

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ─── 探测结果数据结构 ─────────────────────────────────────────

@dataclass
class ProbeResult:
    """单个组件的探测结果"""
    name: str            # 组件名: openocd / gcc / stlink / python / git
    found: bool = False
    version: str = ""
    path: str = ""       # 可执行文件路径（as_posix）
    details: str = ""    # 补充信息（如分支/标签）
    errors: list = field(default_factory=list)


# ─── 平台识别 ─────────────────────────────────────────────────

def detect_platform() -> str:
    """返回: windows / linux / darwin"""
    p = platform.system().lower()
    if p.startswith("win"):
        return "windows"
    if p.startswith("darwin"):
        return "darwin"
    return "linux"


def cubeide_plugins_roots() -> list[Path]:
    """收集 STM32CubeIDE 插件目录候选路径。

    覆盖: E:/ST 与 C:/ST 默认安装、用户目录，以及 D 盘自定义安装
    （D:/STM32CubeIDE/<版本>/STM32CubeIDE/plugins，版本号用 glob 兜底，
    避免 CubeIDE 升级后路径失效）。openocd_registry 复用本函数。
    """
    roots = [
        Path("E:/ST/STM32/STM32CubeIDE/plugins"),
        Path("C:/ST/STM32CubeIDE/plugins"),
        Path.home() / "STM32CubeIDE" / "plugins",
    ]
    d_root = Path("D:/STM32CubeIDE")
    if d_root.exists():
        roots += list(d_root.glob("*/STM32CubeIDE/plugins"))
    return roots


# ─── 环境探测主类 ─────────────────────────────────────────────

class EnvironmentProbe:
    """环境探测器：扫描工具链并返回结构化结果"""

    def __init__(self):
        self.platform = detect_platform()
        self._cache: dict[str, ProbeResult] = {}

    # ─── OpenOCD 探测（多位置扫描）─────────────────────────

    def find_openocd(self) -> ProbeResult:
        """
        扫描所有可能的 OpenOCD 安装位置。

        Windows 候选：
          1. STM32CubeIDE 插件目录（glob 匹配版本号目录）
          2. PATH 中的 openocd（独立安装 / Espressif）
          3. 常见安装路径（C:/OpenOCD, scoop/choco 等）

        Linux/macOS 候选：
          1. PATH 中的 openocd（apt/brew）
          2. ~/esp/openocd-esp32（Espressif 默认约定）
        """
        result = ProbeResult(name="openocd")
        candidates: list[tuple[Path, str]] = []  # (exe_path, 标签)

        if self.platform == "windows":
            # 1. STM32CubeIDE 插件目录（版本号目录用 glob）
            for root in cubeide_plugins_roots():
                if not root.exists():
                    continue
                for p in root.glob("*openocd*/tools/bin/openocd.exe"):
                    candidates.append((p, "st-branch"))
        else:
            # Linux/macOS: PATH + 常见位置
            p = shutil.which("openocd")
            if p:
                candidates.append((Path(p), "mainline-or-esp"))

        # PATH 兜底（Windows 独立安装）
        p = shutil.which("openocd")
        if p and not any(str(c[0]) == p for c in candidates):
            candidates.append((Path(p), "path"))

        # Espressif 专用分支（Windows 常见: C:/Espressif/tools/openocd-esp32）
        if self.platform == "windows":
            esp_root = Path("C:/Espressif/tools/openocd-esp32")
            if esp_root.exists():
                for p in esp_root.glob("*/bin/openocd.exe"):
                    candidates.append((p, "esp32-branch"))
                for p in esp_root.glob("*/openocd-esp32/bin/openocd.exe"):
                    candidates.append((p, "esp32-branch"))

        if not candidates:
            result.errors.append("未找到 OpenOCD，请安装 STM32CubeIDE 或独立 OpenOCD")
            return result

        # 取第一个可用的（并记录所有找到的）
        exe, tag = candidates[0]
        result.found = True
        result.path = exe.as_posix()
        result.details = tag
        version = self._get_version([str(exe), "--version"])
        result.version = version
        return result

    # ─── 交叉编译器探测 ────────────────────────────────────

    def find_gcc(self, prefix: str = "arm-none-eabi-gcc") -> ProbeResult:
        """探测交叉编译器（默认 ARM；可传 riscv32-esp-elf-gcc 等）"""
        result = ProbeResult(name=f"gcc[{prefix}]")
        exe = shutil.which(prefix)
        if not exe and self.platform == "windows":
            # STM32CubeIDE 内置工具链
            roots = cubeide_plugins_roots()
            for root in roots:
                if root.exists():
                    for p in root.glob("*gnu-tools*/tools/bin/" + prefix + ".exe"):
                        exe = str(p)
                        break
                if exe:
                    break
        if not exe and self.platform == "windows":
            # Espressif 工具链（ESP-IDF 安装布局: tools/<名>/<版本>/<名>/bin/）
            esp_roots = [
                Path("C:/Espressif/tools"),
                Path.home() / ".espressif" / "tools",
            ]
            for root in esp_roots:
                if not root.exists():
                    continue
                for p in root.glob("*/**/bin/" + prefix + ".exe"):
                    exe = str(p)
                    break
                if exe:
                    break
        if not exe:
            result.errors.append(f"未找到 {prefix}")
            return result

        result.found = True
        result.path = Path(exe).as_posix()
        result.version = self._get_version([exe, "--version"])
        return result

    # ─── 调试器探测 ────────────────────────────────────────

    def find_stlink(self) -> ProbeResult:
        """
        探测 ST-Link 连接状态。

        优先方法：如果 OpenOCD 已在运行，直接查 TCL 端口确认目标连接
        （这是"调试器能否用"的最可靠判据）。
        辅助方法：USB 设备枚举（VID_0483）。
        """
        result = ProbeResult(name="stlink")

        # 方法 1: 通过已运行的 OpenOCD TCL 端口验证（最可靠）
        tcl_ok = self._check_openocd_tcl()
        if tcl_ok:
            result.found = True
            result.details = "通过 OpenOCD TCL 端口验证（目标已连接）"
            return result

        # 方法 2: USB 枚举（Windows: VID_0483; Linux: lsusb）
        if self.platform == "windows":
            try:
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "Get-PnpDevice -PresentOnly | Where-Object {$_.InstanceId -like '*0483*'} | ForEach-Object {$_.Status}"],
                    capture_output=True, text=True, timeout=10)
                out = (r.stdout or "").strip()
                if "OK" in out or "Unknown" in out:
                    result.found = True
                    result.details = "ST-Link USB 设备（VID_0483）已枚举"
                else:
                    result.errors.append("未检测到 ST-Link USB 设备（VID_0483）")
            except Exception as e:
                result.errors.append(f"ST-Link USB 探测异常: {e}")
        else:
            try:
                r = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=5)
                if "0483:3748" in (r.stdout or ""):
                    result.found = True
                    result.details = "ST-Link V2 (0483:3748)"
                else:
                    result.errors.append("未检测到 ST-Link（lsusb 无 0483:3748）")
            except FileNotFoundError:
                result.errors.append("无 lsusb，跳过 USB 探测")

        if not result.found:
            result.errors.append("（提示：OpenOCD 未运行且 USB 未枚举到 ST-Link）")
        return result

    @staticmethod
    def _check_openocd_tcl() -> bool:
        """检查 6666 端口的 OpenOCD 实例是否真实可用。

        判据（两级）：
        1. target names 有应答（端口级）
        2. 链路健康：Cortex-M 目标必须能读到 CPUID（0xE000ED00）
           —— 防止"坏实例占坑导致误报连接成功"
              （2026-08-29 真机复现验收时发现：遗留坏实例端口通但
               SWD 链路已断，寄存器值全是缓存）
        RISC-V/Xtensa 目标无 CPUID，读到 target names 即通过。
        """
        import re
        import socket

        def _tcl(command: str) -> bytes:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(2)
                s.connect(("127.0.0.1", 6666))
                s.sendall((command + "\x1a").encode("utf-8"))
                data = b""
                while True:
                    chunk = s.recv(4096)
                    data += chunk
                    if not chunk or b"\x1a" in chunk:
                        break
                return data

        try:
            names = _tcl("target names")
            if not (b"cpu" in names or b"target" in names.lower()):
                return False
            low = names.lower()
            if b"riscv" in low or b"esp" in low or b"xtensa" in low:
                return True
            # Cortex-M：链路健康 = CPUID 可读（8 位以上十六进制响应）
            resp = _tcl("mdw 0xE000ED00")
            return bool(re.search(rb"[0-9a-fA-F]{8}", resp))
        except Exception:
            return False

    # ─── 基础环境 ──────────────────────────────────────────

    def find_python(self) -> ProbeResult:
        result = ProbeResult(name="python")
        result.found = True
        result.version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        result.path = Path(sys.executable).as_posix()
        return result

    def find_git(self) -> ProbeResult:
        result = ProbeResult(name="git")
        exe = shutil.which("git")
        if not exe:
            result.errors.append("未找到 git")
            return result
        result.found = True
        result.path = Path(exe).as_posix()
        result.version = self._get_version([exe, "--version"])
        return result

    # ─── 工具函数 ──────────────────────────────────────────

    @staticmethod
    def _get_version(cmd: list[str]) -> str:
        """执行命令取第一行版本信息"""
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            out = (r.stdout or r.stderr or "").strip()
            first = out.splitlines()[0] if out else ""
            return first[:80]
        except Exception:
            return ""

    # ─── 总入口 ────────────────────────────────────────────

    def check_all(self) -> dict[str, ProbeResult]:
        """探测全部组件，返回 {组件名: ProbeResult}"""
        results = {
            "platform": ProbeResult(name="platform", found=True,
                                    version=self.platform),
            "openocd": self.find_openocd(),
            "gcc": self.find_gcc(),
            "stlink": self.find_stlink(),
            "python": self.find_python(),
            "git": self.find_git(),
        }
        self._cache = results
        return results

    def summary_text(self, results: Optional[dict] = None) -> str:
        """人类可读的探测结果摘要（harness env check 输出用）

        results 可传入预计算结果（如 --no-hardware 替换 stlink 后），
        避免重复探测导致调用方的修改被丢弃。
        """
        if results is None:
            results = self.check_all()
        lines = ["=== 环境探测结果 ===", ""]
        for name, r in results.items():
            if r.found:
                status = "OK"
                extra = f" ({r.version})" if r.version else ""
                path = f" [{r.path}]" if r.path else ""
                tag = f" {r.details}" if r.details else ""
                lines.append(f"  [OK]  {name:10s}{extra}{path}{tag}")
            else:
                lines.append(f"  [FAIL] {name:10s} {'; '.join(r.errors)}")
        return "\n".join(lines)


# ─── CLI ─────────────────────────────────────────────────────

if __name__ == "__main__":
    probe = EnvironmentProbe()
    print(probe.summary_text())
