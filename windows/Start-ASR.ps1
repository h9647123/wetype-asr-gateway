$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$env:WECHAT_API_KEY = (Get-Content (Join-Path $root 'api-key.txt') -Raw).Trim()
if ($env:WECHAT_API_KEY.Length -lt 24) { throw 'Configure a strong API key before starting.' }
$version = Get-ChildItem 'C:\Program Files\Tencent\WeType' -Directory |
    Where-Object { Test-Path (Join-Path $_.FullName 'wetype_update.exe') } |
    Sort-Object { [version]$_.Name } -Descending | Select-Object -First 1
if (!$version) { throw 'Install WeType first.' }
$exe = Join-Path $root 'WeChatASRBridge\WeChatASRBridge.exe'
if (!(Test-Path $exe)) { throw 'Portable bridge executable was not found.' }
function Start-WeTypeUpdater {
    if (!(Get-Process wetype_update -ErrorAction SilentlyContinue)) {
        Start-Process (Join-Path $version.FullName 'wetype_update.exe') -WorkingDirectory $version.FullName -WindowStyle Hidden
    }
}
function Get-BridgeProcess {
    Get-Process WeChatASRBridge -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe }
}
Start-WeTypeUpdater
Start-Sleep -Seconds 10
if (!(Get-BridgeProcess)) {
    Start-Process $exe -ArgumentList @('--serve','--backend','wetype','--host','0.0.0.0','--port','8765','--wetype-concurrency','1') -WorkingDirectory (Split-Path $exe) -WindowStyle Hidden
}
Start-Sleep -Seconds 5
# Observe the actual executable, including portable bootloader child processes.
while (Get-BridgeProcess) {
    Start-WeTypeUpdater
    Start-Sleep -Seconds 5
}
throw 'Bridge process exited; the scheduled task restart policy will retry.'
