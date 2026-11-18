"""飞书 PersonalAgent 设备授权流程。

飞书的设备授权接口会先返回一次性 ``verification_uri_complete``，用户
直接用飞书 App 扫码；授权完成后轮询接口才会返回新建 Bot 的 App ID 和
App Secret。因此生成二维码不依赖用户预先填写 App ID。
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.custom.assistant import channels as assistant_channels

logger = logging.getLogger(__name__)

REGISTRATION_URL = "https://accounts.feishu.cn/oauth/v1/app/registration"
DEFAULT_INTERVAL_SECONDS = 5
DEFAULT_EXPIRES_SECONDS = 600


@dataclass
class _Session:
    session_id: str
    device_code: str
    qr_code_url: str
    expires_at: float
    interval_seconds: int
    status: str = "pending"
    error: str = ""
    app_id: str = ""
    task: asyncio.Task[None] | None = None


class FeishuRegistrationManager:
    """在单个后端进程内管理短生命周期的扫码授权会话。"""

    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}

    async def begin(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                REGISTRATION_URL,
                data={
                    "action": "begin",
                    "archetype": "PersonalAgent",
                    "auth_method": "client_secret",
                    "request_user_info": "open_id",
                },
            )
        data = self._json(response)
        if not response.is_success or data.get("error"):
            detail = str(data.get("error_description") or data.get("error") or "飞书二维码申请失败")
            raise RuntimeError(detail)
        device_code = str(data.get("device_code") or "")
        qr_code_url = str(data.get("verification_uri_complete") or "")
        if not device_code or not qr_code_url:
            raise RuntimeError("飞书二维码申请响应缺少设备码")

        session_id = secrets.token_urlsafe(18)
        session = _Session(
            session_id=session_id,
            device_code=device_code,
            qr_code_url=qr_code_url,
            expires_at=time.time() + int(data.get("expires_in") or DEFAULT_EXPIRES_SECONDS),
            interval_seconds=max(2, int(data.get("interval") or DEFAULT_INTERVAL_SECONDS)),
        )
        self._sessions[session_id] = session
        session.task = asyncio.create_task(self._poll(session), name=f"feishu-registration-{session_id}")
        return self._public(session)

    def status(self, session_id: str) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return self._public(session)

    async def _poll(self, session: _Session) -> None:
        try:
            while time.time() < session.expires_at:
                await asyncio.sleep(session.interval_seconds)
                async with httpx.AsyncClient(timeout=15.0) as client:
                    response = await client.post(
                        REGISTRATION_URL,
                        data={"action": "poll", "device_code": session.device_code},
                    )
                data = self._json(response)
                if data.get("client_id") and data.get("client_secret"):
                    session.app_id = str(data["client_id"])
                    assistant_channels.save_channel(
                        "feishu",
                        {
                            "app_id": session.app_id,
                            "app_secret": str(data["client_secret"]),
                        },
                    )
                    probe = await assistant_channels.probe_channel("feishu")
                    if not probe.get("ok"):
                        session.status = "error"
                        session.error = str(probe.get("detail") or "飞书 Bot 验证失败")
                        return
                    session.status = "connected"
                    try:
                        from app.main import app

                        runtime = getattr(app.state, "assistant_channel_runtime", None)
                        if runtime:
                            await runtime.refresh()
                    except Exception:  # noqa: BLE001
                        logger.exception("飞书扫码完成后刷新消息渠道运行时失败")
                    return

                error = str(data.get("error") or "")
                if error in {"authorization_pending", ""}:
                    continue
                if error == "slow_down":
                    session.interval_seconds += 5
                    continue
                session.status = "error"
                session.error = str(data.get("error_description") or error or "飞书扫码授权失败")
                return
            session.status = "expired"
            session.error = "二维码已过期，请重新生成"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("飞书扫码授权轮询失败")
            session.status = "error"
            session.error = str(exc)

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _public(session: _Session) -> dict[str, Any]:
        return {
            "session_id": session.session_id,
            "qr_code_url": session.qr_code_url,
            "status": session.status,
            "error": session.error,
            "app_id": session.app_id,
            "expires_in_seconds": max(0, int(session.expires_at - time.time())),
            "poll_interval_seconds": session.interval_seconds,
        }


feishu_registration = FeishuRegistrationManager()
