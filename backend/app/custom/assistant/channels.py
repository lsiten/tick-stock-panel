"""AI 助手消息渠道配置与凭据探测。

The channel registry deliberately keeps platform secrets in ``secrets.json`` and
only returns non-sensitive connection state to the browser.  Actual message
delivery is owned by platform adapters; this module is the stable settings
contract used by the assistant UI.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app import secrets_store
from app.services import preferences

logger = logging.getLogger(__name__)

CHANNELS: dict[str, dict[str, Any]] = {
    "feishu": {
        "label": "飞书",
        "description": "飞书 Bot 长连接",
        "fields": ("app_id", "app_secret"),
        "secret_fields": ("app_secret",),
        "available": True,
    },
    "slack": {
        "label": "Slack",
        "description": "Slack Socket Mode Bot",
        "fields": ("bot_token", "app_token", "signing_secret"),
        "secret_fields": ("bot_token", "app_token", "signing_secret"),
        "available": True,
    },
    "dingtalk": {
        "label": "钉钉",
        "description": "钉钉 Stream Bot",
        "fields": ("app_key", "app_secret"),
        "secret_fields": ("app_secret",),
        "available": True,
    },
    "wecom": {
        "label": "企业微信",
        "description": "企业微信智能机器人长连接",
        "fields": ("bot_id", "secret"),
        "secret_fields": ("secret",),
        "available": True,
    },
    "telegram": {
        "label": "Telegram",
        "description": "Telegram Bot 长轮询",
        "fields": ("bot_token",),
        "secret_fields": ("bot_token",),
        "available": True,
    },
}


def _configs() -> dict[str, dict[str, str]]:
    raw = preferences.load().get("assistant_message_channels", {})
    return raw if isinstance(raw, dict) else {}


def _secrets() -> dict[str, dict[str, str]]:
    raw = secrets_store.load().get("assistant_message_channels", {})
    return raw if isinstance(raw, dict) else {}


def _is_platform(platform: str) -> bool:
    return platform in CHANNELS


def _masked(value: str) -> str:
    if not value:
        return ""
    return secrets_store.mask(value, prefix=3, suffix=3)


def _public_config(platform: str) -> dict[str, Any]:
    definition = CHANNELS[platform]
    config = _configs().get(platform, {})
    secret = _secrets().get(platform, {})
    result: dict[str, Any] = {"platform": platform}
    for field in definition["fields"]:
        value = str(config.get(field) or secret.get(field) or "")
        result[field] = _masked(value) if field in definition["secret_fields"] else value
    return result


def _required_values(platform: str, incoming: dict[str, Any] | None = None) -> dict[str, str]:
    definition = CHANNELS[platform]
    current = _configs().get(platform, {}).copy()
    current.update(_secrets().get(platform, {}))
    if platform == "wecom":
        # Keep the original monitoring-page WeCom Bot settings and the new
        # assistant channel page on one credential source.
        current.setdefault("bot_id", preferences.get_wecom_bot_id())
        current.setdefault("secret", preferences.get_wecom_bot_secret())
    for key, value in (incoming or {}).items():
        if key in definition["fields"] and value is not None and str(value).strip():
            current[key] = str(value).strip()
    return {field: str(current.get(field) or "").strip() for field in definition["fields"]}


def list_channels() -> list[dict[str, Any]]:
    """Return the UI-safe channel catalog."""
    from app.services import preferences as prefs

    wecom_status = None
    try:
        # Existing WeCom service is already a real long-lived connector.
        from app.main import app

        service = getattr(app.state, "wecom_bot_service", None)
        wecom_status = service.status() if service else None
    except Exception:  # noqa: BLE001
        wecom_status = None

    rows: list[dict[str, Any]] = []
    for platform, definition in CHANNELS.items():
        values = _required_values(platform)
        configured = all(values.get(field) for field in definition["fields"])
        connected = False
        state = "disconnected"
        if platform == "wecom" and wecom_status:
            connected = bool(wecom_status.get("connected"))
            configured = bool(wecom_status.get("bot_id_configured") and wecom_status.get("secret_configured"))
            state = "connected" if connected else ("configured" if configured else "disconnected")
        elif configured:
            # Other adapters are connected only after their explicit probe.
            state = str(prefs.load().get("assistant_message_channel_status", {}).get(platform, "configured"))
            connected = state == "connected"
        rows.append({
            "platform": platform,
            "label": definition["label"],
            "description": definition["description"],
            "fields": list(definition["fields"]),
            "config": _public_config(platform),
            "configured": configured,
            "connected": connected,
            "state": state,
            "available": bool(definition["available"]),
            "agent_names": ["Tick Stock AI 助手"] if connected else [],
        })
    return rows


def save_channel(platform: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not _is_platform(platform):
        raise ValueError("不支持的消息渠道")
    definition = CHANNELS[platform]
    values = _required_values(platform, payload)
    # Preserve a previously saved secret when the UI submits a masked value.
    public: dict[str, str] = {}
    secret: dict[str, str] = {}
    for field in definition["fields"]:
        value = values[field]
        if not value:
            continue
        if field in definition["secret_fields"]:
            secret[field] = value
        else:
            public[field] = value
    preferences.save({
        "assistant_message_channels": {
            **_configs(),
            platform: public,
        },
        "assistant_message_channel_status": {
            **preferences.load().get("assistant_message_channel_status", {}),
            platform: "configured" if all(values.get(field) for field in definition["fields"]) else "disconnected",
        },
    })
    if secret:
        secrets_store.save({
            "assistant_message_channels": {**_secrets(), platform: secret},
        })
    if platform == "wecom":
        preferences.set_wecom_bot_id(values.get("bot_id", ""))
        preferences.set_wecom_bot_secret(values.get("secret", ""))
        preferences.set_wecom_bot_enabled(bool(values.get("bot_id") and values.get("secret")))
        try:
            from app.main import app

            service = getattr(app.state, "wecom_bot_service", None)
            if service:
                service.apply_credential_change()
        except Exception as exc:  # noqa: BLE001
            logger.warning("WeCom assistant credential apply failed: %s", exc)
    return next(row for row in list_channels() if row["platform"] == platform)


async def probe_channel(platform: str) -> dict[str, Any]:
    """Validate credentials against the platform API without sending a message."""
    if not _is_platform(platform):
        raise ValueError("不支持的消息渠道")
    values = _required_values(platform)
    if not all(values.get(field) for field in CHANNELS[platform]["fields"]):
        return {"ok": False, "detail": "凭据尚未填写完整"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if platform == "telegram":
                response = await client.get(f"https://api.telegram.org/bot{values['bot_token']}/getMe")
                data = response.json()
                if not response.is_success or not data.get("ok"):
                    return {"ok": False, "detail": str(data.get("description") or "Telegram 凭据无效")}
            elif platform == "slack":
                response = await client.get(
                    "https://slack.com/api/auth.test",
                    headers={"Authorization": f"Bearer {values['bot_token']}"},
                )
                data = response.json()
                if not response.is_success or not data.get("ok"):
                    return {"ok": False, "detail": str(data.get("error") or "Slack Bot Token 无效")}
            elif platform == "feishu":
                response = await client.post(
                    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    json={"app_id": values["app_id"], "app_secret": values["app_secret"]},
                )
                data = response.json()
                if not response.is_success or data.get("code", 0) != 0:
                    return {"ok": False, "detail": str(data.get("msg") or "飞书凭据无效")}
            elif platform == "dingtalk":
                response = await client.post(
                    "https://api.dingtalk.com/v1.0/oauth2/accessToken",
                    json={"appKey": values["app_key"], "appSecret": values["app_secret"]},
                )
                data = response.json()
                if not response.is_success or not data.get("accessToken"):
                    return {"ok": False, "detail": str(data.get("message") or "钉钉凭据无效")}
            elif platform == "wecom":
                # The long-connection service owns the authoritative WeCom state.
                from app.main import app

                service = getattr(app.state, "wecom_bot_service", None)
                status = service.status() if service else {}
                if not status.get("connected"):
                    return {"ok": False, "detail": str(status.get("last_error") or "企业微信长连接尚未建立")}
        preferences.save({
            "assistant_message_channel_status": {
                **preferences.load().get("assistant_message_channel_status", {}),
                platform: "connected",
            },
        })
        return {"ok": True, "detail": "凭据验证通过"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("message channel probe failed %s: %s", platform, exc)
        return {"ok": False, "detail": f"连接失败: {exc}"}


def disconnect_channel(platform: str) -> None:
    if not _is_platform(platform):
        raise ValueError("不支持的消息渠道")
    configs = _configs()
    configs.pop(platform, None)
    statuses = preferences.load().get("assistant_message_channel_status", {})
    statuses.pop(platform, None)
    preferences.save({"assistant_message_channels": configs, "assistant_message_channel_status": statuses})
    secrets = _secrets()
    secrets.pop(platform, None)
    secrets_store.save({"assistant_message_channels": secrets})
    if platform == "wecom":
        preferences.set_wecom_bot_id("")
        preferences.set_wecom_bot_secret("")
        preferences.set_wecom_bot_enabled(False)
        try:
            from app.main import app

            service = getattr(app.state, "wecom_bot_service", None)
            if service:
                service.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("WeCom assistant disconnect failed: %s", exc)
