@echo off
set OPENOCD="E:/ST/STM32/STM32CubeIDE/plugins/com.st.stm32cube.ide.mcu.externaltools.openocd.win32_2.4.400.202601091506/tools/bin/openocd.exe"
set SCRIPTS="E:/ST/STM32/STM32CubeIDE/plugins/com.st.stm32cube.ide.mcu.debug.openocd_2.3.300.202602021527/resources/openocd/st_scripts"
set WORKSPACE="E:/ST/STM32/MY_workspace/Projects_F407/MY_OTA_GUI"
set BUILD=%WORKSPACE%/build/Debug

%OPENOCD% -s %SCRIPTS% -f %WORKSPACE%/openocd.cfg ^
  -c "program %BUILD%/bootloader.elf verify" ^
  -c "program %BUILD%/MY_OTA_GUI.elf verify" ^
  -c "program %BUILD%/signature.bin 0x080DFF80 verify" ^
  -c "program %WORKSPACE%/param_init.bin 0x080E0000 verify" ^
  -c "reset; exit"
if %ERRORLEVEL% equ 0 (echo SUCCESS) else (echo FAILED)
pause
