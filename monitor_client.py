"""
monitor_client.py — 对 stm32_monitor.py 的封装

通过 subprocess 调用已有的 stm32_monitor.py，解析其输出。
也支持直接通过 OpenOCD TCL 通信的方式（不依赖脚本）。
"""

import subprocess
import csv
import io
import json
import re
import socket
import sys
import time
from pathlib import Path
from typing import Any


# ─── 直接 TCL 通信（不需要调用 stm32_monitor.py） ─────────────────


class OpenOCDClient:
    """
    直接与 OpenOCD TCL 端口通信，不 halt CPU。

    复用 stm32_monitor.py 的底层原理：
    GDB 解析符号 → OpenOCD mdw/mdh/mdb 读内存。
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 6666,
                 gdb_cmd: str = "arm-none-eabi-gdb"):
        self.host = host
        self.port = port
        self.gdb_cmd = gdb_cmd
        self._symbol_cache: dict[str, int] = {}  # symbol → address

    def _tcl_send(self, command: str) -> str | None:
        """通过 TCL socket 发送命令给 OpenOCD"""
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
        except ConnectionRefusedError:
            return None
        except Exception:
            return None

    def is_connected(self) -> bool:
        """检查 OpenOCD 是否在线"""
        resp = self._tcl_send("mdw 0x20000000 1")
        return resp is not None

    def resolve_address(self, elf_path: str, expression: str) -> int | None:
        """用 arm-none-eabi-gdb 解析 C 表达式地址"""
        # GDB 不认 Windows 反斜杠（\b 被解释为退格符），转正斜杠
        elf_path = elf_path.replace("\\", "/")
        cache_key = f"{elf_path}:{expression}"
        if cache_key in self._symbol_cache:
            return self._symbol_cache[cache_key]

        cmd = [
            self.gdb_cmd, "--batch",
            "-ex", f'file "{elf_path}"',
            "-ex", f"print &({expression})",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            match = re.search(r"0x([0-9a-fA-F]+)", result.stdout)
            if match:
                addr = int(match.group(1), 16)
                self._symbol_cache[cache_key] = addr
                return addr
        except Exception:
            pass
        return None

    def read_memory(self, address: int, width: int = 32, count: int = 1) -> int | None:
        """读内存：width=8(mdb), 16(mdh), 32(mdw)"""
        cmd = {8: "mdb", 16: "mdh", 32: "mdw"}.get(width, "mdw")
        response = self._tcl_send(f"{cmd} 0x{address:08X} {count}")
        if response is None:
            return None
        try:
            parts = response.split(":")
            if len(parts) > 1:
                val_str = parts[1].strip().split()[0]
                return int(val_str, 16)
        except (ValueError, IndexError):
            pass
        return None

    def write_memory(self, address: int, value: int, width: int = 32) -> bool:
        """
        写内存：width=8(mwb), 16(mwh), 32(mww)

        用于模拟旋钮、按键等硬件输入。
        """
        cmd = {8: "mwb", 16: "mwh", 32: "mww"}.get(width, "mww")
        response = self._tcl_send(
            f"{cmd} 0x{address:08X} 0x{value:X}"
        )
        return response is not None

    def read_variable(self, elf_path: str, expression: str) -> int | float | str | None:
        """读一个全局变量的当前值"""
        addr = self.resolve_address(elf_path, expression)
        if addr is None:
            return None
        raw = self.read_memory(addr, width=32)
        if raw is None:
            return None
        return raw

    def sample_variable(self, elf_path: str, expression: str,
                         duration: float = 3.0, interval: float = 0.5
                         ) -> list[dict]:
        """
        对单个变量定时采样。

        返回: [{time_offset, value, raw}, ...]
        """
        addr = self.resolve_address(elf_path, expression)
        if addr is None:
            return []

        samples = []
        start = time.time()
        while time.time() - start < duration:
            raw = self.read_memory(addr, width=32)
            if raw is not None:
                samples.append({
                    "time_offset": round(time.time() - start, 3),
                    "value": raw,
                    "raw": hex(raw),
                })
            time.sleep(interval)

        return samples

    def sample_multiple(self, elf_path: str, variables: list[str],
                         duration: float = 3.0, interval: float = 0.5,
                         widths: dict[str, int] = None
                         ) -> dict[str, list[dict]]:
        """
        对多个变量轮流采样。

        参数:
            widths: 按变量名指定读取位宽 {变量名: 8|16|32}
                    未指定的变量默认 width=32
        返回: {变量名: [{time_offset, value, raw}, ...], ...}
        """
        widths = widths or {}
        # 预解析所有地址
        addrs = {}
        for var in variables:
            addr = self.resolve_address(elf_path, var)
            if addr is not None:
                addrs[var] = addr

        if not addrs:
            return {}

        samples = {var: [] for var in addrs}
        start = time.time()
        while time.time() - start < duration:
            for var, addr in addrs.items():
                w = widths.get(var, 32)  # 默认 32-bit
                raw = self.read_memory(addr, width=w)
                if raw is not None:
                    samples[var].append({
                        "time_offset": round(time.time() - start, 3),
                        "value": raw,
                        "raw": hex(raw),
                    })
            time.sleep(interval)

        return samples

    def read_register(self, svd_path: str | None, peripheral: str,
                       register: str) -> int | None:
        """读外设寄存器（需要预先知道地址）"""
        # 简单实现：直接从已知地址读
        # 外设寄存器地址查 SVD 或参考手册
        KNOWN_REGISTERS = {
            "GPIOA_ODR": 0x40020014,
            "GPIOA_IDR": 0x40020010,
            "GPIOA_MODER": 0x40020000,
            "GPIOB_ODR": 0x40020414,
            "USART1_DR": 0x40011024,
            "USART1_SR": 0x40011000,
            "TIM2_CNT": 0x40000024,
            "TIM2_ARR": 0x4000002C,
            "TIM2_CCR1": 0x40000034,
        }
        reg_name = f"{peripheral}_{register}" if peripheral else register
        addr = KNOWN_REGISTERS.get(reg_name) or KNOWN_REGISTERS.get(f"{peripheral}_{register}")
        if addr is None:
            return None
        return self.read_memory(addr, width=32)


# ─── 通过 stm32_monitor.py 脚本 ────────────────────────────────


# ─── SWD 输入模拟器 ─────────────────────────────────────────
# 通过 SWD 写内存模拟旋钮/按键，让 AI 自主导航 UI


class SWDInputSimulator:
    """
    SWD 输入模拟器 — 让 AI 能"动手操作"硬件。

    三种操作模式:
      1. mem_write: 通过固定地址写内存变量（触摸/旋钮/OTA触发）
      2. func_call: 通过 halt→set regs→resume 调用固件函数
      3. mem_read:  通过固定地址读变量

    参考: docs/harness_ai_toolchain.md（由工程 AI 生成的准确地址表）

    用法:
        sim = SWDInputSimulator(ocd_client)
        sim.switch_page("camera")     # 跳转到摄像头页
        sim.touch_click(400, 200)      # 模拟触摸点击
        sim.touch_release()            # 模拟触摸释放
        sim.set_theme(1)               # 切换主题
        sim.read_var("g_pressed")      # 读触摸状态
    """

    # ─── 符号地址表（来自 harness_ai_toolchain.json） ─────────
    # 使用 link_address（.map 文件中的地址，不加 1）
    # 调用方式: halt → resume [link_address]  （实测通过）
    # 不可用:   reg pc + resume（导致 HardFault）

    FUNC_ADDR = {
        "mode_ui_show":     0x08043088,  # link_address (map)
        "mode_ui_hide":     0x080430CC,
        "settings_ui_show": 0x080434C0,
        "settings_ui_hide": 0x080434E8,
        "clock_ui_show":    0x08044648,
        "clock_ui_hide":    0x080445B4,
        "camera_ui_show":   0x080448E8,
        "camera_ui_hide":   0x0804499C,
    }

    # 变量地址（固定，不依赖 GDB 解析）
    VAR_ADDR = {
        "g_camera_active":  0x20017D90,
        "g_theme_id":       0x20017C14,
        "g_ota_pending":    0x20017C17,
        "g_pressed":        0x20017B80,
        "g_last_x":         0x20017B7E,
        "g_last_y":         0x20017B7C,
        "g_width":          0x200000C2,
        "g_height":         0x200000C0,
        "g_can_sensor":     0x20017B84,
    }

    PAGE_FUNCS = {
        "main":    "camera_ui_hide",
        "mode":    "mode_ui_show",
        "settings":"settings_ui_show",
        "clock":   "clock_ui_show",
        "camera":  "camera_ui_show",
    }

    SAFE_LR = 0x080FFFFE  # 安全返回地址（死循环）

    def __init__(self, ocd: "OpenOCDClient"):
        self.ocd = ocd

    def _var_addr(self, name: str) -> int | None:
        """查变量地址表（vs GDB 解析，更快更可靠）"""
        return self.VAR_ADDR.get(name)

    # ─── 内存读写 ──────────────────────────────────────────

    def read_var(self, name: str, width: int = 32) -> int | None:
        """读变量值"""
        addr = self._var_addr(name)
        if addr is None:
            return None
        return self.ocd.read_memory(addr, width=width)

    def write_var(self, name: str, value: int, width: int = 32) -> bool:
        """写变量值"""
        addr = self._var_addr(name)
        if addr is None:
            return False
        return self.ocd.write_memory(addr, value, width=width)

    # ─── 函数调用 ──────────────────────────────────────────

    # 页面切换函数名列表（调用后不可 halt，否则冻住 LVGL 定时器）
    _PAGE_FUNCS = {
        "mode_ui_show", "mode_ui_hide",
        "settings_ui_show", "settings_ui_hide",
        "clock_ui_show", "clock_ui_hide",
        "camera_ui_show", "camera_ui_hide",
    }

    def call_func(self, func_name: str, wait_ms: int = 300,
                  r0: int = 0, r1: int = 0,
                  r2: int = 0, r3: int = 0) -> bool:
        """
        调用固件函数（实机验证通过的方案）。

        调用方式:
          halt → [设参数] → resume [map_addr] → sleep → [halt 检查]

        关键规则（来自实机验证）:
          - 必须使用 resume [map_addr]，不可用 reg pc + resume
          - link_address = .map 文件地址（不加 1，resume 自动设 Thumb 位）
          - resume 保留 LR 原值，函数返回后自然回到 RTOS 上下文
          - **页面函数** (mode/settings/clock/camera): 调用后**不可 halt**
            halt 会冻住 LVGL 的 Systick 定时器，导致动画卡死系统
          - **非页面函数** (backup, EN25Q128 等): 调用后 halt 检查 HardFault
        """
        import time

        func_addr = self.FUNC_ADDR.get(func_name)
        if func_addr is None:
            print(f"  [INPUT] 未知函数: {func_name}")
            return False

        # halt → 设参数（如有）→ resume [map_addr]
        self.ocd._tcl_send("halt")
        time.sleep(0.03)
        if r0 or r1 or r2 or r3:
            self.ocd._tcl_send(f"reg r0 0x{r0:X}")
            self.ocd._tcl_send(f"reg r1 0x{r1:X}")
            self.ocd._tcl_send(f"reg r2 0x{r2:X}")
            self.ocd._tcl_send(f"reg r3 0x{r3:X}")
        self.ocd._tcl_send(f"resume 0x{func_addr:X}")
        time.sleep(wait_ms / 1000)

        # ★ 页面函数不能 halt — halt 会冻住 LVGL Systick 定时器
        if func_name in self._PAGE_FUNCS:
            # 页面函数：跳过 halt，让 LVGL 自由渲染
            return True

        # 非页面函数：halt 检查 HardFault
        self.ocd._tcl_send("halt")
        resp = self.ocd._tcl_send("reg pc")
        failed = resp and "HardFault" in resp
        self.ocd._tcl_send("resume")

        if failed:
            print(f"  [INPUT] [!] {func_name} HardFault")
        return not failed

    # ─── 页面切换 ──────────────────────────────────────────

    # 当前所在页面（用于决定返回主页时调哪个 hide 函数）
    _current_page = "main"

    def switch_page(self, page: str) -> bool:
        """
        跳转到指定页面。

        切换规则（来自固件设计）:
          - 子页面只能从主页进入: main -> mode/settings/clock/camera
          - 返回主页必须调对应子页面的 hide 函数
          - 不允许子页间直接跳转: mode -> settings 会卡死
          - 切换后停留 5 秒等 LVGL 渲染完成
        """
        import time

        if page not in self.PAGE_FUNCS and page != "main":
            print(f"  [INPUT] 未知页面: {page}")
            return False

        if page == "main":
            hide_map = {
                "main": None,
                "mode": "mode_ui_hide",
                "settings": "settings_ui_hide",
                "clock": "clock_ui_hide",
                "camera": "camera_ui_hide",
            }
            hide_func = hide_map.get(self._current_page)
            if hide_func is None:
                print(f"  [INPUT] 已在主页")
                return True
            ok = self.call_func(hide_func, wait_ms=500)
            self._current_page = "main"
        else:
            if self._current_page != "main":
                print(f"  [INPUT] 当前在 {self._current_page}，必须先回主页")
                self.switch_page("main")
            func = self.PAGE_FUNCS[page]
            ok = self.call_func(func, wait_ms=500)
            if ok:
                self._current_page = page

        # 停留 5 秒等 LVGL 渲染、SPI Flash 读图、页面动画完成
        print(f"  [INPUT] 等待 LVGL 渲染完成 (5s)...")
        time.sleep(5)

        status = "OK" if ok else "FAIL"
        print(f"  [INPUT] 页面: {self._current_page} ({status})")
        return ok

    # ─── 触摸模拟 ──────────────────────────────────────────

    def touch_poll(self) -> bool:
        """执行一次触摸扫描（更新 g_touch/g_pressed）"""
        return self.call_func("touch_poll", wait_ms=10)

    def touch_press(self, x: int = 400, y: int = 200) -> bool:
        """模拟触摸按下"""
        self.write_var("g_last_x", x, width=16)
        self.write_var("g_last_y", y, width=16)
        self.write_var("g_pressed", 1, width=8)
        print(f"  [TOUCH] 按下 ({x}, {y})")
        return True

    def touch_release(self) -> bool:
        """模拟触摸释放"""
        self.write_var("g_pressed", 0, width=8)
        print(f"  [TOUCH] 释放")
        return True

    def touch_click(self, x: int = 400, y: int = 200) -> bool:
        """模拟一次触摸点击（按下+释放）"""
        self.touch_press(x, y)
        import time; time.sleep(0.1)
        self.touch_release()
        time.sleep(0.2)
        # 执行触摸扫描让 LVGL 处理事件
        self.touch_poll()
        return True

    # ─── 主题切换 ──────────────────────────────────────────

    def set_theme(self, theme_id: int = 1) -> bool:
        """切换主题: 0=暗黑/1=明亮/2=护眼"""
        self.write_var("g_theme_id", theme_id, width=8)
        self.call_func("settings_ui_show", wait_ms=300)
        print(f"  [THEME] 切换主题 → {theme_id}")
        return True

    # ─── OTA 触发 ──────────────────────────────────────────

    def trigger_ota(self) -> bool:
        """触发 OTA 升级流程"""
        self.write_var("g_ota_pending", 1, width=8)
        print(f"  [OTA] 触发升级")
        return True


class MonitorScriptClient:
    """
    复用已有的 stm32_monitor.py。

    注意：原始的 stm32_monitor.py 输出是实时打印的，
    这里通过 --count 参数限制采样次数并捕获 stdout。
    """

    def __init__(self, script_path: str | Path = None):
        self.script_path = script_path or self._find_default_script()

    def _find_default_script(self) -> str:
        """自动查找 stm32_monitor.py"""
        candidates = [
            "E:/ST/STM32/MY_workspace/知识库/工程实用信息/VScode环境/scripts/stm32_monitor.py",
            "../知识库/工程实用信息/VScode环境/scripts/stm32_monitor.py",
            "scripts/stm32_monitor.py",
        ]
        for path in candidates:
            if Path(path).exists():
                return str(Path(path).resolve())
        # 本地 fallback
        harness_dir = Path(__file__).parent
        local = harness_dir / "scripts" / "stm32_monitor.py"
        return str(local) if local.exists() else candidates[0]

    def sample_variable(self, elf_path: str, expression: str,
                         count: int = 10, interval: float = 0.5,
                         display_type: str = "hex") -> list[dict]:
        """通过 stm32_monitor.py 采样一个变量"""
        cmd = [
            sys.executable, self.script_path,
            "--elf", elf_path,
            "--var", expression,
            "--type", display_type,
            "--count", str(count),
            "--interval", str(interval),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            return self._parse_output(result.stdout)
        except subprocess.TimeoutExpired:
            return []
        except FileNotFoundError:
            # stm32_monitor.py 找不到，fallback 到直接 TCL
            return []

    def _parse_output(self, text: str) -> list[dict]:
        """解析 stm32_monitor.py 的打印输出"""
        samples = []
        pattern = re.compile(r"\[(\d{2}:\d{2}:\d{2})\]\s+(\S+)\s+=\s+(\S+)")
        for line in text.splitlines():
            match = pattern.search(line)
            if match:
                samples.append({
                    "timestamp": match.group(1),
                    "value": self._parse_value(match.group(3)),
                    "raw": match.group(3),
                })
        return samples

    @staticmethod
    def _parse_value(s: str):
        """智能解析数值"""
        if s.startswith("0x") or s.startswith("0X"):
            try:
                return int(s, 16)
            except ValueError:
                return s
        try:
            return float(s) if "." in s else int(s)
        except ValueError:
            return s
