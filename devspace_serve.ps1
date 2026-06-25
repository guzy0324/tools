param(
    [int]$Port = 7676,
    [int]$TimeoutSeconds = 60
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# 确定 DevSpace 配置目录
if (-not [string]::IsNullOrWhiteSpace($env:DEVSPACE_CONFIG_DIR)) {
    $configDirectory = $env:DEVSPACE_CONFIG_DIR
}
else {
    $configDirectory = Join-Path $HOME ".devspace"
}

$authPath = Join-Path $configDirectory "auth.json"

# 检查 auth.json
if (-not (Test-Path -LiteralPath $authPath -PathType Leaf)) {
    Write-Host ""
    Write-Host "[错误] 找不到 DevSpace 认证文件：" -ForegroundColor Red
    Write-Host "  $authPath"
    Write-Host ""
    Write-Host "请先运行："
    Write-Host "  devspace init" -ForegroundColor Yellow
    exit 1
}

try {
    $authContent = Get-Content `
        -LiteralPath $authPath `
        -Raw `
        -Encoding UTF8

    # 顺便验证它是不是有效 JSON
    $null = $authContent | ConvertFrom-Json
}
catch {
    Write-Host ""
    Write-Host "[错误] 无法读取或解析 auth.json：" -ForegroundColor Red
    Write-Host "  $authPath"
    Write-Host ""
    Write-Host $_.Exception.Message
    exit 1
}

Write-Host ""
Write-Host "============================================================"
Write-Host "DevSpace auth.json："
Write-Host "路径：$authPath"
Write-Host ""
Write-Host $authContent -ForegroundColor Yellow
Write-Host "============================================================"
Write-Host ""
Write-Warning "auth.json 中包含 Owner password，请勿分享终端截图。"

# 检查 cloudflared
$cloudflaredCommand = Get-Command `
    cloudflared `
    -ErrorAction SilentlyContinue

if (-not $cloudflaredCommand) {
    Write-Host ""
    Write-Host "[错误] 找不到 cloudflared。" -ForegroundColor Red
    Write-Host "请确认 cloudflared 已安装并加入 PATH。"
    exit 1
}

# 检查 devspace
$devspaceCommand = Get-Command `
    devspace `
    -ErrorAction SilentlyContinue

if (-not $devspaceCommand) {
    Write-Host ""
    Write-Host "[错误] 找不到 devspace。" -ForegroundColor Red
    Write-Host "请确认 DevSpace 已安装并加入 PATH。"
    exit 1
}

$runId = [Guid]::NewGuid().ToString("N")

$stdoutLog = Join-Path `
    $env:TEMP `
    "devspace-cloudflared-$runId.out.log"

$stderrLog = Join-Path `
    $env:TEMP `
    "devspace-cloudflared-$runId.err.log"

$cloudflaredProcess = $null
$exitCode = 0

# 保存原有环境变量，退出时恢复
$oldOwnerToken = $env:DEVSPACE_OAUTH_OWNER_TOKEN
$oldPublicBaseUrl = $env:DEVSPACE_PUBLIC_BASE_URL

try {
    # 确保 DevSpace 使用 auth.json，而不是已有的环境变量密码
    Remove-Item `
        Env:DEVSPACE_OAUTH_OWNER_TOKEN `
        -ErrorAction SilentlyContinue

    Write-Host ""
    Write-Host "正在启动 Cloudflare Quick Tunnel..."

    $cloudflaredArguments = @(
        "tunnel"
        "--no-autoupdate"
        "--url"
        "http://127.0.0.1:$Port"
    )

    $cloudflaredProcess = Start-Process `
        -FilePath $cloudflaredCommand.Source `
        -ArgumentList $cloudflaredArguments `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -PassThru `
        -WindowStyle Hidden

    Write-Host "Cloudflare 进程 PID：$($cloudflaredProcess.Id)"
    Write-Host "正在等待公网 URL..."

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $publicUrl = $null

    while ((Get-Date) -lt $deadline -and -not $publicUrl) {
        Start-Sleep -Seconds 1

        $cloudflaredProcess.Refresh()

        if ($cloudflaredProcess.HasExited) {
            throw "Cloudflare Tunnel 意外退出，退出码为 $($cloudflaredProcess.ExitCode)。"
        }

        $logText = ""

        if (Test-Path -LiteralPath $stdoutLog) {
            $stdoutText = Get-Content `
                -LiteralPath $stdoutLog `
                -Raw `
                -ErrorAction SilentlyContinue

            if ($null -ne $stdoutText) {
                $logText += $stdoutText
            }
        }

        if (Test-Path -LiteralPath $stderrLog) {
            $stderrText = Get-Content `
                -LiteralPath $stderrLog `
                -Raw `
                -ErrorAction SilentlyContinue

            if ($null -ne $stderrText) {
                $logText += [Environment]::NewLine
                $logText += $stderrText
            }
        }

        $urlMatch = [Regex]::Match(
            $logText,
            "https://[A-Za-z0-9.-]+\.trycloudflare\.com"
        )

        if ($urlMatch.Success) {
            $publicUrl = $urlMatch.Value
        }
    }

    if (-not $publicUrl) {
        throw "$TimeoutSeconds 秒内没有获取到 Cloudflare 公网 URL。"
    }

    $mcpUrl = "$publicUrl/mcp"

    # DevSpace 使用不带 /mcp 的公网基地址
    $env:DEVSPACE_PUBLIC_BASE_URL = $publicUrl

    Write-Host ""
    Write-Host "============================================================"
    Write-Host "Cloudflare 公网地址：$publicUrl"
    Write-Host "ChatGPT MCP 地址：  $mcpUrl"
    Write-Host "============================================================"
    Write-Host ""

    if (Get-Command Set-Clipboard -ErrorAction SilentlyContinue) {
        Set-Clipboard -Value $mcpUrl
        Write-Host "MCP 地址已复制到剪贴板。"
    }
    elseif (Get-Command clip.exe -ErrorAction SilentlyContinue) {
        $mcpUrl | clip.exe
        Write-Host "MCP 地址已复制到剪贴板。"
    }
    else {
        Write-Warning "无法自动复制，请手动复制上面的 MCP 地址。"
    }

    Write-Host ""
    Write-Host "正在启动 DevSpace..."
    Write-Host "按 Ctrl+C 停止 DevSpace 和 Cloudflare Tunnel。"
    Write-Host ""

    & devspace serve

    if ($null -ne $LASTEXITCODE) {
        $exitCode = $LASTEXITCODE
    }
}
catch {
    $exitCode = 1

    Write-Host ""
    Write-Host "[错误] $($_.Exception.Message)" -ForegroundColor Red

    if (Test-Path -LiteralPath $stdoutLog) {
        Write-Host ""
        Write-Host "----- cloudflared stdout -----"

        Get-Content `
            -LiteralPath $stdoutLog `
            -ErrorAction SilentlyContinue
    }

    if (Test-Path -LiteralPath $stderrLog) {
        Write-Host ""
        Write-Host "----- cloudflared stderr -----"

        Get-Content `
            -LiteralPath $stderrLog `
            -ErrorAction SilentlyContinue
    }
}
finally {
    if ($cloudflaredProcess) {
        $cloudflaredProcess.Refresh()

        if (-not $cloudflaredProcess.HasExited) {
            Write-Host ""
            Write-Host "正在停止 Cloudflare Tunnel..."

            Stop-Process `
                -Id $cloudflaredProcess.Id `
                -Force `
                -ErrorAction SilentlyContinue

            $cloudflaredProcess.WaitForExit()
        }
    }

    Remove-Item `
        $stdoutLog, `
        $stderrLog `
        -Force `
        -ErrorAction SilentlyContinue

    # 恢复原有环境变量
    if ($null -eq $oldOwnerToken) {
        Remove-Item `
            Env:DEVSPACE_OAUTH_OWNER_TOKEN `
            -ErrorAction SilentlyContinue
    }
    else {
        $env:DEVSPACE_OAUTH_OWNER_TOKEN = $oldOwnerToken
    }

    if ($null -eq $oldPublicBaseUrl) {
        Remove-Item `
            Env:DEVSPACE_PUBLIC_BASE_URL `
            -ErrorAction SilentlyContinue
    }
    else {
        $env:DEVSPACE_PUBLIC_BASE_URL = $oldPublicBaseUrl
    }
}

exit $exitCode
