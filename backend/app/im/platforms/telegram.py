"""Telegram Bot API 适配器：getUpdates 长轮询收，sendMessage 发。

只用官方 Bot API，不需要公网地址或开放端口；``runtime`` 里保存 update 游标，
重启后从上次的位置接着收，不会把旧消息再回一遍。
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

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
    RuntimeState,
    Sender,
    reason_of,
    retry_delay,
    sleep,
)
from ..format import chunk_telegram

CLOUD_API = "https://api.telegram.org"
TEXT_LIMIT = 4096
POLL_TIMEOUT = 50
#: 云端 Bot API 的 getFile 上限是 20 MB。
MAX_FILE_BYTES = 20 * 1024 * 1024
UPDATES = ["message", "callback_query"]


def _name_of(from_: dict[str, Any] | None) -> str:
    data = from_ or {}
    name = " ".join(
        part for part in (data.get("first_name"), data.get("last_name")) if part
    ).strip()
    if data.get("username"):
        return f"@{data['username']}"
    return name or str(data.get("id", ""))


class TelegramAdapter(Adapter):
    id = "telegram"

    async def check(self, fields: dict[str, Any]) -> Account:
        token = str(fields.get("token") or "").strip()
        if token == "":
            raise AdapterError("请先填写 Bot token")
        async with httpx.AsyncClient(timeout=20) as client:
            body = await _call(client, token, "getMe", {})
        if not isinstance(body.get("id"), int) or not isinstance(body.get("username"), str):
            raise AdapterError("Telegram 没有说明这个 token 属于谁")
        return Account(id=str(body["id"]), name=f"@{body['username']}")

    async def connect(
        self,
        fields: dict[str, Any],
        handlers: Handlers,
        runtime: dict[str, Any] | None = None,
    ) -> ConnectionBase:
        return TelegramConnection(fields, handlers, runtime or {})


class TelegramConnection(ConnectionBase):
    def __init__(self, fields: dict[str, Any], handlers: Handlers, runtime: dict[str, Any]) -> None:
        super().__init__(handlers)
        self.token = str(fields.get("token") or "").strip()
        self.state = RuntimeState(runtime)
        self.self_name = ""
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=20.0))

    async def open(self, generation: int) -> None:
        me = await self._call("getMe", {})
        if not self.current(generation):
            return
        self.self_name = f"@{me['username']}" if me.get("username") else ""
        account_id = str(me.get("id", ""))
        self.status(
            "ready",
            account=Account(id=account_id, name=self.self_name or account_id),
        )
        self.spawn(self._poll(generation))

    async def _poll(self, generation: int) -> None:
        attempt = 0
        while self.current(generation):
            try:
                updates = await self._call(
                    "getUpdates",
                    {
                        "offset": self.state.get().get("offset", 0),
                        "timeout": POLL_TIMEOUT,
                        "allowed_updates": UPDATES,
                    },
                    timeout=(POLL_TIMEOUT + 15) * 1000,
                )
            except AdapterError as exc:
                if not self.current(generation):
                    return
                if exc.permanent:
                    self.status("error", str(exc))
                    return
                self.status("retrying", str(exc))
                await sleep(exc.retry_after or retry_delay(attempt))
                attempt += 1
                continue
            attempt = 0
            for update in updates if isinstance(updates, list) else []:
                if not self.current(generation):
                    return
                if isinstance(update.get("update_id"), int):
                    self.state.set(offset=update["update_id"] + 1)
                try:
                    await self._on_update(update)
                except Exception as exc:  # noqa: BLE001 - 单条消息失败不能拖垮整条轮询
                    self.status("retrying", reason_of(exc))
            if self.current(generation):
                self.status("ready")

    async def _on_update(self, update: dict[str, Any]) -> None:
        if "message" in update:
            await self._on_message(update["message"])
            return
        if "callback_query" in update:
            query = update["callback_query"]
            message = query.get("message") or {}
            chat = message.get("chat") or {}
            from_ = query.get("from") or {}
            try:
                await self._call("answerCallbackQuery", {"callback_query_id": query.get("id")})
            except AdapterError:
                pass  # 回答按钮只是让客户端转个圈，失败不影响消息本身
            await self.handlers.on_action(
                InboundAction(
                    chat=ChatRef(
                        key=str(chat.get("id", "")),
                        kind="direct" if chat.get("type") == "private" else "group",
                        title=str(chat.get("title") or _name_of(from_)),
                    ),
                    sender=Sender(id=str(from_.get("id", "")), name=_name_of(from_)),
                    data=str(query.get("data") or ""),
                    message_id=str(message.get("message_id", "")),
                )
            )

    async def _on_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        from_ = message.get("from") or {}
        kind = "direct" if chat.get("type") == "private" else "group"
        raw = str(message.get("text") or message.get("caption") or "")
        mentioned = kind == "group" and self._mentioned(message, raw)
        images, files, notes = await self._media(message)
        body = self._strip(raw) if kind == "group" else raw
        text = "\n".join(part for part in [body, *notes] if part)
        await self.handlers.on_message(
            InboundMessage(
                chat=ChatRef(
                    key=str(chat.get("id", "")),
                    kind=kind,
                    title=str(chat.get("title") or _name_of(from_)),
                ),
                sender=Sender(id=str(from_.get("id", "")), name=_name_of(from_)),
                text=text,
                images=images,
                files=files,
                mentioned=mentioned,
                message_id=str(message.get("message_id", "")),
            )
        )

    def _mentioned(self, message: dict[str, Any], raw: str) -> bool:
        if self.self_name and self.self_name in raw:
            return True
        for entity in message.get("entities") or []:
            if entity.get("type") == "mention":
                start = int(entity.get("offset", 0))
                end = start + int(entity.get("length", 0))
                if raw[start:end] == self.self_name:
                    return True
        replied = (message.get("reply_to_message") or {}).get("from") or {}
        replied_name = replied.get("username")
        if (
            self.self_name
            and replied.get("is_bot") is True
            and replied_name == self.self_name.lstrip("@")
        ):
            return True
        return False

    def _strip(self, text: str) -> str:
        if self.self_name == "":
            return text.strip()
        return " ".join(text.replace(self.self_name, " ").split())

    async def _media(
        self, message: dict[str, Any]
    ) -> tuple[list[InboundFile], list[InboundFile], list[str]]:
        images: list[InboundFile] = []
        files: list[InboundFile] = []
        notes: list[str] = []
        photos = message.get("photo")
        if isinstance(photos, list) and photos:
            best = max(photos, key=lambda item: int(item.get("file_size") or 0))
            got = await self._fetch(best, "photo")
            if isinstance(got, InboundFile):
                images.append(got)
            else:
                notes.append(got)
        picked = None
        for kind in ("video", "animation", "video_note", "document", "voice", "audio"):
            if (message.get(kind) or {}).get("file_id") is not None:
                picked = (kind, message[kind])
                break
        if picked is not None:
            kind, item = picked
            got = await self._fetch(item, kind)
            if isinstance(got, InboundFile):
                files.append(got)
            else:
                notes.append(got)
        return images, files, notes

    async def _fetch(self, item: dict[str, Any], kind: str) -> InboundFile | str:
        """下载一个附件；失败时返回一句给模型看的说明（不抛错，消息照样进智能体）。"""
        label = f'{kind} "{item["file_name"]}"' if item.get("file_name") else kind
        size = int(item.get("file_size") or 0)
        if size > MAX_FILE_BYTES:
            return f"[Telegram {label} 无法下载：超过 Bot API 的 20 MB 上限，请告诉用户]"
        try:
            info = await self._call("getFile", {"file_id": item["file_id"]})
            path = info.get("file_path")
            if not path:
                raise AdapterError("Telegram 没有给出文件路径")
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.get(f"{CLOUD_API}/file/bot{self.token}/{path}")
            if response.status_code >= 400:
                raise AdapterError(f"Telegram 返回 {response.status_code}")
            name = item.get("file_name") or str(path).rsplit("/", 1)[-1]
            header = response.headers.get("content-type") or ""
            media_type = "image/jpeg" if kind == "photo" else header
            return InboundFile(
                name=str(name),
                data=response.content,
                media_type=media_type.split(";")[0],
            )
        except (AdapterError, httpx.HTTPError) as exc:
            return f"[Telegram {label} 无法下载：{reason_of(exc)}，请告诉用户]"

    async def _call(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        timeout: float = 30_000,
    ) -> Any:
        """调用 Bot API。

        Telegram 只认路径里的 token（``/bot<token>/<method>``），无法改成 header；
        因此这里依赖两处兜底：``main.py`` 把 ``httpx`` 日志压到 WARNING（不再回显 URL），
        以及异常文本统一走 :func:`reason_of` 脱敏。
        """
        if self.stopped:
            raise AdapterError("连接已停止", permanent=True)
        try:
            response = await self.client.post(
                f"{CLOUD_API}/bot{self.token}/{method}",
                json=payload,
                timeout=timeout / 1000,
            )
        except httpx.HTTPError as exc:
            raise AdapterError(f"Telegram 连接失败：{reason_of(exc)}") from None
        body: dict[str, Any] = {}
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not body.get("ok"):
            status = body.get("error_code") or response.status_code
            description = str(body.get("description") or f"Telegram 返回 {response.status_code}")
            retry_after = (body.get("parameters") or {}).get("retry_after")
            permanent = isinstance(status, int) and 400 <= status < 500 and status != 429
            if status == 409:
                description = "这个机器人被 webhook 或另一个程序占用了"
            raise AdapterError(
                description,
                permanent=permanent,
                retry_after=float(retry_after) if retry_after is not None else None,
                status=status if isinstance(status, int) else None,
            )
        return body.get("result")

    async def send(self, chat: ChatRef, message: OutboundMessage) -> None:
        chunks = chunk_telegram(message.text, TEXT_LIMIT)
        if not chunks:
            return
        for index, (html, plain) in enumerate(chunks):
            markup: dict[str, Any] = {}
            if index == len(chunks) - 1 and message.buttons:
                markup = {
                    "reply_markup": {
                        "inline_keyboard": [
                            [{"text": button.label, "callback_data": button.data}]
                            for button in message.buttons
                        ]
                    }
                }
            payload = {"chat_id": chat.key, "text": html, "parse_mode": "HTML", **markup}
            try:
                await self._call("sendMessage", payload)
            except AdapterError as exc:
                # Telegram 解析不了实体时，同一段文字退成纯文本再发一次。
                if "parse entities" in str(exc).lower() and plain != html:
                    await self._call("sendMessage", {"chat_id": chat.key, "text": plain, **markup})
                else:
                    raise

    async def typing(self, chat: ChatRef) -> None:
        try:
            await self._call("sendChatAction", {"chat_id": chat.key, "action": "typing"})
        except AdapterError:
            return None

    async def stop(self) -> None:
        await super().stop()
        await self.client.aclose()


async def _call(client: httpx.AsyncClient, token: str, method: str, payload: dict[str, Any]) -> Any:
    """建连接前的一次性调用（``check`` 用）。"""
    try:
        response = await client.post(f"{CLOUD_API}/bot{token}/{method}", json=payload)
    except httpx.HTTPError as exc:
        raise AdapterError(f"Telegram 连接失败：{reason_of(exc)}") from None
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not body.get("ok"):
        raise AdapterError(str(body.get("description") or f"Telegram 返回 {response.status_code}"))
    return body.get("result")


__all__ = ["TelegramAdapter"]
