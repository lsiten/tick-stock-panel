"""Pi CLI 的助手工具轮次适配。"""
from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from app.services.ai_provider import current_ai_model
from app.services.local_ai import run_pi_message_once


async def stream_pi_round(
    messages: Sequence[dict[str, Any]],
    tools: Sequence[dict[str, Any]],
    *,
    temperature: float | None = 0.3,
    timeout: float = 300.0,
) -> AsyncIterator[dict[str, Any]]:
    """执行一轮 Pi 请求并转换成助手统一事件。"""
    del temperature
    message = await run_pi_message_once(
        messages, tools, model=current_ai_model(), timeout=timeout,
    )
    calls = [
        {"id": call.id, "name": call.function.name, "arguments": call.function.arguments}
        for call in message.tool_calls
    ]
    if message.content:
        yield {"type": "text", "delta": message.content}
    yield {"type": "round_end", "tool_calls": calls, "finish_reason": "tool_calls" if calls else "stop"}
