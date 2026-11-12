from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import update

from app import __version__
from app.db import SessionLocal, engine, init_db
from app.main import app
from app.models import AgentProfile, Conversation, Message, SQLModel
from app.runtime.manager import registry
from helpers import cli_options, make_gateway, make_profile



@pytest_asyncio.fixture
async def client():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def make_agent(client: httpx.AsyncClient, **overrides) -> dict:
    """建一个「网关 + 智能体」，返回智能体（网关 id 在 gateway_id 上）。"""
    gateway = await make_gateway(client)
    return await make_profile(client, gateway_id=gateway["id"], **overrides)


async def read_sse(response: httpx.Response) -> list[dict]:
    events: list[dict] = []
    async for line in response.aiter_lines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


async def test_gateway_crud_masks_secrets(client: httpx.AsyncClient):
    gateway = await make_gateway(client)
    assert gateway["api_key_set"] is True
    assert gateway["api_key_preview"] == "sk-s…alue"
    assert "sk-super-secret-value" not in json.dumps(gateway)

    listed = (await client.get("/api/gateways")).json()["items"]
    assert len(listed) == 1
    assert listed[0]["selected_models"] == ["fake-model", "other-model"]

    patched = await client.patch(f"/api/gateways/{gateway['id']}", json={"name": "Renamed"})
    assert patched.json()["name"] == "Renamed"
    # 省略 api_key 表示保留已存密钥
    assert patched.json()["api_key_set"] is True

    cleared = await client.patch(f"/api/gateways/{gateway['id']}", json={"api_key": ""})
    assert cleared.json()["api_key_set"] is False

    assert (await client.delete(f"/api/gateways/{gateway['id']}")).status_code == 204
    assert (await client.get(f"/api/gateways/{gateway['id']}")).status_code == 404


async def test_agent_crud_keeps_gateway_and_model(client: httpx.AsyncClient):
    agent = await make_agent(client)
    assert agent["gateway_kind"] == "custom"
    assert agent["gateway_api_key_set"] is True
    assert agent["model"] == "fake-model"
    assert agent["type_name"] == "自定义 CLI"

    listed = (await client.get("/api/agents")).json()["items"]
    assert listed[0]["id"] == agent["id"]

    patched = await client.patch(f"/api/agents/{agent['id']}", json={"model": "other-model"})
    assert patched.json()["model"] == "other-model"

    assert (await client.delete(f"/api/agents/{agent['id']}")).status_code == 204
    assert (await client.get(f"/api/agents/{agent['id']}")).status_code == 404


async def test_gateway_delete_blocked_while_an_agent_uses_it(client: httpx.AsyncClient):
    agent = await make_agent(client)
    response = await client.delete(f"/api/gateways/{agent['gateway_id']}")
    assert response.status_code == 409
    assert "正被以下智能体使用" in response.json()["detail"]


async def test_agent_types_expose_config_schema_and_availability(client: httpx.AsyncClient):
    types = (await client.get("/api/agent-types")).json()["items"]
    by_id = {t["id"]: t for t in types}
    assert {"claude-code", "codex", "gemini-cli", "openai-harness", "custom-cli"} <= set(by_id)
    claude = by_id["claude-code"]
    assert claude["availability"]["installed"] is True
    assert claude["runtime"] == "cli"
    assert any(f["key"] == "permission_mode" for f in claude["config_fields"])
    assert claude["capabilities"]["sessions"] is True
    assert "system_defaults" in claude
    assert by_id["openai-harness"]["runtime"] == "api"


async def test_agent_types_never_leak_plaintext_secret(client: httpx.AsyncClient, monkeypatch, tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "sk-very-secret-token"}}), encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    types = (await client.get("/api/agent-types")).json()["items"]
    claude = next(t for t in types if t["id"] == "claude-code")
    assert claude["system_defaults"]["api_key_set"] is True
    # 凭据只能以掩码形式出现在接口响应里（AGENTS.md 铁律 6）。
    assert "sk-very-secret-token" not in json.dumps(types)
    assert claude["system_defaults"]["api_key_preview"].endswith("oken")


async def test_system_architecture_endpoint_returns_rendered_docs(client: httpx.AsyncClient):
    response = await client.get("/api/system/architecture")
    assert response.status_code == 200
    data = response.json()
    assert data["source"] == "docs/architecture.md"
    assert data["updated_at"]
    # 文档被实时渲染成 HTML（标题 / 表格 / 代码块都在），且不返回原始 Markdown。
    html = data["html"]
    assert "<h2>1. 分层的目标</h2>" in html
    assert "<table>" in html and "<pre><code" in html
    assert "# 架构说明" not in html


async def test_streaming_conversation_persists_a_full_turn(client: httpx.AsyncClient):
    agent = await make_agent(client)
    role = await make_profile(
        client,
        gateway_id=agent["gateway_id"],
        name="Tester",
        avatar="🧪",
        system_prompt="You are a tester.",
    )
    conversation = (await client.post("/api/conversations", json={"agent_profile_id": role["id"]})).json()

    async with client.stream(
        "POST", f"/api/conversations/{conversation['id']}/messages", json={"content": "hello world"}
    ) as response:
        assert response.status_code == 200
        events = await read_sse(response)

    kinds = [e["type"] for e in events]
    assert kinds[0] == "turn.started"
    assert kinds[-1] == "stream.closed"
    assert "session" in kinds and "tool.call" in kinds and "usage" in kinds
    deltas = "".join(e.get("text") or "" for e in events if e["type"] == "message.delta")
    assert deltas == "Echo: hello world... done"

    detail = (await client.get(f"/api/conversations/{conversation['id']}")).json()
    messages = detail["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["content"] == "Echo: hello world... done"
    assert messages[1]["status"] == "complete"
    assert messages[1]["usage"]["output_tokens"] == 22
    # parts 保留真实时间线：正文 → 工具调用 → 工具结果 → 正文（AGENTS.md 前端渲染契约）。
    timeline = messages[1]["parts"]
    assert [p["kind"] for p in timeline] == ["text", "tool_call", "tool_result", "text"]
    assert timeline[0]["text"] == "Echo: hello world..."
    assert timeline[-1]["text"] == " done"
    assert timeline[1]["name"] == "run_shell" and timeline[2]["output"] == "a.txt"

    # 标题自动取自首条用户消息（智能体记录不受影响）
    assert detail["conversation"]["title"] == "hello world"
    assert detail["conversation"]["session_id"] == "sess-42"


async def test_multi_turn_uses_the_remembered_session(client: httpx.AsyncClient):
    agent = await make_agent(client)
    role = await make_profile(client, gateway_id=agent["gateway_id"], name="T")
    conversation = (await client.post("/api/conversations", json={"agent_profile_id": role["id"]})).json()

    for text in ("first", "second"):
        async with client.stream(
            "POST", f"/api/conversations/{conversation['id']}/messages", json={"content": text}
        ) as response:
            await read_sse(response)

    detail = (await client.get(f"/api/conversations/{conversation['id']}")).json()
    assert len(detail["messages"]) == 4
    assert detail["conversation"]["session_id"] == "sess-42"

    reset = await client.post(f"/api/conversations/{conversation['id']}/session/reset")
    assert reset.json()["session_id"] is None


async def test_configuration_error_returns_409(client: httpx.AsyncClient):
    agent = await make_agent(client, enabled=False)
    conversation = (await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})).json()
    response = await client.post(
        f"/api/conversations/{conversation['id']}/messages", json={"content": "hi"}
    )
    assert response.status_code == 409
    assert "已停用" in response.json()["detail"]


async def test_missing_executable_reports_a_clean_error(client: httpx.AsyncClient):
    options = {**cli_options("sess-42"), "command": "definitely-not-a-real-binary-12345"}
    agent = await make_agent(client, options=options)
    conversation = (await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})).json()
    async with client.stream(
        "POST", f"/api/conversations/{conversation['id']}/messages", json={"content": "hi"}
    ) as response:
        events = await read_sse(response)
    assert any(e["type"] == "error" and "未安装" in (e.get("text") or "") for e in events)
    assert events[-2]["type"] == "run.finished"
    assert events[-2]["payload"]["status"] == "error"


async def test_probe_endpoint_reports_missing_binary(client: httpx.AsyncClient):
    bad = await make_agent(
        client, options={**cli_options("sess-42"), "command": "nope-not-here-98765"}
    )
    result = (await client.post(f"/api/agents/{bad['id']}/test")).json()
    assert result["ok"] is False
    assert any("未在 PATH 中找到" in c["detail"] for c in result["checks"] if not c["ok"])


async def test_agent_and_conversation_guards(client: httpx.AsyncClient):
    agent = await make_agent(client)
    role = await make_profile(client, gateway_id=agent["gateway_id"], name="Solo")
    conversation = (await client.post("/api/conversations", json={"agent_profile_id": role["id"]})).json()

    assert (await client.delete(f"/api/conversations/{conversation['id']}")).status_code == 204
    assert (await client.delete(f"/api/agents/{role['id']}")).status_code == 204

    missing = await client.post(
        "/api/agents", json={"name": "x", "type_id": "custom-cli", "gateway_id": "ghost"}
    )
    assert missing.status_code == 422


async def test_agent_delete_keeps_its_conversations_readable(client: httpx.AsyncClient):
    """有历史对话不阻断删除智能体：软删除后它从列表消失，但历史仍完整可读。"""
    gateway = await make_gateway(client)
    role = await make_profile(client, gateway_id=gateway["id"], name="Solo")
    conversation = (await client.post("/api/conversations", json={"agent_profile_id": role["id"]})).json()

    assert (await client.delete(f"/api/agents/{role['id']}")).status_code == 204

    assert (await client.get("/api/agents")).json()["items"] == []
    assert (await client.get(f"/api/agents/{role['id']}")).status_code == 404
    # 历史对话仍显示当时的名字与头像（对话快照不因智能体删除而失效）
    detail = (await client.get(f"/api/conversations/{conversation['id']}")).json()["conversation"]
    assert detail["agent_name"] == "Solo"
    assert detail["agent_avatar"] == role["avatar"]
    # 已删除的智能体不能再用来开新对话
    rejected = await client.post("/api/conversations", json={"agent_profile_id": role["id"]})
    assert rejected.status_code == 422
    # 重复删除按不存在处理
    assert (await client.delete(f"/api/agents/{role['id']}")).status_code == 404


async def test_soft_deleted_agent_frees_its_gateway(client: httpx.AsyncClient):
    """智能体软删除后不再占用网关（否则网关会被历史引用永久锁死）。"""
    agent = await make_agent(client)
    assert (await client.delete(f"/api/gateways/{agent['gateway_id']}")).status_code == 409

    assert (await client.delete(f"/api/agents/{agent['id']}")).status_code == 204
    assert (await client.delete(f"/api/gateways/{agent['gateway_id']}")).status_code == 204


async def test_system_overview_counts_builtin_types(client: httpx.AsyncClient):
    overview = (await client.get("/api/system/overview")).json()
    assert overview["agent_types"] == 5  # 取消 DeepSeek 内核后只剩 5 个内置适配器
    assert overview["installed_types"] >= 1  # 当前环境中已安装 claude-code
    assert overview["version"] == __version__
    assert overview["version_history"][0]["version"] == overview["version"]
    assert overview["version_history"][0]["highlights"]


async def _conversation_with_workspace(
    client: httpx.AsyncClient, tmp_path: Path
) -> tuple[dict, Path]:
    """建一个工作目录可控的对话，附件测试不必往真实数据目录里写东西。"""
    agent = await make_agent(client)
    role = await make_profile(client, gateway_id=agent["gateway_id"], name="Tester")
    workspace = tmp_path / "ws"
    conversation = (
        await client.post(
            "/api/conversations", json={"agent_profile_id": role["id"], "workspace_dir": str(workspace)}
        )
    ).json()
    return conversation, workspace


async def test_attachment_is_saved_in_the_workspace_and_reaches_the_prompt(
    client: httpx.AsyncClient, tmp_path: Path
):
    conversation, workspace = await _conversation_with_workspace(client, tmp_path)
    raw = "季度报表内容".encode()

    uploaded = await client.post(
        f"/api/conversations/{conversation['id']}/attachments",
        json={"name": "季度报表.txt", "content_base64": base64.b64encode(raw).decode()},
    )
    assert uploaded.status_code == 201, uploaded.text
    info = uploaded.json()
    assert info["name"] == "季度报表.txt"
    assert info["rel_path"].startswith("attachments/")
    assert info["size"] == len(raw)
    saved = workspace / info["rel_path"]
    assert saved.is_file() and saved.read_bytes() == raw

    async with client.stream(
        "POST",
        f"/api/conversations/{conversation['id']}/messages",
        json={"content": "看看这个", "attachments": [info["rel_path"]]},
    ) as response:
        assert response.status_code == 200
        events = await read_sse(response)

    # 附件行真的进了智能体看到的提示词（替身智能体会回显提示词开头）。
    echoed = "".join(e.get("text") or "" for e in events if e["type"] == "message.delta")
    assert "📎 附件" in echoed

    detail = (await client.get(f"/api/conversations/{conversation['id']}")).json()
    assert detail["messages"][1]["status"] == "complete"
    user = detail["messages"][0]
    assert user["role"] == "user"
    assert user["content"].startswith("看看这个")
    assert info["rel_path"] in user["content"]


async def test_attachment_only_message_is_allowed(client: httpx.AsyncClient, tmp_path: Path):
    conversation, _ = await _conversation_with_workspace(client, tmp_path)
    info = (
        await client.post(
            f"/api/conversations/{conversation['id']}/attachments",
            json={"name": "note.md", "content_base64": base64.b64encode(b"# hi").decode()},
        )
    ).json()

    async with client.stream(
        "POST",
        f"/api/conversations/{conversation['id']}/messages",
        json={"content": "   ", "attachments": [info["rel_path"]]},
    ) as response:
        assert response.status_code == 200
        await read_sse(response)

    detail = (await client.get(f"/api/conversations/{conversation['id']}")).json()
    assert detail["messages"][0]["content"].startswith("📎 附件")

    # 既没有正文也没有附件 → 422（与旧行为一致）
    empty = await client.post(
        f"/api/conversations/{conversation['id']}/messages", json={"content": "  "}
    )
    assert empty.status_code == 422


async def test_attachment_upload_guards(client: httpx.AsyncClient, tmp_path: Path, monkeypatch):
    conversation, workspace = await _conversation_with_workspace(client, tmp_path)
    base = f"/api/conversations/{conversation['id']}/attachments"

    invalid = await client.post(base, json={"name": "x.txt", "content_base64": "不是 base64!!"})
    assert invalid.status_code == 422
    assert "base64" in invalid.json()["detail"]

    # 文件名里的目录成分被剥掉：跑不出工作目录
    sneaky = await client.post(
        base, json={"name": "../../逃逸.txt", "content_base64": base64.b64encode(b"x").decode()}
    )
    assert sneaky.status_code == 201, sneaky.text
    assert sneaky.json()["name"] == "逃逸.txt"
    assert ".." not in sneaky.json()["rel_path"]

    # 超过上限的附件被拒（把上限压到 4 字节，不必真造一个 8 MB 的请求）
    from app.api import conversations as api_conversations

    monkeypatch.setattr(api_conversations, "MAX_ATTACHMENT_BYTES", 4)
    oversized = await client.post(
        base, json={"name": "big.bin", "content_base64": base64.b64encode(b"0123456789").decode()}
    )
    assert oversized.status_code == 413
    assert "附件过大" in oversized.json()["detail"]

    # 指向工作目录之外的附件一律拒绝，别让模型去读工作目录以外的文件
    async with client.stream(
        "POST",
        f"/api/conversations/{conversation['id']}/messages",
        json={"content": "hi", "attachments": ["../../etc/hosts"]},
    ) as response:
        assert response.status_code == 409


async def test_attachment_upload_reports_an_unusable_workspace(
    client: httpx.AsyncClient, tmp_path: Path
):
    """工作目录不可用时要给出明确提示，而不是让建目录的异常冒成裸 500。"""
    agent = await make_agent(client)
    role = await make_profile(client, gateway_id=agent["gateway_id"], name="Tester")
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("x", encoding="utf-8")
    conversation = (
        await client.post(
            "/api/conversations", json={"agent_profile_id": role["id"], "workspace_dir": str(blocked)}
        )
    ).json()

    response = await client.post(
        f"/api/conversations/{conversation['id']}/attachments",
        json={"name": "x.txt", "content_base64": base64.b64encode(b"x").decode()},
    )
    assert response.status_code == 409
    assert "工作目录不可用" in response.json()["detail"]


async def test_removed_kernel_gives_a_readable_error(client: httpx.AsyncClient):
    """适配器下架后，历史实例仍留在库里：聊天与重新生成都要给中文提示，而不是裸 500。

    这是「取消某个内核」的兼容性兜底：数据一行不删，但使用到它时要明确说清楚哪里不对。
    """
    agent = await make_agent(client)
    role = await make_profile(client, gateway_id=agent["gateway_id"], name="Legacy")
    conversation = (await client.post("/api/conversations", json={"agent_profile_id": role["id"]})).json()

    async with client.stream(
        "POST", f"/api/conversations/{conversation['id']}/messages", json={"content": "hi"}
    ) as response:
        assert response.status_code == 200
        await read_sse(response)

    # 模拟库里残留的、已经下架的内核（接口不允许这么建，只能直接改库）
    async with SessionLocal() as session:
        await session.execute(
            update(AgentProfile)
            .where(AgentProfile.id == role["id"])
            .values(type_id="deepseek-harness")
        )
        await session.commit()

    response = await client.post(
        f"/api/conversations/{conversation['id']}/messages", json={"content": "再来一轮"}
    )
    assert response.status_code == 409
    assert "未知的智能体类型" in response.json()["detail"]

    regen = await client.post(f"/api/conversations/{conversation['id']}/regenerate")
    assert regen.status_code == 409
    assert "未知的智能体类型" in regen.json()["detail"]

    # 列表接口仍然要把这条智能体展示出来（历史对话照旧可读），只是标成「未知类型」
    items = (await client.get("/api/agents")).json()["items"]
    assert [row["id"] for row in items] == [agent["id"], role["id"]]
    assert items[1]["type_id"] == "deepseek-harness"


async def test_default_agent_star_is_unique_and_drives_new_conversations(client: httpx.AsyncClient):
    """五角星全局唯一：不指定智能体时，新建对话直接用星标着的那个。"""
    first = await make_agent(client)
    # 第一个智能体自动戴上星：装好就能点「新建对话」
    assert first["is_default"] is True
    second = await make_profile(client, gateway_id=first["gateway_id"], name="第二个")
    assert second["is_default"] is False

    starred = await client.patch(f"/api/agents/{second['id']}", json={"is_default": True})
    assert starred.json()["is_default"] is True
    listed = (await client.get("/api/agents")).json()["items"]
    assert [item["is_default"] for item in listed] == [False, True]

    created = await client.post("/api/conversations", json={})
    assert created.status_code == 201, created.text
    assert created.json()["agent_profile_id"] == second["id"]
    assert created.json()["workspace_dir"] is None

    # 摘掉星、而且有不止一个可选智能体 → 报错要能告诉用户去哪儿点星
    assert (
        await client.patch(f"/api/agents/{second['id']}", json={"is_default": False})
    ).json()["is_default"] is False
    denied = await client.post("/api/conversations", json={})
    assert denied.status_code == 422
    assert "五角星" in denied.json()["detail"]


async def test_lonely_agent_is_used_without_a_star(client: httpx.AsyncClient):
    """只有一启用智能体时（还没点过星）也直接用它，别卡住新建。"""
    agent = await make_agent(client)
    await client.patch(f"/api/agents/{agent['id']}", json={"is_default": False})
    created = await client.post("/api/conversations", json={})
    assert created.status_code == 201, created.text
    assert created.json()["agent_profile_id"] == agent["id"]


async def test_deleting_the_starred_agent_promotes_another(client: httpx.AsyncClient):
    first = await make_agent(client)
    second = await make_profile(client, gateway_id=first["gateway_id"], name="第二个")
    assert first["is_default"] is True

    assert (await client.delete(f"/api/agents/{first['id']}")).status_code == 204
    listed = (await client.get("/api/agents")).json()["items"]
    assert [item["id"] for item in listed] == [second["id"]]
    assert listed[0]["is_default"] is True


async def test_conversation_scope_change_resets_the_session(client: httpx.AsyncClient, tmp_path: Path):
    """聊天页输入框上方那两个小图标：换智能体 / 换工作目录都要清掉会话记忆。"""
    agent = await make_agent(client)
    other = await make_profile(client, gateway_id=agent["gateway_id"], name="另一个")
    conversation = (
        await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})
    ).json()

    async def set_session(value: str) -> None:
        async with SessionLocal() as db:
            await db.execute(
                update(Conversation).where(Conversation.id == conversation["id"]).values(session_id=value)
            )
            await db.commit()

    # 只改标题：记忆不动
    await set_session("sess-1")
    renamed = await client.patch(f"/api/conversations/{conversation['id']}", json={"title": "改个名"})
    assert renamed.json()["session_id"] == "sess-1"

    # 换工作目录：记忆清空（换了目录就是另一份上下文）
    moved = await client.patch(
        f"/api/conversations/{conversation['id']}", json={"workspace_dir": str(tmp_path)}
    )
    assert moved.json()["workspace_dir"] == str(tmp_path)
    assert moved.json()["session_id"] is None

    # 换智能体：同样清空
    await set_session("sess-2")
    switched = await client.patch(
        f"/api/conversations/{conversation['id']}", json={"agent_profile_id": other["id"]}
    )
    assert switched.json()["agent_profile_id"] == other["id"]
    assert switched.json()["session_id"] is None

    # 工作目录可以留空 = 回到「按对话自动分配」
    cleared = await client.patch(f"/api/conversations/{conversation['id']}", json={"workspace_dir": ""})
    assert cleared.json()["workspace_dir"] is None

    # 不存在的智能体：422，且这条对话不该被改动
    ghost = await client.patch(
        f"/api/conversations/{conversation['id']}", json={"agent_profile_id": "ghost"}
    )
    assert ghost.status_code == 422
    assert (await client.get(f"/api/conversations/{conversation['id']}")).json()["conversation"][
        "agent_profile_id"
    ] == other["id"]


async def test_running_conversation_refuses_scope_change(client: httpx.AsyncClient):
    """正在跑的对话不让中途换智能体 / 换目录（换了会把这一轮带偏）。"""
    agent = await make_agent(client)
    other = await make_profile(client, gateway_id=agent["gateway_id"], name="另一个")
    conversation = (
        await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})
    ).json()
    registry.register(conversation["id"], "假智能体")
    try:
        blocked = await client.patch(
            f"/api/conversations/{conversation['id']}", json={"agent_profile_id": other["id"]}
        )
        assert blocked.status_code == 409
        assert "正在运行" in blocked.json()["detail"]
    finally:
        registry.cancel_for_conversation(conversation["id"])
