#!/bin/bash
# 配置 Z.ai Coding Plan API Key 直连模式（本地流式代理 + 非签名通道）
# 用法: bash engine_apikey.sh <你的APIKey>
#   1. .env 写入 ZCODE_API_KEY / ZCODE_PROVIDER_ID=zai-api / DEFAULT_MODEL
#   2. 注册个人 zai-api provider（baseUrl 指向本地代理 https://127.0.0.1:8787）
#   3. 代理由 main.py 启动时自动拉起（api_proxy.py + certs/ 自签证书）
set -e
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
KEY="${1:-}"

if [ -z "$KEY" ]; then
    echo "用法: bash engine_apikey.sh <APIKey>"
    echo "获取地址: https://z.ai/manage-apikey/apikey-list （Z.ai Coding Plan 项目 API Key）"
    exit 1
fi

python3 - "$DIR" "$KEY" <<'PYEOF'
import json, os, sys
bot_dir, key = sys.argv[1], sys.argv[2]

# 1) .env
env_path = os.path.join(bot_dir, ".env")
lines = []
if os.path.exists(env_path):
    lines = open(env_path).read().splitlines()

def set_env(k, v):
    global lines
    lines = [l for l in lines if not l.startswith(k + "=")] + [f"{k}={v}"]

set_env("ZCODE_API_KEY", key)
set_env("ZCODE_PROVIDER_ID", "zai-api")
set_env("DEFAULT_MODEL", "glm-4.7-flash")
open(env_path, "w").write("\n".join(lines) + "\n")
print("✅ .env 已更新 (ZCODE_API_KEY / zai-api / glm-4.7-flash)")

# 2) personal zai-api provider（注意：schema 禁止 builtinModelIds 字段！）
path = os.path.expanduser("~/.zcode/v2/provider_config.json")
cfg = json.load(open(path)) if os.path.exists(path) else {"schemaVersion": 1, "config": {}}
cfg.setdefault("config", {}).setdefault("providerConfigRules", {}).setdefault("providerRules", [])
rules = [r for r in cfg["config"]["providerConfigRules"]["providerRules"] if r.get("providerId") != "zai-api"]
rules.append({
    "providerId": "zai-api",
    "templateId": "zai-api",
    "providerName": "Z.ai Coding Plan (Feishu Bot Local Proxy)",
    "enabled": True,
    "config": {
        "group": "standard-personal",
        "access": {"type": "zhipu-coding-plan-api-key", "apiKey": key},
        "api": {"type": "anthropic-messages", "baseUrl": "https://127.0.0.1:8787/api/anthropic"},
        "personalModelIds": ["glm-4.7-flash", "GLM-5.3", "GLM-5.3-Flash", "GLM-5.2", "GLM-5-Turbo"],
    },
})
cfg["config"]["providerConfigRules"]["providerRules"] = rules
cfg["config"]["defaultModelSelection"] = {"providerId": "zai-api", "modelId": "glm-4.7-flash"}
json.dump(cfg, open(path, "w"), indent=2, ensure_ascii=False)
print("✅ 个人 provider 已注册（走本地代理，绕开 V4 签名通道）")
PYEOF

# 3) 自签证书（幂等）
if [ ! -f "$DIR/certs/proxy.crt" ]; then
    mkdir -p "$DIR/certs"
    openssl req -x509 -newkey rsa:2048 -keyout "$DIR/certs/proxy.key" -out "$DIR/certs/proxy.crt" \
        -days 3650 -nodes -subj "/CN=127.0.0.1" -addext "subjectAltName=IP:127.0.0.1,DNS:localhost" 2>/dev/null
    echo "✅ 已生成本地代理自签证书"
fi

echo ""
echo "完成。重启机器人生效: pm2 restart feishu-bot（或重跑 main.py）"
echo "提示: 若 API Key 已挂 GLM-5.x 资源包，可把 DEFAULT_MODEL 改为 glm-5.3-flash-high"
