"""Pi CLI 本地适配的结构化工具协议测试。"""
from __future__ import annotations

import json

import pytest

from app.services import local_ai


def _tool() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "lookup_local_status",
            "description": "读取本地状态",
            "parameters": {"type": "object", "properties": {}},
        },
    }


@pytest.mark.asyncio
async def test_pi_message_once_parses_structured_tool_call(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run(messages, **kwargs):
        assert kwargs["model"] == "local-model"
        return json.dumps({
            "content": "先查本地状态。",
            "tool_calls": [{"name": "lookup_local_status", "arguments": "{}"}],
        })

    monkeypatch.setattr(local_ai, "run_pi_cli", fake_run)
    result = await local_ai.run_pi_message_once(
        [{"role": "user", "content": "查询"}], [_tool()], model="local-model",
    )

    assert result.content == "先查本地状态。"
    assert result.tool_calls[0].function.name == "lookup_local_status"
    assert result.tool_calls[0].function.arguments == "{}"


@pytest.mark.asyncio
async def test_pi_message_once_rejects_unknown_tool_without_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run(*args, **kwargs):
        return json.dumps({"content": "", "tool_calls": [{"name": "shell", "arguments": "{}"}]})

    monkeypatch.setattr(local_ai, "run_pi_cli", fake_run)
    with pytest.raises(RuntimeError, match="格式无效"):
        await local_ai.run_pi_message_once(
            [{"role": "user", "content": "执行"}], [_tool()],
        )


def test_pi_output_extracts_text_deltas_from_json_events() -> None:
    output = b'\n'.join([
        json.dumps({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "本地"}}).encode(),
        json.dumps({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "回答"}}).encode(),
    ])

    assert local_ai._parse_output(output) == "本地回答"
