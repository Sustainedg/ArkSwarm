"""桌面端设置页的信息架构守卫。

设置按**类型**分标签页：一页只放一类设置，只读信息（版本记录 / 系统信息 / 正在运行的智能体）
收进「关于」。分页只是分组 —— 各项设置的组件与文档语义都没变，这条守卫防的是后续改动
把分页拆掉、或又把只读信息摊回每个标签页里。

**移动端设置页不跟这套**（它刻意只留「现在要动的开关」，见 AGENTS.md），因此这里只读桌面端那一份。
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
FRONTEND = REPO / "frontend"

needs_frontend = pytest.mark.skipif(not FRONTEND.is_dir(), reason="未检出前端源码目录")

#: 桌面端设置页：标签分组的信息架构就在这一个文件里。
SETTINGS = FRONTEND / "src" / "pages" / "SettingsPage.tsx"


@needs_frontend
def test_desktop_settings_is_grouped_into_tabs_by_type():
    """设置页把每一类设置放进自己的标签页（标签栏复用全站的 `.tabs` / `.tab`）。"""
    page = SETTINGS.read_text(encoding="utf-8")
    assert "const TABS" in page, "设置页缺少标签页定义"
    assert "className={`tab" in page, "标签栏应当复用全站的 `.tab` 样式"

    for key, label, component in (
        ("appearance", "外观", "THEME_OPTIONS"),
        ("chat", "对话", "<TitleSettingsSection />"),
        ("gateways", "网关", "<GatewayManager />"),
        ("credentials", "凭据", "<CredentialVault />"),
        ("im", "IM", "<ImSettings />"),
        ("network", "网络", "<ProxySettings />"),
        ("kernels", "内核管理", "<KernelManager />"),
        ("about", "关于", "版本记录"),
    ):
        assert f"id: '{key}', label: '{label}'" in page, f"设置页少了「{label}」这个标签"
        assert f"tab === '{key}'" in page, f"少了「{label}」标签页的分支"
        assert component in page, f"「{label}」标签页里没有对应的内容（{component}）"


@needs_frontend
def test_kernel_management_is_not_nested_behind_another_entry():
    """内核管理只有一份，别再嵌套一个「运行时」入口 + 适配器清单。

    这里同时钉住两件事：设置页里不再有指向 `/runtimes` 的入口 / 已注册的适配器清单，
    而老地址 `/runtimes` 仍能落到「设置 → 内核管理」（书签不失效）。
    """
    page = SETTINGS.read_text(encoding="utf-8")
    assert "navigate('/runtimes')" not in page, "设置页不该再嵌一个「运行时」入口（会与内核管理页重复）"
    assert "settings-entries" not in page, "「配置入口」那一排按钮已不再需要"
    assert "已注册的适配器" not in page, "适配器清单已删（内核卡片就在内核管理页里）"
    assert "如何接入一个新的智能体" not in page, "接入说明随适配器清单一并移出设置页"

    app = (FRONTEND / "src" / "App.tsx").read_text(encoding="utf-8")
    assert 'path="/runtimes"' in app, "老地址 /runtimes 要保留重定向"
    assert "/settings?tab=kernels" in app, "/runtimes 应当重定向到「设置 → 内核管理」"
    assert "RuntimesPage" not in app, "桌面端不再有独立的运行时页面"


@needs_frontend
def test_version_history_and_system_info_live_in_the_about_tab():
    """版本记录与系统信息是只读信息，收在「关于」里，不再和设置项混在同一页。"""
    page = SETTINGS.read_text(encoding="utf-8")
    parts = page.split("tab === 'about'", 1)
    assert len(parts) == 2, "设置页里找不到「关于」标签页"
    about = parts[1]
    for marker in ("系统信息", "正在运行的智能体", "版本记录", "version-timeline"):
        assert marker in about, f"「关于」标签页里少了{marker}"
