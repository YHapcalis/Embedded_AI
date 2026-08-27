# flash_f407.ps1 — 以管理员身份烧录 F407 (4段)
# 双击运行或: powershell -ExecutionPolicy Bypass -File flash_f407.ps1

$OPENOCD = "E:/ST/STM32/STM32CubeIDE/plugins/com.st.stm32cube.ide.mcu.externaltools.openocd.win32_2.4.400.202601091506/tools/bin/openocd.exe"
$SCRIPTS = "E:/ST/STM32/STM32CubeIDE/plugins/com.st.stm32cube.ide.mcu.debug.openocd_2.3.300.202602021527/resources/openocd/st_scripts"
$WORKSPACE = "E:/ST/STM32/MY_workspace/Projects_F407/MY_OTA_GUI"
$BUILD = "$WORKSPACE/build/Debug"

# 检查是否管理员权限
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    # 提权重新运行
    Start-Process powershell.exe -ArgumentList "-ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs
    exit
}

Write-Host "=== F407 4段烧录 ==="
Write-Host ""

& $OPENOCD -s $SCRIPTS -f "$WORKSPACE/openocd.cfg" `
    -c "program $BUILD/bootloader.elf verify" `
    -c "program $BUILD/MY_OTA_GUI.elf verify" `
    -c "program $BUILD/signature.bin 0x080DFF80 verify" `
    -c "program $WORKSPACE/param_init.bin 0x080E0000 verify" `
    -c "reset; exit"

if ($LASTEXITCODE -eq 0) {
    Write-Host "`n[OK] F407 烧录成功!" -ForegroundColor Green
} else {
    Write-Host "`n[FAIL] 烧录失败" -ForegroundColor Red
}

Read-Host "按 Enter 退出"
