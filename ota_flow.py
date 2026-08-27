#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ota_flow.py — OTA 升级全流程自动化

封装完整的双区 OTA 升级流程:
  1. 修改 F103 main.c → 启用 GATEWAY_MODE
  2. 编译 F103 工程
  3. 通过 OpenOCD 烧录 F103
  4. 启动 F407 OpenOCD
  5. 在 F407 主页点击 OTA 按钮 (SWD 触控模拟)
  6. 等待 OTA 备份完成 + Bootloader 重启
  7. 通过串口发送新固件 (CAN ISO-TP)
  8. 等待 OTA 完成

依赖:
  pip install pyserial
  OpenOCD (已配置)
  arm-none-eabi-gcc (已配置)

用法:
  python ota_flow.py                    # 全自动运行
  python ota_flow.py --skip-build       # 跳过编译 F103（使用已有 ELF）
"""

import sys, time, os, subprocess, socket, re, io
from pathlib import Path

# ─── Windows GBK 兼容 ────────────────────────────────────────
if hasattr(sys.stdout, 'buffer') and sys.stdout.encoding and 'utf' not in sys.stdout.encoding.lower():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

_HERE = Path(__file__).parent

# ─── 工程路径 ────────────────────────────────────────────────
F103_DIR    = Path("E:/ST/STM32/MY_workspace/Projects_F103/MY_OTA_GUI_F103")
F103_MAIN   = F103_DIR / "Core/Src/main.c"
F103_BUILD  = F103_DIR / "build/Debug"
F103_ELF    = F103_BUILD / "MY_OTA_GUI_F103.elf"
F103_CFG    = F103_DIR / "openocd_f103_fixed.cfg"  # ★ 使用修复版 cfg（克隆版无 NRST）

F407_DIR    = Path("E:/ST/STM32/MY_workspace/Projects_F407/MY_OTA_GUI")
F407_BIN    = F407_DIR / "build/Debug/MY_OTA_GUI.bin"
F407_CFG    = F407_DIR / "openocd.cfg"
F407_ELF    = F407_DIR / "build/Debug/MY_OTA_GUI.elf"
CAN_SCRIPT  = _HERE / "scripts/can_ota_send.py"

# ─── OpenOCD 配置 ─────────────────────────────────────────────
ST_SCRIPTS  = "E:/ST/STM32/STM32CubeIDE/plugins/com.st.stm32cube.ide.mcu.debug.openocd_2.3.300.202602021527/resources/openocd/st_scripts"
OPENOCD_EXE = "E:/ST/STM32/STM32CubeIDE/plugins/com.st.stm32cube.ide.mcu.externaltools.openocd.win32_2.4.400.202601091506/tools/bin/openocd.exe"

# ─── SWD 端 ───────────────────────────────────────────────────
OCD_HOST = "127.0.0.1"
OCD_PORT = 6666


# ═══════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════

def log(msg):
    print(f"  [OTA] {msg}")

def tcl_send(cmd, port=OCD_PORT):
    """向 OpenOCD TCL 端口发送命令"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect((OCD_HOST, port))
            s.sendall((cmd + "\x1a").encode("utf-8"))
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
    except Exception as e:
        return None

def ocd_running(port=OCD_PORT):
    """检查 OpenOCD 是否在运行"""
    r = tcl_send("mdw 0x20000000 1", port)
    return r is not None

def kill_openocd():
    """杀死所有 OpenOCD 进程"""
    subprocess.run(["taskkill", "/f", "/im", "openocd.exe"],
                   capture_output=True, text=True)
    time.sleep(2)

def start_openocd(cfg_path, port=6666):
    """启动 OpenOCD"""
    cmd = [OPENOCD_EXE, "-s", ST_SCRIPTS, "-f", str(cfg_path)]
    # 如果端口不是默认 6666，通过 -c 覆盖
    if port != 6666:
        cmd.extend(["-c", f"tcl_port {port}"])
    log(f"启动 OpenOCD: {' '.join(cmd[:3])}...")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # 等待就绪
    for _ in range(30):
        time.sleep(0.5)
        if ocd_running(port):
            log(f"OpenOCD 就绪 (端口 {port})")
            return proc
    log("OpenOCD 启动超时")
    return None

def halt_resume(func_addr, r0=None, r1=None, r2=None, r3=None,
                wait_ms=500, skip_halt=False, port=OCD_PORT):
    """SWD 函数调用: halt → [设参] → resume [addr] → [halt检查]"""
    tcl_send("halt", port)
    time.sleep(0.03)
    for i, val in enumerate([r0, r1, r2, r3]):
        if val is not None:
            tcl_send(f"reg r{i} 0x{val:X}", port)
    tcl_send(f"resume 0x{func_addr:X}", port)
    time.sleep(wait_ms / 1000)
    if not skip_halt:
        tcl_send("halt", port)
        pc = tcl_send("reg pc", port)
        failed = pc and "HardFault" in pc
        tcl_send("resume", port)
        if failed:
            log(f"HardFault at {func_addr:#x}")
            return False
    return True

def read_byte(addr, port=OCD_PORT):
    """读 1 字节内存"""
    r = tcl_send(f"mdb 0x{addr:08X} 1", port)
    try: return int(r.split(":")[1].strip().split()[0], 16)
    except: return -1

def write_byte(addr, val, port=OCD_PORT):
    """写 1 字节内存"""
    tcl_send(f"mwb 0x{addr:08X} 0x{val:X}", port)

def read_word(addr, port=OCD_PORT):
    """读 4 字节内存"""
    r = tcl_send(f"mdw 0x{addr:08X} 1", port)
    try: return int(r.split(":")[1].strip().split()[0], 16)
    except: return -1


# ═══════════════════════════════════════════════════════════════
# 步骤 1: 修改 F103 main.c → 启用 GATEWAY_MODE
# ═══════════════════════════════════════════════════════════════

def step1_enable_gateway() -> bool:
    """取消 //#define GATEWAY_MODE 的注释"""
    print("\n" + "=" * 60)
    print("步骤 1/7: 启用 F103 GATEWAY_MODE")
    print("=" * 60)

    if not F103_MAIN.exists():
        log(f"[FAIL] F103 main.c 不存在: {F103_MAIN}")
        return False

    content = F103_MAIN.read_text(encoding="utf-8")
    old_line = "//#define GATEWAY_MODE"
    new_line = "#define GATEWAY_MODE"

    if old_line in content:
        content = content.replace(old_line, new_line)
        F103_MAIN.write_text(content, encoding="utf-8")
        log("[OK] 已取消注释 #define GATEWAY_MODE")
        return True
    elif new_line in content:
        log("[OK] GATEWAY_MODE 已经启用")
        return True
    else:
        log(f"[FAIL] 未找到 '{old_line}'")
        log("请检查 F103 main.c 第 12 行的格式")
        return False


# ═══════════════════════════════════════════════════════════════
# 步骤 2: 编译 F103 工程
# ═══════════════════════════════════════════════════════════════

def step2_build_f103() -> bool:
    """编译 F103 工程（GATEWAY_MODE）"""
    print("\n" + "=" * 60)
    print("步骤 2/7: 编译 F103 工程")
    print("=" * 60)

    # 检查 ELF 是否存在且是最新的（可选跳过编译）
    if F103_ELF.exists():
        log(f"[OK] ELF 已存在: {F103_ELF.name}")
        # 检查 main.c 是否比 ELF 新（需要重新编译）
        main_mtime = F103_MAIN.stat().st_mtime
        elf_mtime = F103_ELF.stat().st_mtime
        if main_mtime <= elf_mtime:
            log(f"[OK] main.c 未更改，使用已有 ELF")
            return True
        log(f"[INFO] main.c 已更改，重新编译")

    log("编译 F103 GATEWAY_MODE...")
    try:
        result = subprocess.run(
            ["cmake", "--build", str(F103_BUILD), "--parallel"],
            capture_output=True, text=True, timeout=120,
            cwd=str(F103_DIR),
        )
        if result.returncode == 0:
            log("[OK] F103 编译通过")
            return True
        else:
            log(f"[FAIL] F103 编译失败")
            for line in (result.stdout + result.stderr).split("\n")[-20:]:
                if "error" in line.lower():
                    print(f"    {line}")
            return False
    except subprocess.TimeoutExpired:
        log("[FAIL] 编译超时")
        return False
    except FileNotFoundError as e:
        log(f"[FAIL] {e}")
        return False


# ═══════════════════════════════════════════════════════════════
# 步骤 3: 烧录 F103
# ═══════════════════════════════════════════════════════════════

def _count_stlink_probes() -> int:
    """检测当前连接的 ST-LINK 探针数量"""
    try:
        # PowerShell: $_.FriendlyName 是管道变量（不是 shell 变量）
        # subprocess.run 传参不经 shell，直接给 PowerShell
        cmd = [
            "powershell", "-NoProfile", "-Command",
            '@(Get-PnpDevice -PresentOnly | Where-Object { $_.FriendlyName -match "STM32 STLink" }).Count'
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        count = int(result.stdout.strip())
        return count
    except:
        return -1

def _wait_stlink_count(target: int, timeout: int = 120, label: str = "") -> bool:
    """轮询等待 ST-LINK 探针数量达到目标值"""
    log(f"等待 ST-LINK 数 = {target}{label}")
    for i in range(timeout):
        count = _count_stlink_probes()
        if count == target:
            log(f"检测到 {count} 个 ST-LINK")
            return True
        if i % 10 == 0:
            log(f"  当前 {count} 个... 等待 {target}")
        time.sleep(1)
    log(f"[FAIL] 超时: 等待 {target} 个 ST-LINK")
    return False


def step3_flash_f103() -> bool:
    """烧录 F103 GATEWAY_MODE 固件（人机协作）"""
    print("\n" + "=" * 60)
    print("步骤 3/7: 烧录 F103 GATEWAY_MODE (人机协作)")
    print("=" * 60)

    if not F103_ELF.exists():
        log(f"[FAIL] ELF 不存在: {F103_ELF}")
        return False

    # 先杀所有 OpenOCD
    kill_openocd()

    # 检测当前探针数
    probe_count = _count_stlink_probes()
    log(f"当前 ST-LINK 探针数: {probe_count}")

    # ── 协作 A: 拔 F407 ST-LINK ──
    if probe_count == 2:
        print()
        print("  " + "#" * 54)
        print("  #  需要你协助: 拔掉 F407 的 ST-LINK USB 线")
        print("  #  拔掉后脚本会自动检测并继续")
        print("  " + "#" * 54)
        print()
        if not _wait_stlink_count(1, timeout=120,
                                   label=" (请拔 F407 ST-LINK)"):
            log("[FAIL] 等待拔 USB 超时")
            return False
        time.sleep(2)
    elif probe_count == 1:
        log("[OK] 只有 1 个 ST-LINK (F103 的)，直接烧录")
    elif probe_count == 0:
        log("[FAIL] 未检测到任何 ST-LINK")
        return False
    else:
        log(f"[FAIL] 无法检测 ST-LINK 数量 ({probe_count})")
        return False

    # ── 烧录 F103 (克隆 ST-LINK 不能用 program 命令) ──
    # ★ program 内部做 reset+halt 会超时（克隆版无 NRST）
    #    改用: init → halt 5000 → flash write_image → verify_image → reset
    elf_posix = F103_ELF.as_posix()
    cmd = [OPENOCD_EXE, "-s", ST_SCRIPTS,
           "-f", str(F103_CFG),
           "-c", "init",
           "-c", "halt 5000",
           "-c", f"flash write_image erase {elf_posix}",
           "-c", f"verify_image {elf_posix}",
           "-c", "reset",
           "-c", "exit"]

    log(f"烧录: {cmd[0]} ... flash write_image erase ... verify_image")

    flash_ok = False
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            log("[OK] F103 烧录成功")
            flash_ok = True
        else:
            errors = [l for l in result.stderr.split("\n") if "error" in l.lower()]
            log(f"[FAIL] 烧录失败: {'; '.join(errors[-3:])}")
    except subprocess.TimeoutExpired:
        log("[FAIL] 烧录超时 (60s)")
    except FileNotFoundError as e:
        log(f"[FAIL] 找不到 OpenOCD: {e}")

    if not flash_ok:
        return False

    # ── 协作 B: 插回 F407 ST-LINK ──
    print()
    print("  " + "#" * 54)
    print("  #  F103 烧录完成! 请插回 F407 的 ST-LINK USB 线")
    print("  #  插回后脚本会自动检测并继续")
    print("  " + "#" * 54)
    print()

    _wait_stlink_count(2, timeout=60, label=" (请插回 F407 ST-LINK)")
    time.sleep(2)
    log("F103 GATEWAY_MODE 已启动 (等待 CAN OTA 请求)")
    return True


# ═══════════════════════════════════════════════════════════════
# 步骤 4: 启动 F407 OpenOCD
# ═══════════════════════════════════════════════════════════════

def step4_start_f407_monitor() -> bool:
    """启动 F407 的 OpenOCD 监控"""
    print("\n" + "=" * 60)
    print("步骤 4/7: 启动 F407 监控")
    print("=" * 60)

    # 确保无残余 OpenOCD
    kill_openocd()
    time.sleep(1)

    # 如果 F407 刚被 OTA 复位，bootloader 可能正在运行
    # 尝试连接
    log("启动 F407 OpenOCD...")
    f407_proc = start_openocd(F407_CFG, port=6666)
    if not f407_proc:
        return False

    # ★ OpenOCD 连接后目标可能处于 halt 状态，必须 resume 让系统运行
    log("恢复 F407 运行...")
    tcl_send("reset run")
    time.sleep(2)

    # 检查 g_ota_pending（如果 bootloader 正在跑，这个地址可能不可用）
    for i in range(5):
        val = read_byte(0x20017C17)
        if val >= 0:
            log(f"g_ota_pending = {val}")
            # 确认 uwTick 在增长（系统确实在运行）
            t0 = read_word(0x200002AC)
            time.sleep(0.5)
            t1 = read_word(0x200002AC)
            if t1 > t0:
                log(f"uwTick: {t0} -> {t1} (系统运行中)")
                return True
            else:
                log(f"uwTick 未增长 ({t0} -> {t1})，尝试再次 resume")
                tcl_send("resume")
                time.sleep(1)
                return True
        time.sleep(1)
        log("等待芯片就绪...")

    log("[WARN] g_ota_pending 不可读，可能系统未启动")
    # 尝试 reset run
    tcl_send("reset run")
    time.sleep(1)
    return True


# ═══════════════════════════════════════════════════════════════
# 步骤 5: 点击 OTA 按钮
# ═══════════════════════════════════════════════════════════════

def step5_click_ota_button() -> bool:
    """在 F407 主页点击 OTA 升级按钮"""
    print("\n" + "=" * 60)
    print("步骤 5/7: 点击 OTA 升级按钮")
    print("=" * 60)

    # OTA 按钮位置 (app_ui.c: LV_ALIGN_LEFT_MID, 偏移 20,0)
    # 顶栏 (0,0,800,32) 内，按钮 160x28
    BTN_X = 100
    BTN_Y = 16

    # 先确认系统在主页
    log("确认系统运行中...")
    tick = read_word(0x200002AC)
    log(f"uwTick = {tick}")
    if tick < 0:
        log("[WARN] uwTick 不可读，尝试 reset run")
        tcl_send("reset run")
        time.sleep(2)
    elif tick == 0:
        log("[WARN] uwTick = 0，系统 halt，reset run...")
        tcl_send("reset run")
        time.sleep(3)
        tick = read_word(0x200002AC)
        log(f"uwTick = {tick}")

    # 模拟触摸点击 OTA 按钮
    log(f"触摸点击 OTA 按钮 ({BTN_X}, {BTN_Y})...")
    write_byte(0x20017B7E, BTN_X & 0xFF)   # g_last_x low
    write_byte(0x20017B80, 1)               # g_pressed = 1 (按下)
    time.sleep(0.05)
    write_byte(0x20017B80, 0)               # g_pressed = 0 (释放)
    time.sleep(0.3)

    # 检查 g_ota_pending 是否已置 1
    for i in range(5):
        val = read_byte(0x20017C17)
        log(f"g_ota_pending = {val}")
        if val == 1:
            log("[OK] OTA 已触发! g_ota_pending = 1")
            return True
        time.sleep(1)

    log("[FAIL] OTA 按钮未响应（g_ota_pending 未置 1）")
    log("可能原因: 按钮坐标不准确，或 LVGL 未正确初始化")
    return False


# ═══════════════════════════════════════════════════════════════
# 步骤 6: 等待 OTA 备份完成 + Bootloader 重启
# ═══════════════════════════════════════════════════════════════

def step6_wait_backup() -> bool:
    """等待 OTA 备份完成（芯片会复位进入 bootloader）"""
    print("\n" + "=" * 60)
    print("步骤 6/7: 等待 OTA 备份 + Bootloader 启动")
    print("=" * 60)

    log("等待 g_ota_pending 变化 (备份中)...")

    # OTA 备份通常需要 10-30 秒
    for i in range(60):
        val = read_byte(0x20017C17)
        log(f"  g_ota_pending = {val} (第 {i+1}s)")
        if val == 0 or val == -1:
            # 复位后 g_ota_pending 归零或不可读
            log("[OK] 芯片已复位，Bootloader 启动中")
            time.sleep(2)
            return True
        time.sleep(1)

    log("[FAIL] OTA 备份超时 (60s)")
    return False


# ═══════════════════════════════════════════════════════════════
# 步骤 7: 发送新固件
# ═══════════════════════════════════════════════════════════════

def step7_send_firmware(com_port: str = "COM3") -> bool:
    """通过串口发送新固件到 F103 网关"""
    print("\n" + "=" * 60)
    print("步骤 7/7: 通过 CAN ISO-TP 发送新固件")
    print("=" * 60)

    if not CAN_SCRIPT.exists():
        log(f"[FAIL] can_ota_send.py 不存在: {CAN_SCRIPT}")
        CAN_ALT = F407_DIR / "scripts/can_ota_send.py"
        if CAN_ALT.exists():
            log(f"使用替代路径: {CAN_ALT}")
            CAN_SCRIPT_PATH = CAN_ALT
        else:
            log("请确认 can_ota_send.py 已安装")
            return False
    else:
        CAN_SCRIPT_PATH = CAN_SCRIPT

    if not F407_BIN.exists():
        log(f"[FAIL] 固件 .bin 不存在: {F407_BIN}")
        log("请先编译 F407 工程生成 .bin 文件")
        return False

    cmd = [sys.executable, str(CAN_SCRIPT_PATH), com_port, str(F407_BIN)]
    log(f"运行: {' '.join(cmd[-2:])}")

    try:
        result = subprocess.run(cmd, capture_output=False, text=True, timeout=300)
        # capture_output=False 让输出实时显示
        # 但我们用 timeout 捕获超时
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        log("[FAIL] 固件发送超时 (300s)")
        return False


# ═══════════════════════════════════════════════════════════════
# 全部执行
# ═══════════════════════════════════════════════════════════════

def run_all(com_port: str = "COM3", skip_build: bool = False):
    """执行完整 OTA 升级流程"""
    print("=" * 60)
    print("  OTA 升级全流程自动化")
    print(f"  时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  F103: {F103_DIR.name}")
    print(f"  F407: {F407_DIR.name}")
    print("=" * 60)

    # 步骤 1
    if not step1_enable_gateway():
        sys.exit(1)

    # 步骤 2
    if skip_build:
        log("跳过编译 (--skip-build)")
    else:
        if not step2_build_f103():
            sys.exit(1)

    # 步骤 3
    if not step3_flash_f103():
        sys.exit(1)

    # 步骤 4
    if not step4_start_f407_monitor():
        sys.exit(1)

    # 步骤 5
    if not step5_click_ota_button():
        sys.exit(1)

    # 步骤 6
    if not step6_wait_backup():
        sys.exit(1)

    # 步骤 7
    if not step7_send_firmware(com_port):
        sys.exit(1)

    print("\n" + "=" * 60)
    print("  OTA 升级全流程完成!")
    print("  F407 已通过 CAN ISO-TP 接收到新固件并烧录")
    print("  Bootloader 验证签名后将跳转到新 APP")
    print("=" * 60)
    return True


def main():
    import argparse
    parser = argparse.ArgumentParser(description="OTA 升级全流程自动化")
    parser.add_argument("--com", default="COM3", help="F103 网关串口号 (默认 COM3)")
    parser.add_argument("--skip-build", action="store_true", help="跳过 F103 编译")
    args = parser.parse_args()

    run_all(com_port=args.com, skip_build=args.skip_build)


if __name__ == "__main__":
    main()
