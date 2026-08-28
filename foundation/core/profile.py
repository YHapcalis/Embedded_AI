"""
profile.py — 板卡 Profile 机制（P4 解决，架构无关的关键）

每块板卡 = 一个 profile.json，声明其开发环境与烧录配置。
换 MCU 只需新增 profile.json + openocd.cfg，引擎零改动（S9）。

Profile 结构:
    {
      "mcu": "stm32f407",
      "arch": "cortex_m4",
      "toolchain": "arm-none-eabi-gcc",
      "transport": "swd",          // swd / jtag
      "openocd": "st",             // OpenOCD 分支名（st/esp32/mainline）
      "openocd_cfg": "openocd.cfg",
      "flash_sections": [
        {"file": "build/Debug/bootloader.elf", "addr": "0x08000000"},
        {"file": "build/Debug/MY_OTA_GUI.elf", "addr": "0x08010000"},
        {"file": "build/Debug/signature.bin",  "addr": "0x080DFF80"},
        {"file": "param_init.bin",             "addr": "0x080E0000"}
      ]
    }

用法:
    from core.profile import ProfileManager
    mgr = ProfileManager(search_paths=["boards"])
    profile = mgr.load("stm32f407")
    mgr.list()          # 列出所有可用板卡
"""

import json
import sys
from pathlib import Path
from typing import Optional

# 允许直接运行时也能导入 core 包
sys.path.insert(0, str(Path(__file__).parent.parent))


class ProfileManager:
    """板卡 Profile 管理器：加载/列出/校验 profile.json"""

    def __init__(self, search_paths: Optional[list] = None):
        """
        search_paths: profile.json 的搜索目录（默认 ['boards', 'templates/boards']）
        """
        root = Path(__file__).parent.parent.parent  # 工作流根目录
        self.search_paths = [Path(p) for p in (search_paths or [
            str(root / "boards"),
            str(root / "templates" / "boards"),
        ])]
        self._cache: dict[str, dict] = {}

    # ── 发现 ──────────────────────────────────────────────

    def find_profiles(self) -> list[Path]:
        """扫描所有 profile.json"""
        found = []
        for sp in self.search_paths:
            if sp.exists():
                found.extend(sp.glob("*/profile.json"))
        return found

    def list(self) -> list[str]:
        """列出所有可用板卡名"""
        names = []
        for p in self.find_profiles():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                names.append(data.get("mcu", p.parent.name))
            except Exception:
                names.append(p.parent.name)
        return sorted(set(names))

    # ── 加载 ──────────────────────────────────────────────

    def load(self, board_name: str) -> Optional[dict]:
        """按板卡名加载 profile（mcu 名或目录名均可）"""
        if board_name in self._cache:
            return self._cache[board_name]

        for p in self.find_profiles():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if (data.get("mcu") == board_name
                    or p.parent.name == board_name):
                # 补全相对路径基准（profile 所在目录）
                data.setdefault("_dir", p.parent.as_posix())
                self._cache[board_name] = data
                return data
        return None

    def resolve_file(self, profile: dict, file: str) -> Path:
        """解析 profile 中的相对文件路径（相对 profile 目录或工作流根）"""
        p = Path(file)
        if p.is_absolute():
            return p
        base = Path(profile.get("_dir", "."))
        cand = base / p
        if cand.exists():
            return cand
        return Path(".") / p

    # ── 展示 ──────────────────────────────────────────────

    def summary(self, board_name: str) -> str:
        """人类可读的 Profile 摘要"""
        p = self.load(board_name)
        if not p:
            return f"[Profile] 未找到板卡: {board_name}（可用: {', '.join(self.list())}）"
        lines = [f"=== 板卡 Profile: {board_name} ===", ""]
        for k, v in p.items():
            if k == "flash_sections":
                lines.append(f"  烧录段:")
                for sec in v:
                    lines.append(f"    {sec.get('file')} @ {sec.get('addr', '-')}")
            elif k != "_dir":
                lines.append(f"  {k}: {v}")
        return "\n".join(lines)


# ─── CLI ─────────────────────────────────────────────────────

if __name__ == "__main__":
    mgr = ProfileManager()
    boards = mgr.list()
    print(f"可用板卡: {boards if boards else '(无，请创建 boards/<name>/profile.json)'}")
    if boards:
        print()
        print(mgr.summary(boards[0]))
