# Install distinct commands; keep the npm-installed CLI intact.
$ErrorActionPreference = 'Stop'
$profileFile = $PROFILE.CurrentUserAllHosts
$entry = (Join-Path $PSScriptRoot 'devspace-control.ps1').Replace("'", "''")
$legacyEntry = (Join-Path $PSScriptRoot 'devspace.ps1').Replace("'", "''")
$marker = '# BEGIN managed devspace launcher'
$content = if (Test-Path -LiteralPath $profileFile) { Get-Content -LiteralPath $profileFile -Raw } else { '' }
if ($content -and -not (Test-Path -LiteralPath "$profileFile.before-devspace.bak")) {
    Copy-Item -LiteralPath $profileFile -Destination "$profileFile.before-devspace.bak"
}
$block = @"
$marker
if (Test-Path Function:\devspace) {
    if ((Get-Item Function:\devspace).Definition.Contains('$legacyEntry')) { Remove-Item Function:\devspace }
}
function global:devspace_serve { & '$entry' serve @args }
function global:devspace_stop { & '$entry' stop @args }
function global:devspace_status { & '$entry' status @args }
# END managed devspace launcher
"@
$pattern = '(?ms)^# BEGIN managed devspace launcher\r?\n.*?^# END managed devspace launcher\r?\n?'
$content = [regex]::Replace([string]$content, $pattern, '')
New-Item -ItemType Directory -Path (Split-Path $profileFile) -Force | Out-Null
Set-Content -LiteralPath $profileFile -Value ($content.TrimEnd() + "`n`n" + $block) -Encoding utf8
Write-Host "已安装 devspace_serve、devspace_stop、devspace_status。新终端直接使用；当前终端执行：. '$profileFile'"
