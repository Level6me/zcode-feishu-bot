#!/bin/bash
# ==========================================================
# ZCode Feishu Bot 一键部署脚本
# 用法:
#   bash install.sh            # 一键部署/修复到可用状态（幂等，可重复执行）
#   bash install.sh status     # 查看运行状态
#   bash install.sh stop       # 停止
#   bash install.sh restart    # 重启
#   bash install.sh uninstall  # 停止并注销服务（保留源码与配置）
# 环境要求:
#   - macOS 或 Linux
#   - ZCode 桌面版（已登录 Z.ai 账号）或独立 zcode CLI
#   - Node.js（引擎运行时，ZCode 桌面版自带运行目录则可不装）
#   - Python 3.10+（脚本会自动探测，缺失时提示安装）
# ==========================================================
set -uo pipefail

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

BOT_NAME="zcode-feishu-bot"
LOG_FILE="${DIR}/run.log"
PID_FILE="${DIR}/.bot.pid"
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
fail()  { echo -e "${RED}[✗]${NC} $1"; }

# ---------- 运行管理 ----------
stop_bot() {
    if [ -f "$PID_FILE" ]; then
        local pid; pid=$(cat "$PID_FILE")
        kill -9 "$pid" 2>/dev/null && info "已停止进程 $pid" || warn "进程 $pid 已不存在"
        rm -f "$PID_FILE"
    fi
    # 清理所有残留实例（防止多进程分食消息）。
    # 注意 macOS 会把 venv python 的进程名改写为 ".../MacOS/Python main.py"，
    # 因此按 "main.py" 匹配后再用 cwd 过滤本目录，避免误杀其他项目。
    local p cwd
    for p in $(pgrep -f "main\.py" 2>/dev/null); do
        cwd=$(lsof -w -a -d cwd -p "$p" -Fn 2>/dev/null | tail -1 | sed 's/^n//')
        if [ "$cwd" = "$DIR" ]; then
            kill -9 "$p" 2>/dev/null
            info "已清理残留实例 PID $p"
        fi
    done
    command -v pm2 >/dev/null 2>&1 && pm2 delete "$BOT_NAME" >/dev/null 2>&1
    sleep 1
}

start_bot() {
    stop_bot
    if command -v pm2 >/dev/null 2>&1; then
        pm2 start "$DIR/venv/bin/python3" --name "$BOT_NAME" \
            --output "$LOG_FILE" --error "$LOG_FILE" --merge-logs -- main.py >/dev/null
        pm2 save >/dev/null 2>&1
        info "已通过 PM2 启动（pm2 logs ${BOT_NAME}，日志文件 ${LOG_FILE}）"
    else
        nohup "$DIR/venv/bin/python3" main.py > "$LOG_FILE" 2>&1 < /dev/null &
        echo $! > "$PID_FILE"
        info "已后台启动（日志: ${LOG_FILE}）"
    fi
    # 健康检查：等待飞书 WS 连接
    echo -n "等待飞书连接"
    for i in $(seq 1 20); do
        echo -n "."
        sleep 1
        if grep -q "connected to wss" "$LOG_FILE" 2>/dev/null; then
            echo ""; info "🚀 机器人已上线（飞书 WebSocket 已连接）"; return 0
        fi
    done
    echo ""
    warn "20 秒内未确认 WS 连接，请查看日志: $LOG_FILE"
    return 1
}

case "${1:-install}" in
status)
    echo "=== 运行状态 ==="
    if [ -f "$PID_FILE" ] && kill -0 "$(cat $PID_FILE)" 2>/dev/null; then
        info "运行中 (PID $(cat $PID_FILE))"
    elif command -v pm2 >/dev/null 2>&1 && pm2 describe "$BOT_NAME" >/dev/null 2>&1; then
        info "运行中 (PM2: $BOT_NAME)"
    else
        warn "未运行"
    fi
    grep -q "connected to wss" "$LOG_FILE" 2>/dev/null && info "飞书 WS: 已连接" || warn "飞书 WS: 无连接记录"
    ls "$DIR/venv/bin/python3" >/dev/null 2>&1 && info "虚拟环境: 就绪" || warn "虚拟环境: 未创建"
    exit 0 ;;
stop)     stop_bot; exit 0 ;;
restart)  start_bot; exit 0 ;;
uninstall)
    stop_bot
    command -v pm2 >/dev/null 2>&1 && pm2 save >/dev/null 2>&1
    info "已停止并注销服务（源码与 .env 保留在 ${DIR}）"
    exit 0 ;;
esac

# ---------- 1. 环境探测 ----------
echo "=========================================="
echo "   ZCode Feishu Bot 一键部署"
echo "=========================================="

# zcode 引擎
ZCODE_CJS="/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs"
if command -v zcode >/dev/null 2>&1; then
    info "zcode 引擎: PATH 中的 zcode"
elif [ -f "$ZCODE_CJS" ]; then
    info "zcode 引擎: ZCode 桌面版（${ZCODE_CJS}）"
elif [ -f "$HOME/.zcode/zcode.cjs" ]; then
    info "zcode 引擎: ~/.zcode/zcode.cjs"
else
    fail "未找到 zcode 引擎！请先安装 ZCode 桌面版并登录 Z.ai 账号。"
    exit 1
fi

# Node（跑 zcode.cjs 需要）
if command -v node >/dev/null 2>&1; then
    info "Node.js: $(node --version)"
else
    fail "未找到 node（zcode 引擎运行所需）。请先安装 Node.js 18+。"
    exit 1
fi

# Python 3.10+
PY=""
for cand in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
            PY="$(command -v $cand)"; break
        fi
    fi
done
if [ -z "$PY" ]; then
    for cand in /opt/homebrew/bin/python3.1[0-9] /usr/local/bin/python3.1[0-9]; do
        if [ -x "$cand" ] && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
            PY="$cand"; break
        fi
    done
fi
if [ -z "$PY" ]; then
    fail "未找到 Python 3.10+（当前系统 Python 过旧）。安装示例："
    echo "  macOS: brew install python@3.14"
    echo "  Linux: sudo apt install python3.12-venv python3.12"
    exit 1
fi
info "Python: $($PY --version) ($PY)"

# ---------- 2. 虚拟环境与依赖 ----------
if [ ! -x "$DIR/venv/bin/python3" ]; then
    info "创建虚拟环境..."
    "$PY" -m venv "$DIR/venv" || { fail "venv 创建失败（Linux 需: sudo apt install python3-venv）"; exit 1; }
else
    info "虚拟环境: 已存在"
fi

if ! "$DIR/venv/bin/python3" -c "import lark_oapi" >/dev/null 2>&1; then
    info "安装依赖（默认源失败自动切换清华镜像）..."
    "$DIR/venv/bin/pip" install --upgrade pip -q 2>/dev/null
    "$DIR/venv/bin/pip" install -r requirements.txt -q 2>&1 | tail -2
    if ! "$DIR/venv/bin/python3" -c "import lark_oapi" >/dev/null 2>&1; then
        warn "默认源失败，切换清华镜像重试..."
        "$DIR/venv/bin/pip" install -r requirements.txt -q -i https://pypi.tuna.tsinghua.edu.cn/simple 2>&1 | tail -2
    fi
    "$DIR/venv/bin/python3" -c "import lark_oapi" >/dev/null 2>&1 && info "依赖: 安装完成" || { fail "依赖安装失败"; exit 1; }
else
    info "依赖: 已就绪"
fi

# ---------- 3. 自签证书（本地引擎 API 代理用） ----------
if [ ! -f "$DIR/certs/proxy.crt" ]; then
    mkdir -p "$DIR/certs"
    openssl req -x509 -newkey rsa:2048 -keyout "$DIR/certs/proxy.key" -out "$DIR/certs/proxy.crt" \
        -days 3650 -nodes -subj "/CN=127.0.0.1" -addext "subjectAltName=IP:127.0.0.1,DNS:localhost" >/dev/null 2>&1 \
        && info "已生成本地代理自签证书" || warn "证书生成失败（openssl 未安装？API免费档通道将不可用）"
else
    info "自签证书: 已存在"
fi

# ---------- 4. 配置 .env ----------
if [ ! -f "$DIR/.env" ]; then
    echo "------------------------------------------"
    echo "首次部署：请输入飞书应用凭证（开放平台 → 企业自建应用）"
    read -r -p "👉 FEISHU_APP_ID (例: cli_xxx): " app_id
    read -r -p "👉 FEISHU_APP_SECRET: " app_secret
    if [ -z "$app_id" ] || [ -z "$app_secret" ]; then
        fail "凭证不能为空"; exit 1
    fi
    cat > "$DIR/.env" <<EOF
FEISHU_APP_ID=$app_id
FEISHU_APP_SECRET=$app_secret
DEFAULT_MODEL=glm-5.3-flash-high
ZCODE_PROVIDER_ID=account:zai-start-plan
WORKSPACE_ROOT=$HOME
ENABLE_VOICE_REPLY=true
TTS_VOICE=zh-CN-XiaoxiaoNeural
EOF
    info ".env 已生成"
else
    info "配置: 使用现有 .env"
fi

# ---------- 5. API 免费档通道（可选） ----------
API_KEY=$(grep '^ZCODE_API_KEY=' "$DIR/.env" 2>/dev/null | cut -d= -f2-)
if [ -n "$API_KEY" ]; then
    PYBIN="$DIR/venv/bin/python3"; [ -x "$PYBIN" ] || PYBIN=python3
    "$PYBIN" "$DIR/scripts/configure_provider.py" --api-key "$API_KEY" && \
        info "API 免费档通道: 已配置（ZCODE_API_KEY）" || \
        warn "免费档 provider 配置失败（不影响计划通道）"
else
    info "API 免费档通道: 未配置（可选，仅计划通道也可用）"
fi

# ---------- 6. 引擎登录检查（计划通道） ----------
if [ -f "$HOME/.zcode/v2/credentials.json" ]; then
    info "Z.ai 账号: 检测到登录凭证（计划通道可用）"
else
    warn "Z.ai 账号未登录！机器人需要引擎登录后才能回复。"
    echo "    请先打开 ZCode 桌面版登录，或运行: bash engine_login.sh"
fi

# ---------- 7. 启动 ----------
echo "------------------------------------------"
start_bot

echo ""
echo "=========================================="
echo "🎉 部署完成！"
echo "  • 飞书里发消息即可对话（首个私聊用户自动成为管理员）"
echo "  • 常用指令: /help /plan /model /note /cron /plugins"
echo "  • 状态: bash install.sh status | 日志: tail -f $LOG_FILE"
echo "  • 切换模型通道: /plan 面板一键切换"
echo "=========================================="
