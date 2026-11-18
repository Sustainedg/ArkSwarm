"""生成图片取回对话工作目录：前缀匹配、引用改写、失败回退。"""
from __future__ import annotations

import hashlib

from app import image_ingest

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16
#: 与 PNG 内容对应的 sha256 前 12 位（内容寻址文件名的一部分）
PNG_DIGEST = hashlib.sha256(PNG).hexdigest()[:12]
HUB = "http://192.0.2.5:3050"


async def test_ingest_rewrites_url_to_relative(tmp_path, monkeypatch):
    async def fake_download(client, url):
        return PNG

    monkeypatch.setattr(image_ingest, "_download", fake_download)
    workspace = tmp_path / "ws"
    content = "画好了：![](http://192.0.2.5:3050/image-1.png)"

    new_content, parts, count = await image_ingest.ingest_images(content, [], workspace, [HUB])

    assert count == 1
    expected = f"generated-images/image-1-{PNG_DIGEST}.png"
    assert new_content == f"画好了：![]({expected})"
    assert (workspace / "generated-images" / f"image-1-{PNG_DIGEST}.png").read_bytes() == PNG
    assert parts == []


async def test_ingest_ignores_unconfigured_prefix(tmp_path, monkeypatch):
    async def fake_download(client, url):  # 不该被调用
        raise AssertionError("不该下载未配置前缀的图片")

    monkeypatch.setattr(image_ingest, "_download", fake_download)
    content = "![](http://elsewhere:3050/image-1.png)"

    new_content, _, count = await image_ingest.ingest_images(
        content, [], tmp_path / "ws", [HUB]
    )

    assert count == 0
    assert new_content == content


async def test_ingest_keeps_url_when_download_fails(tmp_path, monkeypatch):
    async def broken_download(client, url):
        raise RuntimeError("网关挂了")

    monkeypatch.setattr(image_ingest, "_download", broken_download)
    content = "![](http://192.0.2.5:3050/image-2.png)"

    new_content, _, count = await image_ingest.ingest_images(
        content, [], tmp_path / "ws", [HUB]
    )

    assert count == 0
    assert new_content == content


async def test_ingest_rewrites_tool_result_parts(tmp_path, monkeypatch):
    async def fake_download(client, url):
        return PNG

    monkeypatch.setattr(image_ingest, "_download", fake_download)
    parts = [
        {"kind": "tool_call", "name": "generate_image", "input": {"prompt": "猫"}},
        {
            "kind": "tool_result",
            "name": "generate_image",
            "output": "已生成：http://192.0.2.5:3050/image-3.png（PNG）",
        },
    ]

    content, new_parts, count = await image_ingest.ingest_images(
        "", parts, tmp_path / "ws", [HUB]
    )

    assert count == 1
    assert content == ""
    assert new_parts[0]["input"] == {"prompt": "猫"}
    expected = f"generated-images/image-3-{PNG_DIGEST}.png"
    assert new_parts[1]["output"] == f"已生成：{expected}（PNG）"


async def test_ingest_same_basename_different_content_no_clobber(tmp_path, monkeypatch):
    """两个 URL 撞基名但内容不同：各落各的文件、各指向各的副本，互不覆盖。"""

    async def fake_download(client, url):
        return PNG + url.encode()  # 内容随 URL 变

    monkeypatch.setattr(image_ingest, "_download", fake_download)
    content = (
        "![](http://192.0.2.5:3050/a/image-1.png)"
        "![](http://192.0.2.5:3050/b/image-1.png)"
    )

    new_content, _, count = await image_ingest.ingest_images(
        content, [], tmp_path / "ws", [HUB]
    )

    assert count == 2
    names = [
        p.name
        for p in sorted((tmp_path / "ws" / "generated-images").iterdir())
    ]
    assert len(names) == 2  # 没有互相覆盖
    # 两个引用分别指向两个不同的相对路径
    references = [f"generated-images/{n}" for n in names]
    for ref in references:
        assert ref in new_content
