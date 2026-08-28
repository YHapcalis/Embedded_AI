"""
openocd_registry.py — OpenOCD 多分支注册表管理

解决"STM32CubeIDE 内置 OpenOCD 换芯片不能用"问题（P3）：
不删除、不覆盖、不二选一 —— 扫描并登记所有 OpenOCD 安装，
按板卡 Profile 的能力标签自动路由到正确分支。

支持的分支类型：
  - st-branch:      STM32CubeIDE 内置（标签: stm32, stlink, swd）
  - esp32-branch:   Espressif openocd-esp32（标签: esp32, xtensa, riscv, jtag）
  - mainline:       官方主线 0.12+（标签: generic, riscv, fpga, jtag）

路由规则（harness openocd use <board>）：
  1. 板卡 Profile 显式指定 openocd 分支名 → 直接使用
  2. Profile 指定能力标签（如 "esp32"）→ Registry 按标签匹配
  3. 未指定 → 按架构自动匹配（cortex_m→st-branch, riscv→mainline/esp32）
  4. 都找不到 → 报错并列出可用分支

用法：
    from core.openocd_registry import OpenOCDRegistry
    reg = OpenOCDRegistry()
    reg.scan()                 # 扫描全部安装
    reg.list()                 # 查看注册表
    entry = reg.resolve(board_profile)   # 按板卡解析
"""

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    from .env_probe import detect_platform
except ImportError:
    from env_probe import detect_platform


# ─── 数据结构 ─────────────────────────────────────────────────

@dataclass
class OpenOCDEntry:
    """Registry 中的一条 OpenOCD 记录"""
    name: str                # 分支名: st / esp32 / mainline
    exe_path: str            # 可执行文件路径
    script_dir: str = ""     # 脚本目录（-s 参数）
    tags: list = field(default_factory=list)  # 能力标签
    version: str = ""
    source: str = ""         # 发现来源（CubeIDE 插件 / PATH / 独立安装）

    def matches(self, tag: str) -> bool:
        """按标签匹配（大小写不敏感）"""
        t = tag.lower()
        return any(x.lower() == t for x in self.tags)


# ─── 能力标签表 ───────────────────────────────────────────────

# 架构 → 推荐分支（自动路由兜底）
ARCH_DEFAULT = {
    "cortex_m":  "st",
    "cortex_a":  "st",
    "cortex_r":  "st",
    "riscv":     "mainline",
    "xtensa":    "esp32",
    "mips":      "mainline",
    "fpga":      "mainline",
}


# ─── 主类 ─────────────────────────────────────────────────────

class OpenOCDRegistry:
    """OpenOCD 多分支注册表"""

    def __init__(self):
        self.platform = detect_platform()
        self.entries: list[OpenOCDEntry] = []
        self._scanned = False

    # ─── 扫描 ──────────────────────────────────────────────

    def scan(self) -> list[OpenOCDEntry]:
        """扫描系统所有 OpenOCD 安装，填充注册表"""
        self.entries = []
        self._scanned = True

        if self.platform == "windows":
            self._scan_windows()
        else:
            self._scan_unix()
        return self.entries

    def _scan_windows(self):
        # 1. STM32CubeIDE 插件目录（ST 分支）
        cubeide_roots = [
            Path("E:/ST/STM32/STM32CubeIDE/plugins"),
            Path("C:/ST/STM32CubeIDE/plugins"),
            Path.home() / "STM32CubeIDE" / "plugins",
        ]
        for root in cubeide_roots:
            if not root.exists():
                continue
            for p in root.glob("*openocd*/tools/bin/openocd.exe"):
                self._add_entry(
                    name="st",
                    exe=p,
                    tags=["stm32", "stlink", "swd", "cortex_m", "cortex_a", "cortex_r"],
                    source="STM32CubeIDE 插件",
                )
            # 脚本目录（st_scripts）
            for p in root.glob("*openocd*/resources/openocd/st_scripts"):
                for e in self.entries:
                    if e.name == "st" and not e.script_dir:
                        e.script_dir = p.as_posix()

        # 2. Espressif 分支
        esp_roots = [
            Path("C:/Espressif/tools/openocd-esp32"),
            Path.home() / "esp" / "openocd-esp32",
        ]
        for root in esp_roots:
            if not root.exists():
                continue
            for p in root.glob("*/bin/openocd.exe"):
                self._add_entry(
                    name="esp32",
                    exe=p,
                    tags=["esp32", "xtensa", "riscv", "jtag", "esp32s2", "esp32s3",
                          "esp32c3", "esp32c6"],
                    source="Espressif 独立安装",
                )
            for p in root.glob("*/openocd-esp32/bin/openocd.exe"):
                self._add_entry(
                    name="esp32",
                    exe=p,
                    tags=["esp32", "xtensa", "riscv", "jtag"],
                    source="Espressif 独立安装",
                )

        # 3. PATH 中的 OpenOCD（判断是主线还是其他）
        p = shutil.which("openocd")
        if p:
            # 排除已登记的
            already = any(Path(e.exe_path).resolve() == Path(p).resolve()
                          for e in self.entries)
            if not already:
                self._add_entry(
                    name="mainline",
                    exe=Path(p),
                    tags=["generic", "riscv", "fpga", "jtag", "swd"],
                    source="PATH",
                )

    def _scan_unix(self):
        # 1. PATH 中的 openocd
        p = shutil.which("openocd")
        if p:
            self._add_entry(
                name="mainline",
                exe=Path(p),
                tags=["generic", "riscv", "fpga", "jtag", "swd"],
                source="PATH",
            )
        # 2. Espressif 默认目录
        esp = Path.home() / "esp" / "openocd-esp32"
        if esp.exists():
            for p in esp.glob("src/openocd"):
                self._add_entry(
                    name="esp32",
                    exe=p,
                    tags=["esp32", "xtensa", "riscv", "jtag"],
                    source="Espressif ~/esp 目录",
                )

    def _add_entry(self, name: str, exe: Path, tags: list, source: str):
        """登记一条记录（去重）"""
        resolved = exe.resolve().as_posix()
        for e in self.entries:
            if e.exe_path == resolved:
                return
        entry = OpenOCDEntry(
            name=name,
            exe_path=resolved,
            tags=tags,
            source=source,
            version=self._version(str(exe)),
        )
        # 补脚本目录（Espressif 分支脚本在安装目录下）
        if name == "esp32":
            sdir = exe.parent.parent / "share" / "openocd" / "scripts"
            if sdir.exists():
                entry.script_dir = sdir.as_posix()
        self.entries.append(entry)

    # ─── 查询 ──────────────────────────────────────────────

    def list(self) -> list[OpenOCDEntry]:
        """返回注册表内容（未扫描则先扫描）"""
        if not self._scanned:
            self.scan()
        return self.entries

    def get(self, name: str) -> Optional[OpenOCDEntry]:
        """按分支名精确获取"""
        for e in self.list():
            if e.name == name:
                return e
        return None

    def resolve(self, board_profile: dict) -> Optional[OpenOCDEntry]:
        """
        按板卡 Profile 解析 OpenOCD 分支。

        board_profile 字段:
          - openocd: 可选，显式分支名（"st" / "esp32" / "mainline"）
          - arch: 可选，架构名（cortex_m / riscv / xtensa ...）
        """
        entries = self.list()
        if not entries:
            return None

        # 1. 显式指定分支名
        explicit = board_profile.get("openocd")
        if explicit:
            e = self.get(explicit)
            if e:
                return e

        # 2. 按架构自动路由
        arch = board_profile.get("arch", "").lower()
        default_name = ARCH_DEFAULT.get(arch)
        if default_name:
            e = self.get(default_name)
            if e:
                return e
            # 兜底：匹配标签
            for e in entries:
                if e.matches(arch):
                    return e

        # 3. 按标签匹配（Profile 可能带 mcu 名如 esp32c3）
        mcu = board_profile.get("mcu", "").lower()
        for e in entries:
            if e.matches(mcu) or any(t in mcu for t in e.tags):
                return e

        # 4. 全都不行 → 第一个可用的
        return entries[0]

    # ─── 展示 ──────────────────────────────────────────────

    def summary_text(self) -> str:
        """人类可读的注册表输出（harness openocd list）"""
        lines = ["=== OpenOCD Registry ===", ""]
        for e in self.list():
            lines.append(f"  [{e.name:8s}] {e.exe_path}")
            lines.append(f"           版本: {e.version or 'N/A'}")
            lines.append(f"           标签: {', '.join(e.tags)}")
            if e.script_dir:
                lines.append(f"           脚本: {e.script_dir}")
            lines.append(f"           来源: {e.source}")
            lines.append("")
        if not self.entries:
            lines.append("  （未找到任何 OpenOCD 安装）")
        return "\n".join(lines)

    # ─── 工具 ──────────────────────────────────────────────

    @staticmethod
    def _version(exe: str) -> str:
        try:
            r = subprocess.run([exe, "--version"], capture_output=True,
                               text=True, timeout=10)
            out = (r.stdout or r.stderr or "").strip()
            return out.splitlines()[0][:80] if out else ""
        except Exception:
            return ""


# ─── CLI ─────────────────────────────────────────────────────

if __name__ == "__main__":
    reg = OpenOCDRegistry()
    print(reg.summary_text())
