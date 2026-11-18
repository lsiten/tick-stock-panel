"""AI 助手 HTTP 端点 — 薄层: 参数校验 + 流式响应映射。

POST /api/custom/assistant/chat      NDJSON 事件流(协议见模块 __init__)
GET  /api/custom/assistant/status     供应商配置状态(前端门控展示)
GET  /api/custom/assistant/suggests   空会话快捷指令
GET  /api/custom/assistant/sessions   会话历史(服务端, 跨设备)
PUT  /api/custom/assistant/sessions   会话历史整体保存(前端防抖写入)
POST /api/custom/assistant/actions/{call_id}/decision
                                     动作工具确认/拒绝(确认卡按钮回调)
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.custom.assistant import actions as assistant_actions
from app.custom.assistant import channels as assistant_channels
from app.custom.assistant.channel_runtime import AssistantChannelRuntime
from app.custom.assistant.feishu_registration import feishu_registration
from app.custom.assistant import sessions as assistant_sessions
from app.custom.assistant import tools as assistant_tools
from app.custom.assistant.chat_service import chat_stream
from app.services.ai_provider import (
    ai_configured,
    current_ai_model,
    current_ai_provider,
    is_local_cli_provider,
)
from app.services.ndjson_heartbeat import with_heartbeat


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=20000)
    reasoning_content: str | None = None
    reasoning_context: str | None = Field(default=None, max_length=64)


class AssistantContext(BaseModel):
    page: str = Field(default="", max_length=100)
    symbol: str = Field(default="", max_length=32)


class ChatRequest(BaseModel):
    messages: list[HistoryMessage] = Field(min_length=1, max_length=200)
    context: AssistantContext | None = None


class ActionDecisionRequest(BaseModel):
    approve: bool


class AssistantSessionsPayload(BaseModel):
    """前端 store.ts 的持久化载荷: 会话数组 + 活跃会话 id。

    sessions 用宽松的 list[dict] (消息形状由前端定义, 后端只裁剪条数),
    上限与前端一致, 防御异常客户端把文件写爆。字段名对齐前端 (activeId)。
    """

    sessions: list[dict] = Field(max_length=assistant_sessions.MAX_SESSIONS)
    active_id: str = Field(default="", max_length=64, alias="activeId")

    model_config = {"populate_by_name": True}


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api/custom/assistant", tags=["custom-assistant"])

    @router.post("/chat")
    async def chat(request: Request, req: ChatRequest):
        engine = getattr(request.app.state, "strategy_engine", None)
        repo = getattr(request.app.state, "repo", None)
        data_dir = str(repo.store.data_dir) if repo is not None else None
        context = req.context.model_dump() if req.context else None

        async def gen():
            async for line in with_heartbeat(chat_stream(
                history=[m.model_dump(exclude_none=True) for m in req.messages],
                context=context,
                engine=engine,
                data_dir=data_dir,
                repo=repo,
                quote_service=getattr(request.app.state, "quote_service", None),
                depth_service=getattr(request.app.state, "depth_service", None),
                capabilities=getattr(request.app.state, "capabilities", None),
                financial_scheduler=getattr(request.app.state, "financial_scheduler", None),
            )):
                yield line + "\n"

        return StreamingResponse(
            gen(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/status")
    def status() -> dict:
        local_provider = is_local_cli_provider()
        configured = local_provider and ai_configured()
        return {
            "configured": configured,
            "provider": current_ai_provider(),
            "model": current_ai_model(),
            "supports_tools": configured,
        }

    @router.get("/suggests")
    def suggests() -> dict:
        return {"suggests": assistant_tools.QUICK_SUGGESTS}

    @router.get("/channels")
    def channels() -> dict:
        return {"channels": assistant_channels.list_channels()}

    @router.get("/channels/feishu/qr")
    async def feishu_qr() -> dict:
        """向飞书申请一次性设备码，返回可直接用飞书扫码的链接。"""
        try:
            return await feishu_registration.begin()
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.get("/channels/feishu/qr/{session_id}")
    def feishu_qr_status(session_id: str) -> dict:
        try:
            return feishu_registration.status(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="飞书扫码会话不存在或已过期") from exc

    class ChannelPayload(BaseModel):
        values: dict[str, str] = Field(default_factory=dict)

    @router.put("/channels/{platform}")
    async def save_channel(platform: str, req: ChannelPayload, request: Request) -> dict:
        try:
            row = assistant_channels.save_channel(platform, req.values)
            runtime = getattr(request.app.state, "assistant_channel_runtime", None)
            if runtime:
                await runtime.refresh()
            return row
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/channels/{platform}/probe")
    async def probe_channel(platform: str, request: Request) -> dict:
        try:
            result = await assistant_channels.probe_channel(platform)
            if result.get("ok"):
                runtime = getattr(request.app.state, "assistant_channel_runtime", None)
                if runtime:
                    await runtime.refresh()
            return result
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/channels/{platform}")
    def disconnect_channel(platform: str) -> dict:
        try:
            assistant_channels.disconnect_channel(platform)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    @router.post("/channels/{platform}/events")
    async def channel_events(platform: str, request: Request) -> dict:
        """Public event endpoint for Feishu/Slack/DingTalk callback mode."""
        payload = await request.json()
        if platform == "feishu" and payload.get("challenge"):
            return {"challenge": payload["challenge"]}
        if platform == "slack" and payload.get("type") == "url_verification":
            return {"challenge": payload.get("challenge", "")}
        runtime: AssistantChannelRuntime | None = getattr(request.app.state, "assistant_channel_runtime", None)
        if runtime is None:
            raise HTTPException(status_code=503, detail="消息渠道运行时未启动")
        if platform == "feishu":
            event = payload.get("event", {})
            message = event.get("message", {})
            content = message.get("content", "")
            try:
                content = json.loads(content).get("text", content)
            except (TypeError, json.JSONDecodeError):
                pass
            chat_id = str(message.get("chat_id", ""))
            asyncio.create_task(runtime.handle_inbound(
                "feishu", chat_id, str(content),
                reply=lambda answer, cid=chat_id: runtime.feishu_send(cid, answer),
            ))
        elif platform == "slack":
            event = payload.get("event", {})
            if event.get("type") == "message" and not event.get("bot_id"):
                values = assistant_channels._required_values("slack")
                channel = str(event.get("channel", ""))
                asyncio.create_task(runtime.handle_inbound(
                    "slack", channel, str(event.get("text", "")),
                    reply=lambda answer, token=values.get("bot_token", ""), cid=channel, thread=event.get("thread_ts"): runtime._slack_send(token, cid, answer, thread),
                ))
        elif platform == "dingtalk":
            message = payload.get("text") or payload.get("content") or ""
            conversation = payload.get("conversationId") or payload.get("conversation_id") or ""
            if message and conversation:
                asyncio.create_task(runtime.handle_inbound(
                    "dingtalk", str(conversation), str(message),
                    reply=lambda answer, cid=str(conversation): runtime.dingtalk_send(cid, answer),
                ))
        return {"ok": True}

    @router.get("/sessions")
    def sessions(request: Request) -> dict:
        repo = getattr(request.app.state, "repo", None)
        if repo is None:
            raise HTTPException(status_code=503, detail="数据目录不可用")
        return assistant_sessions.load_sessions(Path(repo.store.data_dir))

    @router.put("/sessions")
    def put_sessions(request: Request, req: AssistantSessionsPayload) -> dict:
        repo = getattr(request.app.state, "repo", None)
        if repo is None:
            raise HTTPException(status_code=503, detail="数据目录不可用")
        n = assistant_sessions.save_sessions(
            Path(repo.store.data_dir),
            {"sessions": req.sessions, "activeId": req.active_id},
        )
        return {"ok": True, "sessions": n}

    @router.post("/actions/{call_id}/decision")
    async def action_decision(call_id: str, req: ActionDecisionRequest) -> dict:
        """确认卡回调: 唤醒挂起中的动作工具(批准执行或拒绝)。"""
        action = await assistant_actions.registry.resolve(call_id, req.approve)
        if action is None:
            raise HTTPException(status_code=404, detail="确认请求不存在或已过期")
        return {"ok": True, "call_id": call_id, "decision": "approved" if req.approve else "denied"}

    return router
