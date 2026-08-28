"""harness 包入口：python -m harness 即调用 CLI"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from harness_cli import main

if __name__ == "__main__":
    sys.exit(main())
