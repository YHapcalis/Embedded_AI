"""foundation 包入口：python -m foundation 即调用 CLI"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from cli.main import main

if __name__ == "__main__":
    sys.exit(main())
