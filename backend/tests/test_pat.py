"""个人访问令牌（PAT）的端到端测试：签发 / 状态 / 吊销 / 改口令联动 / 跨用户隔离。

PAT 是给 MCP 服务器等程序化调用用的长效凭据（见 ``app/auth.py`` 的 PAT 段与
``api/auth.py`` 的 ``/me/pat`` 三个接口）。这里在「鉴权开启」形态下跑（同 ``test_auth.py``）。
"""
from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from sqlmodel import select

from app.auth import PAT_PREFIX, pat_hash
from app.db import SessionLocal, engine, init_db
from app.main import app
from app.models import SQLModel, User
from app.settings import get_settings


@pytest_asyncio.fixture
async def auth_client(monkeypatch):
    """鉴权开启的客户端：每个测试自带干净库。"""
    monkeypatch.setattr(get_settings(), "auth_enabled", True)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _register(client: httpx.AsyncClient, username: str, password: str = "password123") -> dict:
    resp = await client.post("/api/auth/register", json={"username": username, "password": password})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_pat_roundtrip(auth_client):
    """生成 → 状态可见 → PAT 能调 API → 吊销 → PAT 失效。"""
    session = await _register(auth_client, "alice")
    token = session["access_token"]

    # 还没生成过：状态是「没有」
    status = await auth_client.get("/api/auth/me/pat", headers=_auth(token))
    assert status.status_code == 200
    assert status.json()["has_pat"] is False

    # 生成：明文只出现这一次
    issued = await auth_client.post("/api/auth/me/pat", headers=_auth(token))
    assert issued.status_code == 200
    data = issued.json()
    pat = data["pat"]
    assert pat.startswith("pat_") and len(pat) > 60
    assert data["issued_at"]

    # 状态变为「有」，但永不回令牌本体
    status = await auth_client.get("/api/auth/me/pat", headers=_auth(token))
    assert status.json()["has_pat"] is True
    assert "pat" not in status.json()

    # PAT 能像 JWT 一样调受保护接口
    resp = await auth_client.get("/api/agents", headers=_auth(pat))
    assert resp.status_code == 200

    # 吊销后再用 → 401
    revoked = await auth_client.delete("/api/auth/me/pat", headers=_auth(token))
    assert revoked.status_code == 200
    resp = await auth_client.get("/api/agents", headers=_auth(pat))
    assert resp.status_code == 401


async def test_pat_regeneration_revokes_old(auth_client):
    """重新生成会立即吊销旧令牌（旧哈希被覆盖）。"""
    session = await _register(auth_client, "alice")
    token = session["access_token"]
    first = (await auth_client.post("/api/auth/me/pat", headers=_auth(token))).json()["pat"]
    second = (await auth_client.post("/api/auth/me/pat", headers=_auth(token))).json()["pat"]
    assert first != second
    # 旧的失效，新的可用
    assert (await auth_client.get("/api/agents", headers=_auth(first))).status_code == 401
    assert (await auth_client.get("/api/agents", headers=_auth(second))).status_code == 200


async def test_pat_revoked_on_password_change(auth_client):
    """改口令会一并吊销 PAT（凭据可能已泄露的保守处理）。"""
    session = await _register(auth_client, "alice")
    token = session["access_token"]
    pat = (await auth_client.post("/api/auth/me/pat", headers=_auth(token))).json()["pat"]

    resp = await auth_client.put(
        "/api/auth/me/password",
        json={"old_password": "password123", "new_password": "newpassword456"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    # 旧 PAT 失效
    assert (await auth_client.get("/api/agents", headers=_auth(pat))).status_code == 401
    # JWT 状态也清了（换新令牌登录）
    assert (await auth_client.get("/api/agents", headers=_auth(token))).status_code == 401


async def test_pat_isolated_per_user(auth_client):
    """A 的 PAT 拿不到 B 的资产（PAT 身份 = 属主身份，权限体系原样生效）。"""
    alice = await _register(auth_client, "alice")  # 首任管理员，资产属主
    bob = await _register(auth_client, "bob")
    bob_pat = (await auth_client.post("/api/auth/me/pat", headers=_auth(bob["access_token"]))).json()[
        "pat"
    ]
    # alice 建一个自定义网关；bob 的 PAT 列表里看不到它（非管理员列表只回自己的行）
    resp = await auth_client.post(
        "/api/gateways",
        json={
            "name": "alice 的网关",
            "protocol": "openai",
            "auth_style": "bearer",
            "base_url": "https://api.example.com",
            "api_key": "sk-test-not-a-real-key",
            "selected_models": ["gpt-4o"],
        },
        headers=_auth(alice["access_token"]),
    )
    assert resp.status_code == 201, resp.text
    listed = await auth_client.get("/api/gateways", headers=_auth(bob_pat))
    assert listed.status_code == 200
    items = [item for item in listed.json()["items"] if item["name"] == "alice 的网关"]
    assert items == []
    # 按按 id 读 alice 的网关也是 404（不说 403，避免泄露「存在但无权」）
    gateway_id = resp.json()["id"]
    resp = await auth_client.get(f"/api/gateways/{gateway_id}", headers=_auth(bob_pat))
    assert resp.status_code == 404


async def test_pat_requires_login(auth_client):
    """未登录不能生成 / 查看 / 吊销 PAT。"""
    assert (await auth_client.post("/api/auth/me/pat")).status_code == 401
    assert (await auth_client.get("/api/auth/me/pat")).status_code == 401
    assert (await auth_client.delete("/api/auth/me/pat")).status_code == 401


async def test_pat_hash_only_in_db(auth_client):
    """库里只存哈希：令牌本体绝不出现在任何响应里。"""
    session = await _register(auth_client, "alice")
    token = session["access_token"]
    pat = (await auth_client.post("/api/auth/me/pat", headers=_auth(token))).json()["pat"]

    async with SessionLocal() as db:
        row = await db.execute(select(User).where(User.username == "alice"))
        user = row.scalars().first()
        assert user.pat_hash == pat_hash(pat)
        assert user.pat_hash != pat
        assert pat.startswith(PAT_PREFIX)


async def test_pat_revoked_on_admin_password_reset(auth_client):
    """管理员重置口令同样吊销 PAT：用户 PAT 与内置 MCP 专用令牌都清空。

    PAT 不随 ``token_version`` 校验，重置口令只 +1 不会让它失效；这里确保应急入口
    也把长效凭据一并吊销（内置令牌在下次启动播种时会自动补发，不会弄挂系统管理 MCP）。
    """
    admin = await _register(auth_client, "alice")
    bob = await _register(auth_client, "bob")
    pat = (await auth_client.post("/api/auth/me/pat", headers=_auth(bob["access_token"]))).json()[
        "pat"
    ]

    async with SessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == "bob"))).scalars().first()
        assert user is not None and user.builtin_mcp_pat_hash
        builtin_digest = user.builtin_mcp_pat_hash

    resp = await auth_client.post(
        f"/api/auth/users/{bob['user']['id']}/reset-password",
        json={"old_password": "-", "new_password": "newpassword456"},
        headers=_auth(admin["access_token"]),
    )
    assert resp.status_code == 200, resp.text

    # 旧 PAT 立即失效。
    assert (await auth_client.get("/api/agents", headers=_auth(pat))).status_code == 401
    async with SessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == "bob"))).scalars().first()
        assert user is not None
        assert user.pat_hash is None
        assert user.builtin_mcp_pat_hash is None
        assert user.builtin_mcp_pat_hash != builtin_digest
