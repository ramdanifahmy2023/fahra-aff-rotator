# Memulihkan worker lokal tanpa menutup Chrome dan tanpa menyalakan ulang tunnel.
# Izin Local Service yang hilang pada app.py dipasang lagi, lalu watchdog yang
# sudah hidup mengambilnya pada percobaan berikutnya. Bot Naira dinyalakan hanya
# kalau prosesnya tidak ada.
# Task Fahra-Morning-Health menjalankan skrip ini pukul 06:40 supaya kondisi
# sehat sudah tercapai sebelum pukul 07:00. Pemulihan profil juga memanggilnya
# setiap menit, jadi kerusakan yang sama tidak menunggu sampai pagi.
param(
    [switch]$Install,
    [switch]$Morning
)

$ErrorActionPreference = 'Continue'
$Root = $PSScriptRoot
$LogDir = Join-Path $Root 'data\runtime'
$LogFile = Join-Path $LogDir 'health-repair.log'
$NairaRoot = Join-Path (Split-Path -Parent $Root) 'repliz-worker-naira-aff'
$NairaStart = Join-Path $NairaRoot 'start.ps1'
$ServiceSid = 'S-1-5-19'

function Write-HealthLog([string]$Message) {
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    foreach ($attempt in 1..3) {
        try {
            Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8 -ErrorAction Stop
            break
        } catch {
            Start-Sleep -Milliseconds 200
        }
    }
    if ($Morning) { Write-Output $line }
}

function Install-MorningTask {
    $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -Morning' -f $PSCommandPath
    $action = New-ScheduledTaskAction -Execute $powershell -Argument $arguments -WorkingDirectory $Root
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $trigger = New-ScheduledTaskTrigger -Daily -At '06:40'
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 8) `
        -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName 'Fahra-Morning-Health' -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal `
        -Description 'Pulihkan izin Local Service, worker Fahra, dan bot Naira sebelum pukul 07:00. Chrome dan tunnel tidak dihidupkan ulang.' -Force | Out-Null
    Write-HealthLog 'task Fahra-Morning-Health terpasang, tiap hari pukul 06:40'
}

if ($Install) {
    Install-MorningTask
    exit 0
}

function Test-IsLocalService($Identity) {
    $value = [string]$Identity.Value
    if ($value -eq $ServiceSid -or $value -eq 'NT AUTHORITY\LOCAL SERVICE') { return $true }
    try {
        $sid = $Identity.Translate([System.Security.Principal.SecurityIdentifier])
        return $sid.Value -eq $ServiceSid
    } catch {
        return $false
    }
}

function Test-ServiceRead([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $true }
    try {
        $acl = Get-Acl -LiteralPath $Path
    } catch {
        return $false
    }
    $readData = [System.Security.AccessControl.FileSystemRights]::ReadData
    foreach ($rule in @($acl.Access)) {
        if ($rule.AccessControlType -ne 'Allow') { continue }
        if (-not (Test-IsLocalService $rule.IdentityReference)) { continue }
        if (($rule.FileSystemRights -band $readData) -eq $readData) { return $true }
    }
    return $false
}

function Test-InheritableServiceFull([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { return $false }
    try {
        $acl = Get-Acl -LiteralPath $Path
    } catch {
        return $false
    }
    $full = [System.Security.AccessControl.FileSystemRights]::FullControl
    foreach ($rule in @($acl.Access)) {
        if ($rule.AccessControlType -ne 'Allow') { continue }
        if (-not (Test-IsLocalService $rule.IdentityReference)) { continue }
        if ($rule.InheritanceFlags -notmatch 'ContainerInherit|ObjectInherit') { continue }
        if (($rule.FileSystemRights -band $full) -eq $full) { return $true }
    }
    return $false
}

function Grant-Service([string]$Path, [string]$Rights) {
    & icacls.exe $Path /grant ("*{0}:{1}" -f $ServiceSid, $Rights) /C | Out-Null
    return $LASTEXITCODE -eq 0
}

function Restore-ServiceAccess {
    $fixed = @()
    if (-not (Test-InheritableServiceFull $Root)) {
        if (Grant-Service $Root '(OI)(CI)(F)') { $fixed += 'folder rotator' }
        else { $fixed += 'folder rotator gagal' }
    }
    $files = @(
        (Join-Path $Root 'app.py'),
        (Join-Path $Root 'runtime_supervisor.py'),
        (Join-Path $Root '.env'),
        (Join-Path $Root 'data\affiliate.sqlite3')
    )
    foreach ($path in $files) {
        if (Test-ServiceRead $path) { continue }
        if (Grant-Service $path '(F)') { $fixed += (Split-Path -Leaf $path) }
        else { $fixed += ((Split-Path -Leaf $path) + ' gagal') }
    }
    $directories = @(
        (Join-Path $Root 'data'),
        (Join-Path $Root 'data\runtime')
    )
    foreach ($path in $directories) {
        if ((Test-ServiceRead $path) -and (Test-InheritableServiceFull $path)) { continue }
        if (Grant-Service $path '(OI)(CI)(F)') { $fixed += (Split-Path -Leaf $path) }
        else { $fixed += ((Split-Path -Leaf $path) + ' gagal') }
    }
    return @($fixed)
}

function Get-WorkerHealth {
    try {
        return Invoke-RestMethod -Uri 'http://127.0.0.1:8788/api/health' -TimeoutSec 5
    } catch {
        return $null
    }
}

function Test-WorkerHealthy($Health) {
    return ($null -ne $Health -and [string]$Health.status -eq 'ok' -and $Health.worker -eq $true)
}

function Test-WatchdogAlive {
    $path = Join-Path $LogDir 'worker.json'
    if (-not (Test-Path -LiteralPath $path)) { return $false }
    try {
        $state = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
        $process = Get-Process -Id ([int]$state.supervisor_pid) -ErrorAction SilentlyContinue
        return $null -ne $process
    } catch {
        return $false
    }
}

function Get-NairaProcesses {
    # $null artinya proses tidak terbaca. Jangan dianggap mati, supaya bot yang
    # sedang sehat tidak dinyalakan dua kali.
    $script:NairaQueryOk = $false
    try {
        $pythons = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction Stop)
    } catch {
        return ,@()
    }
    $script:NairaQueryOk = $true
    $found = @($pythons | Where-Object { $_.CommandLine -match '(?i)(^|\s)-m\s+naira(\s|$)' })
    $hidden = @($pythons | Where-Object { [string]$_.CommandLine -eq '' })
    if ($found.Count -eq 0 -and $hidden.Count -gt 0) {
        $script:NairaQueryOk = $false
    }
    return ,$found
}

function Stop-StuckNairaHost {
    try {
        $hosts = @(Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" | Where-Object {
            $_.CommandLine -like '*repliz-worker-naira-aff\start.ps1*'
        })
    } catch {
        return
    }
    foreach ($hostProcess in $hosts) {
        $started = $hostProcess.CreationDate
        if ($started -and ((Get-Date) - [datetime]$started).TotalSeconds -lt 40) { continue }
        Stop-Process -Id ([int]$hostProcess.ProcessId) -Force -ErrorAction SilentlyContinue
    }
}

function Wait-Until([scriptblock]$Probe, [int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        $found = & $Probe
        if ($found) { return $found }
        Start-Sleep -Seconds 3
    } while ((Get-Date) -lt $deadline)
    return $null
}

$actions = @()
$fixed = @(Restore-ServiceAccess)
if ($fixed.Count -gt 0) { $actions += ('izin Local Service: ' + ($fixed -join ', ')) }

$health = Get-WorkerHealth
if (-not (Test-WorkerHealthy $health)) {
    $waitSeconds = 20
    if ($Morning) { $waitSeconds = 60 }
    if (-not (Test-WatchdogAlive)) {
        try {
            Start-ScheduledTask -TaskName 'Fahra-worker' -ErrorAction Stop
            $actions += 'watchdog Fahra dinyalakan'
        } catch {
            $actions += 'watchdog Fahra tidak bisa dinyalakan dari akun ini'
        }
    }
    $health = Wait-Until { $probe = Get-WorkerHealth; if (Test-WorkerHealthy $probe) { $probe } } $waitSeconds
}

$workerReady = Test-WorkerHealthy $health
$readyCount = 0
if ($workerReady -and $health.bridge -and $null -ne $health.bridge.ready_accounts) {
    $readyCount = [int]$health.bridge.ready_accounts
}

$naira = Get-NairaProcesses
$nairaReady = $naira.Count -gt 0
if (-not $script:NairaQueryOk) { $nairaReady = $true }
if (-not $nairaReady -and (Test-Path -LiteralPath $NairaStart)) {
    Stop-StuckNairaHost
    try {
        Start-ScheduledTask -TaskName 'Naira-Bot' -ErrorAction Stop
        $actions += 'bot Naira dinyalakan'
    } catch {
        $actions += 'bot Naira tidak bisa dinyalakan'
    }
    $appeared = Wait-Until {
        $running = Get-NairaProcesses
        if ($script:NairaQueryOk -and $running.Count -gt 0) { $true }
    } 25
    $nairaReady = [bool]$appeared
}

if (-not $Morning -and $actions.Count -eq 0 -and $workerReady) { exit 0 }

$parts = @()
if ($actions.Count -gt 0) { $parts += ($actions -join '; ') }
if ($workerReady) { $parts += ("worker siap, akun bridge {0}" -f $readyCount) }
else { $parts += 'worker belum siap' }
if ($nairaReady) { $parts += 'bot Naira hidup' }
else { $parts += 'bot Naira belum hidup' }
$prefix = 'sehat'
if ($actions.Count -gt 0) { $prefix = 'dipulihkan' }
if (-not $workerReady -or -not $nairaReady) { $prefix = 'belum sehat' }
Write-HealthLog ('{0}: {1}' -f $prefix, ($parts -join '; '))

if ($workerReady -and $nairaReady) {
    if ($actions.Count -gt 0) { exit 2 }
    exit 0
}
exit 1
