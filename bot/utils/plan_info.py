"""Account plan overview utilities for the /plan command.

Data source: the ZCode desktop host polls
    https://zcode.z.ai/api/v1/zcode-plan/billing/balance
every ~30s and logs the complete JSON response (plans + balances) to
    ~/.zcode/v2/logs/YYYY-MM-DD.log
under the marker ``billing/balance 请求完成``. That endpoint itself requires
V4-signed requests, so we parse the freshest logged response instead — always
current as long as the desktop app is running.
"""

import glob
import json
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

from logger import log

_MARK = "billing/balance 请求完成 "
_BEIJING = timezone(timedelta(hours=8))


def _log_files():
    home = os.path.expanduser("~")
    files = glob.glob(os.path.join(home, ".zcode", "v2", "logs", "*.log"))
    return sorted(files, reverse=True)[:3]  # newest first


def _latest_billing_payload() -> Optional[dict]:
    """Return the newest billing payload dict found in desktop logs."""
    for path in _log_files():
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                last_hit = None
                for line in f:
                    if _MARK in line:
                        last_hit = line
            if not last_hit:
                continue
            start = last_hit.find("{")
            # lines can carry trailing renderer noise; trim to balanced JSON
            depth = 0
            for i in range(start, len(last_hit)):
                ch = last_hit[i]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            obj = json.loads(last_hit[start:i + 1])
                        except Exception:
                            break
                        if isinstance(obj.get("payload"), dict):
                            return obj["payload"]
                        if isinstance(obj.get("data"), dict):
                            return obj["data"]
                        break
        except Exception as e:
            log.debug(f"[plan] parse {path} failed: {e}")
    return None


def _fmt_ts(ts) -> str:
    try:
        dt = datetime.fromtimestamp(int(ts), tz=_BEIJING)
        return dt.strftime("%m-%d %H:%M")
    except Exception:
        return ""


def _fmt_expiry(ends_at) -> str:
    try:
        dt = datetime.fromtimestamp(int(ends_at), tz=_BEIJING)
        now = datetime.now(tz=_BEIJING)
        days = (dt - now).total_seconds() / 86400
        base = dt.strftime("%m-%d %H:%M")
        if days < 0:
            return f"{base}（已过期）"
        return f"{base}（剩 {days:.1f} 天）"
    except Exception:
        return ""


def fetch_plan_overview() -> Optional[dict]:
    """Aggregate all account plans (paid / free / activity) for the /plan card."""
    payload = _latest_billing_payload()
    if not payload:
        return None
    data = payload.get("data") or payload
    plans_raw = data.get("plans") or []
    balances_raw = {b.get("entitlement_id"): b for b in (data.get("balances") or [])}

    plans = []
    for p in plans_raw:
        ents = []
        for e in p.get("entitlements", []):
            eid = e.get("entitlement_id", "")
            b = balances_raw.get(eid, {})
            total = b.get("total_units") or e.get("grant_units") or 0
            used = b.get("used_units") or 0
            remaining = b.get("remaining_units", max(0, total - used))
            ents.append({
                "name": e.get("show_name", eid),
                "model": (e.get("capabilities") or [""])[0].replace("model:", ""),
                "total": total,
                "used": used,
                "remaining": remaining,
                "period": e.get("period", ""),
                "expires": _fmt_ts(b.get("expires_at")) if b.get("expires_at") else "",
            })
        plans.append({
            "name": p.get("name", ""),
            "status": p.get("status", ""),
            "active": p.get("status") == "active",
            "starts": _fmt_ts(p.get("starts_at")),
            "ends": _fmt_expiry(p.get("ends_at")),
            "entitlements": ents,
        })

    # 按模型聚合所有套餐桶：服务端按 priority 降序扣减，跳过已耗尽/过期的桶
    import time as _time
    now_ts = _time.time()
    model_buckets = {}
    for p in plans_raw:
        for e in p.get("entitlements", []):
            eid = e.get("entitlement_id", "")
            b = balances_raw.get(eid, {})
            model = (e.get("capabilities") or [""])[0].replace("model:", "")
            if not model:
                continue
            remaining = b.get("remaining_units", 0) or 0
            expires_at = b.get("expires_at") or p.get("ends_at") or 0
            expired = bool(expires_at and int(expires_at) < now_ts) or p.get("status") != "active"
            model_buckets.setdefault(model, []).append({
                "plan": p.get("name", ""),
                "remaining": remaining,
                "total": b.get("total_units") or e.get("grant_units") or 0,
                "priority": e.get("priority", 0) or 0,
                "period": e.get("period", ""),
                "expired": expired,
                "depleted": remaining <= 0,
                "expires": _fmt_ts(expires_at) if expires_at else "",
            })
    for model, buckets in model_buckets.items():
        buckets.sort(key=lambda x: x["priority"], reverse=True)
        # 标记当前实际扣减桶：第一个未过期且有余量的
        for b in buckets:
            b["active"] = not b["expired"] and not b["depleted"]
        # 只有首个 active 为真扣减入口
        seen_active = False
        for b in buckets:
            if b["active"] and not seen_active:
                seen_active = True
            elif b["active"]:
                b["active"] = False

    return {
        "fetched_at": datetime.now(tz=_BEIJING).strftime("%m-%d %H:%M:%S"),
        "server_time": _fmt_ts(data.get("server_time")),
        "plans": plans,
        "models": model_buckets,
    }


def format_tokens(n) -> str:
    n = n or 0
    if n >= 100_000_000:
        return f"{n / 100_000_000:.2f} 亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f} 万"
    return str(int(n))
