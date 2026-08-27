#!/usr/bin/env python3
"""
embed_harness.py — AI 嵌入式自主迭代 Harness 主入口

编排闭环:
  AI 修改代码 → 编译 → 烧录 → 监控变量 → 断言检查 → 反馈给 AI → 再修改

用法:
  python embed_harness.py --elf build/Debug/firmware.elf --scenario scenarios/led_blink.yaml
  python embed_harness.py --elf build/Debug/firmware.elf --task "让 LED 以 2Hz 闪烁"
  python embed_harness.py --interactive   # 交互式，每轮等你给 AI 反馈
"""

import argparse
import json
import subprocess
import sys
import time
import os
from pathlib import Path
from typing import Optional

# ─── 将 harness_ai 目录加入 path 以便内部导入 ───
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from monitor_client import OpenOCDClient, MonitorScriptClient
from expectations import run_checks, CheckResult
from feedback import Observation, AIDecision
from policy import PolicyGateway
from memory import HarnessMemory

# 可选 UI 增强
_UI_AVAILABLE = False
try:
    from ui import HarnessUI
    _UI_AVAILABLE = True
except ImportError:
    pass

try:
    from dashboard_export import DashboardExporter
except ImportError:
    DashboardExporter = None

# AI 决策后端（延迟导入，可能因缺少 API Key 而不可用）
_AI_BACKEND_AVAILABLE = False
try:
    from ai_backend import ClaudeAIBackend
    _AI_BACKEND_AVAILABLE = True
except ImportError:
    pass

# ─── 后端工厂 ─────────────────────────────────────────────────────

_BACKEND_INSTANCE = None  # 全局单例，避免重复启停

def create_backend(name: str, project_dir: str = None, elf_path: str = None):
    """创建后端：swd (真硬件) 或 renode (模拟器)"""
    global _BACKEND_INSTANCE

    if name == "swd":
        return OpenOCDClient()

    elif name == "hybrid":
        from hybrid_backend import HybridBackend
        backend = HybridBackend(
            renode_port=12347,
            elf_path=elf_path,
        )
        # 启动 Renode（不管能不能连上 SWD）
        try:
            plugin_dir = _HERE / "renode" / "plugins"
            backend.start_renode()
            backend._renode.load_component(str(plugin_dir / "NT35510.cs"))
            backend._renode.load_platform(str(plugin_dir / "bate_camera_full.repl"))
            bak_elf = str(Path(elf_path).resolve()) if elf_path else None
            if bak_elf:
                backend._renode.load_firmware(bak_elf)
                backend._renode.reset_and_run()
        except Exception as e:
            print(f"[WARN] Renode 启动失败，仅使用 SWD: {e}")
        _BACKEND_INSTANCE = backend
        return backend

    elif name == "renode":
        from renode.renode_client import RenodeBackend

        plugin_dir = _HERE / "renode" / "plugins"
        cs_file = plugin_dir / "NT35510.cs"
        repl_file = plugin_dir / "bate_camera_full.repl"
        elf_abs = str(Path(elf_path).resolve()) if elf_path else None

        if not cs_file.exists() or not repl_file.exists():
            raise FileNotFoundError(
                f"Renode 文件缺失:\n  {cs_file}\n  {repl_file}")

        backend = RenodeBackend(port=12347)
        backend.start()
        backend.load_component(str(cs_file))
        backend.load_platform(str(repl_file))

        if elf_abs:
            backend.load_firmware(elf_abs)
            backend.reset_and_run()

        _BACKEND_INSTANCE = backend
        return backend

    else:
        raise ValueError(f"未知后端: {name}，可选 swd / renode")


# ─── 配置 ─────────────────────────────────────────────────────────────

class HarnessConfig:
    """每项工程的配置——按你的实际环境修改"""
    # OpenOCD
    OPENOCD_HOST = "127.0.0.1"
    OPENOCD_TCL_PORT = 6666

    # 工具链（按 CubeIDE 实际版本号）
    ST_SCRIPTS = "E:/ST/STM32/STM32CubeIDE/plugins/com.st.stm32cube.ide.mcu.debug.openocd_2.3.300.202602021527/resources/openocd/st_scripts"
    OPENOCD_EXE = "E:/ST/STM32/STM32CubeIDE/plugins/com.st.stm32cube.ide.mcu.externaltools.openocd.win32_2.4.400.202601091506/tools/bin/openocd.exe"

    # GDB
    GDB_CMD = "arm-none-eabi-gdb"

    # 默认采样参数
    SAMPLE_INTERVAL = 0.3  # 秒
    SAMPLE_DURATION = 3.0  # 秒
    BOOT_WAIT = 1.5        # 烧录后等芯片启动

    # Monitor 脚本路径（已有的 stm32_monitor.py）
    MONITOR_SCRIPT = "E:/ST/STM32/MY_workspace/知识库/工程实用信息/VScode环境/scripts/stm32_monitor.py"


# ─── 构建与烧录 ───────────────────────────────────────────────────────

class BuildSystem:
    """编译 + 烧录"""

    def __init__(self, project_dir: str, elf_path: str,
                 openocd_cfg: str = "openocd.cfg",
                 config: HarnessConfig = None):
        self.project_dir = Path(project_dir).resolve()
        self.elf_path = Path(elf_path)
        if not self.elf_path.is_absolute():
            self.elf_path = self.project_dir / self.elf_path
        self.elf_path = self.elf_path.resolve()
        self.openocd_cfg = openocd_cfg
        self.cfg = config or HarnessConfig()

        # 从 elf 路径推断构建目录
        self.build_dir = self.elf_path.parent
        self.openocd_cfg_path = self.project_dir / self.openocd_cfg

        # ── OTA 双区架构检测 ──
        # 如果构建目录存在 bootloader.elf，视为 OTA 项目
        self._ota_mode = False
        self._bootloader_elf = self.build_dir / "bootloader.elf"
        self._signature_bin = self.build_dir / "signature.bin"
        self._param_bin = self.project_dir / "param_init.bin"

        if self._bootloader_elf.exists():
            self._ota_mode = True
            print(f"  [OTA] 检测到双区架构: {self._bootloader_elf.name}")
            if self._signature_bin.exists():
                print(f"  [OTA] 签名文件: {self._signature_bin.name} ({self._signature_bin.stat().st_size}B)")
            if self._param_bin.exists():
                print(f"  [OTA] 参数扇区: {self._param_bin.name} ({self._param_bin.stat().st_size}B)")

    @property
    def flash_segments(self) -> list[tuple[Path, str]]:
        """
        返回待烧录段列表: [(file_path, openocd_program_cmd), ...]

        单区模式: 仅烧主 ELF
        OTA 模式: BL + APP + 签名 + 参数（共 4 段）

        注意: OpenOCD 不认 Windows 反斜杠，所有路径用正斜杠。
        """
        def _posix(p: Path) -> str:
            """将 Windows 路径转为正斜杠格式（OpenOCD 兼容）"""
            return p.as_posix()

        if not self._ota_mode:
            elf_posix = _posix(self.elf_path)
            return [(self.elf_path, f"program {elf_posix} verify")]

        segments = []

        # 1. Bootloader
        bl_posix = _posix(self._bootloader_elf)
        segments.append((
            self._bootloader_elf,
            f"program {bl_posix} verify"
        ))

        # 2. APP (主 ELF)
        app_posix = _posix(self.elf_path)
        segments.append((
            self.elf_path,
            f"program {app_posix} verify"
        ))

        # 3. 签名 (如有)
        if self._signature_bin.exists():
            sig_addr = "0x080DFF80"
            sig_posix = _posix(self._signature_bin)
            segments.append((
                self._signature_bin,
                f"program {sig_posix} {sig_addr} verify"
            ))

        # 4. 参数扇区 (如有)
        if self._param_bin.exists():
            param_addr = "0x080E0000"
            param_posix = _posix(self._param_bin)
            segments.append((
                self._param_bin,
                f"program {param_posix} {param_addr} verify"
            ))

        return segments

    def build(self) -> tuple[bool, str]:
        """
        cmake --build
        返回: (成功?, 日志)
        """
        if not self.build_dir.exists():
            return False, f"构建目录不存在: {self.build_dir}"

        # 检查是否已 configure（有 build.ninja）
        ninja_file = self.build_dir / "build.ninja"
        if not ninja_file.exists():
            # 先 cmake --preset
            cmake_preset = self.project_dir / "CMakePresets.json"
            if cmake_preset.exists():
                preset = "Debug"
                cmd = ["cmake", "--preset", preset]
            else:
                # fallback: 直接在 build 目录 cmake
                cmd = [
                    "cmake", "-G", "Ninja",
                    "-DCMAKE_BUILD_TYPE=Debug",
                    "-DCMAKE_TOOLCHAIN_FILE=cmake/gcc-arm-none-eabi.cmake",
                    "-B", str(self.build_dir),
                    "-S", str(self.project_dir),
                ]
            subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        # 实际编译
        try:
            result = subprocess.run(
                ["cmake", "--build", str(self.build_dir), "--parallel"],
                capture_output=True, text=True, timeout=120,
                cwd=self.project_dir,
            )
            success = result.returncode == 0
            log = result.stdout + result.stderr
            return success, log[-2000:]  # 保留末尾 2000 字符
        except subprocess.TimeoutExpired:
            return False, "编译超时（120s）"
        except FileNotFoundError as e:
            return False, f"找不到 cmake: {e}"

    @staticmethod
    def _kill_stale_openocd():
        """烧录前清理残余 OpenOCD 进程，释放 ST-LINK USB 设备"""
        import subprocess
        try:
            r = subprocess.run(
                ["taskkill", "/f", "/im", "openocd.exe"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                import time
                time.sleep(1)  # 等待 USB 设备释放
        except:
            pass

    def flash(self) -> tuple[bool, str]:
        """
        通过 OpenOCD 烧录固件 + 复位。

        单区模式: 烧录单个 ELF
        OTA 双区模式: 烧录 BL + APP + 签名 + 参数（4 段）

        参考 MY_OTA_GUI 的 vscode-debug-环境说明.md 中的命令格式。
        """
        # 烧录前清理残余 OpenOCD 进程，释放 ST-LINK USB
        self._kill_stale_openocd()

        cfg_path = self.openocd_cfg_path
        if not cfg_path.exists():
            return False, f"openocd.cfg 不存在: {cfg_path}"

        # 检查每个待烧录段是否存在
        segments = self.flash_segments
        for file_path, _ in segments:
            if not file_path.exists():
                return False, f"烧录文件不存在: {file_path}"

        # 检查 ST_SCRIPTS 目录
        scripts_dir = Path(self.cfg.ST_SCRIPTS)
        if not scripts_dir.exists():
            return False, f"ST 脚本目录不存在: {self.cfg.ST_SCRIPTS}"

        # 构造 OpenOCD 命令
        seg_count = len(segments)
        cmd = [self.cfg.OPENOCD_EXE, "-s", str(scripts_dir), "-f", str(cfg_path)]
        for _, prog_cmd in segments:
            cmd.extend(["-c", prog_cmd])
        cmd.extend(["-c", "reset; exit"])

        # 烧录超时: 根据实际测量 ~20s，留余量
        flash_timeout = 60

        # 输出烧录计划
        if self._ota_mode:
            total_kb = sum(
                p.stat().st_size for p, _ in segments if p.exists()
            ) / 1024
            names = [p.name for p, _ in segments]
            print(f"  [FLASH] OTA 模式: {' + '.join(names)} ({seg_count} 段, "
                  f"{total_kb:.0f}KB)")
        print(f"  [FLASH] 命令: {cmd[0]} ... -c \"{segments[0][1][:60]}...\" "
              f"(-c \"reset; exit\")")
        print(f"  [FLASH] 超时: {flash_timeout}s")

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=flash_timeout,
            )

            log = result.stdout + result.stderr

            # 以 OpenOCD 返回码为主要判定依据
            # 关键词检查仅用于补充诊断信息，不覆盖返回码
            if result.returncode == 0:
                success = True
                # OTA 模式附加验证：确认所有段都被提及
                if self._ota_mode:
                    missing = [
                        p.name for p, _ in segments
                        if p.name not in log
                    ]
                    if missing:
                        # 文件未出现在日志中 ≠ 烧录失败
                        # 可能日志格式不同，不因此判失败
                        log += f"\n[INFO] 未在日志中找到烧录记录: {missing}"
            else:
                # 返回码非零 → 真正失败，提取错误信息
                success = False
                # 提取 OpenOCD 错误行
                error_lines = [
                    l for l in log.split("\n")
                    if any(kw in l.lower() for kw in
                           ["error:", "failed", "unable"])
                ]
                if error_lines:
                    log = "OpenOCD 返回错误:\n" + "\n".join(error_lines[-5:])

            return success, log[-3000:]
        except subprocess.TimeoutExpired:
            return False, (
                f"烧录超时（{flash_timeout}s）—— {seg_count} 段, "
                f"bootloader 较大(约1700KB)需更长时间"
            )
        except FileNotFoundError:
            return False, f"找不到 OpenOCD: {self.cfg.OPENOCD_EXE}"


# ─── Harness 主循环 ───────────────────────────────────────────────────

class AIEmbedHarness:
    """
    AI 嵌入式自主迭代 Harness 主类。

    一个循环 = 编译 → 烧录 → 监控 → 断言 → 反馈
    """

    def __init__(self,
                 project_dir: str,
                 elf_path: str,
                 openocd_cfg: str = "openocd.cfg",
                 config: HarnessConfig = None,
                 monitor=None,
                 ui: Optional["HarnessUI"] = None,
                 dashboard: Optional["DashboardExporter"] = None):
        self.config = config or HarnessConfig()
        self.build_sys = BuildSystem(project_dir, elf_path, openocd_cfg, config)
        self.ui = ui or (_UI_AVAILABLE and HarnessUI(plain=False))
        self.dashboard = dashboard
        if monitor is not None:
            self.monitor = monitor
        else:
            self.monitor = OpenOCDClient(
                host=self.config.OPENOCD_HOST,
                port=self.config.OPENOCD_TCL_PORT,
                gdb_cmd=self.config.GDB_CMD,
            )
        self.history: list[Observation] = []
        self.iteration = 0
        self.policy = PolicyGateway(project_dir)
        self.memory = HarnessMemory(project_dir)
        self._scenario_desc = ""
        self._skip_build = False
        self._monitor_only = False

    def execute_one_iteration(self, variables: list[str],
                               expectations: list[dict] = None,
                               scenario_desc: str = "",
                               sample_duration: float = None,
                               sample_interval: float = None) -> Observation:
        """
        执行一轮: 编译 → 烧录 → 监控 → 断言

        参数:
          variables:     要监控的全局变量名列表
          expectations:  断言期望列表
          scenario_desc: 场景描述（传给 AI 反馈用）
          sample_duration: 采样时长（秒）
          sample_interval: 采样间隔（秒）

        返回:
          Observation 对象（含所有阶段结果）
        """
        self.iteration += 1
        dur = sample_duration or self.config.SAMPLE_DURATION
        interv = sample_interval or self.config.SAMPLE_INTERVAL

        obs = Observation(
            iteration=self.iteration,
            timestamp=time.time(),
        )

        # ── 1. 编译 ──
        if expectations:
            obs.build = None  # placeholder
        _t0 = time.time()
        if self._skip_build:
            # 跳过编译，使用已有的 ELF
            ok = self.build_sys.elf_path.exists()
            log = (f"[SKIP] 跳过编译，使用已有 ELF: {self.build_sys.elf_path}"
                   if ok else f"ELF 不存在: {self.build_sys.elf_path}")
            obs.build_success = ok
            obs.build_log = log
            if self.ui:
                self.ui.log(f"跳过编译，使用已有 ELF", style="cyan")
            else:
                print(f"[{self.iteration}] 跳过编译...", end=" ")
                print("[OK]" if ok else "[FAIL]")
            if not ok:
                obs.errors.append(f"跳过编译但 ELF 不存在")
                self.history.append(obs)
                return obs
        else:
            if self.ui:
                self.ui.show_iteration_header(self.iteration, 0, scenario_desc)
                self.ui.log(f"编译中...", style="blue")
            else:
                print(f"\n[{self.iteration}] 编译...", end=" ", flush=True)
            ok, log = self.build_sys.build()
            obs.build_success = ok
            obs.build_log = log
            _elapsed = time.time() - _t0
            if self.ui:
                self.ui.show_compile(ok, _elapsed, log)
            else:
                print("[OK]" if ok else "[FAIL]")
            if not ok:
                obs.errors.append("编译失败")
                self.history.append(obs)
                return obs

        # ── 2. 烧录 (仅监控模式跳过) ──
        if self._monitor_only:
            obs.flash_success = True
            obs.flash_log = "[SKIP] 仅监控模式，跳过烧录"
            if self.ui:
                self.ui.log("仅监控模式，跳过烧录", style="cyan")
        else:
            if self.ui:
                segments = getattr(self.build_sys, 'flash_segments', [])
                self.ui.show_flash_start(segments, 60)
                self.ui.log(f"烧录中...", style="blue")
            else:
                print(f"[{self.iteration}] 烧录...", end=" ", flush=True)

            _t0 = time.time()
            ok, log = self.build_sys.flash()
            obs.flash_success = ok
            obs.flash_log = log
            _elapsed = time.time() - _t0

            if self.ui:
                if ok:
                    seg_count = len(getattr(self.build_sys, 'flash_segments', []))
                    self.ui.show_flash_success(seg_count, _elapsed)
                else:
                    diagnosis = self._diagnose_flash_failure(log)
                    obs.errors.append(f"烧录失败: {diagnosis['summary']}")
                    obs.errors.extend(diagnosis["details"])
                    self.history.append(obs)
                    self.ui.show_flash_fail(diagnosis)
                    if diagnosis["fatal"]:
                        self.ui.show_halt("烧录失败为硬件/配置问题，停止迭代。修复后重新运行。")
                        self._save_history()
                        sys.exit(1)
                    return obs
            else:
                print("[OK]" if ok else "[FAIL]")
                if not ok:
                    diagnosis = self._diagnose_flash_failure(log)
                    obs.errors.append(f"烧录失败: {diagnosis['summary']}")
                    obs.errors.extend(diagnosis["details"])
                    self.history.append(obs)
                    print(f"  [HALT] 烧录失败: {diagnosis['summary']}")
                    if diagnosis["fatal"]:
                        self._save_history()
                        sys.exit(1)
                    return obs

            # 等待芯片启动（烧录后才需要）
            time.sleep(self.config.BOOT_WAIT)

        # 显示后端健康状态（仅 hybrid 模式）
        if hasattr(self.monitor, 'mode_info'):
            print(f"  [BACKEND] {self.monitor.mode_info}")

        # 确保监控后端可用
        self._ensure_monitor_ready()

        # ── 3. 监控 ──
        if self.ui:
            self.ui.log(f"监控 {variables}...", style="blue")
        else:
            print(f"[{self.iteration}] 监控 {variables}...", end=" ", flush=True)

        # 从期望中提取各变量的读取位宽
        var_widths = {}
        if expectations:
            for exp in expectations:
                v = exp.get("var", "")
                w = exp.get("width")
                if v and w in (8, 16, 32):
                    var_widths[v] = w

        samples = self.monitor.sample_multiple(
            elf_path=str(self.build_sys.elf_path),
            variables=variables,
            duration=dur,
            interval=interv,
            widths=var_widths if var_widths else None,
        )
        obs.samples = samples
        has_data = any(len(v) > 0 for v in samples.values())

        if self.ui:
            if has_data:
                self.ui.show_samples(samples)
            else:
                self.ui.log("监控无数据", style="red")
        else:
            print(f"{'[OK] 有数据' if has_data else '[FAIL] 无数据'}")

        if not has_data:
            obs.errors.append("监控无数据——检查 OpenOCD 是否运行，变量名是否正确")
            if not self.monitor.is_connected():
                obs.errors.append("OpenOCD TCL 端口不通——确认 F5 调试中或已单独启动 OpenOCD")
            self.history.append(obs)
            return obs

        # ── 4. 断言检查 ──
        if expectations:
            results = run_checks(samples, expectations, interv)
            obs.check_results = results
            if self.ui:
                self.ui.show_assertions(results)
            else:
                passed = sum(1 for r in results if r.passed)
                print(f"[{self.iteration}] 断言检查: {passed}/{len(results)} 通过")

        self.history.append(obs)
        self.memory.record(obs, scenario_desc)
        return obs

    def interactive_mode(self, variables: list[str],
                          expectations: list[dict] = None,
                          scenario_desc: str = ""):
        """
        交互模式：每轮执行后打印反馈，等你输入 AI 决策。

        适用场景：你手动把反馈给 Claude，Claude 给出修改方案，
        你应用修改后再回车继续下一轮。
        """
        print("\n=== AI 嵌入式 Harness — 交互模式 ===\n")
        print("按 Enter 执行下一轮，输入 q 退出\n")
        print("提示: 如遇到策略违规，Harness 会自动拒绝危险修改\n")

        try:
            while True:
                obs = self.execute_one_iteration(
                    variables=variables,
                    expectations=expectations,
                    scenario_desc=scenario_desc,
                )

                # 打印结构化反馈（含记忆上下文）
                memory_context = self.memory.build_context(scenario_desc)
                print("\n" + obs.to_ai_prompt(
                    scenario_desc,
                    memory_context=memory_context,
                    compact=True,
                ))
                print()

                if obs.all_passed():
                    print("\n[GOAL] 所有检查通过！目标达成。")
                    self._save_history()
                    break

                action = input("-> 按 Enter 继续下一轮（或 q 退出）: ").strip()
                if action.lower() in ("q", "quit", "exit"):
                    print("退出。")
                    self._save_history()
                    break

        except KeyboardInterrupt:
            print("\n\n用户中断。")

    def auto_mode(self, variables: list[str],
                   expectations: list[dict],
                   scenario_desc: str = "",
                   max_iterations: int = 20,
                   ai_api_func=None):
        """
        自动模式：需要传入 ai_api_func，它接受 Observation，
        返回 AIDecision。

        ai_api_func(observation, scenario_desc, memory_context) → AIDecision

        流程:
          编译 → 烧录 → 监控 → 断言 → AI 决策
          → 策略门控 → 应用修改 → 下一轮
        """
        self._scenario_desc = scenario_desc
        consecutive_policy_fails = 0

        for i in range(max_iterations):
            obs = self.execute_one_iteration(
                variables=variables,
                expectations=expectations,
                scenario_desc=scenario_desc,
            )

            # ── 初始化 SWD 输入模拟器（OpenOCD 已就绪） ──
            if (hasattr(self, '_ai_backend')
                    and self._ai_backend.input_sim is None):
                try:
                    from monitor_client import SWDInputSimulator
                    sim = SWDInputSimulator(self.monitor)
                    self._ai_backend.input_sim = sim
                    if self.ui:
                        self.ui.log("SWD 输入模拟器就绪 — AI 可自主翻页",
                                    style="cyan")
                except Exception as e:
                    if self.ui:
                        self.ui.log(f"输入模拟器初始化失败: {e}",
                                    style="yellow")

            # ── 如果任务包含翻页验收，自动执行完整翻页序列 ──
            if (hasattr(self, '_ai_backend')
                    and self._ai_backend.input_sim is not None
                    and ("翻页" in scenario_desc or "navigate" in scenario_desc.lower())):
                self._execute_navigation_sequence(scenario_desc)

            # 构建记忆上下文（合并短期+长期记忆）
            memory_context = self.memory.build_context(scenario_desc)
            obs_prompt = obs.to_ai_prompt(
                scenario_desc,
                memory_context=memory_context,
                compact=True,
            )

            # ── Dashboard 导出 ──
            if self.dashboard:
                self.dashboard.export_observation(obs, scenario_desc)

            # ── UI 汇总 ──
            if self.ui:
                self.ui.show_iteration_summary(obs)
            else:
                print(obs_prompt)

            if obs.all_passed():
                if self.ui:
                    self.ui.show_goal(i + 1)
                else:
                    print(f"\n[GOAL] 第 {i+1} 轮达成目标！所有断言通过。")
                self._save_history()
                if self.dashboard:
                    self.dashboard.close("goal_achieved")
                return True

            if ai_api_func:
                if self.ui:
                    self.ui.log("AI 决策中...", style="blue")
                else:
                    print(f"\n--- AI 决策中... ---")

                decision = ai_api_func(obs, scenario_desc,
                                        memory_context)

                # ── Dashboard 导出决策 ──
                if self.dashboard:
                    self.dashboard.export_decision(obs, decision,
                                                    scenario_desc)

                if decision.goal_achieved:
                    if self.ui:
                        self.ui.show_goal(i + 1)
                    else:
                        print(f"\n[GOAL] AI 判定目标达成！")
                    self._save_history()
                    if self.dashboard:
                        self.dashboard.close("ai_goal_achieved")
                    return True

                if self.ui:
                    self.ui.show_ai_decision(i + 1, decision)
                else:
                    print(f"AI 决定修改 {len(decision.code_changes)} 个文件")

                # 策略门控
                ok = self._apply_ai_changes(decision,
                                            scenario=scenario_desc)
                if not ok:
                    consecutive_policy_fails += 1
                    if consecutive_policy_fails >= 3:
                        msg = (f"连续 {consecutive_policy_fails} 轮策略违规，终止迭代")
                        if self.ui:
                            self.ui.show_halt(msg)
                        else:
                            print(f"\n[FAIL] {msg}")
                        self._save_history()
                        if self.dashboard:
                            self.dashboard.close("policy_violation")
                        return False
                else:
                    consecutive_policy_fails = 0
                    # 记录 AI 决策到长期记忆
                    self.memory.record_decision(obs, decision,
                                                scenario_desc)
            else:
                # ai_api_func=None → 纯监控模式
                # 一轮采样显示结果后直接退出
                if self.dashboard:
                    self.dashboard.close("monitor_complete")
                return True

        msg = f"达到最大迭代次数 {max_iterations}"
        if self.ui:
            self.ui.show_halt(msg)
        else:
            print(f"\n[FAIL] {msg}")
        self._save_history()
        if self.dashboard:
            self.dashboard.close("max_iterations")
        return False

    def _apply_ai_changes(self, decision: AIDecision,
                          scenario: str = "") -> bool:
        """
        应用 AI 的代码修改（经过策略门控）。

        安全措施:
          - 策略门控检查：文件白名单、内容安全
          - 路径遍历防护：禁止修改项目目录以外的文件
          - 自动创建父目录：如果子目录不存在则创建

        返回:
            True  = 所有修改已应用
            False = 有违规被拒绝，修改未全部应用
        """
        # ── 策略门控 ──
        violations = self.policy.check_all(
            decision.code_changes,
            scenario=scenario or self._scenario_desc,
            elf_path=str(self.build_sys.elf_path)
            if self.build_sys.elf_path.exists() else None,
        )

        if violations:
            print(f"\n  [POLICY] ⛔ 策略违规 ({len(violations)} 项):")
            for v in violations:
                print(f"    ├─ {v}")

            if self.policy.has_repeated_violations:
                print(f"    └─ ⚠️ 连续多次违规，AI 可能在绕过策略")

            # 将违规信息加入历史 Observation 供 AI 感知
            error_msg = "策略门控拒绝: " + "; ".join(violations)
            if self.history:
                self.history[-1].errors.append(error_msg)
            return False

        # ── 应用修改 ──
        project_dir = self.build_sys.project_dir.resolve()
        applied = 0

        for change in decision.code_changes:
            file_rel = change.get("file", "")
            if not file_rel:
                continue

            # 路径解析 + 遍历防护（双重保险）
            abs_path = (project_dir / file_rel).resolve()
            try:
                abs_path.relative_to(project_dir)
            except ValueError:
                print(f"  [WARN] ⛔ 路径越界，已跳过: {file_rel}")
                continue

            if "content" in change and change["content"]:
                abs_path.parent.mkdir(parents=True, exist_ok=True)
                abs_path.write_text(change["content"])
                print(f"  [EDIT] ✅ {file_rel} ({len(change['content'])} chars)")
                applied += 1

        if applied == 0 and decision.code_changes:
            print(f"  [WARN] 所有修改均被跳过（路径越界或内容为空）")
            return False

        return True

    # ── 自动翻页序列 ───────────────────────────────────────────

    NAV_SEQUENCE = [
        ("main", "mode",    "mode_ui_show"),
        ("mode", "main",    "mode_ui_hide"),
        ("main", "settings","settings_ui_show"),
        ("settings", "main","settings_ui_hide"),
        ("main", "clock",   "clock_ui_show"),
        ("clock", "main",   "clock_ui_hide"),
        ("main", "camera",  "camera_ui_show"),
        ("camera", "main",  "camera_ui_hide"),
    ]

    def _execute_navigation_sequence(self, scenario_desc: str = ""):
        """
        自动执行完整翻页序列：main->mode->main->settings->main->clock->main->camera->main

        在 AI 决策前执行，确保 AI 能看到各页面的状态。
        """
        if self.ui:
            self.ui.log("执行预设翻页序列 (8次)...", style="cyan")
        else:
            print("\n[NAV] 执行预设翻页序列 (8次)...")

        sim = self._ai_backend.input_sim
        if not sim:
            if self.ui:
                self.ui.log("翻页跳过: 输入模拟器未就绪", style="yellow")
            return False

        success_count = 0
        total = len(self.NAV_SEQUENCE)

        for i, (from_page, to_page, func_name) in enumerate(self.NAV_SEQUENCE, 1):
            if self.ui:
                self.ui.log(f"  [{i}/{total}] {from_page} -> {to_page} ({func_name})",
                            style="blue")
            else:
                print(f"  [NAV] [{i}/{total}] {from_page} -> {to_page}")

            try:
                ok = sim.switch_page(to_page)
                if ok:
                    success_count += 1
                    if self.ui:
                        self.ui.log(f"    [OK] 已切换到 {to_page} 页", style="green")
                else:
                    if self.ui:
                        self.ui.log(f"    [FAIL] {to_page} 切换失败", style="red")
            except Exception as e:
                if self.ui:
                    self.ui.log(f"    [ERROR] {e}", style="red")

        # 最后确认回到主页
        if sim._current_page != "main":
            sim.switch_page("main")

        status = f"翻页完成: {success_count}/{total} 成功"
        if self.ui:
            self.ui.log(status, style="green" if success_count == total else "yellow")
        else:
            print(f"\n[NAV] {status}")

        return success_count == total

    # ── 烧录失败诊断 ───────────────────────────────────────────

    def _diagnose_flash_failure(self, log: str) -> dict:
        """
        分析 OpenOCD 烧录失败日志，返回诊断结果。

        返回:
            {
                "summary": str,       # 一句话总结
                "details": [str],     # 具体错误行
                "suggestion": str,    # 修复建议
                "fatal": bool,        # True=不能自动恢复, False=可重试
            }
        """
        log_lower = log.lower()
        details = []

        # ── 错误模式匹配（优先级从高到低） ──

        # 1. OpenOCD 进程未启动
        if "cannot connect to server" in log_lower or "connection refused" in log_lower:
            return {
                "summary": "OpenOCD 未运行",
                "details": ["无法连接到 OpenOCD TCL 端口 (localhost:6666)"],
                "suggestion": "在 VSCode 中按 F5 启动调试，或手动启动 OpenOCD",
                "fatal": True,
            }

        # 2. ST-LINK 未检测到
        if any(kw in log_lower for kw in [
            "stlink connection error", "cannot identify the target",
            "stlink usb communication error", "unable to find a valid",
            "no stlink found", "stlink send error",
        ]):
            return {
                "summary": "ST-LINK 调试器连接异常",
                "details": ["OpenOCD 正在运行但无法与 ST-LINK 通信"],
                "suggestion": "检查 USB 连接，确认 ST-LINK 驱动正常 (lsusb 或设备管理器)",
                "fatal": True,
            }

        # 3. 目标芯片未响应
        if any(kw in log_lower for kw in [
            "target not found", "no target found",
            "target not halted", "init mode failed",
            "can't find cortex-m", "tdi/idle loop",
            "timeout waiting for target halt",
        ]):
            details.append("OpenOCD 已连接 ST-LINK 但目标芯片无响应")
            # 尝试连接 TCL 端口确认 OpenOCD 状态
            swd_connected = self._check_swd_connection()
            if not swd_connected:
                return {
                    "summary": "芯片未响应或 SWD 接线异常",
                    "details": details + ["OpenOCD TCL 端口不可达"],
                    "suggestion": "1) 检查 SWD 四线连接 (SWDIO/SWCLK/GND/3.3V)\n"
                                  "2) 确认芯片供电正常\n"
                                  "3) 检查复位电路",
                    "fatal": True,
                }
            return {
                "summary": "芯片未响应或 SWD 接线异常",
                "details": details,
                "suggestion": "1) 检查 SWD 四线连接\n2) 按一下开发板复位键\n3) 检查 openocd.cfg 配置",
                "fatal": True,
            }

        # 4. Flash 烧录错误
        if any(kw in log_lower for kw in [
            "flash write error", "flash programming failed",
            "verify error", "write timeout",
            "cannot write to flash", "protection error",
            "write protected",
        ]):
            return {
                "summary": "Flash 写入失败",
                "details": ["OpenOCD 可以连接芯片但 Flash 写入异常"],
                "suggestion": "1) 检查 Flash 写保护状态\n"
                              "2) 确认芯片未处于低功耗模式\n"
                              "3) 尝试复位后重试",
                "fatal": True,
            }

        # 5. 文件未找到
        if any(kw in log_lower for kw in [
            "file not found", "cannot open", "no such file",
            "failed to open", "not a valid",
        ]):
            return {
                "summary": "烧录文件不存在",
                "details": ["指定的 ELF 或 BIN 文件未找到"],
                "suggestion": f"检查 --elf 路径是否正确: {self.build_sys.elf_path}",
                "fatal": True,
            }

        # 6. ST_SCRIPTS 路径
        if "st_scripts" in log_lower or "cannot find script" in log_lower:
            return {
                "summary": "OpenOCD 脚本路径错误",
                "details": ["ST_SCRIPTS 目录不存在，CubeIDE 版本更新后路径会变"],
                "suggestion": f"检查 ST_SCRIPTS 路径: {self.config.ST_SCRIPTS}",
                "fatal": True,
            }

        # 7. 未知错误（打印日志尾部供排查）
        log_tail = log[-400:].strip()
        return {
            "summary": f"未知烧录错误 (OpenOCD exit code != 0)",
            "details": [f"日志尾部:\n{log_tail}"],
            "suggestion": "检查 OpenOCD 日志中的具体错误信息",
            "fatal": True,
        }

    def _check_swd_connection(self) -> bool:
        """检查 OpenOCD TCL 端口是否可达"""
        try:
            if hasattr(self.monitor, 'is_connected'):
                return self.monitor.is_connected()
            import socket
            s = socket.socket()
            s.settimeout(2)
            s.connect((self.config.OPENOCD_HOST, self.config.OPENOCD_TCL_PORT))
            s.close()
            return True
        except:
            return False

    def _ensure_monitor_ready(self):
        """确保监控后端可访问。

        烧录后 OpenOCD 会 exit，监控前需要重新连接。
        策略:
          1. 如果已有连接（F5 调试中），直接使用
          2. 如果无连接，尝试启动后台 OpenOCD 用于监控
        """
        # 检查是否已连接
        if hasattr(self.monitor, 'is_connected'):
            if self.monitor.is_connected():
                return  # 已有连接（F5 调试中）

        # 尝试自动启动后台 OpenOCD
        if self.ui:
            self.ui.log("OpenOCD 未运行，尝试自动启动...", style="yellow")
        else:
            print("[MONITOR] OpenOCD 未运行，尝试自动启动...")

        try:
            import subprocess
            cfg_path = self.build_sys.openocd_cfg_path
            scripts_dir = Path(self.config.ST_SCRIPTS)

            proc = subprocess.Popen(
                [self.config.OPENOCD_EXE, "-s", str(scripts_dir),
                 "-f", str(cfg_path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

            # 等待 TCL 端口就绪 + SWD 连接可用
            import socket, time
            tcl_ready = False
            for _ in range(20):  # 最多等 10 秒 TCL 就绪
                time.sleep(0.5)
                try:
                    s = socket.socket()
                    s.settimeout(1)
                    s.connect((self.config.OPENOCD_HOST,
                               self.config.OPENOCD_TCL_PORT))
                    s.close()
                    tcl_ready = True
                    break
                except:
                    continue

            if not tcl_ready:
                proc.kill()
                if self.ui:
                    self.ui.log("OpenOCD TCL 端口未就绪", style="red")
                return

            # TCL 就绪后等待 SWD 连接建立
            if hasattr(self.monitor, '_tcl_send'):
                swd_ok = False
                for _ in range(10):
                    time.sleep(0.5)
                    resp = self.monitor._tcl_send("mdw 0x20000000 1")
                    if resp and "0x" in resp:
                        swd_ok = True
                        break

                if swd_ok:
                    # 让芯片从复位向量重新运行（OpenOCD 连接时会 halt）
                    self.monitor._tcl_send("reset run")
                    time.sleep(0.5)
                    if self.ui:
                        self.ui.log("OpenOCD + SWD 连接就绪",
                                    style="green")
                    return

            if self.ui:
                self.ui.log("OpenOCD 启动但 SWD 未连接", style="yellow")
            else:
                print("[MONITOR] OpenOCD 启动但 SWD 未连接")
        except Exception as e:
            if self.ui:
                self.ui.log(f"OpenOCD 启动异常: {e}", style="red")

    def _save_history(self):
        """保存完整迭代历史到文件"""
        output = {
            "harness": "AI Embedded Harness v1",
            "project": str(self.build_sys.project_dir),
            "elf": str(self.build_sys.elf_path),
            "total_iterations": self.iteration,
            "history": [
                {
                    "iteration": h.iteration,
                    "build_success": h.build_success,
                    "flash_success": h.flash_success,
                    "summary": h.summary(),
                    "all_passed": h.all_passed(),
                    "errors": h.errors,
                }
                for h in self.history
            ],
        }
        out_path = _HERE / f"run_{int(time.time())}.json"
        out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False))
        print(f"\n[SAVED] 运行记录已保存: {out_path}")


# ─── 场景文件加载 ────────────────────────────────────────────────────

def load_scenario(path: str) -> dict:
    """加载 YAML 场景文件"""
    import yaml
    path = Path(path)
    if not path.exists():
        print(f"场景文件不存在: {path}")
        sys.exit(1)
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def scenario_to_harness(scenario: dict):
    """从场景 dict 提取 harness 参数"""
    targets = scenario.get("targets", [])
    variables = list(dict.fromkeys(
        t["variable"] for t in targets
    ))
    expectations = []
    for t in targets:
        exp = {
            "var": t["variable"],
            "check": t["check"],
            "params": t.get("params", {}),
        }
        # 透传可选字段: width(8/16/32), type(float等)
        if "width" in t:
            exp["width"] = t["width"]
        if "type" in t:
            exp["type"] = t["type"]
        expectations.append(exp)
    return variables, expectations


# ─── 入口 ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="AI 嵌入式 Harness — 自主迭代闭环",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # 工程参数
    parser.add_argument("--dir", help="工程根目录（默认当前目录）",
                        default=".")
    parser.add_argument("--elf", help="ELF 文件路径（相对或绝对）",
                        default="build/Debug/firmware.elf")
    parser.add_argument("--cfg", help="openocd.cfg 文件名",
                        default="openocd.cfg")

    # 任务定义
    parser.add_argument("--scenario", "-s", help="场景 YAML 文件路径")
    parser.add_argument("--task", "-t", help="自然语言描述的任务目标")

    # 监控参数
    parser.add_argument("--var", nargs="+", help="要监控的变量名列表")
    parser.add_argument("--interval", type=float, default=0.3,
                        help="采样间隔（秒）")
    parser.add_argument("--duration", type=float, default=3.0,
                        help="采样时长（秒）")
    parser.add_argument("--iterations", type=int, default=20,
                        help="最大迭代次数")

    # 后端选择
    parser.add_argument("--backend", choices=["swd", "renode", "hybrid"], default="swd",
                        help="硬件后端: swd=真硬件, renode=模拟器, hybrid=混合")

    # 模式
    parser.add_argument("--interactive", action="store_true",
                        help="交互模式（每轮手动给 AI 反馈）")
    parser.add_argument("--dry-run", action="store_true",
                        help="仅检查配置，不执行循环")
    parser.add_argument("--skip-build", action="store_true",
                        help="跳过编译步骤（使用已有的 ELF 直接烧录+监控）")
    parser.add_argument("--monitor-only", action="store_true",
                        help="仅监控模式（跳过编译和烧录，直连已有 OpenOCD 采样）")

    # UI 选项
    parser.add_argument("--plain", action="store_true",
                        help="纯文本模式（禁用 Rich 彩色输出）")
    parser.add_argument("--export-dashboard", type=str, nargs="?",
                        const="./traces", default=None,
                        help="导出运行轨迹到目录（默认 ./traces），"
                             "供 AgentFlow Dashboard 使用")

    # AI 决策后端（自动模式用）
    parser.add_argument("--ai-model",
                        default=None,
                        help="模型名（默认: claude-sonnet-5; DeepSeek 后端自动用 deepseek-chat）")
    parser.add_argument("--no-ai", action="store_true",
                        help="禁用 AI 决策后端（自动模式也不调用 AI）")

    args = parser.parse_args()

    # ── 装载场景 ──
    scenario_desc = args.task or "(未指定)"
    variables = args.var or []
    expectations = []

    if args.scenario:
        scenario = load_scenario(args.scenario)
        scenario_desc = scenario.get("name") or scenario.get("description", args.scenario)
        vars_from_scenario, exps_from_scenario = scenario_to_harness(scenario)
        variables = variables or vars_from_scenario
        expectations = exps_from_scenario
        if args.task:
            scenario_desc += f" | {args.task}"

    if not variables:
        parser.print_help()
        print("\n错误: 请指定 --var 或 --scenario")
        sys.exit(1)

    if args.dry_run:
        print("=== Harness 配置检查 ===\n")
        print(f"  工程目录: {args.dir}")
        print(f"  ELF 路径:  {args.elf}")
        print(f"  场景:      {scenario_desc}")
        print(f"  监控变量:  {variables}")
        print(f"  断言的期望: {[e.get('check') for e in expectations]}")
        print(f"  采样:      {args.interval}s 间隔, {args.duration}s 时长")
        print(f"  迭代上限:  {args.iterations}")
        print(f"  模式:      {'交互' if args.interactive else '自动'}")
        print("\n[OK] 配置检查完成")
        return

    # ── 创建硬件后端 ──
    backend_client = None
    if args.backend == "renode":
        print(f"使用 Renode 模拟器后端")
        backend_client = create_backend("renode", args.dir, args.elf)
        # Renode 不需要编译烧录
    elif args.backend == "hybrid":
        print(f"使用混合后端 (Renode LCD + SWD CAN)")
        backend_client = create_backend("hybrid", args.dir, args.elf)
    else:
        print(f"使用真硬件 SWD 后端")

    # ── 创建 UI 和 Dashboard ──
    harness_ui = None
    if not args.plain and _UI_AVAILABLE:
        harness_ui = HarnessUI(plain=False)
        harness_ui.log("Harness AI 启动", style="bold green")

    dash_exporter = None
    if args.export_dashboard and DashboardExporter is not None:
        dash_exporter = DashboardExporter(trace_dir=args.export_dashboard)
        if harness_ui:
            harness_ui.log(f"Dashboard 导出: {args.export_dashboard}", style="cyan")
        else:
            print(f"[DASHBOARD] 导出到: {args.export_dashboard}")

    # ── 创建 Harness ──
    harness = AIEmbedHarness(
        project_dir=args.dir,
        elf_path=args.elf,
        openocd_cfg=args.cfg,
        monitor=backend_client,
        ui=harness_ui,
        dashboard=dash_exporter,
    )

    # 修改采样参数
    harness.config.SAMPLE_INTERVAL = args.interval
    harness.config.SAMPLE_DURATION = args.duration
    harness._skip_build = args.skip_build or args.monitor_only
    harness._monitor_only = args.monitor_only

    # ── 运行 ──
    if args.interactive:
        harness.interactive_mode(
            variables=variables,
            expectations=expectations or None,
            scenario_desc=scenario_desc,
        )
    else:
        # 自动模式：接入 AI 决策后端
        ai_api_func = None
        if not args.no_ai and _AI_BACKEND_AVAILABLE:
            try:
                ai_backend = ClaudeAIBackend(
                    model=args.ai_model,
                    project_dir=args.dir,
                )
                harness._ai_backend = ai_backend  # 供 auto_mode 中设置 input_sim
                ai_api_func = ai_backend.decide
                print(f"[AI] 使用 Claude {args.ai_model} 作为 AI 决策后端")
                print(f"[AI] 在 auto 模式下全自动迭代\n")
            except ValueError as e:
                print(f"[WARN] {e}")
                print("[WARN] 自动模式需要 API Key，降级为交互模式\n"
                      "  设置 ANTHROPIC_API_KEY 环境变量后自动生效\n"
                      "  或使用 --interactive 手动交互\n")
                harness.interactive_mode(
                    variables=variables,
                    expectations=expectations or None,
                    scenario_desc=scenario_desc,
                )
                return
        elif args.no_ai:
            print("[INFO] --no-ai 已指定，跳过 AI 决策\n")

        if ai_api_func:
            harness.auto_mode(
                variables=variables,
                expectations=expectations,
                scenario_desc=scenario_desc,
                max_iterations=args.iterations,
                ai_api_func=ai_api_func,
            )
        else:
            # 纯监控模式（无 AI 决策）
            if harness.ui:
                harness.ui.log("纯监控模式 --no-ai：执行一轮采样后退出",
                               style="cyan")
            else:
                print("[INFO] 纯监控模式：执行一轮采样后退出")
            harness.auto_mode(
                variables=variables,
                expectations=expectations or None,
                scenario_desc=scenario_desc,
                max_iterations=1,
                ai_api_func=None,
            )


if __name__ == "__main__":
    main()
