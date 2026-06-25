#!/usr/bin/env bash

set -Eeuo pipefail

PORT="${DEVSPACE_PORT:-7676}"
TIMEOUT_SECONDS="${DEVSPACE_TUNNEL_TIMEOUT:-60}"

CONFIG_DIR="${DEVSPACE_CONFIG_DIR:-${HOME}/.devspace}"
AUTH_FILE="${CONFIG_DIR}/auth.json"

LOG_FILE="$(mktemp "${TMPDIR:-/tmp}/devspace-cloudflared.XXXXXX.log")"

CF_PID=""
PUBLIC_URL=""
EXIT_CODE=0

cleanup() {
    local status=$?

    echo
    echo "正在停止 Cloudflare Tunnel..."

    if [[ -n "${CF_PID}" ]]; then
        if command -v taskkill.exe >/dev/null 2>&1; then
            taskkill.exe /PID "${CF_PID}" /T /F >/dev/null 2>&1 || true
        elif kill -0 "${CF_PID}" >/dev/null 2>&1; then
            kill "${CF_PID}" >/dev/null 2>&1 || true
            wait "${CF_PID}" >/dev/null 2>&1 || true
        fi
    fi

    rm -f "${LOG_FILE}"

    exit "${status}"
}

trap cleanup EXIT

# 检查依赖
for command_name in cloudflared devspace grep; do
    if ! command -v "${command_name}" >/dev/null 2>&1; then
        echo "[错误] 找不到命令：${command_name}" >&2
        echo "请确认它已安装并加入 PATH。" >&2
        exit 1
    fi
done

# 检查 auth.json
if [[ ! -f "${AUTH_FILE}" ]]; then
    echo "[错误] 找不到 DevSpace 认证文件：" >&2
    echo "  ${AUTH_FILE}" >&2
    echo >&2
    echo "请先运行：" >&2
    echo "  devspace init" >&2
    exit 1
fi

if [[ ! -s "${AUTH_FILE}" ]]; then
    echo "[错误] DevSpace 认证文件为空：" >&2
    echo "  ${AUTH_FILE}" >&2
    exit 1
fi

echo
echo "============================================================"
echo "DevSpace auth.json"
echo "路径：${AUTH_FILE}"
echo
cat "${AUTH_FILE}"
echo
echo "============================================================"
echo
echo "[警告] auth.json 包含 Owner password，请勿分享终端截图。"
echo

# 确保 DevSpace 使用 auth.json，而不是已有的环境变量密码
unset DEVSPACE_OAUTH_OWNER_TOKEN || true

echo "正在启动 Cloudflare Quick Tunnel..."

cloudflared tunnel \
    --no-autoupdate \
    --url "http://127.0.0.1:${PORT}" \
    >"${LOG_FILE}" 2>&1 &

CF_PID=$!

echo "Cloudflare 进程 PID：${CF_PID}"
echo "正在等待公网 URL..."

for ((second = 1; second <= TIMEOUT_SECONDS; second++)); do
    if ! kill -0 "${CF_PID}" >/dev/null 2>&1; then
        echo
        echo "[错误] Cloudflare Tunnel 意外退出。" >&2
        echo
        echo "----- cloudflared 日志 -----" >&2
        cat "${LOG_FILE}" >&2
        exit 1
    fi

    PUBLIC_URL="$(
        grep -Eo \
            'https://[A-Za-z0-9.-]+\.trycloudflare\.com' \
            "${LOG_FILE}" |
        head -n 1 ||
        true
    )"

    if [[ -n "${PUBLIC_URL}" ]]; then
        break
    fi

    sleep 1
done

if [[ -z "${PUBLIC_URL}" ]]; then
    echo
    echo "[错误] ${TIMEOUT_SECONDS} 秒内没有获取到 Cloudflare 公网 URL。" >&2
    echo
    echo "----- cloudflared 日志 -----" >&2
    cat "${LOG_FILE}" >&2
    exit 1
fi

MCP_URL="${PUBLIC_URL}/mcp"

# DevSpace 使用不带 /mcp 的公网基地址
export DEVSPACE_PUBLIC_BASE_URL="${PUBLIC_URL}"

echo
echo "============================================================"
echo "Cloudflare 公网地址：${PUBLIC_URL}"
echo "ChatGPT MCP 地址：  ${MCP_URL}"
echo "============================================================"
echo

# 自动复制 MCP URL
if command -v clip.exe >/dev/null 2>&1; then
    printf '%s' "${MCP_URL}" | clip.exe
    echo "MCP 地址已复制到 Windows 剪贴板。"
elif command -v pbcopy >/dev/null 2>&1; then
    printf '%s' "${MCP_URL}" | pbcopy
    echo "MCP 地址已复制到 macOS 剪贴板。"
elif command -v xclip >/dev/null 2>&1; then
    printf '%s' "${MCP_URL}" | xclip -selection clipboard
    echo "MCP 地址已复制到剪贴板。"
else
    echo "未找到剪贴板命令，请手动复制 MCP 地址。"
fi

echo
echo "正在启动 DevSpace..."
echo "按 Ctrl+C 停止 DevSpace 和 Cloudflare Tunnel。"
echo

set +e
devspace serve
EXIT_CODE=$?
set -e

exit "${EXIT_CODE}"
