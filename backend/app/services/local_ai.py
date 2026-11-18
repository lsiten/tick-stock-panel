"""本地 AI CLI 运行时适配。

本模块只负责本机 CLI 的进程边界。业务工具、权限和动作确认仍由调用方
注入；Pi 的工具轮次使用与 Codex 相同的结构化 JSON 约定。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

PI_DEFAULT_COMMAND = "pi"
PI_MIN_TIMEOUT_SECONDS = 300.0

_LOCAL_ENV_KEYS = {
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "HOME", "USERPROFILE",
    "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES",
    "PROGRAMFILES(X86)", "TEMP", "TMP", "TMPDIR", "SHELL", "USER", "LOGNAME", "LANG",
    "LC_ALL", "LC_CTYPE", "TZ", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY",
}


def _pi_command() -> list[str]:
    resolved = shutil.which(PI_DEFAULT_COMMAND)
    if not resolved:
        raise RuntimeError("未找到 Pi CLI 命令: pi")
    return [resolved]


def pi_cli_available() -> bool:
    """Pi CLI 是否能从本机 PATH 解析。

    Pi 的不同发行版未统一实现 ``--version``，真实连通性由设置页的
    ``probe_ai_config`` 通过一次最小 JSON 请求验证。
    """
    try:
        _pi_command()
    except RuntimeError:
        return False
    return True


async def run_pi_cli(
    messages: Sequence[dict[str, Any]],
    *,
    model: str = "",
    max_tokens: int | None = None,
    timeout: float = PI_MIN_TIMEOUT_SECONDS,
) -> str:
    """通过 Pi 的 JSON 事件流执行一次无状态本地请求。"""
    prompt = _build_prompt(messages, max_tokens=max_tokens)
    run_dir = Path(tempfile.mkdtemp(prefix="tickflow-pi-run-"))
    try:
        args = [*_pi_command(), "-p", "--mode", "json", "--no-session", "--no-tools"]
        if model.strip():
            args.extend(["--model", model.strip()])
        env = _local_environment()
        try:
            returncode, stdout, stderr = await asyncio.to_thread(
                _run_process, args, prompt, env, max(timeout, PI_MIN_TIMEOUT_SECONDS), run_dir,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Pi CLI 调用超时({max(timeout, PI_MIN_TIMEOUT_SECONDS):g} 秒)") from exc
        if returncode != 0:
            detail = _clean(stderr) or _clean(stdout) or f"exit code {returncode}"
            raise RuntimeError(f"Pi CLI 调用失败: {detail[-1200:]}")
        return _parse_output(stdout)
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


async def run_pi_message_once(
    messages: Sequence[dict[str, Any]],
    tools: Sequence[dict[str, Any]],
    *,
    model: str = "",
    max_tokens: int | None = None,
    timeout: float = PI_MIN_TIMEOUT_SECONDS,
) -> SimpleNamespace:
    """请求 Pi 生成一轮结构化正文和工具调用。"""
    names = sorted({str(tool.get("function", {}).get("name", "")) for tool in tools})
    names = [name for name in names if name]
    if not names:
        raise ValueError("Pi CLI 工具调用需要至少一个工具 schema")
    schema = {"content": "用户可见回答", "tool_calls": [{"name": names, "arguments": "JSON 对象字符串"}]}
    request = [
        {
            "role": "system",
            "content": (
                "只生成一轮回答并严格返回一个 JSON 对象。字段 content 为字符串，"
                "tool_calls 为数组；每个元素含 name 和 arguments。只能使用声明的工具。"
                "需要本地数据时先请求工具，不要编造工具结果；准备回答时 tool_calls 必须为空。"
                f"可用工具名: {', '.join(names)}。结构提示: {json.dumps(schema, ensure_ascii=False)}"
            ),
        },
        {"role": "user", "content": json.dumps({"messages": list(messages), "tools": list(tools)}, ensure_ascii=False)},
    ]
    raw = await run_pi_cli(request, model=model, max_tokens=max_tokens, timeout=timeout)
    payload = _parse_json_payload(raw)
    content = payload.get("content")
    calls = payload.get("tool_calls")
    if not isinstance(content, str) or not isinstance(calls, list):
        raise RuntimeError("Pi CLI 工具响应格式无效, 请重试或检查模型输出")
    parsed_calls = []
    for call in calls:
        if not isinstance(call, dict) or call.get("name") not in names or not isinstance(call.get("arguments"), str):
            raise RuntimeError("Pi CLI 工具响应格式无效, 请重试或检查模型输出")
        arguments = call["arguments"]
        try:
            parsed_args = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Pi CLI 工具响应格式无效, 参数不是 JSON 对象") from exc
        if not isinstance(parsed_args, dict):
            raise RuntimeError("Pi CLI 工具响应格式无效, 参数不是 JSON 对象")
        parsed_calls.append(SimpleNamespace(
            id=f"pi-{len(parsed_calls)}", type="function",
            function=SimpleNamespace(name=call["name"], arguments=arguments),
        ))
    if not content.strip() and not parsed_calls:
        raise RuntimeError("Pi CLI 未返回正文或工具请求")
    return SimpleNamespace(content=content, tool_calls=parsed_calls)


def _build_prompt(messages: Sequence[dict[str, Any]], *, max_tokens: int | None) -> str:
    parts = [
        "You are Tick Stock Panel's local AI provider.",
        "Use only the conversation payload below. Do not inspect or modify files.",
        "Return only the final answer or the exact JSON contract requested by the system message.",
    ]
    if max_tokens:
        parts.append(f"Keep the final answer within about {max_tokens} output tokens.")
    for message in messages:
        parts.append(f"\n<{message.get('role', 'user')}>\n{message.get('content', '')}\n</{message.get('role', 'user')}>")
    return "\n".join(parts)


def _local_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key in _LOCAL_ENV_KEYS and value}


def _run_process(
    args: Sequence[str], prompt: str, env: dict[str, str], timeout: float, run_dir: Path,
) -> tuple[int, bytes, bytes]:
    result = subprocess.run(
        list(args), input=prompt.encode("utf-8"), capture_output=True, env=env,
        cwd=run_dir, timeout=timeout, check=False,
    )
    return result.returncode, result.stdout, result.stderr


def _parse_output(stdout: bytes) -> str:
    raw = stdout.decode("utf-8", errors="replace")
    text: list[str] = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "message_update":
            update = event.get("assistantMessageEvent") or {}
            if update.get("type") == "text_delta":
                text.append(str(update.get("delta") or ""))
        if event.get("type") == "error":
            raise RuntimeError(f"Pi CLI 返回错误: {event.get('message') or '未知错误'}")
    return "".join(text).strip() or raw.strip()


def _parse_json_payload(raw: str) -> dict[str, Any]:
    candidate = raw.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`").removeprefix("json").strip()
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Pi CLI 工具响应格式无效, 不是 JSON 对象") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Pi CLI 工具响应格式无效, 不是 JSON 对象")
    return payload


def _clean(value: bytes) -> str:
    return value.decode("utf-8", errors="replace").strip()
