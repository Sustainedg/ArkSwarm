"""前端静态资源的守卫测试：应用图标 / 内核 Logo / 头像包。

内核 Logo（`frontend/public/icons/kernels/*`）的约定：图形是手工/厂商产物（不生成），
但**必须由适配器声明**（`AgentDefinition.logo`，`logo_style` 决定遮罩还是原色），
且两张运行时页都只留「Logo · 内核名 · 公司 · 版本」。
"""
from __future__ import annotations

import importlib.util
import re
import struct
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
FRONTEND = REPO / "frontend"
MOBILE_SRC = FRONTEND / "src" / "mobile"

needs_frontend = pytest.mark.skipif(not FRONTEND.is_dir(), reason="未检出前端源码目录")


# --- 运行时卡片：内核 Logo 与精简后的文案 -------------------------------------

KERNEL_LOGO_DIR = REPO / "frontend" / "public" / "icons" / "kernels"


SVG_SUFFIX = ".svg"
RASTER_SUFFIXES = (".png", ".webp", ".jpg", ".jpeg")
MAX_RASTER_BYTES = 256 * 1024
MAX_RASTER_SIDE = 512


@needs_frontend
def test_every_kernel_declares_a_logo_that_is_committed():
    """每个内核都要有官方 Logo，且声明的图形文件真的在仓库里（界面按路径直接取）。

    两种形态：
    * ``logo_style = "mask"``（默认）—— 24×24 单色 SVG，界面当遮罩染色；
    * ``logo_style = "image"`` —— 真彩色位图（PNG/WebP），按原色显示。
    """
    from app.agents import all_agents

    for definition in all_agents():
        logo = definition.logo
        assert logo, f"{definition.id} 未声明 logo"
        assert logo.startswith("/icons/kernels/"), (
            f"{definition.id} 的 logo 应指向 /icons/kernels/*，实际是 {logo}"
        )
        path = KERNEL_LOGO_DIR / logo.rsplit("/", 1)[-1]
        assert path.is_file(), f"{definition.id} 声明的 Logo 文件不存在：{path}"
        assert definition.logo_style in ("mask", "image"), (
            f"{definition.id} 的 logo_style 只能是 mask 或 image"
        )
        if path.suffix == SVG_SUFFIX:
            text = path.read_text(encoding="utf-8")
            assert 'viewBox="0 0 24 24"' in text, f"{path.name} 缺少统一画布（24×24）"
            assert "<path" in text, f"{path.name} 里没有图形"
            continue

        assert path.suffix in RASTER_SUFFIXES, f"{path.name} 不是受支持的图片格式"
        assert definition.logo_style == "image", (
            f"{definition.id} 用的是位图 Logo，必须声明 logo_style = \"image\""
        )
        blob = path.read_bytes()
        assert blob[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} 不是 PNG（当前只校验 PNG 位图）"
        width, height = struct.unpack(">II", blob[16:24])
        assert width == height and width <= MAX_RASTER_SIDE, (
            f"{path.name} 应为正方形且不超过 {MAX_RASTER_SIDE}px，实际 {width}×{height}"
        )
        assert len(blob) <= MAX_RASTER_BYTES, (
            f"{path.name} 超过 {MAX_RASTER_BYTES // 1024} KB，请压缩后再提交"
        )


@needs_frontend
def test_runtime_cards_use_the_shared_logo_and_trimmed_copy():
    """内核卡片：名字前面挂官方 Logo，文案只留名称 / 公司 / 当前版本。

    桌面端这份 UI 已经收进「设置 → 内核管理」（`components/KernelManager.tsx`，
    列表卡片 + 详情弹层）；移动端仍是自己一页（卡片 + 底部弹层）。
    """
    pages = [
        FRONTEND / "src" / "components" / "KernelManager.tsx",
        MOBILE_SRC / "pages" / "MobileRuntimesPage.tsx",
    ]
    for path in pages:
        text = path.read_text(encoding="utf-8")
        assert "KernelLogo" in text, f"{path.name} 要在内核名字前显示官方 Logo"
        # 已经是最新版就不给更新入口；有更新时按钮写明目标版本。
        assert "showUpdate" in text, f"{path.name} 缺「按需显示更新按钮」的判断"
        assert "更新到 ${updateTarget}" in text, f"{path.name} 的更新按钮要写明「更新到 X」"
        for dropped in ("runtime.description", "runtime.instances", "provisioning.path"):
            assert dropped not in text, f"{path.name} 的卡片文案已精简，不该再渲染 {dropped}"

    logo = (FRONTEND / "src" / "components" / "KernelLogo.tsx").read_text(encoding="utf-8")
    assert "maskImage" in logo, "单色 Logo 用遮罩染色，深浅两套外观才能共用一份图形"
    assert "<img" in logo, "真彩色位图 Logo 要用 <img> 原色显示"
    for css in (FRONTEND / "src" / "index.css", MOBILE_SRC / "mobile.css"):
        text = css.read_text(encoding="utf-8")
        assert ".kernel-logo" in text, f"{css.name} 缺少 .kernel-logo 规则"
        assert ".kernel-logo-image" in text, f"{css.name} 缺少位图 Logo 的样式"


# --- 应用图标与品牌标 ---------------------------------------------------------

ICON_DIR = FRONTEND / "public" / "icons"
ICON_GENERATOR = REPO / "scripts" / "gen_icons.py"


def _load_icon_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_icons", ICON_GENERATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@needs_frontend
def test_committed_app_icons_are_up_to_date():
    """应用图标是生成产物：改了 `scripts/gen_icons.py` 就要重新生成，否则线上还是旧图。"""
    module = _load_icon_generator()
    svg_file = ICON_DIR / "icon.svg"
    assert svg_file.is_file(), "缺少 frontend/public/icons/icon.svg"
    assert svg_file.read_text(encoding="utf-8") == module.build_svg(), (
        "icon.svg 与 scripts/gen_icons.py 的产物不一致，请重新执行 python3 scripts/gen_icons.py"
    )
    # PNG 只比 192 这一张（生成最快），其余几张同一趟脚本一起产出。
    png_file = ICON_DIR / "icon-192.png"
    assert png_file.is_file(), "缺少 frontend/public/icons/icon-192.png"
    assert png_file.read_bytes() == _png_bytes(module, 192), (
        "icon-192.png 与生成脚本的产物不一致，请重新执行 python3 scripts/gen_icons.py"
    )
    for name, size in (
        ("icon-512.png", 512),
        ("icon-maskable-512.png", 512),
        ("apple-touch-icon.png", 180),
    ):
        blob = (ICON_DIR / name).read_bytes()
        assert blob[:8] == b"\x89PNG\r\n\x1a\n", f"{name} 不是 PNG"
        assert struct.unpack(">II", blob[16:24]) == (size, size), f"{name} 尺寸不对"


def _png_bytes(module: ModuleType, size: int) -> bytes:
    """把生成器的渲染结果写成 PNG 字节（走临时目录，避免覆盖仓库里的文件）。"""
    import tempfile
    from pathlib import Path as _Path

    with tempfile.TemporaryDirectory() as tmp:
        target = _Path(tmp) / "x.png"
        module._write_png(target, size, module.render_icon(size))
        return target.read_bytes()


@needs_frontend
def test_brand_mark_is_one_shared_logo_on_both_ends():
    """界面里的品牌标只有一份实现，且直接用用户提供的品牌图（与应用图标同一张）。"""
    mark = (FRONTEND / "src" / "components" / "BrandMark.tsx").read_text(encoding="utf-8")
    assert "<img" in mark, "品牌标用 <img> 显示用户提供的品牌位图"
    assert "icon-512.png" in mark, "品牌标要引用与应用图标同源的品牌图"

    users = [
        FRONTEND / "src" / "App.tsx",
        FRONTEND / "src" / "components" / "LoginScreen.tsx",
        MOBILE_SRC / "components" / "MobileDrawer.tsx",
    ]
    for path in users:
        text = path.read_text(encoding="utf-8")
        assert "BrandMark" in text, f"{path.name} 应当用共享的品牌标组件"
        assert "✦" not in text, f"{path.name} 里还留着旧的 ✦ 字符"

    # 顶栏左上角不再是品牌 logo 而是汉堡折叠按钮（用户指定的导航入口形态），
    # 品牌标留在侧栏 / 抽屉 / 登录页；这里锁住「汉堡用共享的线条图标」。
    menu = (MOBILE_SRC / "components" / "MobileMenuButton.tsx").read_text(encoding="utf-8")
    assert "MenuIcon" in menu, "顶栏菜单按钮应当用 Icons 里的汉堡线条图标"


@needs_frontend
def test_avatar_styles_exist_on_both_ends():
    """头像组件两端共用（移动端不加载 index.css），样式两边都要有。"""
    for css in (FRONTEND / "src" / "index.css", MOBILE_SRC / "mobile.css"):
        text = css.read_text(encoding="utf-8")
        for selector in (".user-avatar", ".avatar-editor", ".avatar-editor-pic"):
            assert selector in text, f"{css.name} 缺少 {selector}"
        assert "--avatar-size" in text, f"{css.name} 的头像尺寸要走 --avatar-size 变量"


AVATAR_DIR = FRONTEND / "public" / "avatars"
AVATAR_MANIFEST = FRONTEND / "src" / "lib" / "avatarPack.ts"


@needs_frontend
def test_avatar_pack_manifest_matches_files():
    """内置头像包：清单与图片必须一一对应。

    「随机生成」直接按清单抽编号拼 `/avatars/<编号>.webp`，清单多一条少一条都会让抽到
    的头像 404。清单由 `scripts/gen_avatar_pack.py` 生成，两边只允许来自同一次运行。
    """
    assert AVATAR_MANIFEST.is_file(), "缺少 frontend/src/lib/avatarPack.ts（跑 scripts/gen_avatar_pack.py）"
    manifest_ids = re.findall(r"'(pack-\d{3})'", AVATAR_MANIFEST.read_text(encoding="utf-8"))
    file_ids = sorted(path.stem for path in AVATAR_DIR.glob("pack-*.webp"))
    assert manifest_ids == file_ids, "头像包清单与 public/avatars/ 下的图片不一致，请重跑生成脚本"
    # 太少就失去「随机」的意义：至少两排才有抽卡感。
    assert len(manifest_ids) >= 20
