param(
    [string]$ConfigFile = (Join-Path $PSScriptRoot 'data\runtime-config.json')
)

$ErrorActionPreference = 'Stop'
$runtime = Join-Path $PSScriptRoot 'data\runtime'
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
$resultFile = Join-Path $runtime 'install.json'
function Save-Result([string]$Status, [string]$Message) {
    @{ status = $Status; message = $Message; updated_at = (Get-Date).ToUniversalTime().ToString('o') } |
        ConvertTo-Json | Set-Content -LiteralPath $resultFile -Encoding UTF8
}
function Grant-Access([string]$Path, [string]$Grant, [switch]$Recurse) {
    $arguments = @($Path, '/grant', $Grant, '/q')
    if ($Recurse) { $arguments += '/t' }
    & icacls.exe @arguments | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Gagal mengatur akses: $Path" }
}
function Protect-ServiceDirectory([string]$Path) {
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($entry in @(@('S-1-5-18', 'FullControl'), @('S-1-5-32-544', 'FullControl'), @('S-1-5-19', 'ReadAndExecute'))) {
        $sid = [Security.Principal.SecurityIdentifier]::new($entry[0])
        $rule = [Security.AccessControl.FileSystemAccessRule]::new($sid, $entry[1], 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    $acl.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
    Set-Acl -LiteralPath $Path -AclObject $acl
}

try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Startup sebelum login membutuhkan PowerShell Run as Administrator (UAC).'
    }
    $config = Get-Content -LiteralPath $ConfigFile -Raw | ConvertFrom-Json
    foreach ($path in @($config.python, $config.ssh, $config.ssh_key_file, $config.ssh_known_hosts, $config.chrome)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "File tidak ditemukan: $path" }
    }
    if ($config.ssh_target -notmatch '^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+$') { throw 'Target SSH tidak valid.' }
    if ($config.chrome_profile -notmatch '^(Default|Profile [0-9]+)$') { throw 'Profil Chrome tidak valid.' }
    if ($config.windows_user_sid -notmatch '^S-1-5-21-[0-9-]+$') { throw 'SID pengguna Windows tidak valid.' }
    # Resolve uv's version alias junction before granting access to the actual runtime.
    $resolvedPython = & $config.python -c 'from pathlib import Path; import sys; print(Path(sys._base_executable).resolve())'
    if ($LASTEXITCODE -ne 0) { throw 'Tidak dapat menentukan interpreter Python asli.' }
    $config.python = $resolvedPython.Trim()
    $pythonDirectory = Split-Path -Parent $config.python
    $pythonw = Join-Path $pythonDirectory 'pythonw.exe'
    if (-not (Test-Path -LiteralPath $pythonw)) { throw 'pythonw.exe tidak ditemukan di instalasi Python.' }
    # No Windows password is stored. Only this low-privilege account reads runtime secrets.
    $serviceDirectory = Join-Path $env:ProgramData 'FahraAffiliateRotator'
    Protect-ServiceDirectory $serviceDirectory
    $serviceKey = Join-Path $serviceDirectory 'tunnel_ed25519'
    $serviceHosts = Join-Path $serviceDirectory 'known_hosts'
    Copy-Item -LiteralPath $config.ssh_key_file -Destination $serviceKey -Force
    Copy-Item -LiteralPath $config.ssh_known_hosts -Destination $serviceHosts -Force
    & icacls.exe $serviceKey /setowner '*S-1-5-19' /q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Gagal menetapkan pemilik SSH key untuk Local Service.' }
    $config.ssh_key_file = $serviceKey
    $config.ssh_known_hosts = $serviceHosts
    $serviceConfig = Join-Path $serviceDirectory 'runtime-config.json'
    $config | ConvertTo-Json | Set-Content -LiteralPath $serviceConfig -Encoding UTF8

    Grant-Access $pythonDirectory '*S-1-5-19:(OI)(CI)(RX)' -Recurse
    Grant-Access $PSScriptRoot '*S-1-5-19:(RX)'
    foreach ($name in @('app.py', 'runtime_supervisor.py', '.env')) {
        Grant-Access (Join-Path $PSScriptRoot $name) '*S-1-5-19:(R)'
    }
    Grant-Access (Join-Path $PSScriptRoot 'data') '*S-1-5-19:(OI)(CI)(M)' -Recurse

    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
    $boot = New-ScheduledTaskTrigger -AtStartup
    $boot.Delay = 'PT20S'
    # Also recover a watchdog that was stopped cleanly, once a minute.
    $retry = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
    $servicePrincipal = New-ScheduledTaskPrincipal -UserId 'S-1-5-19' -LogonType ServiceAccount -RunLevel Limited
    foreach ($mode in @('worker', 'tunnel')) {
        $name = "Fahra-$mode"
        $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($existing) {
            Export-ScheduledTask -TaskName $name | Set-Content -LiteralPath (Join-Path $runtime "$name.before.xml") -Encoding UTF8
        }
        $arguments = '"{0}" {1} --config "{2}"' -f (Join-Path $PSScriptRoot 'runtime_supervisor.py'), $mode, $serviceConfig
        $action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $PSScriptRoot
        Register-ScheduledTask -TaskName $name -Action $action -Trigger @($boot, $retry) `
            -Settings $settings -Principal $servicePrincipal -Description "Fahra $mode watchdog; startup and automatic recovery." -Force | Out-Null
    }
    $chromeLauncher = Join-Path $PSScriptRoot 'open-affiliate-profiles.ps1'
    if (-not (Test-Path -LiteralPath $chromeLauncher -PathType Leaf)) {
        throw 'open-affiliate-profiles.ps1 tidak ditemukan.'
    }
    $chromeTrigger = New-ScheduledTaskTrigger -AtLogOn -User $config.windows_user_sid
    $chromeTrigger.Delay = 'PT30S'
    $chromePrincipal = New-ScheduledTaskPrincipal -UserId $config.windows_user_sid -LogonType Interactive -RunLevel Limited
    $chromeAction = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
        -Argument ('-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $chromeLauncher) `
        -WorkingDirectory $PSScriptRoot
    $chromeSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName 'Fahra-Chrome' -Action $chromeAction -Trigger $chromeTrigger `
        -Settings $chromeSettings -Principal $chromePrincipal -Description 'Buka profil Chrome Shopee Affiliate saat pengguna Windows login.' -Force | Out-Null
    Start-ScheduledTask -TaskName 'Fahra-worker'
    Start-ScheduledTask -TaskName 'Fahra-tunnel'
    Save-Result 'installed' 'Startup worker/tunnel dan pemulihan Chrome terpasang. Periksa health dan status watchdog.'
} catch {
    Save-Result 'failed' $_.Exception.Message
    Write-Error $_
    exit 1
}
