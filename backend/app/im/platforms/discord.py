"""Discord 适配器：Gateway（WebSocket）收，REST 发。

私聊（没有 ``guild_id``）算 direct，服务器频道里的消息只有在机器人被 @ 时才算数。
按钮走 message components，点击从 ``INTERACTION_CREATE`` 回来。
"""
from __future__ import annotations

import asyncio
import json
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
    InboundAction,
    InboundFile,
    InboundMessage,
    OutboundMessage,
    Sender,
    int_or,
    json_body,
    reason_of,
)
from ..format import chunk_markdown

API = "https://discord.com/api/v10"
GATEWAY = "wss://gateway.discord.gg/?v=10&encoding=json"
#: guilds | guild messages | direct messages | message content
INTENTS = (1 << 0) | (1 << 9) | (1 << 12) | (1 << 15)
TEXT_LIMIT = 2000


def _error_of(body: dict[str, Any], status: int) -> AdapterError:
    retry_after = body.get("retry_after")
    return AdapterError(
        str(body.get("message") or f"Discord 返回 {status}"),
        status=status,
        retry_after=float(retry_after) if isinstance(retry_after, (int, float)) else None,
        permanent=400 <= status < 500 and status != 429,
    )


class DiscordAdapter(Adapter):
    id = "discord"

    async def check(self, fields: dict[str, Any]) -> Account:
        token = str(fields.get("token") or "").strip()
        if token == "":
            raise AdapterError("请先填写 Bot token")
        async with httpx.AsyncClient(timeout=20) as client:
            body = await _request(client, token, "/users/@me", method="GET")
        if body.get("id") is None or body.get("bot") is not True:
            raise AdapterError("这个 token 不属于一个 Discord 机器人")
        name = f"@{body['username']}" if body.get("username") else str(body["id"])
        return Account(id=str(body["id"]), name=name)

    async def connect(
        self,
        fields: dict[str, Any],
        handlers: Handlers,
        runtime: dict[str, Any] | None = None,
    ) -> ConnectionBase:
        return DiscordConnection(fields, handlers, runtime or {})


class DiscordConnection(ConnectionBase):
    def __init__(self, fields: dict[str, Any], handlers: Handlers, runtime: dict[str, Any]) -> None:
        super().__init__(handlers)
        self.token = str(fields.get("token") or "").strip()
        self.client = httpx.AsyncClient(timeout=30)
        self.socket: Any = None
        self.self_id = ""
        self.self_name = ""
        self.sequence: int | None = None
        self.session_id: str | None = None
        self.resume_url: str | None = None
        self.gateway_url: str | None = None
        self.heartbeat_task: asyncio.Task | None = None
        self.heartbeat_acked = True

    # -- 连接 ---------------------------------------------------------------
    async def open(self, generation: int) -> None:
        try:
            info = await self._rest("/gateway/bot", method="GET")
            url = _gateway_url(info.get("url") or GATEWAY)
        except AdapterError as exc:
            if exc.permanent:
                # token 失效之类再试也没用：停在 error，而不是反复连公共 Gateway。
                self.status("error", str(exc))
                return
            url = _gateway_url(GATEWAY)
        if not self.current(generation):
            return
        self.gateway_url = url
        await self._connect_socket(generation, resume=False)

    async def _connect_socket(self, generation: int, *, resume: bool) -> None:
        if resume and self.resume_url:
            url = self.resume_url
        else:
            url = self.gateway_url or GATEWAY
        try:
            socket = await websockets.connect(
                url, max_size=None, ping_interval=None, open_timeout=20
            )
        except Exception as exc:  # noqa: BLE001 - 握手失败要重连
            raise AdapterError(f"Discord 无法建立连接：{reason_of(exc)}") from None
        if not self.current(generation):
            await socket.close()
            return
        self.socket = socket
        self.status("connecting")
        try:
            async for raw in socket:
                if not self.current(generation):
                    return
                try:
                    packet = json.loads(raw)
                except ValueError:
                    continue
                await self._on_packet(generation, packet)
        except websockets.ConnectionClosed:
            pass
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

    def _send(self, packet: dict[str, Any]) -> None:
        socket = self.socket
        if socket is None:
            return
        self.spawn(socket.send(json.dumps(packet)))

    async def _on_packet(self, generation: int, packet: dict[str, Any]) -> None:
        if isinstance(packet.get("s"), int):
            self.sequence = packet["s"]
        op = packet.get("op")
        if op == 10:
            interval = int_or(
                (packet.get("d") or {}).get("heartbeat_interval"), 40_000
            )
            self._start_heartbeat(interval)
            if self.session_id and self.sequence is not None:
                self._send(
                    {
                        "op": 6,
                        "d": {
                            "token": self.token,
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
                            "token": self.token,
                            "intents": INTENTS,
                            "properties": {
                                "os": "linux",
                                "browser": "coda",
                                "device": "coda",
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
                self.resume_url = None
                self.sequence = None
            await self._close_socket()
            return
        if op != 0:
            return
        kind = packet.get("t")
        data = packet.get("d") or {}
        if kind == "READY":
            self.session_id = data.get("session_id")
            self.resume_url = data.get("resume_gateway_url")
            user = data.get("user") or {}
            self.self_id = str(user.get("id", ""))
            self.self_name = str(user.get("username", ""))
            display = f"@{self.self_name}" if self.self_name else self.self_id
            self.status("ready", account=Account(id=self.self_id, name=display))
        elif kind == "RESUMED":
            self.status("ready")
        elif kind == "MESSAGE_CREATE":
            self.spawn(self._on_message(data))
        elif kind == "INTERACTION_CREATE":
            self.spawn(self._on_interaction(data))

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

    async def _close_socket(self) -> None:
        socket = self.socket
        self.socket = None
        if socket is not None:
            try:
                await socket.close()
            except Exception:  # noqa: BLE001 - 已经断开的 socket 关不掉也无所谓
                pass

    # -- 入站 ---------------------------------------------------------------
    async def _on_message(self, data: dict[str, Any]) -> None:
        author = data.get("author") or {}
        if author.get("bot") is True:
            return
        direct = data.get("guild_id") is None
        mentions = data.get("mentions") or []
        mentioned = (
            False
            if direct
            else any(str(item.get("id")) == self.self_id for item in mentions)
        )
        images: list[InboundFile] = []
        files: list[InboundFile] = []
        notes: list[str] = []
        for item in data.get("attachments") or []:
            name = str(item.get("filename") or "file")
            url = item.get("url")
            if not url:
                notes.append(f"[Discord 附件「{name}」无法下载：没有下载地址，请告诉用户]")
                continue
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    response = await client.get(url)
            except httpx.HTTPError as exc:
                notes.append(
                    f"[Discord 附件「{name}」无法下载：{reason_of(exc)}，请告诉用户]"
                )
                continue
            if response.status_code >= 400:
                notes.append(
                    f"[Discord 附件「{name}」无法下载：HTTP {response.status_code}，"
                    "请告诉用户]"
                )
                continue
            media_type = str(item.get("content_type") or "")
            got = InboundFile(
                name=name,
                data=response.content,
                media_type=media_type.split(";")[0],
            )
            if media_type.startswith("image/"):
                images.append(got)
            else:
                files.append(got)
        text = str(data.get("content") or "")
        if mentioned and self.self_id:
            text = (
                text.replace(f"<@{self.self_id}>", "")
                .replace(f"<@!{self.self_id}>", "")
                .strip()
            )
        if notes:
            text = "\n".join(part for part in [text, *notes] if part)
        await self.handlers.on_message(
            InboundMessage(
                chat=ChatRef(
                    key=str(data.get("channel_id", "")),
                    kind="direct" if direct else "group",
                    title=str(
                        (data.get("channel") or {}).get("name")
                        or author.get("username")
                        or ""
                    ),
                ),
                sender=Sender(
                    id=str(author.get("id", "")),
                    name=str(author.get("username") or ""),
                ),
                text=text,
                images=images,
                files=files,
                mentioned=mentioned,
                message_id=str(data.get("id", "")),
            )
        )

    async def _on_interaction(self, data: dict[str, Any]) -> None:
        if data.get("type") != 3:
            return
        interaction_id = data.get("id")
        token = data.get("token")
        try:
            await self._rest(
                f"/interactions/{interaction_id}/{token}/callback",
                method="POST",
                body={"type": 6},
            )
        except AdapterError:
            pass  # 延迟应答失败不影响按钮本身
        from_ = data.get("user") or (data.get("member") or {}).get("user") or {}
        await self.handlers.on_action(
            InboundAction(
                chat=ChatRef(
                    key=str(
                        data.get("channel_id")
                        or (data.get("channel") or {}).get("id")
                        or ""
                    ),
                    kind="direct" if data.get("guild_id") is None else "group",
                ),
                sender=Sender(
                    id=str(from_.get("id", "")),
                    name=str(from_.get("username") or ""),
                ),
                data=str((data.get("data") or {}).get("custom_id") or ""),
                message_id=str((data.get("message") or {}).get("id", "")),
            )
        )

    # -- 出站 ---------------------------------------------------------------
    async def send(self, chat: ChatRef, message: OutboundMessage) -> None:
        chunks = chunk_markdown(message.text, TEXT_LIMIT)
        for index, chunk in enumerate(chunks):
            components: list[dict[str, Any]] = []
            if index == len(chunks) - 1 and message.buttons:
                components = [
                    {
                        "type": 1,
                        "components": [
                            {
                                "type": 2,
                                "style": 1,
                                "label": button.label[:80],
                                "custom_id": button.data,
                            }
                            for button in message.buttons[:5]
                        ],
                    }
                ]
            await self._rest(
                f"/channels/{chat.key}/messages",
                method="POST",
                body={"content": chunk, "components": components},
            )

    async def typing(self, chat: ChatRef) -> None:
        try:
            await self._rest(f"/channels/{chat.key}/typing", method="POST")
        except AdapterError:
            return None

    async def stop(self) -> None:
        # 先置位 stopped：读循环的 finally 就不会把「主动停机」误报成掉线并重连。
        await super().stop()
        if self.heartbeat_task is not None:
            self.heartbeat_task.cancel()
            self.heartbeat_task = None
        await self._close_socket()
        await self.client.aclose()

    async def _rest(
    self,
    path: str,
    *,
    method: str = "POST",
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
        return await _request(self.client, self.token, path, method=method, body=body)


async def _request(
    client: httpx.AsyncClient,
    token: str,
    path: str,
    *,
    method: str = "POST",
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        response = await client.request(
            method,
            f"{API}{path}",
            headers={
                "authorization": f"Bot {token}",
                "content-type": "application/json",
            },
            content=None if body is None else json.dumps(body),
        )
    except httpx.HTTPError as exc:
        raise AdapterError(f"Discord 连接失败：{reason_of(exc)}") from None
    data = json_body(response)
    if response.status_code >= 400:
        raise _error_of(data, response.status_code)
    return data


def _gateway_url(base: str) -> str:
    """补上 v/encoding 查询参数；给不出合法地址时回落到官方 Gateway。"""
    text = str(base)
    if text.startswith("ws"):
        separator = "&" if "?" in text else "?"
        return f"{text}{separator}v=10&encoding=json"
    return GATEWAY


__all__ = ["DiscordAdapter"]
