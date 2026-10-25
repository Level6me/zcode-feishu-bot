#!/usr/bin/env python3
"""配置 ~/.zcode/v2/provider_config.json 中的 zai-api 个人 provider（免费档通道）。

用法: python3 configure_provider.py --api-key <KEY>
幂等：重复执行会更新而非重复添加。
"""
import argparse
import json
import os
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--base-url", default="https://127.0.0.1:8787/api/anthropic")
    args = parser.parse_args()

    path = os.path.expanduser("~/.zcode/v2/provider_config.json")
    cfg = {"schemaVersion": 1, "config": {}}
    if os.path.exists(path):
        try:
            cfg = json.load(open(path))
        except Exception:
            cfg = {"schemaVersion": 1, "config": {}}

    config = cfg.setdefault("config", {})
    rules = config.setdefault("providerConfigRules", {}).setdefault("providerRules", [])
    rules = [r for r in rules if r.get("providerId") != "zai-api"]
    rules.append({
        "providerId": "zai-api",
        "templateId": "zai-api",
        "providerName": "Z.ai Coding Plan (Feishu Bot Free Channel)",
        "enabled": True,
        "config": {
            # 注意：schema 禁止 builtinModelIds；个人模型放 personalModelIds
            "group": "standard-personal",
            "access": {"type": "zhipu-coding-plan-api-key", "apiKey": args.api_key},
            "api": {"type": "anthropic-messages", "baseUrl": args.base_url},
            "personalModelIds": ["glm-4.7-flash", "GLM-5.3", "GLM-5.3-Flash"],
        },
    })
    config["providerConfigRules"]["providerRules"] = rules
    json.dump(cfg, open(path, "w"), indent=2, ensure_ascii=False)
    print(f"ok: zai-api provider -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
