#!/bin/bash
# zcode 引擎一次性登录辅助：打印/打开 Z.ai 授权页并等待完成。
# 用法: bash engine_login.sh   （授权完成后 /model 面板与 AI 回复全量可用）
set -e
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

ZCODE_ENTRY=""
if command -v zcode &> /dev/null; then
    ZCODE_ENTRY=(zcode)
elif [ -f "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs" ]; then
    ZCODE_ENTRY=(node /Applications/ZCode.app/Contents/Resources/glm/zcode.cjs)
elif [ -f "$HOME/.zcode/zcode.cjs" ]; then
    ZCODE_ENTRY=(node "$HOME/.zcode/zcode.cjs")
else
    echo "❌ 未找到 zcode 引擎（可安装 ZCode Desktop 或 zcode CLI，或在 .env 设置 ZCODE_BIN）"
    exit 1
fi

echo "🔑 正在发起 Z.ai OAuth 授权（zcode 引擎登录）..."
PY="${DIR}/venv/bin/python3"
if [ -x "$PY" ]; then
    exec "$PY" - "$DIR" <<'EOF'
import pexpect, subprocess, sys, time, os, webbrowser

cands = []
if subprocess.run(["sh", "-c", "command -v zcode"], capture_output=True).stdout.strip():
    cands.append(["zcode"])
for p in ("/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs",
          os.path.expanduser("~/.zcode/zcode.cjs")):
    if os.path.exists(p):
        cands.append(["node", p])
        break

child = pexpect.spawn(cands[0][0], cands[0][1:] + ["login", "zai", "--no-browser"],
                      encoding="utf-8", timeout=600, echo=False)
child.expect("https://\\S+")
url = child.match.group(0)
print("👇 请在浏览器中完成授权（若未自动打开请手动复制该链接）：")
print(url)
try:
    webbrowser.open(url)
except Exception:
    pass
print("⏳ 等待授权完成（最长 10 分钟）...")
i = child.expect(["success", "成功", "Logged in", "logged in", pexpect.EOF, pexpect.TIMEOUT])
out = child.before or ""
print(out[-500:])
print("✅ 引擎登录流程结束。现在可在飞书中发送 /model 验证模型面板。")
EOF
else
    "${ZCODE_ENTRY[@]}" login zai
fi
