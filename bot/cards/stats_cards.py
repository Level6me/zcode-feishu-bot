import os
import json
import base64
import urllib.request
import ssl
from datetime import datetime, timezone, timedelta

from cards.common import create_footer

# 北京时间 (UTC+8) 时区定义
BEIJING_TZ = timezone(timedelta(hours=8))

def get_zcode_account() -> str:
    """获取 zcode 引擎当前登录的账号 Email"""
    try:
        from utils.quota import get_zcode_account as _get
        return _get()
    except Exception:
        return ""


def get_local_tz_info():
    """获取当前系统设备的本地时区对象与 UTC 偏移量字符串 (如 UTC+8, UTC-5)"""
    try:
        local_now = datetime.now().astimezone()
        tz = local_now.tzinfo
        offset = local_now.utcoffset()
        if offset is None:
            return timezone.utc, "UTC+0"
        total_seconds = int(offset.total_seconds())
        sign = "+" if total_seconds >= 0 else "-"
        abs_seconds = abs(total_seconds)
        hours = abs_seconds // 3600
        minutes = (abs_seconds % 3600) // 60
        if minutes == 0:
            tz_str = f"UTC{sign}{hours}"
        else:
            tz_str = f"UTC{sign}{hours}:{minutes:02d}"
        return tz, tz_str
    except Exception:
        return timezone.utc, "UTC+0"

LOCAL_TZ, LOCAL_TZ_STR = get_local_tz_info()


def build_quota_card(quota_data):
    account_email = get_zcode_account()
    quota_data = quota_data or {}
    logged_in = quota_data.get("logged_in", bool(account_email))
    engine_version = quota_data.get("engine_version", "")
    plan_provider = quota_data.get("plan_provider", "")
    default_model = quota_data.get("default_model", "")

    header_content = "⚡ **ZCode 引擎账户看板** (z.ai Coding Plan)"
    if account_email:
        header_content += f"\n👤 **当前登录账号**: `{account_email}`"
    if engine_version:
        header_content += f"\n🛠️ **引擎版本**: `{engine_version}`"

    elements = [
        {"tag": "markdown", "content": header_content},
        {"tag": "hr"},
    ]

    if not logged_in:
        elements.append({
            "tag": "markdown",
            "content": "❌ **引擎未登录**\n请在宿主机运行 `zcode login zai` 完成 Z.ai 账号授权后再使用。"
        })
    else:
        rows = []
        if plan_provider:
            rows.append(f"• **订阅通道**: `{plan_provider}`")
        if default_model:
            rows.append(f"• **默认模型**: `{default_model}`")
        rows.append("• **用量明细**: 前往 [Z.ai 控制台](https://z.ai/manage-apikey/subscription) 查看套餐余量")
        elements.append({"tag": "markdown", "content": "\n".join(rows)})

    elements += [
        {"tag": "hr"},
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🔄 刷新账户状态"},
                    "type": "primary",
                    "value": {"action": "refresh_quota"}
                }
            ]
        },
        create_footer(),
    ]

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"content": "📊 Z.ai 套餐账户查询", "tag": "plain_text"}
        },
        "elements": elements
    }


def build_context_card(stats):
    """
    构建飞书卡片消息：上下文使用率看板 (Context Usage Card)
    """
    model = stats.get("model", "glm-5.3-flash-high")
    total_tokens = stats.get("total_tokens", 0)
    max_tokens = stats.get("max_tokens", 1000000)
    total_pct = stats.get("total_pct", 0.0)
    
    user_tokens = stats.get("user_tokens", 0)
    user_pct = stats.get("user_pct", 0.0)
    agent_tokens = stats.get("agent_tokens", 0)
    agent_pct = stats.get("agent_pct", 0.0)
    tool_tokens = stats.get("tool_tokens", 0)
    tool_pct = stats.get("tool_pct", 0.0)
    free_tokens = stats.get("free_tokens", max_tokens)
    free_pct = stats.get("free_pct", 100.0)
    conv_id = stats.get("conv_id", "N/A")
    steps_count = stats.get("steps_count", 0)

    def format_k(num):
        if num >= 1000000:
            return f"{num / 1000000:.1f}M"
        elif num >= 1000:
            return f"{num / 1000:.1f}K"
        return str(num)

    def make_progress_bar(pct, length=12):
        filled = int(round((pct / 100.0) * length))
        filled = max(0, min(length, filled))
        return "🟩" * filled + "⬜" * (length - filled)

    bar_str = make_progress_bar(total_pct)

    header_template = "blue"
    if total_pct > 80:
        header_template = "red"
    elif total_pct > 50:
        header_template = "orange"

    elements = [
        {
            "tag": "markdown",
            "content": f"🤖 **当前模型**：`{model}`\n💬 **会话 ID**：`{conv_id}` (`{steps_count}` 步历史步骤)"
        },
        {"tag": "hr"},
        {
            "tag": "markdown",
            "content": f"⚡ **总体上下文用量 (Total Usage)**\n`{format_k(total_tokens)} / {format_k(max_tokens)} tokens` (**{total_pct}%**)\n{bar_str}"
        },
        {"tag": "hr"},
        {
            "tag": "column_set",
            "flex_mode": "none",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": f"👤 **User Messages**\n`{format_k(user_tokens)}` tokens (**{user_pct}%**)"
                        }
                    ]
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": f"🤖 **Agent Responses**\n`{format_k(agent_tokens)}` tokens (**{agent_pct}%**)"
                        }
                    ]
                }
            ]
        },
        {
            "tag": "column_set",
            "flex_mode": "none",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": f"🛠️ **Tool Calls**\n`{format_k(tool_tokens)}` tokens (**{tool_pct}%**)"
                        }
                    ]
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": f"🟩 **Free Space**\n`{format_k(free_tokens)}` tokens (**{free_pct}%**)"
                        }
                    ]
                }
            ]
        },
        {"tag": "hr"},
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🔄 刷新数据"},
                    "type": "primary",
                    "value": {"action": "user_choice", "choice": "/context", "label": "刷新数据"}
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🧹 清空上下文"},
                    "type": "danger",
                    "value": {"action": "user_choice", "choice": "/clear", "label": "清空上下文"}
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🎛️ 切换模型"},
                    "type": "default",
                    "value": {"action": "user_choice", "choice": "/model", "label": "切换模型"}
                }
            ]
        },
        create_footer()
    ]

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": header_template,
            "title": {"content": "🧠 上下文容量看板 (Context Usage)", "tag": "plain_text"}
        },
        "elements": elements
    }
