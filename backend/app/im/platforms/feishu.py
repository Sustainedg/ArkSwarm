"""飞书 / Lark 适配器：长连接（WebSocket）收，OpenAPI 发。

长连接传的是 protobuf 的 ``pbbp2.Frame``：客户端按服务端给的间隔发 control ping，
数据帧要原样回传（payload 换成 ``{"code":200}``）才算 ACK，``sum/seq`` 分片按序拼回。
私聊是 ``p2p``；群消息只有机器人被 @ 时才算数。发送走 OpenAPI（tenant_access_token），
有按钮的平台用交互卡片，改消息走 PATCH。

下面这个极小的 protobuf 编解码只实现这一种帧的形状。
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

TEXT_LIMIT = 4000
SOCKET_TIMEOUT = 30.0


# ---- pbbp2.Frame 编解码（protobuf wire format，只实现这一种消息）----------
# Frame 字段号：SeqID u64=1、LogID u64=2、service i32=3、method i32=4、
# headers repeated Header=5、payload_encoding string=6、payload_type string=7、
# payload bytes=8、LogIDNew string=9。
# Header 字段号：key string=1、value string=2。
# wire type：0 varint，2 length-delimited。

def _uvarint(value: int) -> bytes:
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _field(number: int, wire: int, value: Any) -> bytes:
    tag = _uvarint((number << 3) | wire)
    if wire == 0:
        return tag + _uvarint(int(value))
    body = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    return tag + _uvarint(len(body)) + body


def _encode_header(header: dict[str, str]) -> bytes:
    return _field(1, 2, header.get("key", "")) + _field(2, 2, header.get("value", ""))


def encode_frame(frame: dict[str, Any]) -> bytes:
    out = bytearray()
    for number, key in ((1, "SeqID"), (2, "LogID"), (3, "service"), (4, "method")):
        if frame.get(key) is not None:
            out += _field(number, 0, frame[key])
    for header in frame.get("headers") or []:
        out += _field(5, 2, _encode_header(header))
    text_fields = (
        (6, "payloadEncoding"),
        (7, "payloadType"),
        (8, "payload"),
        (9, "LogIDNew"),
    )
    for number, key in text_fields:
        if frame.get(key) is not None:
            out += _field(number, 2, frame[key])
    return bytes(out)


def decode_frame(buffer: bytes) -> dict[str, Any]:
    frame: dict[str, Any] = {"headers": []}
    at = 0
    size = len(buffer)

    def varint() -> int:
        nonlocal at
        value = 0
        shift = 0
        while at < size:
            byte = buffer[at]
            at += 1
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
        return value

    def blob() -> bytes:
        nonlocal at
        length = varint()
        part = buffer[at : at + length]
        at += length
        return part

    def header(raw: bytes) -> dict[str, str]:
        out: dict[str, str] = {}
        index = 0
        end = len(raw)

        def inner_varint() -> int:
            nonlocal index
            value = 0
            shift = 0
            while index < end:
                byte = raw[index]
                index += 1
                value |= (byte & 0x7F) << shift
                if not byte & 0x80:
                    break
                shift += 7
            return value

        while index < end:
            tag = inner_varint()
            number, wire = tag >> 3, tag & 7
            if wire == 2:
                length = inner_varint()
                part = raw[index : index + length]
                index += length
                if number == 1:
                    out["key"] = part.decode("utf-8", "replace")
                elif number == 2:
                    out["value"] = part.decode("utf-8", "replace")
            elif wire == 0:
                inner_varint()
            else:
                break
        return out

    while at < size:
        tag = varint()
        number, wire = tag >> 3, tag & 7
        if wire == 0:
            value = varint()
            if number == 1:
                frame["SeqID"] = value
            elif number == 2:
                frame["LogID"] = value
            elif number == 3:
                frame["service"] = value
            elif number == 4:
                frame["method"] = value
        elif wire == 2:
            value = blob()
            if number == 5:
                frame["headers"].append(header(value))
            elif number == 6:
                frame["payloadEncoding"] = value.decode("utf-8", "replace")
            elif number == 7:
                frame["payloadType"] = value.decode("utf-8", "replace")
            elif number == 8:
                frame["payload"] = value
            elif number == 9:
                frame["LogIDNew"] = value.decode("utf-8", "replace")
        else:
            break
    return frame
# ---------------------------------------------------------------------------


def _api_base(domain: str) -> str:
    return "https://open.larksuite.com" if domain == "lark" else "https://open.feishu.cn"


def _ping_interval(value: Any) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if 1.0 <= seconds <= 2_147_483.0:
        return seconds
    return None


class FeishuAdapter(Adapter):
    id = "feishu"

    async def check(self, fields: dict[str, Any]) -> Account:
        app_id = str(fields.get("app_id") or "").strip()
        app_secret = str(fields.get("app_secret") or "").strip()
        if app_id == "" or app_secret == "":
            raise AdapterError("请填写 App ID 与 App Secret")
        base = _api_base(str(fields.get("domain") or "feishu"))
        async with httpx.AsyncClient(timeout=20) as client:
            token, _expires = await _tenant_token(client, base, app_id, app_secret)
            me = await _api(client, base, token, "/open-apis/bot/v3/info", method="GET")
        bot = me.get("bot") or me
        return Account(
            id=str(bot.get("open_id") or bot.get("app_id") or app_id),
            name=str(bot.get("app_name") or app_id),
        )

    async def connect(
        self,
        fields: dict[str, Any],
        handlers: Handlers,
        runtime: dict[str, Any] | None = None,
    ) -> ConnectionBase:
        return FeishuConnection(fields, handlers, runtime or {})


class FeishuConnection(ConnectionBase):
    def __init__(self, fields: dict[str, Any], handlers: Handlers, runtime: dict[str, Any]) -> None:
        super().__init__(handlers)
        self.app_id = str(fields.get("app_id") or "").strip()
        self.app_secret = str(fields.get("app_secret") or "").strip()
        self.base = _api_base(str(fields.get("domain") or "feishu"))
        self.client = httpx.AsyncClient(timeout=30)
        self.socket: Any = None
        self.token: str | None = None
        self.token_expires_at = 0.0
        self.refreshing: asyncio.Task | None = None
        self.bot_open_id = ""
        self.ping_task: asyncio.Task | None = None
        self.service = 0
        #: 分片消息按 (sum, seq) 拼回
        self.parts: dict[str, dict[int, bytes]] = {}

    # -- 令牌 ---------------------------------------------------------------
    async def _fresh_token(self) -> str:
        if self.token is not None and time.time() < self.token_expires_at - 30:
            return self.token
        if self.refreshing is None:
            self.refreshing = asyncio.create_task(self._refresh_token())
        try:
            await self.refreshing
        finally:
            self.refreshing = None
        if self.token is None:
            raise AdapterError("飞书没有给出 tenant_access_token", permanent=True)
        return self.token

    async def _refresh_token(self) -> None:
        token, expires_at = await _tenant_token(
            self.client, self.base, self.app_id, self.app_secret
        )
        self.token = token
        self.token_expires_at = expires_at

    # -- 连接 ---------------------------------------------------------------
    async def open(self, generation: int) -> None:
        try:
            await self._fresh_token()
            info = await _api(
                self.client,
                self.base,
                await self._fresh_token(),
                "/open-apis/bot/v3/info",
                method="GET",
            )
            bot = info.get("bot") or info
            self.bot_open_id = str(bot.get("open_id") or "")
            answer = await self.client.post(
                f"{self.base}/callback/ws/endpoint",
                headers={"authorization": f"Bearer {await self._fresh_token()}"},
                json={"AppID": self.app_id, "AppSecret": self.app_secret},
            )
        except httpx.HTTPError as exc:
            raise AdapterError(f"飞书连接失败：{reason_of(exc)}") from None
        body = json_body(answer)
        data = body.get("data") or {}
        url = data.get("URL")
        if url is None or (body.get("code") is not None and body.get("code") != 0):
            message = str(body.get("msg") or f"飞书返回 {answer.status_code}")
            if body.get("code") is not None:
                message = f"{message}（code {body.get('code')}）"
            raise AdapterError(
                message,
                permanent=answer.status_code == 200
                or (400 <= answer.status_code < 500 and answer.status_code != 429),
                status=answer.status_code,
            )
        if not self.current(generation):
            return
        raw_url = str(url)
        if "service_id=" in raw_url:
            self.service = int_or(raw_url.split("service_id=")[-1].split("&")[0], 0)
        else:
            self.service = 0
        client_config = data.get("ClientConfig") or {}
        ping_every = _ping_interval(client_config.get("PingInterval")) or 90.0
        try:
            socket = await websockets.connect(
                raw_url,
                max_size=None,
                ping_interval=None,
                open_timeout=SOCKET_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - 握手失败要重连
            # 这条 URL 带 ticket / device_id，异常文本会回显完整 URI —— 必须脱敏。
            raise AdapterError(f"飞书无法建立连接：{reason_of(exc)}") from None
        if not self.current(generation):
            await socket.close()
            return
        self.socket = socket
        self.status(
            "ready",
            account=Account(id=self.bot_open_id or self.app_id, name=self.app_id),
        )
        self._start_ping(ping_every)
        self.spawn(self._read_loop(generation, socket))

    def _start_ping(self, interval: float) -> None:
        if self.ping_task is not None:
            self.ping_task.cancel()

        async def beat() -> None:
            while True:
                await asyncio.sleep(interval)
                await self._send_frame(
                    encode_frame(
                        {
                            "SeqID": 0,
                            "LogID": 0,
                            "service": self.service,
                            "method": 0,
                            "headers": [{"key": "type", "value": "ping"}],
                        }
                    )
                )

        self.ping_task = asyncio.create_task(beat())

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
                    frame = decode_frame(raw if isinstance(raw, bytes) else str(raw).encode())
                except Exception:  # noqa: BLE001 - 解析不了就跳过这一帧
                    continue
                await self._on_frame(generation, frame)
        finally:
            if self.socket is socket:
                self.socket = None
            if self.current(generation):
                self.status("connecting")
                self.retry(generation, "飞书连接已断开")

    async def _on_frame(self, generation: int, frame: dict[str, Any]) -> None:
        headers = {item.get("key"): item.get("value") for item in frame.get("headers") or []}
        kind = headers.get("type")
        if frame.get("method") == 0 and kind == "ping":
            await self._send_frame(
                encode_frame(
                    {
                        **frame,
                        "headers": [{"key": "type", "value": "pong"}],
                        "payload": b"pong",
                    }
                )
            )
            return
        if frame.get("method") == 0 and kind == "pong":
            raw_payload = (frame.get("payload") or b"").decode("utf-8", "replace")
            interval = _ping_interval(_safejson_body(raw_payload).get("PingInterval"))
            if interval is not None:
                self._start_ping(interval)
            return
        if frame.get("method") == 1 and kind == "reconnect":
            self.retry(generation, "飞书要求重连")
            return
        if frame.get("method") != 1 or kind not in ("event", "card"):
            return
        message_id = headers.get("message_id") or ""
        # header 来自网络：脏值按 1 处理，不能让一个坏帧把读循环打挂。
        total = int_or(headers.get("sum"), 1)
        seq = int_or(headers.get("seq"), 1)
        await self._send_frame(
            encode_frame(
                {
                    **frame,
                    "headers": [
                        item
                        for item in frame.get("headers") or []
                        if item.get("key") != "biz_rt"
                    ]
                    + [{"key": "biz_rt", "value": "0"}],
                    "payload": json.dumps({"code": 200, "headers": {}, "data": ""}).encode(),
                    "payloadType": "json",
                }
            )
        )
        payload = frame.get("payload") or b""
        if total > 1:
            held = self.parts.setdefault(message_id, {})
            held[seq] = payload
            if len(held) != total:
                return
            payload = b"".join(held[index] for index in sorted(held))
            self.parts.pop(message_id, None)
        try:
            body = json.loads(payload.decode("utf-8", "replace"))
        except ValueError:
            return
        if kind == "event":
            self.spawn(self._on_event(body))
        else:
            self.spawn(self._on_card(body))

    async def _send_frame(self, frame: bytes) -> None:
        socket = self.socket
        if socket is None:
            return
        try:
            await socket.send(frame)
        except Exception:  # noqa: BLE001 - 正在断开的连接发不出去只影响这一帧
            return

    async def _on_event(self, body: dict[str, Any]) -> None:
        if (body.get("header") or {}).get("event_type") != "im.message.receive_v1":
            return
        event = body.get("event") or {}
        message = event.get("message") or {}
        direct = message.get("chat_type") == "p2p"
        mentions = message.get("mentions") or []
        mentioned = not direct and any(
            (item.get("id") or {}).get("open_id") == self.bot_open_id
            or item.get("key") == "@_user_1"
            for item in mentions
        )
        content = _safejson_body(str(message.get("content") or "{}"))
        text = _MENTION_TOKEN.sub("", str(content.get("text") or "")).strip()
        images: list[InboundFile] = []
        notes: list[str] = []
        if message.get("message_type") == "image" and content.get("image_key"):
            image_key = content["image_key"]
            message_id = message.get("message_id")
            url = (
                f"{self.base}/open-apis/im/v1/messages/{message_id}"
                f"/resources/{image_key}?type=image"
            )
            try:
                token = await self._fresh_token()
                response = await self.client.get(
                    url, headers={"authorization": f"Bearer {token}"}
                )
                if response.status_code < 400:
                    images.append(
                        InboundFile(
                            name=f"{image_key}.jpg",
                            data=response.content,
                            media_type=_image_media_type(response.content),
                        )
                    )
                else:
                    notes.append(
                        f"[飞书图片无法下载：HTTP {response.status_code}，请告诉用户]"
                    )
            except (httpx.HTTPError, AdapterError) as exc:
                # 不能静默：纯图片消息的正文本来就是空的，丢掉图 = 整条消息消失。
                notes.append(f"[飞书图片无法下载：{reason_of(exc)}，请告诉用户]")
        if notes:
            text = "\n".join(part for part in [text, *notes] if part)
        sender_info = event.get("sender") or {}
        sender = (
            (sender_info.get("sender_id") or {}).get("open_id")
            or sender_info.get("user_id")
            or ""
        )
        await self.handlers.on_message(
            InboundMessage(
                chat=ChatRef(
                    key=str(message.get("chat_id") or ""),
                    kind="direct" if direct else "group",
                    title=str(message.get("chat_id") or ""),
                ),
                sender=Sender(id=str(sender), name=str(sender)),
                text=text,
                images=images,
                mentioned=mentioned,
                message_id=str(message.get("message_id") or ""),
            )
        )

    async def _on_card(self, body: dict[str, Any]) -> None:
        event = body.get("event") or {}
        action = event.get("action") or (event.get("actions") or [{}])[0] or {}
        data = (
            action.get("value")
            or (action.get("action_data") or {}).get("value")
            or action.get("action_data")
            or {}
        )
        if isinstance(data, dict):
            raw = data.get("v") or json.dumps(data, ensure_ascii=False)
        else:
            raw = str(data)
        context = event.get("context") or {}
        chat_key = context.get("open_chat_id") or (event.get("message") or {}).get("chat_id")
        operator = (event.get("operator") or {}).get("open_id") or event.get("user_id")
        message_id = event.get("message_id") or context.get("open_message_id")
        await self.handlers.on_action(
            InboundAction(
                chat=ChatRef(key=str(chat_key or "")),
                sender=Sender(id=str(operator or "")),
                data=raw,
                message_id=str(message_id or ""),
            )
        )

    # -- 出站 ---------------------------------------------------------------
    async def send(self, chat: ChatRef, message: OutboundMessage) -> None:
        chunks = chunk_markdown(message.text, TEXT_LIMIT)
        for index, chunk in enumerate(chunks):
            interactive = bool(message.buttons) and index == len(chunks) - 1
            if interactive:
                content = _card(chunk, message.buttons)
            else:
                content = json.dumps({"text": chunk}, ensure_ascii=False)
            body = {
                "receive_id": chat.key,
                "msg_type": "interactive" if interactive else "text",
                "content": content,
            }
            await _api(
                self.client,
                self.base,
                await self._fresh_token(),
                "/open-apis/im/v1/messages?receive_id_type=chat_id",
                method="POST",
                body=body,
            )

    async def stop(self) -> None:
        # 先置位 stopped：读循环的 finally 就不会把「主动停机」误报成掉线并重连。
        await super().stop()
        if self.ping_task is not None:
            self.ping_task.cancel()
            self.ping_task = None
        if self.refreshing is not None:
            self.refreshing.cancel()
            self.refreshing = None
        socket = self.socket
        self.socket = None
        if socket is not None:
            try:
                await socket.close()
            except Exception:  # noqa: BLE001 - 已断开的 socket 关不掉也无所谓
                pass
        await self.client.aclose()


_MENTION_TOKEN = re.compile(r"@_user_\d+")


def _card(text: str, buttons: list[Any] | None) -> str:
    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": text}]
    if buttons:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "type": "primary",
                        "text": {"tag": "plain_text", "content": button.label[:20]},
                        "value": {"v": button.data},
                    }
                    for button in buttons[:5]
                ],
            }
        )
    return json.dumps({"schema": "2.0", "body": {"elements": elements}}, ensure_ascii=False)


def _image_media_type(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _safejson_body(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


async def _tenant_token(
    client: httpx.AsyncClient, base: str, app_id: str, app_secret: str
) -> tuple[str, float]:
    try:
        response = await client.post(
            f"{base}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
        )
    except httpx.HTTPError as exc:
        raise AdapterError(f"飞书连接失败：{reason_of(exc)}") from None
    body = json_body(response)
    if body.get("tenant_access_token") is None:
        raise AdapterError(
            str(body.get("msg") or "飞书拒绝了这组凭据"),
            permanent=response.status_code < 500 and response.status_code != 429,
            status=response.status_code,
        )
    expire = int_or(body.get("expire"), 7200)
    return str(body["tenant_access_token"]), time.time() + max(60, expire - 300)


async def _api(
    client: httpx.AsyncClient,
    base: str,
    token: str,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        response = await client.request(
            method,
            f"{base}{path}",
            headers={
                "authorization": f"Bearer {token}",
                "content-type": "application/json",
            },
            content=(
                None
                if body is None
                else json.dumps(body, ensure_ascii=False).encode("utf-8")
            ),
        )
    except httpx.HTTPError as exc:
        raise AdapterError(f"飞书连接失败：{reason_of(exc)}") from None
    data = json_body(response)
    failed = response.status_code >= 400
    if failed or (data.get("code") is not None and data.get("code") != 0):
        raise AdapterError(
            str(data.get("msg") or f"飞书返回 {response.status_code}"),
            status=response.status_code,
            permanent=(
                400 <= response.status_code < 500 and response.status_code != 429
            ),
        )
    return data


__all__ = ["FeishuAdapter", "decode_frame", "encode_frame"]
