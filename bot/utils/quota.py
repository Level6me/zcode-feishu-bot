"""Quota & account utilities for the zcode engine.

The zcode CLI stores its OAuth credentials in
    <ZCODE_HOME>/v2/credentials.json
AES-256-GCM encrypted with a machine-derived secret (or the
ZCODE_CREDENTIAL_SECRET env var). We decrypt via a tiny node one-liner —
node is guaranteed present because the zcode engine itself runs on it.

Public API:
    is_quota_error() / detect_and_format_quota_error()
    check_latest_zcode_log_for_quota_error()
"""

import asyncio
import json
import os
import re
import subprocess
import time
from typing import Optional

from logger import log

_NODE_DECRYPT_SCRIPT = r"""
const crypto = require("crypto");
const os = require("os");
const value = process.argv[1];
if (!value || !value.startsWith("enc:v1:")) { console.log(""); process.exit(0); }
function resolveSecret(env) {
  const explicit = (env.ZCODE_CREDENTIAL_SECRET || "").trim();
  if (explicit) return explicit;
  let username = "unknown";
  try { username = os.userInfo().username; } catch (e) {}
  return `zcode-credential-fallback:${os.platform()}:${os.homedir()}:${username}`;
}
try {
  const key = crypto.createHash("sha256").update(resolveSecret(process.env)).digest();
  const [iv, tag, ct] = value.slice("enc:v1:".length).split(".");
  const d = crypto.createDecipheriv("aes-256-gcm", key, Buffer.from(iv, "base64url"));
  d.setAuthTag(Buffer.from(tag, "base64url"));
  const out = Buffer.concat([d.update(Buffer.from(ct, "base64url")), d.final()]).toString();
  console.log(out);
} catch (e) { console.log(""); }
"""

_ACCOUNT_CACHE = {"ts": 0, "data": None}
_ACCOUNT_CACHE_TTL = 300


def _node_decrypt(value: str) -> str:
    """Decrypt one enc:v1 credential value via node (engine dependency)."""
    if not value or not value.startswith("enc:v1:"):
        return value
    try:
        out = subprocess.check_output(
            ["node", "-e", _NODE_DECRYPT_SCRIPT, value],
            text=True, timeout=5,
        )
        return out.strip()
    except Exception as e:
        log.debug(f"[quota] node decrypt failed: {e}")
        return ""


def _load_account_info() -> dict:
    """Load {email, provider, user_id} from the zcode credential store."""
    now = time.time()
    if _ACCOUNT_CACHE["data"] is not None and now - _ACCOUNT_CACHE["ts"] < _ACCOUNT_CACHE_TTL:
        return _ACCOUNT_CACHE["data"]
    info = {"email": "", "provider": "", "user_id": ""}
    try:
        from config import get_oauth_token_path

        path = get_oauth_token_path()
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                info["provider"] = _node_decrypt(data.get("/oauth:active_provider", "")) or ""
                raw_user = _node_decrypt(data.get("/oauth:zai:user_info", ""))
                if raw_user:
                    try:
                        user = json.loads(raw_user)
                        info["email"] = user.get("email") or ""
                        info["user_id"] = user.get("user_id") or ""
                    except Exception:
                        pass
    except Exception as e:
        log.debug(f"[quota] load account info failed: {e}")
    _ACCOUNT_CACHE["ts"] = now
    _ACCOUNT_CACHE["data"] = info
    return info



def get_zcode_engine_version() -> str:
    try:
        from config import find_zcode_bin
        bin_argv = find_zcode_bin()
        if not bin_argv:
            return ""
        out = subprocess.check_output(bin_argv + ["version"], text=True, timeout=5).strip()
        return out.splitlines()[0] if out else ""
    except Exception:
        return ""


_QUOTA_ERROR_PATTERNS = (
    "insufficient balance",
    "no resource package",
    "额度已用尽",
    "配额不足",
    "quota exceeded",
    "rate limit",
    "usage limit",
    "quota has been exhausted",
    "resource_exhausted",
    "429",
    "exceeded your current quota",
)


def is_quota_error(reply_text: str) -> bool:
    if not reply_text:
        return False
    lowered = str(reply_text).lower()
    return any(p in lowered for p in _QUOTA_ERROR_PATTERNS)


def check_latest_zcode_log_for_quota_error(max_lines: int = 50) -> Optional[str]:
    """Scan the tail of the latest zcode log file for quota errors."""
    try:
        from config import get_zcode_home

        log_dir = os.path.join(get_zcode_home(), "v2", "logs")
        if not os.path.isdir(log_dir):
            return None
        files = [os.path.join(log_dir, f) for f in os.listdir(log_dir) if f.endswith(".log")]
        if not files:
            return None
        latest = max(files, key=os.path.getmtime)
        with open(latest, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()[-max_lines:]
        tail = "".join(lines).lower()
        for pattern in _QUOTA_ERROR_PATTERNS:
            if pattern in tail:
                return f"在引擎日志中发现配额相关错误: {pattern}"
        return None
    except Exception as e:
        log.warning(f"[quota] Failed to check latest zcode log: {e}")
        return None



async def detect_and_format_quota_error(error_text: str, model: str = "") -> Optional[str]:
    """If the failure looks like quota exhaustion, return a friendly reply."""
    text = (error_text or "").strip()
    if not text or not is_quota_error(text):
        # Duration fallback via engine log tail
        log_line = check_latest_zcode_log_for_quota_error()
        if not log_line:
            return None
        text = log_line

    info = _load_account_info()
    account_note = f" (`{info['email']}`)" if info.get("email") else ""
    model_note = f"\n> 🤖 模型：`{model}`" if model else ""
    return (
        "⚠️ **额度/配额受限**\n\n"
        f"> 👤 当前登录账号{account_note}{model_note}\n"
        "> 💡 **建议**：前往 [Z.ai 控制台](https://z.ai/manage-apikey/subscription) 查看套餐余量与用量明细，"
        "或稍后重试 / 切换更轻量的模型（如 `glm-5-turbo`）。"
    )
