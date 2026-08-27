"""
hybrid_backend.py — 混合后端

Renode 优先，SWD 补缺。

每个变量可以指定 backend:
  - "renode": 从 Renode 仿真读
  - "swd":   从真硬件 SWD 读
  - "auto":   尝试 Renode，失败回退到 SWD

用法:
    backend = HybridBackend(
        renode_port=12347,
        swd_host="127.0.0.1", swd_port=6666,
        elf_path="build/Debug/firmware.elf"
    )
    val = backend.read_variable("g_can_sensor.temperature", backend="swd")
    val = backend.read_variable("led_state", backend="renode")
"""

import time
import subprocess
import re
import struct
import socket
from pathlib import Path
from typing import Optional


# ─── 熔断器 ─────────────────────────────────────────────────────


class CircuitBreaker:
    """
    熔断器 — 三个状态: CLOSED(正常) / OPEN(熔断) / HALF_OPEN(半开)

    状态变迁:
      CLOSED → 连续 threshold 次失败 → OPEN
      OPEN   → 冷却 cooldown 秒后    → HALF_OPEN（探活）
      HALF_OPEN → 成功一次 → CLOSED
      HALF_OPEN → 再次失败  → OPEN（继续冷却）

    用法:
        breaker = CircuitBreaker("Renode", threshold=3, cooldown=30)
        result = breaker.call(my_func, arg1, arg2)
        # 熔断时返回 None，不会调用 my_func
    """

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

    def __init__(self, name: str, threshold: int = 3,
                 cooldown: float = 30.0):
        self.name = name
        self.threshold = threshold
        self.cooldown = cooldown
        self.state = self.CLOSED
        self.failure_count = 0
        self.last_failure_time = 0.0
        self.last_state_change = 0.0

    def call(self, func, *args, **kwargs):
        """
        执行受熔断保护的函数调用。

        返回:
            func 的返回值（成功时）
            None（熔断跳过 或 函数调用失败）
        """
        now = time.time()

        # ── OPEN: 直接拒绝 ──
        if self.state == self.OPEN:
            if now - self.last_state_change > self.cooldown:
                # 冷却结束 → HALF_OPEN 尝试恢复
                self.state = self.HALF_OPEN
                self.last_state_change = now
                print(f"  [BREAKER] {self.name} HALF_OPEN -> 尝试恢复")
            else:
                remaining = self.cooldown - (now - self.last_state_change)
                if int(remaining) % 10 == 0 or remaining < 5:
                    print(f"  [BREAKER] {self.name} OPEN 跳过 "
                          f"（剩余 {remaining:.0f}s 冷却）")
                return None

        # ── CLOSED / HALF_OPEN: 实际调用 ──
        try:
            result = func(*args, **kwargs)
            if result is None:
                # 返回 None 视为调用失败（无有效数据返回）
                raise ConnectionError(f"{self.name} 返回 None")
            # 成功
            if self.state == self.HALF_OPEN:
                self.state = self.CLOSED
                self.last_state_change = now
                print(f"  [BREAKER] {self.name} CLOSED <- 恢复成功")
            self.failure_count = 0
            return result
        except Exception as e:
            self.failure_count += 1
            self.last_failure_time = now
            if self.failure_count >= self.threshold \
                    and self.state != self.OPEN:
                self.state = self.OPEN
                self.last_state_change = now
                print(f"  [BREAKER] {self.name} OPEN <- "
                      f"连续 {self.failure_count} 次失败")
            return None

    @property
    def is_available(self) -> bool:
        """快速判断当前是否可用（不触发实际调用）"""
        if self.state == self.OPEN:
            now = time.time()
            if now - self.last_state_change > self.cooldown:
                return True  # 即将进入 HALF_OPEN
            return False
        return True

    def reset(self):
        """手动重置熔断器"""
        self.state = self.CLOSED
        self.failure_count = 0
        self.last_state_change = time.time()
        print(f"  [BREAKER] {self.name} 手动重置 -> CLOSED")


class HybridBackend:
    """混合后端：Renode 优先 + SWD 补缺"""

    def __init__(self, renode_port=12347, swd_host="127.0.0.1", swd_port=6666,
                 elf_path=None, renode_elf_path=None):
        self.renode_port = renode_port
        self.swd_host = swd_host
        self.swd_port = swd_port
        self.elf_path = elf_path
        self.renode_elf_path = renode_elf_path or elf_path

        # 后端实例（延迟初始化）
        self._renode = None
        self._swd = None

        # 符号缓存: {var: (addr, width)}
        self._symbols = {}

        # 熔断器
        self._renode_breaker = CircuitBreaker("Renode", threshold=3, cooldown=30.0)
        self._swd_breaker = CircuitBreaker("SWD", threshold=5, cooldown=60.0)
        self._hybrid_breaker = CircuitBreaker("Hybrid全链路", threshold=10, cooldown=120.0)

        # 当前运行模式（由熔断状态自动切换）
        # "hybrid" | "renode_only" | "swd_only" | "degraded"
        self._current_mode = "hybrid"

    # ─── 初始化 ─────────────────────────────────────────────────

    def start_renode(self):
        """启动 Renode 后端（如果还没启动）"""
        if self._renode is not None:
            return self._renode
        from renode.renode_client import RenodeBackend
        self._renode = RenodeBackend(port=self.renode_port)
        self._renode.start()
        return self._renode

    def start_swd(self):
        """启动 SWD/OpenOCD 后端（如果还没启动）"""
        if self._swd is not None:
            return self._swd
        from monitor_client import OpenOCDClient
        self._swd = OpenOCDClient(
            host=self.swd_host,
            port=self.swd_port,
        )
        return self._swd

    # ─── 模式自动切换 ──────────────────────────────────────────

    def _update_mode(self):
        """根据熔断器状态自动切换运行模式"""
        renode_ok = self._renode_breaker.is_available
        swd_ok = self._swd_breaker.is_available
        hybrid_ok = self._hybrid_breaker.is_available

        if not hybrid_ok:
            new_mode = "degraded"
        elif not renode_ok and not swd_ok:
            new_mode = "degraded"
        elif not renode_ok and swd_ok:
            new_mode = "swd_only"
        elif renode_ok and not swd_ok:
            new_mode = "renode_only"
        else:
            new_mode = "hybrid"

        if new_mode != self._current_mode:
            old = self._current_mode
            self._current_mode = new_mode
            print(f"  [HYBRID] 模式切换: {old} -> {new_mode}")

    # ─── 变量路由（受熔断保护） ────────────────────────────────

    # ─── LCD 虚拟变量（路由到 Renode 外设模型） ──────────────

    LCD_VIRTUAL_VARS = {
        "_lcd_active_pixels": "lcd_nonzero_pixels",
        "_lcd_screen_updated": "lcd_has_new_content",
    }

    def _read_lcd_var(self, name: str) -> Optional[int]:
        """通过 Renode LCD 外设接口读取虚拟变量"""
        if self._renode is None:
            return None
        try:
            if name == "_lcd_active_pixels":
                return self._renode.lcd_nonzero_pixels()
            elif name == "_lcd_screen_updated":
                # 采样两次看是否有新像素
                before = self._renode.lcd_nonzero_pixels()
                import time; time.sleep(0.1)
                after = self._renode.lcd_nonzero_pixels()
                return 1 if after != before else 0
        except Exception:
            return None
        return None

    def read_variable(self, name: str, backend: str = "auto") -> Optional[int]:
        """
        读变量，按 backend 策略路由到具体后端。

        LCD 虚拟变量 (_lcd_*) 直接路由到 Renode 外设模型。
        其他变量根据 _current_mode 路由:
          hybrid:      Renode 优先，失败回退 SWD
          renode_only: 仅 Renode（SWD 已熔断）
          swd_only:    仅 SWD（Renode 已熔断）
          degraded:    全部熔断，每次都尝试 Renode（希望恢复）
        """
        # LCD 虚拟变量 → 直接 Renode（不经过熔断器，LCD 只依赖 Renode）
        if name.startswith("_lcd_"):
            return self._read_lcd_var(name)

        self._update_mode()

        if self._current_mode == "degraded":
            # 全部熔断: 仍尝试 Renode（可能是短暂故障）
            val = self._renode_breaker.call(self._read_renode, name)
            if val is not None:
                return val
            print(f"  [HYBRID] 全部后端不可用，跳过 {name}")
            return None

        if backend == "renode" or self._current_mode == "renode_only":
            return self._renode_breaker.call(self._read_renode, name)
        elif backend == "swd" or self._current_mode == "swd_only":
            return self._swd_breaker.call(self._read_swd, name)
        else:  # auto + hybrid 模式: Renode 优先
            val = self._renode_breaker.call(self._read_renode, name)
            if val is not None:
                return val
            # Renode 失败，尝试 SWD
            return self._swd_breaker.call(self._read_swd, name)

    def read_variable_32(self, name: str, backend: str = "auto") -> Optional[int]:
        """读 32 位变量（受熔断保护）"""
        self._update_mode()

        if self._current_mode == "degraded":
            val = self._renode_breaker.call(self._read_renode_32, name)
            if val is not None:
                return val
            return None

        if backend == "renode" or self._current_mode == "renode_only":
            return self._renode_breaker.call(self._read_renode_32, name)
        elif backend == "swd" or self._current_mode == "swd_only":
            return self._swd_breaker.call(self._read_swd_32, name)
        else:
            val = self._renode_breaker.call(self._read_renode_32, name)
            if val is not None:
                return val
            return self._swd_breaker.call(self._read_swd_32, name)

    # ─── 单值读取（路由到具体后端） ─────────────────────────────

    def _read_renode(self, name: str) -> Optional[int]:
        if self._renode is None:
            return None
        return self._renode.read_variable(name)

    def _try_read_renode(self, name: str) -> Optional[int]:
        try:
            return self._read_renode(name)
        except Exception:
            return None

    def _read_renode_32(self, name: str) -> Optional[int]:
        if self._renode is None:
            return None
        return self._renode.read_variable_32(name)

    def _try_read_renode_32(self, name: str) -> Optional[int]:
        try:
            return self._read_renode_32(name)
        except Exception:
            return None

    def _read_swd(self, name: str) -> Optional[int]:
        """通过 OpenOCD 读变量"""
        if self._swd is None or not self._is_swd_connected():
            return None
        addr, width = self._resolve_swd(name)
        if addr is None:
            return None
        return self._swd_read(addr, width)

    def _read_swd_32(self, name: str) -> Optional[int]:
        if self._swd is None or not self._is_swd_connected():
            return None
        addr, _ = self._resolve_swd(name)
        if addr is None:
            return None
        return self._swd_read(addr, 4)

    def _is_swd_connected(self) -> bool:
        try:
            s = socket.socket()
            s.settimeout(1)
            s.connect((self.swd_host, self.swd_port))
            s.close()
            return True
        except:
            return False

    # ─── SWD 内存操作 ──────────────────────────────────────────

    def _resolve_swd(self, name: str) -> tuple:
        """通过 GDB 解析变量名→地址+宽度"""
        if name in self._symbols:
            return self._symbols[name]
        if not self.elf_path:
            return (None, 1)
        cmd_addr = ["arm-none-eabi-gdb", "--batch",
                    "-ex", f'file "{self.elf_path}"',
                    "-ex", f"print &({name})"]
        try:
            r = subprocess.run(cmd_addr, capture_output=True, text=True, timeout=10)
            m = re.search(r"0x([0-9a-fA-F]+)", r.stdout)
            if m:
                addr = int(m.group(1), 16)
                cmd_sz = ["arm-none-eabi-gdb", "--batch",
                          "-ex", f'file "{self.elf_path}"',
                          "-ex", f"print sizeof({name})"]
                r2 = subprocess.run(cmd_sz, capture_output=True, text=True, timeout=10)
                m2 = re.search(r"=\s*(\d+)", r2.stdout)
                width = int(m2.group(1)) if m2 else 4
                self._symbols[name] = (addr, width)
                return (addr, width)
        except:
            pass
        return (None, 1)

    def _swd_read(self, addr: int, width: int) -> Optional[int]:
        """通过 OpenOCD TCL 读内存"""
        try:
            s = socket.socket()
            s.settimeout(3)
            s.connect((self.swd_host, self.swd_port))
            w = {1: "mdb", 2: "mdh", 4: "mdw"}.get(width, "mdw")
            s.sendall(f"{w} 0x{addr:08X} 1\x1a".encode())
            resp = b""
            while b"\x1a" not in resp:
                resp += s.recv(1024)
            s.close()
            val_str = resp.decode(errors="replace").split(":")[1].strip().split()[0]
            return int(val_str, 16)
        except:
            return None

    # ─── 高级采样 ──────────────────────────────────────────────

    def sample_multiple(self, elf_path: str = "", variables: list[str] = None,
                        duration: float = 3.0, interval: float = 0.3,
                        widths: dict[str, int] = None,
                        variable_map: dict[str, str] = None,
                        ) -> dict[str, list[dict]]:
        """
        多变量采样（接口与 OpenOCDClient 兼容）。

        参数:
            elf_path: ELF 路径（不使用，兼容接口）
            variables: 变量名列表（主接口）
            duration / interval: 时间和间隔
            widths: 读取位宽（由 expectations 中的 width 字段驱动）
            variable_map: (兼容旧接口) {变量名: "renode"|"swd"|"auto"}

        返回: {变量名: [{time_offset, value, raw}, ...]}
        """
        self._update_mode()

        if variable_map:
            vars_list = list(variable_map.keys())
        else:
            vars_list = variables or []
            # 统一的后端路由: LCD 虚拟变量走 Renode，其余 auto
            variable_map = {
                v: ("renode" if v.startswith("_lcd_") else "auto")
                for v in vars_list
            }

        samples = {v: [] for v in vars_list}
        n = int(duration / interval)
        for i in range(n):
            for var in vars_list:
                backend = variable_map.get(var, "auto")
                val = self.read_variable(var, backend)
                if val is not None:
                    samples[var].append({
                        "time_offset": round(i * interval, 3),
                        "value": val,
                        "raw": hex(val),
                    })
            time.sleep(interval)
        return samples

    def close(self):
        if self._renode:
            self._renode.close()
        # SWD (OpenOCD) 不关闭，它可能是由 F5 调试启动的

    # ─── 诊断 ──────────────────────────────────────────────────

    @property
    def mode_info(self) -> str:
        """返回后端状态摘要文本"""
        r_state = self._renode_breaker.state
        s_state = self._swd_breaker.state
        return (f"mode={self._current_mode}"
                f" renode={r_state}(fail={self._renode_breaker.failure_count})"
                f" swd={s_state}(fail={self._swd_breaker.failure_count})")

    def reset_breakers(self):
        """手动重置所有熔断器"""
        self._renode_breaker.reset()
        self._swd_breaker.reset()
        self._hybrid_breaker.reset()
        self._current_mode = "hybrid"
