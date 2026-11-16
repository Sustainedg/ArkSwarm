"""pytest 引导：在导入应用之前，先把数据目录指向一个临时目录。"""
import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND))

_DATA_DIR = Path(tempfile.mkdtemp(prefix="arkswarm-tests-"))
os.environ.setdefault("ARKSWARM_DATA_DIR", str(_DATA_DIR))
os.environ.setdefault("ARKSWARM_SECRET_KEY", "test-secret-key")
os.environ.setdefault("ARKSWARM_WORKSPACE_DIR", str(_DATA_DIR / "workspaces"))
#: 既有业务测试在「鉴权关闭」形态下跑：它们验的是归属无关的 CRUD / 流式 / 协作逻辑，
#: 鉴权与归属过滤由专门的 ``test_auth.py`` / ``test_ownership.py`` 在开启鉴权时验证。
os.environ.setdefault("ARKSWARM_AUTH_ENABLED", "false")
