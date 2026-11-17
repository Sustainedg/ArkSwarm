"""企业微信「智能机器人」适配器：一条常驻 WebSocket。

协议是 ``{ cmd, headers: { req_id }, body }``：``aibot_subscribe`` 完成鉴权、``ping`` 心跳、
``aibot_msg_callback`` 送进消息（用回调的 ``req_id`` 走 ``aibot_respond_msg`` 被动回复），
更晚的消息用 ``aibot_send_msg`` 主动发。模板卡片按钮从 ``aibot_event_callback`` 回来。
"""
from __future__ import annotations

import asyncio
import hashlib
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
    InboundAction,
    InboundFile,
    InboundMessage,
    OutboundMessage,
    Sender,
    int_or,
    json_body,
    reason_of,
)
from ..crypto import b64, decrypt_aes256_cbc
from ..format import chunk_markdown

WSS = "wss://openws.work.weixin.qq.com"
TEXT_LIMIT = 4000
#: 附件整体读进内存再交出去，所以给个上限。
FILE_LIMIT = 100 * 1024 * 1024
IMAGE_LIMIT = 20 * 1024 * 1024


class WeComAdapter(Adapter):
    id = "wecom"

    async def check(self, fields: dict[str, Any]) -> Account:
        bot_id = str(fields.get("bot_id") or "").strip()
        secret = str(fields.get("secret") or "").strip()
        if bot_id == "" or secret == "":
            raise AdapterError("请填写 Bot ID 与 Secret")
        try:
            socket = await websockets.connect(
                WSS, max_size=None, ping_interval=None, open_timeout=15
            )
        except Exception as exc:  # noqa: BLE001 - 握手失败就是凭据 / 网络问题
            raise AdapterError(f"企业微信无法连接：{reason_of(exc)}") from None
        try:
            await socket.send(
                json.dumps(
                    {
                        "cmd": "aibot_subscribe",
                        "headers": {"req_id": "check-1"},
                        "body": {"secret": secret, "bot_id": bot_id},
                    }
                )
            )
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                left = max(0.1, deadline - time.monotonic())
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=left)
                except asyncio.TimeoutError:
                    break
                try:
                    packet = json.loads(raw)
                except ValueError:
                    continue
                code = packet.get("errcode")
                if code is None:
                    continue
                if int_or(code, -1) == 0:
                    return Account(id=bot_id, name=bot_id)
                raise AdapterError(str(packet.get("errmsg") or f"企业微信返回 {code}"))
            raise AdapterError("企业微信没有回应这次订阅")
        finally:
            try:
                await socket.close()
            except Exception:  # noqa: BLE001 - 探测用的连接关不掉也无所谓
                pass

    async def connect(
        self,
        fields: dict[str, Any],
        handlers: Handlers,
        runtime: dict[str, Any] | None = None,
    ) -> ConnectionBase:
        return WeComConnection(fields, handlers, runtime or {})


class WeComConnection(ConnectionBase):
    def __init__(self, fields: dict[str, Any], handlers: Handlers, runtime: dict[str, Any]) -> None:
        super().__init__(handlers)
        self.bot_id = str(fields.get("bot_id") or "").strip()
        self.secret = str(fields.get("secret") or "").strip()
        self.client = httpx.AsyncClient(timeout=60)
        self.socket: Any = None
        self.counter = 0
        self.last_error: str | None = None
        #: 每个聊天最近一次回调的 req_id（被动回复要带它）
        self.last_req_id: dict[str, str] = {}
        self.pending: dict[str, asyncio.Future] = {}
        self.ping_task: asyncio.Task | None = None

    def _next_req_id(self) -> str:
        self.counter += 1
        return f"r{self.counter}"

    async def open(self, generation: int) -> None:
        try:
            socket = await websockets.connect(
                WSS, max_size=None, ping_interval=None, open_timeout=20
            )
        except Exception as exc:  # noqa: BLE001 - 握手失败要重连
            raise AdapterError(f"企业微信无法建立连接：{reason_of(exc)}") from None
        if not self.current(generation):
            await socket.close()
            return
        self.socket = socket
        self.status("connecting")
        await self._send_frame(
            {
                "cmd": "aibot_subscribe",
                "headers": {"req_id": self._next_req_id()},
                "body": {"secret": self.secret, "bot_id": self.bot_id},
            }
        )
        self._start_ping()
        self.spawn(self._read_loop(generation, socket))

    def _start_ping(self) -> None:
        if self.ping_task is not None:
            self.ping_task.cancel()

        async def beat() -> None:
            while True:
                await asyncio.sleep(30)
                await self._send_frame({"cmd": "ping", "headers": {"req_id": self._next_req_id()}})

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
                    packet = json.loads(raw)
                except ValueError:
                    continue
                await self._on_packet(packet)
        finally:
            if self.socket is socket:
                self.socket = None
            if self.current(generation):
                self.status("connecting")
                self.retry(generation, self.last_error or "企业微信连接已断开")

    async def _on_packet(self, packet: dict[str, Any]) -> None:
        code = packet.get("errcode")
        if code is not None:
            req_id = str((packet.get("headers") or {}).get("req_id") or "")
            held = self.pending.pop(req_id, None)
            if int_or(code, -1) != 0:
                message = str(packet.get("errmsg") or f"企业微信返回 {code}")
                self.last_error = message
                self.status("error", message)
                if held is not None and not held.done():
                    held.set_exception(AdapterError(message, permanent=True))
                return
            self.last_error = None
            self.status("ready", account=Account(id=self.bot_id, name=self.bot_id))
            if held is not None and not held.done():
                held.set_result(packet.get("body"))
            return
        cmd = packet.get("cmd")
        headers = packet.get("headers") or {}
        body = packet.get("body") or {}
        if cmd == "aibot_msg_callback":
            self.spawn(self._on_callback(headers, body))
        elif cmd == "aibot_event_callback":
            self.spawn(self._on_event(headers, body))

    async def _send_frame(self, packet: dict[str, Any]) -> None:
        socket = self.socket
        if socket is None:
            return
        try:
            await socket.send(json.dumps(packet))
        except Exception:  # noqa: BLE001 - 正在断开的连接发不出去只影响这一帧
            return

    async def _on_callback(self, headers: dict[str, Any], body: dict[str, Any]) -> None:
        direct = body.get("chattype") == "single"
        if direct:
            key = str((body.get("from") or {}).get("userid") or "")
        else:
            key = str(body.get("chatid") or "")
        self.last_req_id[key] = str(headers.get("req_id") or "")
        text = str(
            (body.get("text") or {}).get("content")
            or (body.get("voice") or {}).get("content")
            or ""
        ).strip()
        images: list[InboundFile] = []
        files: list[InboundFile] = []
        notes: list[str] = []
        msgtype = body.get("msgtype")
        if msgtype == "image" and (body.get("image") or {}).get("url"):
            got = await _download(self.client, body["image"], IMAGE_LIMIT)
            if isinstance(got, InboundFile):
                images.append(got)
            else:
                notes.append(got)
        if msgtype in ("file", "video"):
            media = body.get(msgtype) or {}
            if media.get("url"):
                got = await _download(self.client, media, FILE_LIMIT)
                if isinstance(got, InboundFile):
                    files.append(got)
                else:
                    notes.append(got)
        mixed_text: list[str] = []
        if msgtype == "mixed":
            for item in (body.get("mixed") or {}).get("msg_item") or []:
                if (item or {}).get("msgtype") == "text":
                    piece = str((item.get("text") or {}).get("content") or "").strip()
                    if piece:
                        mixed_text.append(piece)
                    continue
                image = (item or {}).get("image") or {}
                if (item or {}).get("msgtype") != "image" or not image.get("url"):
                    continue
                got = await _download(self.client, image, IMAGE_LIMIT)
                if isinstance(got, InboundFile):
                    images.append(got)
                else:
                    notes.append(got)
        words = "\n".join(part for part in [text, *mixed_text, *notes] if part)
        sender = body.get("from") or {}
        sender_id = str(sender.get("userid") or "")
        await self.handlers.on_message(
            InboundMessage(
                chat=ChatRef(key=key, kind="direct" if direct else "group"),
                sender=Sender(
                    id=sender_id, name=str(sender.get("name") or sender_id)
                ),
                text=words,
                images=images,
                files=files,
                mentioned=not direct and body.get("mention_robot") is True,
                message_id=str(body.get("msgid") or ""),
            )
        )

    async def _on_event(self, headers: dict[str, Any], body: dict[str, Any]) -> None:
        event = body.get("event") or {}
        card_event = "template_card_event"
        if (
            event.get("event_type") != card_event
            and body.get("event_type") != card_event
        ):
            return
        card = event.get("template_card_event") or body.get(card_event) or {}
        direct = body.get("chattype") == "single"
        from_user = (body.get("from") or {}).get("userid")
        if direct:
            key = str(from_user or "")
        else:
            key = str(body.get("chatid") or card.get("chatid") or "")
        await self.handlers.on_action(
            InboundAction(
                chat=ChatRef(key=key, kind="direct" if direct else "group"),
                sender=Sender(id=str(from_user or card.get("userid") or "")),
                data=str(card.get("event_key") or card.get("key") or ""),
                message_id=str(body.get("msgid") or ""),
            )
        )

    # -- 出站 ---------------------------------------------------------------
    async def send(self, chat: ChatRef, message: OutboundMessage) -> None:
        chunks = chunk_markdown(message.text, TEXT_LIMIT)
        for index, chunk in enumerate(chunks):
            body: dict[str, Any] = {"msgtype": "markdown", "markdown": {"content": chunk}}
            if index == len(chunks) - 1 and message.buttons:
                body = {
                    "msgtype": "template_card",
                    "template_card": {
                        "card_type": "text_notice",
                        "main_title": {"title": chunk[:100]},
                        "card_action": {"type": 2, "url": "https://work.weixin.qq.com"},
                        "button_selection": {
                            "question_key": "choice",
                            "option_list": [
                                {"id": button.data, "text": button.label[:30]}
                                for button in message.buttons[:10]
                            ],
                        },
                    },
                }
            req_id = self.last_req_id.get(chat.key)
            if req_id is not None:
                await self._send_frame(
                    {
                        "cmd": "aibot_respond_msg",
                        "headers": {"req_id": req_id},
                        "body": body,
                    }
                )
                continue
            payload = {
                **body,
                "chatid": chat.key if chat.kind == "group" else None,
                "userid": chat.key if chat.kind == "direct" else None,
            }
            await self._request("aibot_send_msg", payload)

    async def _request(self, cmd: str, body: dict[str, Any]) -> Any:
        req_id = self._next_req_id()
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self.pending[req_id] = future
        await self._send_frame({"cmd": cmd, "headers": {"req_id": req_id}, "body": body})
        try:
            return await asyncio.wait_for(future, timeout=15)
        except asyncio.TimeoutError:
            raise AdapterError("企业微信没有回应这次发送") from None
        finally:
            self.pending.pop(req_id, None)

    async def stop(self) -> None:
        # 先置位 stopped：读循环的 finally 就不会把「主动停机」误报成掉线并重连。
        await super().stop()
        if self.ping_task is not None:
            self.ping_task.cancel()
            self.ping_task = None
        for future in list(self.pending.values()):
            if not future.done():
                future.cancel()
        self.pending.clear()
        socket = self.socket
        self.socket = None
        if socket is not None:
            try:
                await socket.close()
            except Exception:  # noqa: BLE001 - 已断开的 socket 关不掉也无所谓
                pass
        await self.client.aclose()


async def _download(
    client: httpx.AsyncClient, medium: dict[str, Any], limit: int
) -> InboundFile | str:
    """下载并解密企业微信的媒体。

    失败时返回一句给模型看的说明（而不是静默丢掉）——否则「只发了一张图」的消息
    会整条消失，用户完全不知道发生了什么。
    """
    url = str(medium.get("url") or "")
    if url == "":
        return "[企业微信附件无法下载：没有下载地址，请告诉用户]"
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        return f"[企业微信附件无法下载：{reason_of(exc)}，请告诉用户]"
    if response.status_code >= 400:
        return f"[企业微信附件无法下载：HTTP {response.status_code}，请告诉用户]"
    if len(response.content) > limit:
        return "[企业微信附件无法下载：超过大小上限，请告诉用户]"
    key = b64(medium.get("aeskey"))
    if len(key) != 32:
        return "[企业微信附件无法下载：密钥无效，请告诉用户]"
    data = decrypt_aes256_cbc(response.content, key)
    # 文件名按 URL 的内容摘要派生：``hash()`` 每个进程都不同，重启后同一附件会换名。
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    name = f"wecom-{digest}.bin"
    media_type = str(medium.get("content_type") or "")
    if media_type.startswith("image/"):
        name = f"wecom-{digest}.jpg"
    return InboundFile(name=name, data=data, media_type=media_type.split(";")[0])


__all__ = ["WeComAdapter"]
