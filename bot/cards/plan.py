"""Card builder: account plans overview (/plan)."""

from cards.common import create_footer
from utils.plan_info import format_tokens


def _bar(remaining, total, length=12):
    if not total:
        return "⬜" * length
    pct = max(0.0, min(1.0, remaining / total))
    filled = int(round(pct * length))
    return "🟩" * filled + "⬜" * (length - filled)


_STATUS_ICON = {"active": "✅", "expired": "⛔", "": "❔"}


def _pick_plan_button_model(entitlements):
    """选该套餐当前最值得用的模型：优先有余量的、剩得最多的。"""
    usable = [e for e in entitlements if e.get("remaining", 0) > 0]
    if not usable:
        return None, 0
    best = max(usable, key=lambda e: e["remaining"])
    return best, best["remaining"]


def build_plan_card(overview, current_model=""):
    elements = [{
        "tag": "markdown",
        "content": (
            "🎫 **账户全部套餐总览**\n"
            f"> 数据时间：`{overview.get('fetched_at', '?')}`（ZCode 桌面端实时计费）"
        )
    }, {"tag": "hr"}]

    plans = overview.get("plans", [])
    if not plans:
        elements.append({
            "tag": "markdown",
            "content": "❌ 未获取到套餐数据。请确认 ZCode 桌面端已登录并正在运行，然后点刷新。",
        })
    else:
        for p in plans:
            icon = _STATUS_ICON.get(p.get("status"), "❔")
            header = f"{icon} **{p.get('name', '未命名套餐')}**"
            if p.get("status") == "active":
                header += "  `生效中`"
            if p.get("ends"):
                header += f"\n🕒 有效期至：{p['ends']}"
            body = header + "\n"
            for e in p.get("entitlements", []):
                body += (
                    f"\n• **{e['name']}**（`{e['model'] or '?'}`）"
                    f"\n  `{_bar(e['remaining'], e['total'])}` "
                    f"剩余 **{format_tokens(e['remaining'])}** / {format_tokens(e['total'])}"
                    f"　已用 {format_tokens(e['used'])}"
                )
                if e.get("period") == "daily":
                    body += "　🕐每日重置"
            elements.append({"tag": "markdown", "content": body.strip()})

            # 每个套餐后面的专属按钮
            btn_model, btn_remaining = _pick_plan_button_model(p.get("entitlements", []))
            if btn_model and p.get("active"):
                elements.append({
                    "tag": "action",
                    "actions": [{
                        "tag": "button",
                        "text": {"tag": "plain_text",
                                 "content": f"✅ 使用本套餐（{btn_model['name']} · 剩 {format_tokens(btn_remaining)}）"},
                        "type": "primary",
                        "value": {
                            "action": "switch_channel",
                            "channel": "plan",
                            "model": f"{btn_model['model']}-medium",
                            "plan": p.get("name", ""),
                        },
                    }],
                })
            else:
                elements.append({
                    "tag": "action",
                    "actions": [{
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "⛔ 本套餐暂无可用额度"},
                        "type": "danger",
                        "value": {"action": "noop"},
                    }],
                })
            elements.append({"tag": "hr"})

    if current_model:
        elements.append({
            "tag": "markdown",
            "content": f"🎯 当前会话模型：`{current_model}`",
        })
    elements.append({
        "tag": "action",
        "actions": [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🆓 API免费档(不耗套餐)"},
                "type": "default",
                "value": {"action": "switch_channel", "channel": "free"},
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🔄 刷新"},
                "type": "default",
                "value": {"action": "user_choice", "choice": "/plan", "label": "刷新套餐"},
            },
        ],
    })
    elements.append(create_footer())

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "violet",
            "title": {"content": "🎫 账户套餐与额度总览", "tag": "plain_text"},
        },
        "elements": elements,
    }
