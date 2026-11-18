"""测试自身的数据隔离：继承正式库路径时也必须落到临时目录。

真实事故：容器里 ``ARKSWARM_DATA_DIR=/app/data`` 被 pytest 继承，旧引导用
``setdefault`` 没有覆盖它，测试夹具的 ``drop_all`` 直接清空了 Docker 正式库。
这里用子进程复现「从容器继承路径」的场景，确保引导层会强制改写且不碰哨兵文件。
"""
from __future__ import annotations

import json
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent


def test_conftest_overrides_inherited_production_paths(tmp_path: Path) -> None:
    sentinel = tmp_path / "arkswarm.db"
    sentinel.write_bytes(b"must-not-be-touched")
    env = os.environ.copy()
    env.update(
        {
            "ARKSWARM_DATA_DIR": str(tmp_path),
            "ARKSWARM_DATABASE_URL": f"sqlite+aiosqlite:///{sentinel}",
            "ARKSWARM_WORKSPACE_DIR": str(tmp_path / "workspaces"),
        }
    )
    code = """
import json
import conftest
from app.settings import get_settings

settings = get_settings()
print(json.dumps({
    "data_dir": str(settings.data_dir),
    "database_url": settings.sqlalchemy_url,
    "workspace_dir": str(settings.workspaces_root),
    "test_root": str(conftest._DATA_DIR),
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    resolved = json.loads(result.stdout)

    assert sentinel.read_bytes() == b"must-not-be-touched"
    assert Path(resolved["data_dir"]) == Path(resolved["test_root"])
    assert resolved["data_dir"] != str(tmp_path)
    assert sentinel.as_posix() not in resolved["database_url"]
    assert resolved["test_root"] in resolved["database_url"]
    assert resolved["test_root"] in resolved["workspace_dir"]


def test_assert_isolated_rejects_non_temporary_database(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = importlib.util.spec_from_file_location("arkswarm_root_conftest", BACKEND / "conftest.py")
    assert spec and spec.loader
    conftest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conftest)

    monkeypatch.setenv("ARKSWARM_DATA_DIR", "/app/data")
    monkeypatch.setenv("ARKSWARM_DATABASE_URL", "sqlite+aiosqlite:////app/data/arkswarm.db")
    with pytest.raises(RuntimeError, match="拒绝启动 pytest"):
        conftest._assert_isolated()


def test_setdefault_would_have_kept_production_path() -> None:
    """把事故机制钉住：``setdefault`` 在变量已存在时不会改写。"""
    inherited = {"ARKSWARM_DATA_DIR": "/app/data"}
    inherited.setdefault("ARKSWARM_DATA_DIR", "/tmp/isolated")
    assert inherited["ARKSWARM_DATA_DIR"] == "/app/data"
