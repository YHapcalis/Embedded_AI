import clr
import sys

# Add our plugin directory to the assembly search path
sys.path.append(r'E:\ST\STM32\MY_workspace\harness_ai\renode\plugins')

# Load the NT35510 assembly
clr.AddReference('NT35510')

# Now the type Video.NT35510 should be available
from Antmicro.Renode.Peripherals.Video import NT35510
print('NT35510 type loaded:', NT35510)
