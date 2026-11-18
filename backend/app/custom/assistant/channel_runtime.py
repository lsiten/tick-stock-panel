"""Runtime bridge from external chat channels into the local AI assistant.

The bridge keeps platform transport concerns outside ``chat_service``.  Each
inbound message is normalized to a short assistant history, executed by the
same local Codex/Pi path as the web drawer, and sent back through the platform
adapter.  Transport failures are isolated and reconnectable.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import websockets

from app.custom.assistant.chat_service import chat_stream
from app.custom.assistant.channels import _required_values
from app.services import preferences

logger = logging.getLogger(__name__)


class AssistantChannelRuntime:
    def __init__(self, app_state: Any) -> None:
        self.app_state = app_state
        self._tasks: dict[str, asyncio.Task] = {}
        self._histories: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
        self._stopped = False

    async def start(self) -> None:
        self._stopped = False
        await self.refresh()

    async def stop(self) -> None:
        self._stopped = True
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def refresh(self) -> None:
        await self.stop()
        if self._stopped:
            self._stopped = False
        statuses = preferences.load().get("assistant_message_channel_status", {})
        if _required_values("telegram") and statuses.get("telegram") == "connected":
            self._tasks["telegram"] = asyncio.create_task(self._telegram_loop(), name="assistant-telegram")
        if _required_values("feishu") and statuses.get("feishu") == "connected":
            self._tasks["feishu"] = asyncio.create_task(self._feishu_loop(), name="assistant-feishu")
        if _required_values("slack") and statuses.get("slack") == "connected":
            self._tasks["slack"] = asyncio.create_task(self._slack_loop(), name="assistant-slack")
        if _required_values("dingtalk") and statuses.get("dingtalk") == "connected":
            self._tasks["dingtalk"] = asyncio.create_task(self._dingtalk_loop(), name="assistant-dingtalk")

    async def handle_inbound(
        self,
        platform: str,
        conversation_id: str,
        text: str,
        *,
        sender: str = "",
        reply: Any = None,
    ) -> str:
        content = str(text or "").strip()
        if not content:
            return ""
        key = (platform, conversation_id)
        if content == "/new" or content.startswith("/new "):
            self._histories.pop(key, None)
            content = content[4:].strip()
            if not content:
                return "已创建新对话。"
        elif content == "/clear" or content.startswith("/clear "):
            self._histories.pop(key, None)
            content = content[6:].strip()
            if not content:
                return "已清空当前对话上下文。"
        history = self._histories[key]
        history.append({"role": "user", "content": content})
        history[:] = history[-16:]
        engine = getattr(self.app_state, "strategy_engine", None)
        repo = getattr(self.app_state, "repo", None)
        output: list[str] = []
        try:
            async for line in chat_stream(
                history=history,
                context={"page": f"message:{platform}", "symbol": ""},
                engine=engine,
                data_dir=str(repo.store.data_dir) if repo is not None else None,
                repo=repo,
                quote_service=getattr(self.app_state, "quote_service", None),
                depth_service=getattr(self.app_state, "depth_service", None),
                capabilities=getattr(self.app_state, "capabilities", None),
                financial_scheduler=getattr(self.app_state, "financial_scheduler", None),
            ):
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if event.get("type") == "delta":
                    output.append(str(event.get("content") or ""))
                elif event.get("type") == "error":
                    output.append(str(event.get("message") or "AI 助手处理失败"))
        except Exception as exc:  # noqa: BLE001
            logger.exception("%s assistant AI generation failed: %s", platform, exc)
            output.append(f"AI 助手处理失败: {exc}")
        answer = "".join(output).strip() or "AI 助手没有生成可发送的回复。"
        history.append({"role": "assistant", "content": answer})
        history[:] = history[-16:]
        if reply is not None:
            await reply(answer)
        return answer

    async def _telegram_loop(self) -> None:
        offset = 0
        while not self._stopped:
            values = _required_values("telegram")
            token = values.get("bot_token", "")
            if not token:
                return
            try:
                async with httpx.AsyncClient(timeout=40) as client:
                    response = await client.get(
                        f"https://api.telegram.org/bot{token}/getUpdates",
                        params={"timeout": 30, "offset": offset, "allowed_updates": json.dumps(["message"])},
                    )
                    data = response.json()
                    if not data.get("ok"):
                        raise RuntimeError(str(data.get("description") or "Telegram getUpdates failed"))
                    for update in data.get("result", []):
                        offset = max(offset, int(update.get("update_id", 0)) + 1)
                        message = update.get("message") or {}
                        text = str(message.get("text") or "").strip()
                        chat = message.get("chat") or {}
                        if not text or not chat.get("id"):
                            continue
                        if chat.get("type") in {"group", "supergroup"} and "@" not in text:
                            continue
                        await self.handle_inbound(
                            "telegram", str(chat["id"]), text,
                            sender=str((message.get("from") or {}).get("id") or ""),
                            reply=lambda answer, chat_id=chat["id"], message_id=message.get("message_id"): self._telegram_send(token, chat_id, answer, message_id),
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("Telegram assistant loop failed: %s", exc)
                await asyncio.sleep(5)

    async def _feishu_loop(self) -> None:
        """飞书 PersonalAgent 长连接，接收事件并确认二进制数据帧。"""
        while not self._stopped:
            values = _required_values("feishu")
            app_id, app_secret = values.get("app_id", ""), values.get("app_secret", "")
            if not app_id or not app_secret:
                return
            try:
                endpoint = await self._feishu_endpoint(app_id, app_secret)
                ping_interval = max(10, int(endpoint["ping_interval"]))
                async with websockets.connect(endpoint["url"], ping_interval=20, close_timeout=5) as ws:
                    logger.info("Feishu assistant long connection established")
                    while not self._stopped:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=ping_interval)
                        except asyncio.TimeoutError:
                            await ws.send(_encode_frame(service=endpoint["service_id"], frame_type="ping"))
                            continue
                        if isinstance(raw, str):
                            logger.warning("Feishu assistant dropped text websocket frame")
                            continue
                        frame = _decode_frame(raw)
                        if frame is None:
                            logger.warning("Feishu assistant dropped malformed websocket frame")
                            continue
                        frame_type = frame["headers"].get("type", "")
                        if frame["method"] == 0:
                            if frame_type == "ping":
                                await ws.send(_encode_frame(service=endpoint["service_id"], frame_type="pong"))
                            continue
                        if frame["method"] != 1:
                            continue
                        await ws.send(_encode_ack(frame, ok=True))
                        event = _decode_feishu_message(frame["payload"])
                        if event is None:
                            continue
                        logger.info("Feishu assistant received a message chat_id=%s", event["chat_id"])
                        asyncio.create_task(
                            self._handle_feishu_message(event),
                            name="assistant-feishu-message",
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("Feishu assistant long connection failed: %s", exc)
                await asyncio.sleep(5)

    async def _handle_feishu_message(self, event: dict[str, str]) -> None:
        try:
            await self.handle_inbound(
                "feishu",
                event["chat_id"],
                event["text"],
                sender=event["sender_id"],
                reply=lambda answer, cid=event["chat_id"]: self.feishu_send(cid, answer),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Feishu assistant failed to reply chat_id=%s: %s", event["chat_id"], exc)

    async def _feishu_endpoint(self, app_id: str, app_secret: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                "https://open.feishu.cn/callback/ws/endpoint",
                json={"AppID": app_id, "AppSecret": app_secret},
            )
            response.raise_for_status()
            data = response.json()
        endpoint = data.get("data") or {}
        if data.get("code") != 0 or not endpoint.get("URL"):
            raise RuntimeError(str(data.get("msg") or "飞书长连接地址获取失败"))
        config = endpoint.get("ClientConfig") or {}
        service_id = int(parse_qs(urlparse(endpoint["URL"]).query).get("service_id", ["0"])[0])
        if not service_id:
            raise RuntimeError("飞书长连接响应缺少 service_id")
        return {
            "url": endpoint["URL"],
            "service_id": service_id,
            "ping_interval": int(config.get("PingInterval") or 90),
        }

    async def _telegram_send(self, token: str, chat_id: int | str, text: str, reply_to: int | None = None) -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            payload: dict[str, Any] = {"chat_id": chat_id, "text": text[:4096]}
            if reply_to:
                payload["reply_parameters"] = {"message_id": reply_to}
            await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)

    async def _slack_loop(self) -> None:
        """Slack Socket Mode loop for apps using an xapp app token."""
        while not self._stopped:
            values = _required_values("slack")
            bot_token, app_token = values.get("bot_token", ""), values.get("app_token", "")
            if not bot_token or not app_token:
                return
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    opened = await client.post(
                        "https://slack.com/api/apps.connections.open",
                        headers={"Authorization": f"Bearer {app_token}"},
                    )
                    data = opened.json()
                    if not data.get("ok") or not data.get("url"):
                        raise RuntimeError(str(data.get("error") or "Slack Socket Mode open failed"))
                async with websockets.connect(data["url"], ping_interval=20, close_timeout=5) as ws:
                    async for raw in ws:
                        envelope = json.loads(raw)
                        if envelope.get("envelope_id"):
                            await ws.send(json.dumps({"envelope_id": envelope["envelope_id"]}))
                        event = envelope.get("payload", {}).get("event", {})
                        if event.get("type") != "message" or event.get("subtype") or event.get("bot_id"):
                            continue
                        text = str(event.get("text") or "").strip()
                        if not text:
                            continue
                        await self.handle_inbound(
                            "slack", str(event.get("channel") or ""), text,
                            sender=str(event.get("user") or ""),
                            reply=lambda answer, channel=event.get("channel"), thread=event.get("thread_ts"): self._slack_send(bot_token, channel, answer, thread),
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("Slack assistant loop failed: %s", exc)
                await asyncio.sleep(5)

    async def _dingtalk_loop(self) -> None:
        """DingTalk Stream mode connector (one supervised socket per app)."""
        while not self._stopped:
            values = _required_values("dingtalk")
            if not values.get("app_key") or not values.get("app_secret"):
                return
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    opened = await client.post(
                        "https://api.dingtalk.com/v1.0/gateway/connections/open",
                        json={
                            "clientId": values["app_key"],
                            "clientSecret": values["app_secret"],
                            "subscriptions": [
                                {"type": "SYSTEM", "topic": "ping"},
                                {"type": "SYSTEM", "topic": "disconnect"},
                                {"type": "CALLBACK", "topic": "/v1.0/im/bot/messages/get"},
                            ],
                            "ua": "tick-stock-panel/1.0",
                        },
                    )
                    data = opened.json()
                    endpoint, ticket = data.get("endpoint"), data.get("ticket")
                    if not endpoint or not ticket:
                        raise RuntimeError(str(data.get("message") or "钉钉 Stream 连接创建失败"))
                    separator = "&" if "?" in endpoint else "?"
                    endpoint = f"{endpoint}{separator}ticket={ticket}"
                async with websockets.connect(endpoint, ping_interval=20, close_timeout=5) as ws:
                    async for raw in ws:
                        frame = json.loads(raw)
                        headers = frame.get("headers") or {}
                        frame_type = frame.get("type")
                        topic = headers.get("topic")
                        if frame_type == "SYSTEM" and topic == "ping":
                            await ws.send(json.dumps({"code": 200, "headers": {"messageId": headers.get("messageId", ""), "contentType": "application/json"}, "message": "ok", "data": frame.get("data", "")}))
                            continue
                        if frame_type != "CALLBACK" or topic != "/v1.0/im/bot/messages/get":
                            continue
                        await ws.send(json.dumps({"code": 200, "headers": {"messageId": headers.get("messageId", ""), "contentType": "application/json"}, "message": "", "data": ""}))
                        callback = json.loads(frame.get("data") or "{}")
                        text = str((callback.get("text") or {}).get("content") or "").strip()
                        conversation = str(callback.get("conversationId") or "")
                        if text and conversation:
                            await self.handle_inbound(
                                "dingtalk", conversation, text,
                                sender=str(callback.get("senderStaffId") or ""),
                                reply=lambda answer, cid=conversation: self.dingtalk_send(cid, answer),
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("DingTalk assistant loop failed: %s", exc)
                await asyncio.sleep(5)

    async def _slack_send(self, token: str, channel: str, text: str, thread_ts: str | None = None) -> None:
        payload: dict[str, Any] = {"channel": channel, "text": text[:39000]}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post("https://slack.com/api/chat.postMessage", headers={"Authorization": f"Bearer {token}"}, json=payload)

    async def feishu_send(self, chat_id: str, text: str) -> None:
        values = _required_values("feishu")
        async with httpx.AsyncClient(timeout=15) as client:
            token_resp = await client.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": values["app_id"], "app_secret": values["app_secret"]},
            )
            token_resp.raise_for_status()
            token_data = token_resp.json()
            token = token_data.get("tenant_access_token")
            if not token:
                raise RuntimeError(str(token_data.get("msg") or "飞书 tenant_access_token 获取失败"))
            response = await client.post(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                headers={"Authorization": f"Bearer {token}"},
                json={"receive_id": chat_id, "msg_type": "text", "content": json.dumps({"text": text[:30000]}, ensure_ascii=False)},
            )
            response.raise_for_status()
            result = response.json()
            if result.get("code", 0) != 0:
                raise RuntimeError(str(result.get("msg") or "飞书消息发送失败"))

    async def dingtalk_send(self, conversation_id: str, text: str) -> None:
        values = _required_values("dingtalk")
        async with httpx.AsyncClient(timeout=15) as client:
            token_resp = await client.post(
                "https://api.dingtalk.com/v1.0/oauth2/accessToken",
                json={"appKey": values["app_key"], "appSecret": values["app_secret"]},
            )
            token = token_resp.json().get("accessToken")
            if not token:
                raise RuntimeError("钉钉 accessToken 获取失败")
            await client.post(
                "https://api.dingtalk.com/v1.0/robot/groupMessages",
                headers={"x-acs-dingtalk-access-token": token},
                json={
                    "robotCode": values["app_key"],
                    "openConversationId": conversation_id,
                    "msgKey": "sampleText",
                    "msgParam": json.dumps({"content": text[:20000]}, ensure_ascii=False),
                },
            )


def _encode_varint(value: int) -> bytes:
    value = max(0, int(value))
    result = bytearray()
    while value >= 0x80:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def _encode_bytes(field_number: int, value: bytes) -> bytes:
    tag = _encode_varint(field_number << 3 | 2)
    return tag + _encode_varint(len(value)) + value


def _encode_varint_field(field_number: int, value: int) -> bytes:
    return _encode_varint(field_number << 3) + _encode_varint(value)


def _encode_frame(
    *,
    service: int,
    method: int = 0,
    frame_type: str = "",
    headers: list[tuple[str, str]] | None = None,
    payload: bytes | None = None,
) -> bytes:
    all_headers = list(headers or [])
    if frame_type:
        all_headers.insert(0, ("type", frame_type))
    frame = bytearray()
    frame.extend(_encode_varint_field(1, 0))
    frame.extend(_encode_varint_field(2, 0))
    frame.extend(_encode_varint_field(3, service))
    frame.extend(_encode_varint_field(4, method))
    for key, value in all_headers:
        header = _encode_bytes(1, key.encode()) + _encode_bytes(2, value.encode())
        frame.extend(_encode_bytes(5, header))
    frame.extend(_encode_bytes(6, b""))
    frame.extend(_encode_bytes(7, b""))
    if payload is not None:
        frame.extend(_encode_bytes(8, payload))
    frame.extend(_encode_bytes(9, b""))
    return bytes(frame)


def _encode_ack(frame: dict[str, Any], *, ok: bool) -> bytes:
    payload = json.dumps(
        {"code": 200 if ok else 500, "headers": None, "data": None},
        separators=(",", ":"),
    ).encode()
    return _encode_frame(
        service=frame["service"],
        method=frame["method"],
        headers=frame["header_items"],
        payload=payload,
    )


def _read_varint(raw: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(raw):
        byte = raw[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
        shift += 7
        if shift > 63:
            raise ValueError("protobuf varint too long")
    raise ValueError("truncated protobuf varint")


def _read_bytes(raw: bytes, offset: int) -> tuple[bytes, int]:
    length, offset = _read_varint(raw, offset)
    end = offset + length
    if end > len(raw):
        raise ValueError("truncated protobuf bytes")
    return raw[offset:end], end


def _decode_header(raw: bytes) -> tuple[str, str]:
    key = value = ""
    offset = 0
    while offset < len(raw):
        tag, offset = _read_varint(raw, offset)
        field_number, wire_type = tag >> 3, tag & 7
        if wire_type != 2:
            raise ValueError("invalid Feishu frame header")
        data, offset = _read_bytes(raw, offset)
        if field_number == 1:
            key = data.decode(errors="replace")
        elif field_number == 2:
            value = data.decode(errors="replace")
    return key, value


def _decode_frame(raw: bytes) -> dict[str, Any] | None:
    try:
        frame: dict[str, Any] = {"method": 0, "service": 0, "headers": {}, "header_items": [], "payload": b""}
        offset = 0
        while offset < len(raw):
            tag, offset = _read_varint(raw, offset)
            field_number, wire_type = tag >> 3, tag & 7
            if wire_type == 0:
                value, offset = _read_varint(raw, offset)
                if field_number == 3:
                    frame["service"] = value
                elif field_number == 4:
                    frame["method"] = value
            elif wire_type == 2:
                value, offset = _read_bytes(raw, offset)
                if field_number == 5:
                    key, header_value = _decode_header(value)
                    frame["header_items"].append((key, header_value))
                    frame["headers"][key] = header_value
                elif field_number == 8:
                    frame["payload"] = value
            else:
                raise ValueError("unsupported Feishu frame wire type")
        return frame
    except (UnicodeDecodeError, ValueError):
        return None


def _decode_feishu_message(payload: bytes) -> dict[str, str] | None:
    try:
        envelope = json.loads(payload)
        if envelope.get("header", {}).get("event_type") != "im.message.receive_v1":
            return None
        event = envelope.get("event") or {}
        if (event.get("sender") or {}).get("sender_type") == "bot":
            return None
        message = event.get("message") or {}
        chat_id = str(message.get("chat_id") or "")
        message_type = str(message.get("message_type") or "text")
        content = message.get("content") or ""
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError:
                content = {"text": content}
        text = str((content or {}).get("text") or "").strip()
        sender_id = str(((event.get("sender") or {}).get("sender_id") or {}).get("open_id") or "")
        if not chat_id or not text or message_type not in {"text", "post"}:
            return None
        return {"chat_id": chat_id, "text": text, "sender_id": sender_id}
    except (TypeError, json.JSONDecodeError):
        return None
