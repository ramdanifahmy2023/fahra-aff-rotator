param([string]$ReportFile)
$ErrorActionPreference = 'Stop'
$report = @{ tasks = @(); watchdogs = @(); health = $null }
$report.tasks = @(Get-ScheduledTask -TaskName 'Fahra-*' -ErrorAction SilentlyContinue | ForEach-Object {
    $info = $_ | Get-ScheduledTaskInfo
    [PSCustomObject]@{ Task = $_.TaskName; State = $_.State.ToString(); LastResult = $info.LastTaskResult;
        LastRun = $info.LastRunTime; User = $_.Principal.UserId; Limit = $_.Settings.ExecutionTimeLimit;
        RestartCount = $_.Settings.RestartCount; RestartInterval = $_.Settings.RestartInterval;
        Action = @($_.Actions | Select-Object Execute,Arguments,WorkingDirectory);
        Triggers = @($_.Triggers | Select-Object Enabled,Delay,StartBoundary,Repetition) }
})
foreach ($mode in @('worker', 'tunnel')) {
    $path = Join-Path $PSScriptRoot "data\runtime\$mode.json"
    if (Test-Path -LiteralPath $path) {
        $state = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
        $alive = $null -ne (Get-Process -Id $state.supervisor_pid -ErrorAction SilentlyContinue)
        $fresh = ((Get-Date).ToUniversalTime() - [datetime]::Parse($state.updated_at).ToUniversalTime()).TotalSeconds -lt 45
        $report.watchdogs += [PSCustomObject]@{ Mode = $mode; Status = $state.status; WatchdogAlive = $alive; Fresh = $fresh; Starts = $state.starts; ChildPID = $state.child_pid }
    }
}
try {
    $health = Invoke-RestMethod 'http://127.0.0.1:8788/api/health' -TimeoutSec 5
    $report.health = [PSCustomObject]@{ Worker = $health.worker; BridgeConnected = $health.bridge.connected; BridgeReady = $health.bridge.ready }
} catch { Write-Warning 'Webapp tidak merespons pada port default 8788; periksa .env dan log watchdog.' }
if ($ReportFile) {
    $report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ReportFile -Encoding UTF8
    return
}
$report.tasks | Select-Object Task,State,LastResult,LastRun | Format-Table -AutoSize
$report.watchdogs | Format-Table -AutoSize
$report.health | Format-List
if ('Fahra-worker' -notin $report.tasks.Task) { Write-Warning 'Task Local Service mungkin tidak terlihat tanpa Administrator. Jalankan status dari PowerShell Administrator.' }
Write-Output 'Status SSH bukan bukti MCP dari VPS. Jalankan check_mcp.py di VPS untuk uji end-to-end.'
