"""应用配置的解析约定：超时相关的环境变量怎么写都不会把服务弄挂。

普通对话默认不限时之后，运维最自然的写法就是「留空」或干脆不配；空字符串对 ``int``
是解析错误，若照旧抛出去，服务会直接起不来。这里把「空 = 不配置 = 不限时」固定住。
"""
from __future__ import annotations

import pytest

from app.settings import Settings

ENV = "ARKSWARM_RUN_TIMEOUT_SECONDS"


def test_unset_run_timeout_means_unlimited(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(ENV, raising=False)
    assert Settings().run_timeout_seconds is None


def test_blank_run_timeout_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch):
    """``ARKSWARM_RUN_TIMEOUT_SECONDS=`` 是「不限时」，不是启动故障。"""
    monkeypatch.setenv(ENV, "")
    assert Settings().run_timeout_seconds is None


def test_blank_run_timeout_with_spaces_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENV, "   ")
    assert Settings().run_timeout_seconds is None


def test_explicit_run_timeout_is_kept(monkeypatch: pytest.MonkeyPatch):
    """显式配置的护栏照常生效，别被空值兜底顺手抹掉。"""
    monkeypatch.setenv(ENV, "7200")
    assert Settings().run_timeout_seconds == 7200
