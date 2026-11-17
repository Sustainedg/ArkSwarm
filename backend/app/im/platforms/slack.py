"""Slack 适配器：Socket Mode（WebSocket）收，Web API 发。

``apps.connections.open`` 用 App-Level Token（xapp-）换一条 WebSocket 地址；每个 envelope
都要原样回 ``envelope_id`` 才算 ACK，``disconnect`` 帧表示要重连。发消息 / 改消息走
Web API（xoxb-）。``im`` 频道的消息算私聊，``app_mention`` 是群里 @ 机器人的触发条件。
"""
from __future__ import annotations

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
    json_body,
    reason_of,
    retry_after_of,
)
from ..format import chunk_markdown

API = "https://slack.com/api/"
TEXT_LIMIT = 4000
#: 这些错误值得重试；其余（token 失效、频道不存在…）是永久失败。
RETRYABLE = {
    "rate_limited",
    "fatal_error",
    "internal_error",
    "request_timeout",
    "service_unavailable",
}


def _fail(reason: str, status: int | None = None) -> AdapterError:
    return AdapterError(
        f"Slack 返回 {str(reason).replace('_', ' ')}",
        permanent=str(reason) not in RETRYABLE,
        status=status,
    )


class SlackAdapter(Adapter):
    id = "slack"

    async def check(self, fields: dict[str, Any]) -> Account:
        app_token = str(fields.get("app_token") or "").strip()
        bot_token = str(fields.get("bot_token") or "").strip()
        if app_token == "" or bot_token == "":
            raise AdapterError("请填写 App-Level Token 与 Bot Token")
        async with httpx.AsyncClient(timeout=20) as client:
            body = await _call(client, bot_token, "auth.test", {})
        user_id = body.get("user_id")
        if user_id is None:
            raise AdapterError("这组凭据不属于一个 Slack 机器人")
        name = f"@{body['user']}" if body.get("user") else str(user_id)
        return Account(id=str(user_id), name=name)

    async def connect(
        self,
        fields: dict[str, Any],
        handlers: Handlers,
        runtime: dict[str, Any] | None = None,
    ) -> ConnectionBase:
        return SlackConnection(fields, handlers, runtime or {})


class SlackConnection(ConnectionBase):
    def __init__(self, fields: dict[str, Any], handlers: Handlers, runtime: dict[str, Any]) -> None:
        super().__init__(handlers)
        self.app_token = str(fields.get("app_token") or "").strip()
        self.bot_token = str(fields.get("bot_token") or "").strip()
        self.client = httpx.AsyncClient(timeout=30)
        self.socket: Any = None
        self.self_id = ""

    async def open(self, generation: int) -> None:
        if self.self_id == "":
            try:
                me = await self._api("auth.test", {})
                self.self_id = str(me.get("user_id") or "")
            except AdapterError:
                pass  # 拿不到身份也还能收消息，只是群里 @ 判断会弱一点
        try:
            url = await self._open_url()
        except AdapterError as exc:
            if exc.permanent:
                self.status("error", str(exc))
                return
            raise
        if not self.current(generation):
            return
        try:
            socket = await websockets.connect(url, max_size=None, ping_interval=20, open_timeout=20)
        except Exception as exc:  # noqa: BLE001 - 握手失败要重连
            raise AdapterError(f"Slack 无法建立连接：{reason_of(exc)}") from None
        if not self.current(generation):
            await socket.close()
            return
        self.socket = socket
        self.status("connecting")
        self.spawn(self._read_loop(generation, socket))

    async def _open_url(self) -> str:
        try:
            response = await self.client.post(
                f"{API}apps.connections.open",
                headers={"authorization": f"Bearer {self.app_token}"},
            )
        except httpx.HTTPError as exc:
            raise AdapterError(f"Slack 连接失败：{reason_of(exc)}") from None
        if response.status_code == 429:
            raise AdapterError("Slack 限流了这次连接", retry_after=retry_after_of(response))
        body = json_body(response)
        if not body.get("ok"):
            raise _fail(
            str(body.get("error") or f"HTTP {response.status_code}"),
            response.status_code,
        )
        return str(body.get("url") or "")

    async def _read_loop(self, generation: int, socket: Any) -> None:
        try:
            while self.current(generation):
                try:
                    raw = await socket.recv()
                except websockets.ConnectionClosed:
                    break
                if not self.current(generation):
                    return
                try:
                    packet = json.loads(raw)
                except ValueError:
                    continue
                await self._on_packet(generation, socket, packet)
        finally:
            if self.socket is socket:
                self.socket = None
            if self.current(generation):
                self.status("connecting")
                self.retry(generation, "Slack 连接已断开", delay=2.0)

    async def _on_packet(self, generation: int, socket: Any, packet: dict[str, Any]) -> None:
        kind = packet.get("type")
        if kind == "hello":
            name = f"@{self.self_id}" if self.self_id else "Slack"
            self.status("ready", account=Account(id=self.self_id, name=name))
            return
        envelope_id = packet.get("envelope_id")
        if envelope_id is not None:
            try:
                await socket.send(json.dumps({"envelope_id": envelope_id}))
            except Exception:  # noqa: BLE001 - 连接正在关闭
                return
        if kind == "disconnect":
            self.retry(generation, "Slack 要求重连", delay=1.0)
            return
        payload = packet.get("payload") or {}
        if kind == "events_api" and payload.get("type") == "event_callback":
            self.spawn(self._on_event(payload.get("event") or {}))
        elif kind == "interactive":
            self.spawn(self._on_interactive(payload))

    async def _on_event(self, event: dict[str, Any]) -> None:
        if event.get("bot_id") is not None:
            return
        subtype = event.get("subtype")
        if subtype is not None and subtype != "file_share":
            return
        direct = event.get("type") == "message" and event.get("channel_type") == "im"
        mentioned = event.get("type") == "app_mention"
        if not direct and not mentioned:
            return
        images: list[InboundFile] = []
        files: list[InboundFile] = []
        notes: list[str] = []
        for item in event.get("files") or []:
            name = str(item.get("name") or "file")
            url = item.get("url_private")
            if not url:
                notes.append(f"[Slack 附件「{name}」无法下载：没有下载地址，请告诉用户]")
                continue
            headers = {"authorization": f"Bearer {self.bot_token}"}
            try:
                response = await self.client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                notes.append(
                    f"[Slack 附件「{name}」无法下载：{reason_of(exc)}，请告诉用户]"
                )
                continue
            if response.status_code >= 400:
                notes.append(
                    f"[Slack 附件「{name}」无法下载：HTTP {response.status_code}，请告诉用户]"
                )
                continue
            media_type = str(item.get("mimetype") or "")
            got = InboundFile(
                name=name,
                data=response.content,
                media_type=media_type.split(";")[0],
            )
            if media_type.startswith("image/"):
                images.append(got)
            else:
                files.append(got)
        text = str(event.get("text") or "")
        if self.self_id:
            text = text.replace(f"<{self.self_id}>", "")
        if notes:
            text = "\n".join(part for part in [text.strip(), *notes] if part)
        await self.handlers.on_message(
            InboundMessage(
                chat=ChatRef(
                    key=str(event.get("channel") or ""),
                    kind="direct" if direct else "group",
                ),
                sender=Sender(
                    id=str(event.get("user") or ""), name=str(event.get("user") or "")
                ),
                text=text.strip(),
                images=images,
                files=files,
                mentioned=mentioned,
                message_id=str(event.get("ts") or ""),
            )
        )

    async def _on_interactive(self, payload: dict[str, Any]) -> None:
        if payload.get("type") != "block_actions":
            return
        actions = payload.get("actions") or []
        if not actions:
            return
        action = actions[0] or {}
        user = payload.get("user") or {}
        await self.handlers.on_action(
            InboundAction(
                chat=ChatRef(
                    key=str((payload.get("channel") or {}).get("id") or ""), kind="direct"
                ),
                sender=Sender(
                    id=str(user.get("id") or ""),
                    name=str(user.get("name") or user.get("id") or ""),
                ),
                data=str(action.get("value") or action.get("action_id") or ""),
                message_id=str((payload.get("message") or {}).get("ts") or ""),
            )
        )

    # -- 出站 ---------------------------------------------------------------
    async def send(self, chat: ChatRef, message: OutboundMessage) -> None:
        chunks = chunk_markdown(message.text, TEXT_LIMIT)
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "channel": chat.key,
                "text": chunk,
                "blocks": _blocks(
                    chunk, message.buttons if index == len(chunks) - 1 else None
                ),
            }
            await self._api("chat.postMessage", payload)

    async def _api(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await _call(self.client, self.bot_token, method, payload)

    async def stop(self) -> None:
        # 先置位 stopped：读循环的 finally 就不会把「主动停机」误报成掉线并重连。
        await super().stop()
        socket = self.socket
        self.socket = None
        if socket is not None:
            try:
                await socket.close()
            except Exception:  # noqa: BLE001 - 已断开的 socket 关不掉也无所谓
                pass
        await self.client.aclose()


def _blocks(text: str, buttons: list[Any] | None) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": _to_mrkdwn(text)}}
    ]
    if buttons:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": f"b{index}",
                        "text": {"type": "plain_text", "text": button.label[:75]},
                        "value": button.data,
                    }
                    for index, button in enumerate(buttons[:5])
                ],
            }
        )
    return blocks


def _to_mrkdwn(markdown: str) -> str:
    """把 Markdown 的常见写法换成 Slack 的 mrkdwn（代码块保持原样）。"""
    parts = markdown.split("```")
    out: list[str] = []
    for index, part in enumerate(parts):
        if index % 2 == 1:
            out.append(part)
            continue
        text = part.replace("**", "*")
        out.append(text)
    return "```".join(out)


async def _call(
    client: httpx.AsyncClient, token: str, method: str, payload: dict[str, Any]
) -> dict[str, Any]:
    try:
        response = await client.post(
            f"{API}{method}",
            headers={
                "authorization": f"Bearer {token}",
                "content-type": "application/json; charset=utf-8",
            },
            json=payload,
        )
    except httpx.HTTPError as exc:
        raise AdapterError(f"Slack 连接失败：{reason_of(exc)}") from None
    if response.status_code == 429:
        raise AdapterError("Slack 限流了这次请求", retry_after=retry_after_of(response))
    body = json_body(response)
    if not body.get("ok"):
        raise _fail(
            str(body.get("error") or f"HTTP {response.status_code}"),
            response.status_code,
        )
    return body


__all__ = ["SlackAdapter"]
