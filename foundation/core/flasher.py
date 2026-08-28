"""
flasher.py — 烧录封装（多段 verify，架构无关）

按板卡 Profile 路由 OpenOCD 分支，支持多段 program verify 烧录
（如 BL + APP + 签名 + 参数）。基于已验证的烧录命令规范：

    openocd -s <scripts> -f <cfg> \
        -c "program <elf> verify" \
        -c "program <bin> <addr> verify" \
        -c "reset; exit"

用法:
    from core.flasher import Flasher
    f = Flasher(board_profile=profile)
    result = f.flash()   # 全部段烧录 + verify
"""

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


# ─── 数据结构 ────────────────────────────────────────────────

class FlashResult:
    """烧录结果"""
    def __init__(self):
        self.ok = False
        self.sections = []       # [(名称, 地址, 是否通过)]
        self.log = ""

    @property
    def summary(self) -> str:
        parts = [f"{name}: {'✅' if ok else '❌'}" for name, _, ok in self.sections]
        return " | ".join(parts) if parts else "无烧录段"

    def __str__(self):
        return f"[{'OK' if self.ok else 'FAIL'}] {self.summary}"


# ─── 烧录器 ──────────────────────────────────────────────────

class Flasher:
    """多段烧录器（架构无关，按 Profile 选 OpenOCD 分支）"""

    def __init__(self, board_profile: Optional[dict] = None,
                 project_dir: str = "."):
        self.profile = board_profile or {}
        self.project_dir = Path(project_dir)

    def build_command(self) -> Optional[list]:
        """构造 OpenOCD 烧录命令"""
        reg = OpenOCDRegistry()
        entry = reg.resolve(self.profile)
        if not entry:
            return None

        cmd = [entry.exe_path]
        # 脚本目录（ST 分支需要 st_scripts）
        if entry.script_dir:
            cmd += ["-s", entry.script_dir]
        # 板卡配置
        cfg = self.profile.get("openocd_cfg")
        if cfg:
            cmd += ["-f", cfg]
        # 烧录段
        sections = self.profile.get("flash_sections", [])
        for sec in sections:
            fpath = self._resolve_path(sec["file"])
            if not fpath.exists():
                return None
            if "addr" in sec:
                cmd += ["-c", f"program {fpath.as_posix()} {sec['addr']} verify"]
            else:
                cmd += ["-c", f"program {fpath.as_posix()} verify"]
        cmd += ["-c", "reset; exit"]
        return cmd

    def _resolve_path(self, file: str) -> Path:
        """解析烧录文件路径（相对项目目录）"""
        p = Path(file)
        if p.is_absolute():
            return p
        return self.project_dir / p

    def flash(self) -> FlashResult:
        """执行完整烧录"""
        result = FlashResult()
        cmd = self.build_command()
        if not cmd:
            result.log = "命令构造失败：检查 board_profile（openocd 分支/配置/文件）"
            return result

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=300)
            output = proc.stdout + proc.stderr
            result.log = output
            # 解析每段的 Verified OK（按出现顺序对应段顺序）
            import re
            ok_count = len(re.findall(r"Verified OK", output))
            sections = self.profile.get("flash_sections", [])
            for i, sec in enumerate(sections):
                fpath = self._resolve_path(sec["file"])
                name = fpath.name
                ok = (ok_count > i)  # 第 i 段成功 = 至少有 i+1 个 Verified OK
                result.sections.append((name, sec.get("addr", "-"), ok))
            result.ok = all(ok for _, _, ok in result.sections) and proc.returncode == 0
        except subprocess.TimeoutExpired:
            result.log = "烧录超时（300s）"
        except Exception as e:
            result.log = f"烧录异常: {e}"
        return result


# ─── CLI ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import json

    # 演示用 F407 profile
    profile = {
        "mcu": "stm32f407",
        "arch": "cortex_m4",
        "openocd": "st",
        "openocd_cfg": "E:/ST/STM32/MY_workspace/Projects_F407/MY_OTA_GUI/openocd.cfg",
        "flash_sections": [
            {"file": "E:/ST/STM32/MY_workspace/Projects_F407/MY_OTA_GUI/build/Debug/bootloader.elf", "addr": "0x08000000"},
            {"file": "E:/ST/STM32/MY_workspace/Projects_F407/MY_OTA_GUI/build/Debug/MY_OTA_GUI.elf", "addr": "0x08010000"},
            {"file": "E:/ST/STM32/MY_workspace/Projects_F407/MY_OTA_GUI/build/Debug/signature.bin", "addr": "0x080DFF80"},
            {"file": "E:/ST/STM32/MY_workspace/Projects_F407/MY_OTA_GUI/param_init.bin", "addr": "0x080E0000"},
        ],
    }
    f = Flasher(board_profile=profile)
    cmd = f.build_command()
    if cmd:
        print("=== 烧录命令 ===")
        print(" ".join(cmd))
        print()
        print("（连接开发板后执行烧录，此处仅展示命令构造）")
    else:
        print("命令构造失败")
