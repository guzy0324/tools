param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ScriptArgs
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$callerDir = (Get-Location).ProviderPath
$shScript = Join-Path $PSScriptRoot "devspace_serve.sh"

if (-not (Test-Path -LiteralPath $shScript -PathType Leaf)) {
    Write-Host "[错误] 找不到脚本：" -ForegroundColor Red
    Write-Host "  $shScript"
    exit 1
}

$shCommand = Get-Command sh.exe -ErrorAction SilentlyContinue

if (-not $shCommand) {
    $shCommand = Get-Command sh -ErrorAction SilentlyContinue
}

if (-not $shCommand) {
    Write-Host "[错误] 找不到 sh。" -ForegroundColor Red
    Write-Host "请安装 Git Bash，并确保 sh.exe 已加入 PATH。"
    exit 1
}

$exitCode = 1

try {
    $env:DEVSPACE_ALLOWED_ROOTS = $callerDir

    Push-Location -LiteralPath $PSScriptRoot

    & $shCommand.Source "./devspace_serve.sh" @ScriptArgs

    if ($null -ne $LASTEXITCODE) {
        $exitCode = $LASTEXITCODE
    }
    else {
        $exitCode = 0
    }
}
catch {
    Write-Host ""
    Write-Host "[错误] 无法运行 devspace_serve.sh：" -ForegroundColor Red
    Write-Host $_.Exception.Message
    $exitCode = 1
}
finally {
    Pop-Location
}

exit $exitCode
