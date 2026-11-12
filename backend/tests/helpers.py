"""接口层测试的共享夹具：造网关、造智能体。"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx

FAKE_AGENT = str(Path(__file__).parent / "fakes" / "fake_jsonl_agent.py")
FAKE_COLLAB_AGENT = str(Path(__file__).parent / "fakes" / "fake_collab_agent.py")


def collab_options(script_path: object, state_dir: object, *, fake: str | None = None) -> dict:
    """协作替身智能体的运行参数（所有岗位共用同一份剧本）。"""
    return {
        "command": sys.executable,
        "args": [fake or FAKE_COLLAB_AGENT, str(script_path), str(state_dir)],
        "prompt_mode": "stdin",
        "output_format": "jsonl",
        "text_path": "delta",
        "delta_mode": True,
        "session_path": "session_id",
        "usage_path": "usage",
    }


def cli_options(*script_args: object) -> dict:
    """假 CLI 内核的运行参数（custom-cli 的声明式配置）。"""
    return {
        "command": sys.executable,
        "args": [FAKE_AGENT, *[str(arg) for arg in script_args]],
        "prompt_mode": "stdin",
        "output_format": "jsonl",
        "text_path": "delta",
        "delta_mode": True,
        "session_path": "session_id",
        "tool_path": "tool_call.name",
    }


async def make_gateway(client: httpx.AsyncClient, **overrides) -> dict:
    body = {
        "name": "测试网关",
        "base_url": "https://gw.example/v1",
        "protocol": "openai",
        "auth_style": "bearer",
        "api_key": "sk-super-secret-value",
        "selected_models": ["fake-model", "other-model"],
    }
    body.update(overrides)
    response = await client.post("/api/gateways", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def make_profile(client: httpx.AsyncClient, **overrides) -> dict:
    body: dict = {
        "name": "假智能体",
        "type_id": "custom-cli",
        "model": "fake-model",
        "options": cli_options("sess-42"),
    }
    body.update(overrides)
    response = await client.post("/api/agents", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def make_agent(client: httpx.AsyncClient, **overrides) -> dict:
    """建一个「网关 + 智能体」，返回智能体（网关 id 在 gateway_id 上）。"""
    gateway = await make_gateway(client)
    return await make_profile(client, gateway_id=gateway["id"], **overrides)
