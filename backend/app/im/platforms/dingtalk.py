"""钉钉适配器：Stream 模式（WebSocket）。

``connections/open`` 用 Client ID / Secret 换一张带 ticket 的 WebSocket 地址；每帧都要回
ACK（把 ``messageId`` 原样带回、``code=200``），系统 ping 回 pong。机器人回调里带着
``sessionWebhook``，被动回复优先走它（20 分钟有效），过了窗口再走 OpenAPI 主动发送。

钉钉没有卡片按钮，选择用「回复编号」。
"""
from __future__ import annotations

import asyncio
import json
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
)
from ..format import chunk_markdown

GATEWAY = "https://api.dingtalk.com"
OAPI = "https://oapi.dingtalk.com"
TEXT_LIMIT = 5000
SOCKET_TIMEOUT = 30.0
#: 10 分钟收不到任何帧就重连。
IDLE_TIMEOUT = 10 * 60.0


class DingTalkAdapter(Adapter):
    id = "dingtalk"

    async def check(self, fields: dict[str, Any]) -> Account:
        client_id = str(fields.get("client_id") or "").strip()
        client_secret = str(fields.get("client_secret") or "").strip()
        if client_id == "" or client_secret == "":
            raise AdapterError("请填写 Client ID 与 Client Secret")
        async with httpx.AsyncClient(timeout=20) as client:
            await _app_token(client, client_id, client_secret)
        return Account(id=client_id, name=client_id)

    async def connect(
        self,
        fields: dict[str, Any],
        handlers: Handlers,
        runtime: dict[str, Any] | None = None,
    ) -> ConnectionBase:
        return DingTalkConnection(fields, handlers, runtime or {})


class DingTalkConnection(ConnectionBase):
    def __init__(self, fields: dict[str, Any], handlers: Handlers, runtime: dict[str, Any]) -> None:
        super().__init__(handlers)
        self.client_id = str(fields.get("client_id") or "").strip()
        self.client_secret = str(fields.get("client_secret") or "").strip()
        self.client = httpx.AsyncClient(timeout=30)
        self.socket: Any = None
        self.token: str | None = None
        self.token_expires_at = 0.0
        #: 被动回复的 sessionWebhook（按聊天 key 记，20 分钟有效）
        self.webhooks: dict[str, tuple[str, float]] = {}

    async def open(self, generation: int) -> None:
        try:
            await self._ensure_token()
            endpoint, ticket = await self._open_socket_endpoint()
        except AdapterError as exc:
            if exc.permanent:
                self.status("error", str(exc))
                return
            self.status("retrying", str(exc))
            await asyncio.sleep(2)
            if self.current(generation):
                self.spawn(self._reopen(0.0))
            return
        if not self.current(generation):
            return
        try:
            socket = await websockets.connect(
                f"{endpoint}?ticket={ticket}",
                max_size=None,
                ping_interval=None,
                open_timeout=SOCKET_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - 握手失败要重连
            raise AdapterError(f"钉钉无法建立连接：{reason_of(exc)}") from None
        if not self.current(generation):
            await socket.close()
            return
        self.socket = socket
        self.status("ready", account=Account(id=self.client_id, name=self.client_id))
        self.spawn(self._read_loop(generation, socket))

    async def _open_socket_endpoint(self) -> tuple[str, str]:
        try:
            response = await self.client.post(
                f"{GATEWAY}/v1.0/gateway/connections/open",
                json={
                    "clientId": self.client_id,
                    "clientSecret": self.client_secret,
                    "ua": "arkswarm",
                    "subscriptions": [{"type": "CALLBACK", "topic": "/v1.0/im/bot/messages/get"}],
                },
            )
        except httpx.HTTPError as exc:
            raise AdapterError(f"钉钉连接失败：{reason_of(exc)}") from None
        body = json_body(response)
        if body.get("endpoint") is None:
            raise AdapterError(
                str(body.get("message") or body.get("error") or "钉钉拒绝了这次连接"),
                permanent=response.status_code == 200,
                status=response.status_code,
            )
        return str(body["endpoint"]), str(body.get("ticket") or "")

    async def _read_loop(self, generation: int, socket: Any) -> None:
        try:
            while self.current(generation):
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=IDLE_TIMEOUT)
                except asyncio.TimeoutError:
                    self.retry(generation, "钉钉 10 分钟没有收到任何数据")
                    return
                except websockets.ConnectionClosed:
                    break
                if not self.current(generation):
                    return
                try:
                    packet = json.loads(raw)
                except ValueError:
                    continue
                await self._on_packet(generation, packet)
        finally:
            if self.socket is socket:
                self.socket = None
            if self.current(generation):
                self.retry(generation, "钉钉连接已断开")

    async def _on_packet(self, generation: int, packet: dict[str, Any]) -> None:
        headers = packet.get("headers") or {}
        kind = packet.get("type")
        if kind == "SYSTEM":
            topic = headers.get("topic")
            data = packet.get("data")
            is_ping = (
                "ping" in str(data or "")
                or headers.get("eventType") == "ping"
                or topic == "ping"
            )
            if topic == "disconnect" or is_ping:
                await self._send_frame(
                    {"code": 200, "headers": headers, "message": "OK", "data": data}
                )
            if topic == "disconnect":
                self.retry(generation, "钉钉要求重连")
            return
        if kind in ("CALLBACK", "EVENT"):
            await self._ack(headers)
            try:
                payload = json.loads(str(packet.get("data") or "{}"))
            except ValueError:
                return
            self.spawn(self._on_callback(payload))

    async def _ack(self, headers: dict[str, Any]) -> None:
        await self._send_frame(
            {
                "specVersion": "1.0",
                "code": 200,
                "headers": {
                    "appType": "STREAM",
                    "clientId": self.client_id,
                    "messageId": str(headers.get("messageId") or ""),
                    "time": str(int(time.time() * 1000)),
                },
                "message": "OK",
                "data": json.dumps({"ok": True}),
            }
        )

    async def _send_frame(self, packet: dict[str, Any]) -> None:
        socket = self.socket
        if socket is None:
            return
        try:
            await socket.send(json.dumps(packet))
        except Exception:  # noqa: BLE001 - 连接正在断开时发送失败只影响这一帧
            return

    async def _on_callback(self, data: dict[str, Any]) -> None:
        msgtype = data.get("msgtype")
        if data.get("text") is None and msgtype not in ("text", "richText", "picture"):
            return
        direct = str(data.get("conversationType")) == "1"
        key = str(
            (data.get("senderStaffId") or data.get("senderId") or "")
            if direct
            else (data.get("conversationId") or "")
        )
        hook = data.get("sessionWebhook")
        if hook:
            self.webhooks[key] = (str(hook), time.time() + 20 * 60)
        sender = str(data.get("senderStaffId") or data.get("senderId") or "")
        await self.handlers.on_message(
            InboundMessage(
                chat=ChatRef(key=key, kind="direct" if direct else "group"),
                sender=Sender(id=sender, name=str(data.get("senderNick") or sender)),
                text=str((data.get("text") or {}).get("content") or "").strip(),
                mentioned=False if direct else data.get("isInAtList") is True,
                message_id=str(data.get("msgId") or ""),
            )
        )

    # -- 出站 ---------------------------------------------------------------
    async def send(self, chat: ChatRef, message: OutboundMessage) -> None:
        for chunk in chunk_markdown(message.text, TEXT_LIMIT):
            hook = self.webhooks.get(chat.key)
            if hook is not None and hook[1] > time.time():
                await self._send_webhook(hook[0], chunk)
                continue
            await self._send_robot(chat, chunk)

    async def _send_webhook(self, url: str, text: str) -> None:
        try:
            response = await self.client.post(
                url,
                json={"msgtype": "markdown", "markdown": {"title": text[:30], "text": text}},
            )
        except httpx.HTTPError as exc:
            raise AdapterError(f"钉钉回复失败：{reason_of(exc)}") from None
        body = json_body(response)
        if body.get("errcode") not in (None, 0):
            raise AdapterError(str(body.get("errmsg") or "钉钉拒绝了这条回复"), permanent=True)

    async def _send_robot(self, chat: ChatRef, text: str) -> None:
        token = await self._ensure_token()
        msg_param = json.dumps({"title": text[:30], "text": text})
        payload = {"msgKey": "sampleMarkdown", "msgParam": msg_param}
        if chat.kind == "direct":
            path = "/v1.0/robot/oToMessages/batchSend"
            body = {"robotCode": self.client_id, "userIds": [chat.key], **payload}
        else:
            path = "/v1.0/robot/groupMessages/send"
            body = {"robotCode": self.client_id, "openConversationId": chat.key, **payload}
        try:
            response = await self.client.post(
                f"{GATEWAY}{path}",
                headers={"x-acs-dingtalk-access-token": token},
                json=body,
            )
        except httpx.HTTPError as exc:
            raise AdapterError(f"钉钉发送失败：{reason_of(exc)}") from None
        if response.status_code >= 400:
            data = json_body(response)
            raise AdapterError(
                str(data.get("message") or data.get("errmsg") or f"钉钉返回 {response.status_code}"),
                status=response.status_code,
                permanent=400 <= response.status_code < 500 and response.status_code != 429,
            )

    async def _ensure_token(self) -> str:
        if self.token is not None and self.token_expires_at > time.time() + 30:
            return self.token
        self.token, self.token_expires_at = await _app_token(
            self.client, self.client_id, self.client_secret
        )
        return self.token

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


async def _app_token(
    client: httpx.AsyncClient, client_id: str, client_secret: str
) -> tuple[str, float]:
    """oapi gettoken：钉钉的 app 级访问令牌（发送主动消息用）。

    凭据走 POST body 而不是 query：URL 会进 httpx 的访问日志与异常文本。
    """
    try:
        response = await client.post(
            f"{OAPI}/gettoken",
            json={"appkey": client_id, "appsecret": client_secret},
        )
    except httpx.HTTPError as exc:
        raise AdapterError(f"钉钉连接失败：{reason_of(exc)}") from None
    body = json_body(response)
    if body.get("errcode") not in (0,) or body.get("access_token") is None:
        raise AdapterError(
            str(body.get("errmsg") or "钉钉拒绝了这组凭据"),
            permanent=response.status_code < 500 and response.status_code != 429,
            status=response.status_code,
        )
    expires_in = int_or(body.get("expires_in"), 7200)
    return str(body["access_token"]), time.time() + max(60, expires_in - 300)


__all__ = ["DingTalkAdapter"]
