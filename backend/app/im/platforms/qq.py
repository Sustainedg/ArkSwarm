"""QQ 开放平台适配器：app 访问令牌 + 类 Discord 的 Gateway（WebSocket）。

收：``C2C_MESSAGE_CREATE``（私聊）与 ``GROUP_AT_MESSAGE_CREATE``（群里 @ 机器人）。
发：被动回复带收到的 ``msg_id``，并给每条回复一个递增的 ``msg_seq``；超出平台的回复窗口
时平台会明确拒绝，那种失败不重发。QQ 没有卡片按钮，选择用「回复编号」。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

import httpx
import websockets

from ..base import (
    Account,
    Adapter,
    AdapterError,
    ChatRef,
    ConnectionBase,
    Handlers,
    InboundMessage,
    OutboundMessage,
    Sender,
    int_or,
    json_body,
    reason_of,
    retry_after_of,
)
from ..format import chunk_markdown, to_plain

API = "https://api.sgroup.qq.com"
TOKEN_API = "https://bots.qq.com"
TEXT_LIMIT = 2000
#: GROUP_AND_C2C：覆盖 C2C_MESSAGE_CREATE 与 GROUP_AT_MESSAGE_CREATE。
INTENTS = 1 << 25
#: 平台拒绝 markdown（没有该权限）时的业务码，退成纯文本再发。
NO_MARKDOWN_CODE = 40034090


class QQAdapter(Adapter):
    id = "qq"

    async def check(self, fields: dict[str, Any]) -> Account:
        app_id = str(fields.get("app_id") or "").strip()
        app_secret = str(fields.get("app_secret") or "").strip()
        if app_id == "" or app_secret == "":
            raise AdapterError("请填写 App ID 与 App Secret")
        async with httpx.AsyncClient(timeout=20) as client:
            token, _expires = await _get_token(client, app_id, app_secret)
            me = await _api(client, token, "/users/@me", method="GET")
        name = f"@{me['username']}" if me.get("username") else app_id
        return Account(id=app_id, name=name)

    async def connect(
        self,
        fields: dict[str, Any],
        handlers: Handlers,
        runtime: dict[str, Any] | None = None,
    ) -> ConnectionBase:
        return QQConnection(fields, handlers, runtime or {})


class QQConnection(ConnectionBase):
    def __init__(self, fields: dict[str, Any], handlers: Handlers, runtime: dict[str, Any]) -> None:
        super().__init__(handlers)
        self.app_id = str(fields.get("app_id") or "").strip()
        self.app_secret = str(fields.get("app_secret") or "").strip()
        self.client = httpx.AsyncClient(timeout=30)
        self.socket: Any = None
        self.token: str | None = None
        self.expires_at = 0.0
        self.sequence: int | None = None
        self.session_id: str | None = None
        self.gateway_url: str | None = None
        self.heartbeat_task: asyncio.Task | None = None
        self.heartbeat_acked = True
        self.refresh_task: asyncio.Task | None = None
        #: 被动回复要用的「最近一条消息 id」与「已用序号」
        self.last_msg_id: dict[str, str] = {}
        self.msg_seq: dict[str, int] = {}

    async def open(self, generation: int) -> None:
        try:
            self.token, self.expires_at = await _get_token(
            self.client, self.app_id, self.app_secret
        )
            gateway = await _api(self.client, self.token, "/gateway", method="GET")
        except AdapterError as exc:
            if exc.permanent:
                self.status("error", str(exc))
                return
            raise
        if not self.current(generation):
            return
        url = gateway.get("url")
        if not url:
            self.status("error", "QQ 没有给出可用的 Gateway 地址")
            return
        self.gateway_url = str(url)
        self._start_refresh()
        await self._connect_socket(generation, resume=False)

    def _start_refresh(self) -> None:
        if self.refresh_task is not None:
            self.refresh_task.cancel()

        async def refresh() -> None:
            while True:
                wait = max(5.0, self.expires_at - time.time() - 60)
                await asyncio.sleep(wait)
                try:
                    self.token, self.expires_at = await _get_token(
            self.client, self.app_id, self.app_secret
        )
                except AdapterError:
                    continue

        self.refresh_task = asyncio.create_task(refresh())

    async def _connect_socket(self, generation: int, *, resume: bool) -> None:
        if not self.gateway_url:
            raise AdapterError("QQ 还没有 Gateway 地址")
        try:
            socket = await websockets.connect(
                self.gateway_url, max_size=None, ping_interval=None, open_timeout=20
            )
        except Exception as exc:  # noqa: BLE001 - 握手失败要重连
            raise AdapterError(f"QQ 无法建立连接：{reason_of(exc)}") from None
        if not self.current(generation):
            await socket.close()
            return
        self.socket = socket
        self.status("connecting")
        try:
            while self.current(generation):
                try:
                    raw = await socket.recv()
                except websockets.ConnectionClosed:
                    break
                try:
                    packet = json.loads(raw)
                except ValueError:
                    continue
                await self._on_packet(generation, packet, resume=resume)
        finally:
            if self.socket is socket:
                self.socket = None
            if self.current(generation):
                self.status("connecting")
                self.spawn(self._reconnect(generation))

    async def _reconnect(self, generation: int) -> None:
        await asyncio.sleep(2)
        if not self.current(generation):
            return
        try:
            await self._connect_socket(generation, resume=self.session_id is not None)
        except AdapterError as exc:
            if not self.current(generation):
                return
            self.status("retrying", str(exc))
            self.spawn(self._reconnect(generation))

    async def _on_packet(self, generation: int, packet: dict[str, Any], *, resume: bool) -> None:
        if isinstance(packet.get("s"), int):
            self.sequence = packet["s"]
        op = packet.get("op")
        if op == 10:
            interval = int_or((packet.get("d") or {}).get("heartbeat_interval"), 40_000)
            self._start_heartbeat(interval)
            token = f"QQBot {self.token}"
            if resume and self.session_id:
                self._send(
                    {
                        "op": 6,
                        "d": {
                            "token": token,
                            "session_id": self.session_id,
                            "seq": self.sequence,
                        },
                    }
                )
            else:
                self._send(
                    {
                        "op": 2,
                        "d": {
                            "token": token,
                            "intents": INTENTS,
                            "shard": [0, 1],
                            "properties": {
                        "os": "linux",
                        "browser": "arkswarm",
                        "device": "arkswarm",
                    },
                        },
                    }
                )
            return
        if op == 11:
            self.heartbeat_acked = True
            return
        if op == 1:
            self._send({"op": 1, "d": self.sequence})
            return
        if op == 7:
            await self._close_socket()
            return
        if op == 9:
            if packet.get("d") is not True:
                self.session_id = None
                self.sequence = None
            await self._close_socket()
            return
        if op != 0:
            return
        kind = packet.get("t")
        data = packet.get("d") or {}
        if kind == "READY":
            self.session_id = data.get("session_id")
            user = data.get("user") or {}
            name = f"@{user['username']}" if user.get("username") else self.app_id
            self.status("ready", account=Account(id=self.app_id, name=name))
        elif kind == "RESUMED":
            self.status("ready")
        elif kind in ("C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"):
            self.spawn(self._on_message(kind, data))

    def _start_heartbeat(self, interval_ms: int) -> None:
        if self.heartbeat_task is not None:
            self.heartbeat_task.cancel()
        self.heartbeat_acked = True

        async def beat() -> None:
            while True:
                await asyncio.sleep(interval_ms / 1000)
                if not self.heartbeat_acked:
                    await self._close_socket()
                    return
                self.heartbeat_acked = False
                self._send({"op": 1, "d": self.sequence})

        self.heartbeat_task = asyncio.create_task(beat())

    def _send(self, packet: dict[str, Any]) -> None:
        socket = self.socket
        if socket is not None:
            self.spawn(socket.send(json.dumps(packet)))

    async def _close_socket(self) -> None:
        socket = self.socket
        self.socket = None
        if socket is not None:
            try:
                await socket.close()
            except Exception:  # noqa: BLE001 - 已经断开的 socket 关不掉也无所谓
                pass

    async def _on_message(self, kind: str, data: dict[str, Any]) -> None:
        c2c = kind == "C2C_MESSAGE_CREATE"
        author = data.get("author") or {}
        if c2c:
            key = str(author.get("user_openid") or data.get("channel_id") or "")
        else:
            key = str(data.get("group_openid") or data.get("channel_id") or "")
        self.last_msg_id[key] = str(data.get("id") or "")
        sender = (
            author.get("user_openid")
            if c2c
            else (author.get("member_openid") or author.get("id"))
        )
        await self.handlers.on_message(
            InboundMessage(
                chat=ChatRef(key=key, kind="direct" if c2c else "group"),
                sender=Sender(id=str(sender or ""), name=str(sender or "")),
                text=str(data.get("content") or "").strip(),
                mentioned=not c2c,
                message_id=str(data.get("id") or ""),
            )
        )

    # -- 出站 ---------------------------------------------------------------
    async def send(self, chat: ChatRef, message: OutboundMessage) -> None:
        if self.token is None:
            raise AdapterError("QQ 还没有拿到访问令牌")
        chunks = chunk_markdown(message.text, TEXT_LIMIT)
        if chat.kind == "direct":
            base = f"/v2/users/{chat.key}/messages"
        else:
            base = f"/v2/groups/{chat.key}/messages"
        msg_id = self.last_msg_id.get(chat.key)
        for chunk in chunks:
            reply: dict[str, Any] = {}
            if msg_id:
                seq = self.msg_seq.get(msg_id, 0) + 1
                self.msg_seq[msg_id] = seq
                reply = {"msg_id": msg_id, "msg_seq": seq}
            try:
                await _api(
                    self.client,
                    self.token,
                    base,
                    method="POST",
                    body={"markdown": {"content": chunk}, "msg_type": 2, **reply},
                )
            except AdapterError as exc:
                # 平台拒绝 markdown（没有该权限）时退成纯文本再发一次。
                if exc.status is None or exc.biz_code != NO_MARKDOWN_CODE:
                    raise
                await _api(
                    self.client,
                    self.token,
                    base,
                    method="POST",
                    body={"content": to_plain(chunk), "msg_type": 0, **reply},
                )

    async def stop(self) -> None:
        # 先置位 stopped 并取消任务，再关 socket：否则读循环的 finally 会误报一次
        # 「连接已断开」并触发重连（见 ConnectionBase.stop 的语义）。
        await super().stop()
        for task in (self.heartbeat_task, self.refresh_task):
            if task is not None:
                task.cancel()
        self.heartbeat_task = self.refresh_task = None
        await self._close_socket()
        await self.client.aclose()


async def _get_token(
    client: httpx.AsyncClient, app_id: str, app_secret: str
) -> tuple[str, float]:
    try:
        response = await client.post(
            f"{TOKEN_API}/app/getAppAccessToken",
            json={"appId": app_id, "clientSecret": app_secret},
        )
    except httpx.HTTPError as exc:
        raise AdapterError(f"QQ 连接失败：{reason_of(exc)}") from None
    body = json_body(response)
    if body.get("access_token") is None:
        reason = str(
            body.get("error_reason") or body.get("error") or "QQ 拒绝了这组凭据"
        )
        raise AdapterError(
            reason,
            permanent=response.status_code < 500 and response.status_code != 429,
            status=response.status_code,
        )
    expires_in = int_or(body.get("expires_in"), 7200)
    return str(body["access_token"]), time.time() + max(10, expires_in - 300)


async def _api(
    client: httpx.AsyncClient,
    token: str,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        response = await client.request(
            method,
            f"{API}{path}",
            headers={"authorization": f"QQBot {token}", "content-type": "application/json"},
            content=None if body is None else json.dumps(body),
        )
    except httpx.HTTPError as exc:
        raise AdapterError(f"QQ 连接失败：{reason_of(exc)}") from None
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400 or data.get("code") is not None:
        reason = str(
            data.get("message") or data.get("error") or f"QQ 返回 {response.status_code}"
        )
        error = AdapterError(
            reason,
            status=response.status_code,
            permanent=(400 <= response.status_code < 500 and response.status_code != 429)
            or bool(_REPLY_WINDOW.search(reason)),
            retry_after=retry_after_of(response, only_on_429=True),
        )
        error.biz_code = (
            data.get("code") if data.get("code") is not None else data.get("err_code")
        )
        raise error
    return data


_REPLY_WINDOW = re.compile(r"reply.*window|quota|frequency|超过|过期", re.IGNORECASE)


__all__ = ["QQAdapter"]
