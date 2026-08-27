"""
renode_client.py — Renode 仿真后端

Harness 后端接口（与 monitor_client.py 的 OpenOCDClient 一致）:
    start()             启动 Renode
    load_firmware(path) 加载 ELF 到仿真 Flash
    reset_and_run()     复位 CPU 并开始执行
    read_variable(name) -> int | None   读 8/16 位变量
    read_variable_32(name) -> int | None 读 32 位变量
    sample_multiple(...) -> dict[str, list[dict]]
    close()             关闭仿真

先决条件:
    Renode 1.16+ 已安装 (C:/Program Files/Renode/bin/Renode.exe)
    NT35510.cs 已放置于 plugins/ 目录
    平台 .repl 文件已创建
"""

import socket
import subprocess
import time
import re
from pathlib import Path


class RenodeBackend:
    """Harness 的 Renode 后端"""

    RENODE_EXE = "C:/Program Files/Renode/bin/Renode.exe"
    PLATFORM_FILE = None  # 由 setup_for_project() 设置
    PLUGIN_DIR = Path(__file__).parent / "plugins"

    def __init__(self, port: int = 12347, headless: bool = True):
        self.port = port
        self.headless = headless
        self.process: subprocess.Popen | None = None
        self._socket: socket.socket | None = None
        self._machine_ready = False

    # ─── 生命周期管理 ───────────────────────────────────────────

    def start(self) -> None:
        """启动 Renode 进程并等待就绪"""
        cmd = [self.RENODE_EXE, "-P", str(self.port), "--disable-xwt", "-p"]
        self.process = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self._wait_for_connection(timeout=30)
        self._machine_ready = False

    def _wait_for_connection(self, timeout: int = 30):
        start = time.time()
        while time.time() - start < timeout:
            try:
                self._connect()
                return
            except ConnectionRefusedError:
                time.sleep(0.5)
        raise TimeoutError(f"Renode 未在 {timeout}s 内启动")

    def _connect(self):
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.settimeout(5)
        self._socket.connect(("127.0.0.1", self.port))

    def close(self):
        if self._socket:
            self._socket.close()
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()

    # ─── 固件加载 ──────────────────────────────────────────────

    def load_component(self, cs_path: str):
        """加载 C# 外设插件"""
        self._monitor_cmd(f"include @{cs_path}")

    def load_platform(self, repl_path: str):
        """创建机器 + 加载平台 + 配置 RCC"""
        self._run_seq([
            "mach create",
            f"machine LoadPlatformDescription @{repl_path}",
            "sysbus WriteDoubleWord 0x40023800 0x03030301",
        ])
        self._machine_ready = True

    def load_firmware(self, elf_path: str):
        """加载 ELF 到 Flash"""
        if not self._machine_ready:
            raise RuntimeError("先调用 load_platform()")
        self._monitor_cmd(f'sysbus LoadELF "{elf_path}"')

    def reset_and_run(self):
        """从复位向量启动 CPU"""
        vals = self._read_hex("sysbus ReadDoubleWord 0x08000000")
        sp = vals[-1] if vals else 0
        vals = self._read_hex("sysbus ReadDoubleWord 0x08000004")
        pc = vals[-1] if vals else 0
        if pc & 1:
            pc -= 1  # Thumb bit
        self._run_seq([
            f"sysbus.cpu SP 0x{sp:X}",
            f"sysbus.cpu PC 0x{pc:X}",
            "sysbus.cpu IsStarted true",
            "start",
        ])

    # ─── 变量读取 ──────────────────────────────────────────────

    def read_variable(self, name: str) -> int | None:
        """读 8/16 位变量"""
        addr = self._resolve_symbol(name)
        if addr is None:
            return None
        vals = self._read_hex(f"sysbus ReadByte 0x{addr:X}")
        return vals[-1] if vals else None

    def read_variable_32(self, name: str) -> int | None:
        """读 32 位变量"""
        addr = self._resolve_symbol(name)
        if addr is None:
            return None
        vals = self._read_hex(f"sysbus ReadDoubleWord 0x{addr:X}")
        return vals[-1] if vals else None

    def read_register(self, addr: int) -> int | None:
        """读外设寄存器"""
        vals = self._read_hex(f"sysbus ReadDoubleWord 0x{addr:X}")
        return vals[-1] if vals else None

    # ─── 采样 ──────────────────────────────────────────────────

    def sample_multiple(self, variables: list[str], duration: float = 3.0,
                        interval: float = 0.3) -> dict[str, list[dict]]:
        """对多个变量轮流采样（与 monitor_client 接口一致）"""
        samples = {v: [] for v in variables}
        n = int(duration / interval)
        for i in range(n):
            for var in variables:
                val = self.read_variable(var) if var != "uwTick" else self.read_variable_32(var)
                if val is not None:
                    samples[var].append({
                        "time_offset": round(i * interval, 3),
                        "value": val,
                        "raw": hex(val),
                    })
            time.sleep(interval)
        return samples

    # ─── LCD 外设操作 ──────────────────────────────────────────

    def lcd_get_pixel(self, x: int, y: int) -> int | None:
        vals = self._read_hex(f"lcd GetPixel {x} {y}")
        return vals[-1] if vals else None

    def lcd_nonzero_pixels(self) -> int:
        vals = self._read_hex("lcd NonZeroPixelCount")
        return vals[-1] if vals else 0

    def lcd_inject_test_pattern(self):
        """注入测试图案到 LCD 帧缓冲（绕过固件，直接写外设）"""
        self._monitor_cmd("lcd FillTestPattern")

    # ─── 内部方法 ──────────────────────────────────────────────

    def _monitor_cmd(self, cmd: str) -> str:
        """发送 Monitor 命令并返回原始文本"""
        if not self._socket:
            raise ConnectionError("未连接到 Renode")
        self._socket.sendall((cmd + "\n").encode("utf-8"))
        time.sleep(0.3)
        resp = b""
        try:
            while True:
                chunk = self._socket.recv(4096)
                if not chunk:
                    break
                resp += chunk
                if b"(machine" in chunk or b"(monitor)" in chunk:
                    break
        except socket.timeout:
            pass
        return resp.decode("ascii", errors="ignore")

    def _read_hex(self, cmd: str) -> list[int]:
        """发送 Monitor 命令并解析所有 0x 值"""
        text = self._monitor_cmd(cmd)
        return [int(v, 16) for v in re.findall(r"0x([0-9a-fA-F]+)", text)]

    def _resolve_symbol(self, name: str) -> int | None:
        """从 ELF 符号表解析变量地址"""
        vals = self._read_hex(f"sysbus GetSymbolAddress {name}")
        return vals[-1] if vals else None

    def _run_seq(self, cmds: list[str]):
        """依次执行多条命令"""
        for c in cmds:
            self._monitor_cmd(c)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.close()
