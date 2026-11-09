"""pytest 引导：在导入应用之前，先把数据目录指向一个临时目录。"""
import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND))

_DATA_DIR = Path(tempfile.mkdtemp(prefix="magenta-tests-"))
os.environ.setdefault("MAGENTA_DATA_DIR", str(_DATA_DIR))
os.environ.setdefault("MAGENTA_SECRET_KEY", "test-secret-key")
os.environ.setdefault("MAGENTA_WORKSPACE_DIR", str(_DATA_DIR / "workspaces"))
