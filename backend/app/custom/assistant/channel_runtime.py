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
            token = token_resp.json().get("tenant_access_token")
            if not token:
                raise RuntimeError("飞书 tenant_access_token 获取失败")
            await client.post(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                headers={"Authorization": f"Bearer {token}"},
                json={"receive_id": chat_id, "msg_type": "text", "content": json.dumps({"text": text[:30000]}, ensure_ascii=False)},
            )

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
