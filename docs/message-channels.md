# AI 助手消息渠道

AI 助手复用同一条本地 Codex/Pi 对话链路，支持飞书、Slack、钉钉、企业微信和 Telegram。每个平台的消息会按“平台 + 会话 ID”隔离上下文，`/new` 创建新上下文，`/clear` 清空当前上下文。

在 **设置 → 消息渠道** 保存凭据后，后端会先做平台凭据探测，再启动相应的桥接：

| 平台 | 凭据 | 接收方式 | 回复方式 |
| --- | --- | --- | --- |
| 飞书 | App ID / App Secret | 事件订阅回调 | 飞书 `im/v1/messages` |
| Slack | Bot Token / App Token / Signing Secret | Socket Mode；也支持 Events API 回调 | `chat.postMessage` |
| 钉钉 | AppKey / AppSecret | Stream WebSocket；也支持回调入口 | `robot/groupMessages` |
| 企业微信 | BotID / Secret | 智能机器人 WebSocket 长连接 | `aibot_send_msg` |
| Telegram | Bot Token | `getUpdates` 长轮询 | `sendMessage` |

公网入口部署时，飞书和钉钉事件回调地址分别使用：

```text
https://<面板域名>/api/custom/assistant/channels/feishu/events
https://<面板域名>/api/custom/assistant/channels/dingtalk/events
```

Slack 可直接使用 Socket Mode；如果选择 Events API，回调地址为：

```text
https://<面板域名>/api/custom/assistant/channels/slack/events
```

平台消息回调不使用面板浏览器 Cookie，由平台自己的签名/凭据保护。不要把 Bot Token、App Secret 或 Bot Secret 写进仓库、日志或聊天记录。删除渠道会清除本地加密凭据并停止对应轮询/连接任务。
