"""pgwalnew GUI 启动入口。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pgwalnew.gui.app import main  # noqa: E402

if __name__ == "__main__":
    main()
