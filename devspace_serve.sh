#!/usr/bin/env bash

set -Eeuo pipefail

PORT="${DEVSPACE_PORT:-7676}"
TIMEOUT_SECONDS="${DEVSPACE_TUNNEL_TIMEOUT:-60}"

CONFIG_DIR="${DEVSPACE_CONFIG_DIR:-${HOME}/.devspace}"
AUTH_FILE="${CONFIG_DIR}/auth.json"

DEVSPACE_NPM_PACKAGE="${DEVSPACE_NPM_PACKAGE:-@waishnav/devspace}"

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

extract_version() {
    grep -Eo '[0-9]+(\.[0-9]+){1,3}([-+][A-Za-z0-9.-]+)?' | head -n 1 || true
}

version_gt() {
    local latest="$1"
    local current="$2"

    node -e '
        const latest = process.argv[1];
        const current = process.argv[2];

        function parse(v) {
            return String(v)
                .replace(/^v/, "")
                .split(/[.-]/)
                .map(x => /^[0-9]+$/.test(x) ? Number(x) : x);
        }

        const a = parse(latest);
        const b = parse(current);
        const len = Math.max(a.length, b.length);

        for (let i = 0; i < len; i++) {
            const x = a[i] ?? 0;
            const y = b[i] ?? 0;

            if (typeof x === "number" && typeof y === "number") {
                if (x > y) process.exit(0);
                if (x < y) process.exit(1);
            } else {
                const xs = String(x);
                const ys = String(y);
                if (xs > ys) process.exit(0);
                if (xs < ys) process.exit(1);
            }
        }

        process.exit(1);
    ' "${latest}" "${current}"
}

ask_yes_no() {
    local prompt="$1"
    local default="${2:-n}"
    local answer=""

    if [[ "${default}" == "y" ]]; then
        read -r -p "${prompt} [Y/n]: " answer || answer=""
        answer="${answer:-y}"
    else
        read -r -p "${prompt} [y/N]: " answer || answer=""
        answer="${answer:-n}"
    fi

    case "${answer}" in
        y|Y|yes|YES|1|true|TRUE)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

# 检查依赖
for command_name in cloudflared devspace grep node; do
    if ! command -v "${command_name}" >/dev/null 2>&1; then
        echo "[错误] 找不到命令：${command_name}" >&2
        echo "请确认它已安装并加入 PATH。" >&2
        exit 1
    fi
done

# 检查 DevSpace 是否有新版本，并选择是否更新
echo
echo "正在检查 DevSpace 是否有新版本..."

CURRENT_DEVSPACE_VERSION="$(
    devspace --version 2>/dev/null |
    extract_version
)"

if [[ -n "${CURRENT_DEVSPACE_VERSION}" ]]; then
    echo "当前 DevSpace 版本：${CURRENT_DEVSPACE_VERSION}"
else
    echo "[警告] 无法解析当前 DevSpace 版本。"
fi

if ! command -v npm >/dev/null 2>&1; then
    echo "[警告] 找不到 npm，无法检查或更新 DevSpace。"
elif ! command -v node >/dev/null 2>&1; then
    echo "[警告] 找不到 node，无法比较 DevSpace 版本。"
else
    set +e
    LATEST_DEVSPACE_VERSION="$(
        npm view "${DEVSPACE_NPM_PACKAGE}" version 2>/dev/null |
        extract_version
    )"
    NPM_VIEW_EXIT_CODE=$?
    set -e

    if [[ "${NPM_VIEW_EXIT_CODE}" -ne 0 || -z "${LATEST_DEVSPACE_VERSION}" ]]; then
        echo "[警告] 无法从 npm 检查 DevSpace 最新版本。"
        echo "包名：${DEVSPACE_NPM_PACKAGE}"

        DEVSPACE_UPDATE_CHOICE="${DEVSPACE_UPDATE:-}"

        if [[ -z "${DEVSPACE_UPDATE_CHOICE}" ]]; then
            echo
            if ask_yes_no "是否仍然强制更新 DevSpace？" "n"; then
                DEVSPACE_UPDATE_CHOICE="y"
            else
                DEVSPACE_UPDATE_CHOICE="n"
            fi
        fi

        case "${DEVSPACE_UPDATE_CHOICE:-n}" in
            y|Y|yes|YES|1|true|TRUE)
                echo
                echo "正在强制更新 DevSpace..."
                npm install -g "${DEVSPACE_NPM_PACKAGE}@latest"
                echo "DevSpace 更新完成。"
                ;;
            *)
                echo "已选择：不更新 DevSpace。"
                ;;
        esac
    else
        echo "最新 DevSpace 版本：${LATEST_DEVSPACE_VERSION}"

        SHOULD_UPDATE="unknown"

        if [[ -n "${CURRENT_DEVSPACE_VERSION}" ]]; then
            if version_gt "${LATEST_DEVSPACE_VERSION}" "${CURRENT_DEVSPACE_VERSION}"; then
                SHOULD_UPDATE="yes"
            else
                SHOULD_UPDATE="no"
            fi
        fi

        if [[ "${SHOULD_UPDATE}" == "no" ]]; then
            echo "DevSpace 已经是最新版本，跳过更新。"
        else
            if [[ "${SHOULD_UPDATE}" == "yes" ]]; then
                echo
                echo "发现新版本：${CURRENT_DEVSPACE_VERSION} -> ${LATEST_DEVSPACE_VERSION}"
            else
                echo
                echo "无法确认当前版本是否低于最新版本。"
            fi

            DEVSPACE_UPDATE_CHOICE="${DEVSPACE_UPDATE:-}"

            if [[ -z "${DEVSPACE_UPDATE_CHOICE}" ]]; then
                echo
                if ask_yes_no "是否更新 DevSpace？" "n"; then
                    DEVSPACE_UPDATE_CHOICE="y"
                else
                    DEVSPACE_UPDATE_CHOICE="n"
                fi
            fi

            case "${DEVSPACE_UPDATE_CHOICE:-n}" in
                y|Y|yes|YES|1|true|TRUE)
                    echo
                    echo "正在更新 DevSpace..."
                    npm install -g "${DEVSPACE_NPM_PACKAGE}@latest"
                    echo "DevSpace 更新完成。"

                    echo
                    echo "更新后的 DevSpace 版本："
                    devspace --version || true
                    ;;
                *)
                    echo "已选择：不更新 DevSpace。"
                    ;;
            esac
        fi
    fi
fi

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

# 此变量仅用于启动器接收 PowerShell 调用目录；新版 DevSpace 不再读取它。
if [[ -z "${DEVSPACE_ALLOWED_ROOTS:-}" ]]; then
    export DEVSPACE_ALLOWED_ROOTS="$(pwd -P)"
fi

# Git Bash 的 /c/... 路径需转换为 Windows Node 可识别的路径。
CONFIG_PATH="${CONFIG_DIR}/config.jsonc"
PROJECT_ROOT="${DEVSPACE_ALLOWED_ROOTS}"
if command -v cygpath >/dev/null 2>&1; then
    CONFIG_PATH="$(cygpath -w "${CONFIG_PATH}")"
    PROJECT_ROOT="$(cygpath -w "${PROJECT_ROOT}")"
fi

# config 输出经过 DevSpace 解析、校验的 JSON，避免自行解析 JSONC。
# 保留其他设置（包括 subagents），首次改写前备份原文件。
DEVSPACE_CONFIG_JSON="$(devspace config)"
printf '%s' "${DEVSPACE_CONFIG_JSON}" | node -e '
    const fs = require("node:fs");
    const path = require("node:path");
    const [file, publicUrl, root, portText] = process.argv.slice(1);
    const config = JSON.parse(fs.readFileSync(0, "utf8"));
    const port = Number(portText);
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
        throw new Error("Invalid DEVSPACE_PORT");
    }
    config.server = { ...config.server, publicBaseUrl: publicUrl, port };
    config.workspaces = { ...config.workspaces, allowedRoots: [root] };
    config.ui = { ...config.ui, enabled: false };
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const backup = file + ".before-launcher.bak";
    if (fs.existsSync(file) && !fs.existsSync(backup)) fs.copyFileSync(file, backup);
    const temporary = file + ".launcher-" + process.pid + ".tmp";
    fs.writeFileSync(temporary, JSON.stringify(config, null, 2) + "\n", { mode: 0o600 });
    fs.renameSync(temporary, file);
' "${CONFIG_PATH}" "${PUBLIC_URL}" "${PROJECT_ROOT}" "${PORT}"
unset DEVSPACE_CONFIG_JSON

echo "已更新 DevSpace 配置：${CONFIG_PATH}"

echo
echo "============================================================"
echo "Cloudflare 公网地址：${PUBLIC_URL}"
echo "ChatGPT MCP 地址：  ${MCP_URL}"
echo "DevSpace 允许根目录：${DEVSPACE_ALLOWED_ROOTS}"
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
