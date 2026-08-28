"""
vscode_gen.py — VSCode 配置自动生成（P4 解决）

按板卡 Profile 自动生成 .vscode/launch.json 和 tasks.json，
换 MCU 只需换 Profile → 重新生成，无需手写 VSCode 配置。

支持:
  - ARM Cortex-M: 用 cortex-debug 扩展（servertype=openocd）
  - 生成 build/flash 任务

用法:
    from core.vscode_gen import VSCodeGen
    gen = VSCodeGen("stm32f407", profile_mgr)
    gen.generate("E:/path/to/project")   # 生成 .vscode/launch.json + tasks.json
"""

import json
import sys
from pathlib import Path
from typing import Optional

# 允许直接运行时也能导入 core 包
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from core.profile import ProfileManager
except ImportError:
    from profile import ProfileManager


class VSCodeGen:
    """按 Profile 生成 VSCode 调试/任务配置"""

    def __init__(self, board_name: str, profile_mgr: Optional[ProfileManager] = None):
        self.board_name = board_name
        self.mgr = profile_mgr or ProfileManager()
        self.profile = self.mgr.load(board_name)
        if not self.profile:
            raise ValueError(f"未找到板卡 Profile: {board_name}")

    # ── launch.json ───────────────────────────────────────

    def build_launch_configs(self) -> list[dict]:
        """构造 launch.json 的 configurations"""
        p = self.profile
        arch = p.get("arch", "")
        elf = p.get("elf", "")
        cfg = p.get("openocd_cfg", "")
        transport = p.get("transport", "swd")

        configs = []
        if "cortex" in arch:
            # ARM Cortex-M: cortex-debug 扩展
            configs.append({
                "name": f"Debug {self.board_name} (OpenOCD)",
                "type": "cortex-debug",
                "request": "launch",
                "servertype": "openocd",
                "cwd": "${workspaceRoot}",
                "executable": elf or "${workspaceRoot}/build/Debug/firmware.elf",
                "interface": transport,
                "toolchainPrefix": p.get("toolchain", "arm-none-eabi"),
                "configFiles": [cfg] if cfg else [],
                "runToEntryPoint": "main",
            })
        elif "riscv" in arch or "xtensa" in arch:
            # RISC-V/Xtensa: 通用 cppdbg + OpenOCD（或对应扩展）
            configs.append({
                "name": f"Debug {self.board_name} (OpenOCD)",
                "type": "cppdbg",
                "request": "launch",
                "cwd": "${workspaceRoot}",
                "program": elf or "${workspaceRoot}/build/firmware.elf",
                "miDebuggerServerAddress": "localhost:3333",
                "miDebuggerPath": self._guess_gdb(p),
            })
        return configs

    def _guess_gdb(self, p: dict) -> str:
        """猜测 GDB 路径（按 toolchain 前缀）"""
        tc = p.get("toolchain", "")
        if "riscv" in tc:
            return tc.replace("-gcc", "-gdb")
        if "xtensa" in tc:
            return tc.replace("-gcc", "-gdb")
        return "arm-none-eabi-gdb"

    # ── tasks.json ────────────────────────────────────────

    def build_tasks(self) -> list[dict]:
        """构造 tasks.json 的 tasks（build/flash）"""
        p = self.profile
        build_dir = p.get("build_dir", "build")
        return [
            {
                "label": "Build",
                "type": "shell",
                "command": "cmake",
                "args": ["--build", build_dir, "--parallel", "8"],
                "group": {"kind": "build", "isDefault": True},
                "problemMatcher": ["$gcc"],
            },
            {
                "label": "Flash",
                "type": "shell",
                "command": "python",
                "args": ["-m", "foundation", "flash", self.board_name],
                "group": "build",
            },
            {
                "label": "Diagnose",
                "type": "shell",
                "command": "python",
                "args": ["-m", "foundation", "diagnose", "hardfault"],
                "group": "build",
            },
        ]

    # ── 生成 ──────────────────────────────────────────────

    def generate(self, project_dir: str) -> dict:
        """生成 .vscode/launch.json + tasks.json，返回生成的文件路径"""
        vscode_dir = Path(project_dir) / ".vscode"
        vscode_dir.mkdir(parents=True, exist_ok=True)

        launch_path = vscode_dir / "launch.json"
        launch_path.write_text(
            json.dumps({"version": "0.2.0",
                        "configurations": self.build_launch_configs()},
                       ensure_ascii=False, indent=4),
            encoding="utf-8")

        tasks_path = vscode_dir / "tasks.json"
        tasks_path.write_text(
            json.dumps({"version": "2.0.0", "tasks": self.build_tasks()},
                       ensure_ascii=False, indent=4),
            encoding="utf-8")

        return {"launch": str(launch_path), "tasks": str(tasks_path)}


# ─── CLI ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="VSCode 配置自动生成")
    parser.add_argument("board", help="板卡名（如 stm32f407）")
    parser.add_argument("--project", default=".",
                        help="项目目录（生成 .vscode/ 到这里）")
    args = parser.parse_args()

    try:
        gen = VSCodeGen(args.board)
        files = gen.generate(args.project)
        print("=== VSCode 配置已生成 ===")
        for k, v in files.items():
            print(f"  {k}: {v}")
    except ValueError as e:
        print(f"错误: {e}")
