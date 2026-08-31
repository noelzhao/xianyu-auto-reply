"""
飞书 Webhook 通知模块

在滑块验证失败（需要人工更新 Cookie）和 Token 恢复成功时，
通过飞书自定义机器人 Webhook 发送通知。

配置项存储在数据库 xy_system_settings 表：
  - notification.feishu_webhook_url : 飞书机器人 Webhook 地址
  - notification.feishu_enabled     : 是否启用飞书通知 (true/false)
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp
from loguru import logger

# 防刷控制：同一账号的最小通知间隔（秒）
_NOTIFY_COOLDOWN_SECONDS = 600  # 10 分钟

# 记录每个账号上次发送"需要更新Cookie"通知的时间
_last_notify_time: dict[str, float] = {}

# 记录哪些账号当前处于"异常"状态（用于恢复通知判断）
_account_alerting: set[str] = set()


# 飞书机器人 Webhook 地址（默认值，可通过 DB 覆盖）
_DEFAULT_WEBHOOK_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/10250c18-6c5a-4677-b5d5-d6530e80064b"


def _get_webhook_url() -> Optional[str]:
    """从数据库读取飞书 Webhook URL。

    优先使用 DB 中配置的地址；若 DB 未配置则使用硬编码默认值。
    """
    try:
        from common.db.compat import db_manager
        # 检查是否启用
        enabled = db_manager.get_system_setting("notification.feishu_enabled", "true")
        if enabled not in ("true", "1", True):
            return None
        url = db_manager.get_system_setting("notification.feishu_webhook_url", "")
        if url and url.strip():
            return url.strip()
    except Exception as e:
        logger.debug(f"读取飞书 Webhook 配置失败，使用默认值: {e}")
    return _DEFAULT_WEBHOOK_URL


async def send_feishu_message(title: str, content: str, webhook_url: str = None) -> bool:
    """发送飞书 Webhook 消息（富文本 post 格式）。

    Args:
        title: 消息标题（加粗显示）
        content: 消息正文（纯文本，换行用 \\n）
        webhook_url: 可选，不传则从数据库读取

    Returns:
        发送成功返回 True，失败返回 False
    """
    if not webhook_url:
        webhook_url = _get_webhook_url()
    if not webhook_url:
        return False

    # 使用飞书 post 消息格式
    tz = timezone(timedelta(hours=8))
    now_str = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

    payload = {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": title,
                    "content": [
                        [
                            {"tag": "text", "text": content},
                        ],
                        [
                            {"tag": "text", "text": f"\n时间: {now_str}"},
                        ],
                    ],
                }
            }
        },
    }

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(webhook_url, json=payload) as resp:
                resp_text = await resp.text()
                if resp.status == 200:
                    resp_json = json.loads(resp_text)
                    if resp_json.get("StatusCode") == 0 or resp_json.get("code") == 0:
                        logger.info(f"飞书通知发送成功: {title}")
                        return True
                    else:
                        logger.warning(f"飞书通知发送失败，响应: {resp_text[:200]}")
                        return False
                else:
                    logger.warning(f"飞书通知 HTTP {resp.status}: {resp_text[:200]}")
                    return False
    except Exception as e:
        logger.warning(f"飞书通知发送异常: {e}")
        return False


async def notify_cookie_update_needed(account_name: str, reason: str = "") -> None:
    """通知某账号需要手动更新 Cookie。

    带防刷控制：同一账号 10 分钟内只通知一次。

    Args:
        account_name: 账号名称
        reason: 失败原因（可选）
    """
    now = time.time()
    last = _last_notify_time.get(account_name, 0)
    if now - last < _NOTIFY_COOLDOWN_SECONDS:
        logger.debug(f"【{account_name}】飞书通知冷却中，跳过（距上次 {int(now - last)}s）")
        return

    _last_notify_time[account_name] = now
    _account_alerting.add(account_name)

    content = (
        f"账号 [{account_name}] Token 刷新失败，滑块验证未通过\n"
        f"原因: {reason}\n"
        f"\n操作步骤:\n"
        f"1. 浏览器打开 goofish.com（闲鱼网页版）\n"
        f"2. 通过滑块验证\n"
        f"3. 确认 Cookie 中包含 x5sec\n"
        f"4. 复制完整 Cookie 更新到系统"
    )
    await send_feishu_message("闲鱼账号需要手动更新Cookie", content)


async def notify_account_recovered(account_name: str) -> None:
    """通知某账号已恢复正常。

    只有之前处于异常状态的账号才会发送恢复通知。

    Args:
        account_name: 账号名称
    """
    if account_name not in _account_alerting:
        return

    _account_alerting.discard(account_name)
    _last_notify_time.pop(account_name, None)

    content = f"账号 [{account_name}] Token 刷新成功，已恢复正常运行"
    await send_feishu_message("闲鱼账号已恢复", content)
