"""pytest 引导：在导入应用之前，强制把数据与数据库指向临时目录。

容器里会继承 ``ARKSWARM_DATA_DIR=/app/data``（见 ``docker/Dockerfile``）。若这里只
用 ``setdefault``，测试就会把 Docker 的正式库当成测试库；测试夹具的 ``drop_all``
会直接清掉用户数据。因此这些路径变量必须**无条件覆盖**，并在导入应用前断言
数据库确实落在临时目录内。
"""
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlparse

BACKEND = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND))

#: 所有测试共用的隔离根目录；每次 pytest 会话一个，绝不沿用外部环境变量。
_DATA_DIR = Path(tempfile.mkdtemp(prefix="arkswarm-tests-")).resolve()
_DATABASE_PATH = _DATA_DIR / "arkswarm.db"


def _sqlite_path(url: str) -> Path | None:
    """从 SQLite URL 里取出文件路径；非文件型 SQLite 或其它数据库返回 None。"""
    parsed = urlparse(url)
    if not parsed.scheme.startswith("sqlite"):
        return None
    raw = parsed.path or parsed.netloc
    if not raw or raw == ":memory:" or raw.startswith("file::memory:"):
        return None
    return Path(unquote(raw)).resolve()


def _assert_isolated() -> None:
    """在应用导入前检查路径；不满足就立即失败，绝不冒险跑 ``drop_all``。"""
    data_dir = Path(os.environ["ARKSWARM_DATA_DIR"]).resolve()
    database_url = os.environ["ARKSWARM_DATABASE_URL"]
    db_path = _sqlite_path(database_url)
    if data_dir != _DATA_DIR or db_path != _DATABASE_PATH:
        raise RuntimeError(
            "测试数据库没有落在临时目录："
            f"ARKSWARM_DATA_DIR={data_dir}、ARKSWARM_DATABASE_URL={database_url}；"
            "拒绝启动 pytest，以免清空正式库"
        )
    if not db_path.is_relative_to(_DATA_DIR):
        raise RuntimeError(f"测试数据库不在隔离目录内：{db_path}")


#: 路径类变量必须覆盖：外部（尤其 Docker）的值会指向正式库。
os.environ["ARKSWARM_DATA_DIR"] = str(_DATA_DIR)
os.environ["ARKSWARM_DATABASE_URL"] = f"sqlite+aiosqlite:///{_DATABASE_PATH}"
os.environ["ARKSWARM_WORKSPACE_DIR"] = str(_DATA_DIR / "workspaces")
os.environ["ARKSWARM_SECRET_KEY"] = "test-secret-key"
_assert_isolated()

#: 既有业务测试在「鉴权关闭」形态下跑：它们验的是归属无关的 CRUD / 流式 / 协作逻辑，
#: 鉴权与归属过滤由专门的 ``test_auth.py`` / ``test_ownership.py`` 在开启鉴权时验证。
os.environ["ARKSWARM_AUTH_ENABLED"] = "false"
