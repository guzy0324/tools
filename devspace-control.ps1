param([Parameter(ValueFromRemainingArguments = $true)][string[]]$CommandArgs)

$official = Get-Command devspace.cmd -ErrorAction Stop
$cli = Join-Path (Split-Path $official.Source) 'node_modules/@waishnav/devspace/bin/devspace.js'
if (-not (Test-Path -LiteralPath $cli)) { throw "找不到官方 DevSpace CLI：$cli" }
& node (Join-Path $PSScriptRoot 'devspace-manager.mjs') --cli $cli @CommandArgs
