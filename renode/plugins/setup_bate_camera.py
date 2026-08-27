# -*- coding: utf-8 -*-
# Step 1: Load NT35510 DLL into AppDomain
import clr
import sys
dll_dir = r'E:\ST\STM32\MY_workspace\harness_ai\renode\plugins'
sys.path.append(dll_dir)
ref = clr.AddReference('NT35510')
print('NT35510 loaded:', ref.FullName)

# Step 2: Create machine and load platform via Monitor commands
# The monitor commands should work because the DLL is now in the AppDomain
