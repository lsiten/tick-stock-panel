"""助手只接受本地 CLI provider 的边界测试。"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom.assistant import chat_service, routes


@pytest.mark.asyncio
async def test_chat_stream_rejects_remote_provider_before_model_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_service, "is_codex_cli_provider", lambda provider=None: False)
    monkeypatch.setattr(chat_service, "is_local_cli_provider", lambda provider=None: False)

    events = [json.loads(line) async for line in chat_service.chat_stream(
        history=[{"role": "user", "content": "查询市场"}],
    )]

    assert events[0]["type"] == "error"
    assert events[0]["kind"] == "provider"
    assert events[0]["hint"] == "/settings?tab=ai"
    assert events[-1] == {"type": "done"}


def test_status_marks_remote_provider_unavailable_for_assistant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes, "is_local_cli_provider", lambda provider=None: False)
    monkeypatch.setattr(routes, "ai_configured", lambda provider=None: True)
    monkeypatch.setattr(routes, "current_ai_provider", lambda: "openai_compat")
    monkeypatch.setattr(routes, "current_ai_model", lambda: "remote-model")
    app = FastAPI()
    app.include_router(routes.build_router())

    body = TestClient(app).get("/api/custom/assistant/status").json()

    assert body["configured"] is False
    assert body["supports_tools"] is False
    assert body["provider"] == "openai_compat"
