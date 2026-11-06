# Menjaga sepuluh profil affiliate tetap bisa dipakai.
# Profil yang jendelanya hilang dibuka lagi.
# Halaman captcha atau anti-bot ditutup tanpa diisi, lalu Custom Link dibuka setelah 5 menit.
# Cookie dashboard yang mati dimasuki ulang. Extension yang diam dibangunkan lewat halaman opsi.
param(
    [switch]$Install,
    [switch]$Check,
    [switch]$RecoverNow
)

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
. (Join-Path $Root 'affiliate-chrome.ps1')

$LogDir = Join-Path $Root 'data\runtime'
$LogFile = Join-Path $LogDir 'profile-repair.log'
$StateFile = Join-Path $LogDir 'profile-repair.json'
$HoldMinutes = 5

function Write-RepairLog([string]$Message) {
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
    Write-Output $line
}

function Install-RepairTask {
    $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $PSCommandPath
    $action = New-ScheduledTaskAction -Execute $powershell -Argument $arguments -WorkingDirectory $Root
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $start = (Get-Date).AddMinutes(1)
    $trigger = New-ScheduledTaskTrigger -Once -At $start `
        -RepetitionInterval (New-TimeSpan -Minutes 1) `
        -RepetitionDuration (New-TimeSpan -Days 31)
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 3) `
        -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName 'Fahra-Profile-Repair' -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal `
        -Description 'Pulihkan profil Chrome affiliate yang tertutup. Tutup captcha Shopee tanpa menyelesaikannya, lalu buka Custom Link setelah 5 menit.' -Force | Out-Null
    Write-RepairLog 'task Fahra-Profile-Repair terpasang, ulang tiap 1 menit'
}

if ($Install) {
    Install-RepairTask
    exit 0
}

function Update-RepairTaskIfExpiring {
    $task = Get-ScheduledTask -TaskName 'Fahra-Profile-Repair' -ErrorAction SilentlyContinue
    if (-not $task) {
        Install-RepairTask
        return
    }
    $trigger = $task.Triggers | Select-Object -First 1
    if (-not $trigger -or -not $trigger.StartBoundary) { return }
    $start = [datetime]::Parse([string]$trigger.StartBoundary)
    if ((Get-Date) -lt $start.AddDays(29)) { return }
    Install-RepairTask
}

function Read-RepairState {
    $state = @{ holds = @{}; reloads = @{}; misses = @{}; checked = @{}; bridge = $null }
    if (-not (Test-Path -LiteralPath $StateFile)) { return $state }
    try {
        $raw = Get-Content -LiteralPath $StateFile -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        return $state
    }
    if ($raw.holds) {
        foreach ($property in $raw.holds.PSObject.Properties) {
            $state.holds[$property.Name] = [string]$property.Value
        }
    }
    foreach ($key in @('reloads', 'checked')) {
        $source = $raw.$key
        if (-not $source) { continue }
        foreach ($property in $source.PSObject.Properties) {
            $state[$key][$property.Name] = [string]$property.Value
        }
    }
    if ($raw.misses) {
        foreach ($property in $raw.misses.PSObject.Properties) {
            $state.misses[$property.Name] = [int]$property.Value
        }
    }
    if ($raw.bridge) {
        $names = @()
        if ($raw.bridge.not_ready) { $names = @($raw.bridge.not_ready) }
        $state.bridge = @{
            at = [string]$raw.bridge.at
            ready_count = [int]$raw.bridge.ready_count
            not_ready = @($names | ForEach-Object { [string]$_ })
        }
    }
    return $state
}

function Write-RepairState($State) {
    $holds = @{}
    $reloads = @{}
    $checked = @{}
    $misses = @{}
    foreach ($name in @($State.holds.Keys)) { $holds[$name] = [string]$State.holds[$name] }
    foreach ($name in @($State.reloads.Keys)) { $reloads[$name] = [string]$State.reloads[$name] }
    foreach ($name in @($State.checked.Keys)) { $checked[$name] = [string]$State.checked[$name] }
    foreach ($name in @($State.misses.Keys)) { $misses[$name] = [int]$State.misses[$name] }
    $payload = @{ holds = $holds; reloads = $reloads; misses = $misses; checked = $checked }
    if ($State.bridge) {
        $payload.bridge = @{
            at = [string]$State.bridge.at
            ready_count = [int]$State.bridge.ready_count
            not_ready = @($State.bridge.not_ready)
        }
    }
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    $payload | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $StateFile -Encoding UTF8
}

function Get-HoldUntil($State, [string]$Name) {
    if (-not $State.holds.ContainsKey($Name)) { return $null }
    try { return [datetime]::Parse($State.holds[$Name]) } catch { return $null }
}

function Set-Hold($State, [string]$Name, [datetime]$Until) {
    $State.holds[$Name] = $Until.ToString('o')
    Write-RepairState $State
}

function Clear-Hold($State, [string]$Name) {
    if (-not $State.holds.ContainsKey($Name)) { return }
    $State.holds.Remove($Name)
    Write-RepairState $State
}

function Test-CustomLinkReady($Window) {
    if ($Window.Url -match '(?i)offer/custom_link' -and -not (Test-AffiliateCaptchaUrl $Window.Url)) { return $true }
    foreach ($tab in @($Window.Tabs)) {
        if ($tab.StartsWith('Shopee Affiliate Program')) { return $true }
    }
    return $false
}

function Test-CaptchaWindow($Window) {
    if (Test-AffiliateCaptchaUrl $Window.Url) { return $true }
    foreach ($tab in @($Window.Tabs)) {
        if ($tab -match '(?i)captcha|anti-?bot') { return $true }
    }
    return $false
}

function Send-WindowKeys([Int64]$Hwnd, [string]$Keys) {
    Add-Type -AssemblyName System.Windows.Forms
    if (-not (Focus-AffiliateWindow $Hwnd)) { return $false }
    [System.Windows.Forms.SendKeys]::SendWait($Keys)
    return $true
}

function Select-WindowTab([Int64]$Hwnd, [string]$Pattern) {
    if (-not (Focus-AffiliateWindow $Hwnd)) { return $false }
    $root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$Hwnd)
    $cond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::TabItem)
    foreach ($tab in @($root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond))) {
        $tabName = [string]$tab.Current.Name
        if ($tabName -notmatch $Pattern) { continue }
        try {
            $selection = $tab.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern)
            $selection.Select()
            Start-Sleep -Milliseconds 300
            return $true
        } catch {
            return $false
        }
    }
    return $false
}

function Close-ShopeePages([Int64]$Hwnd) {
    # Tutup tab Shopee setelah URL-nya terbukti captcha. Jangan klik isi halaman.
    # Tab terakhir diarahkan ke about:blank lewat omnibox supaya jendela profil tidak ikut tertutup.
    $confirmed = $false
    $guard = 0
    while ($guard -lt 8) {
        $guard++
        $matched = @(Get-AffiliateWindows | Where-Object { $_.Hwnd -eq $Hwnd })
        if (-not $matched) { return $false }
        $window = $matched[0]
        $activeCaptcha = Test-AffiliateCaptchaUrl $window.Url
        $activeShopee = $window.Url -match '(?i)shopee\.co\.id'
        if ($activeCaptcha) { $confirmed = $true }
        if (-not $confirmed) {
            $picked = Select-WindowTab $Hwnd '(?i)captcha|anti-?bot|verify/captcha'
            if (-not $picked) { return $false }
            continue
        }
        if (-not $activeCaptcha -and -not $activeShopee) {
            $picked = Select-WindowTab $Hwnd '(?i)Shopee|captcha|anti-?bot'
            if (-not $picked) { return $true }
            continue
        }
        if (@($window.Tabs).Count -le 1) {
            return (Send-WindowKeys $Hwnd '^labout:blank{ENTER}')
        }
        if (-not (Send-WindowKeys $Hwnd '^w')) { return $false }
        Start-Sleep -Milliseconds 400
    }
    return $confirmed
}

function Open-CustomLink([Int64]$Hwnd) {
    if (-not (Send-WindowKeys $Hwnd '^t')) { return $false }
    Start-Sleep -Milliseconds 400
    $url = $script:AffiliateCustomUrl
    if (-not (Send-WindowKeys $Hwnd ($url + '{ENTER}'))) { return $false }
    return $true
}

function Start-MissingProfile($Account) {
    $chrome = Get-AffiliateChromePath
    $folder = Join-Path $env:LOCALAPPDATA "Google\Chrome\User Data\$($Account.Dir)"
    if (-not (Test-Path -LiteralPath $folder)) {
        Write-RepairLog ("tidak ada folder {0} untuk {1}" -f $Account.Dir, $Account.Name)
        return
    }
    $launcher = New-Object System.Threading.Mutex($false, 'Local\FahraAffiliateChromeProfiles')
    if (-not $launcher.WaitOne(5000)) {
        Write-RepairLog ("tunda buka {0}: peluncur profil lain masih berjalan" -f $Account.Name)
        return
    }
    try {
        $arguments = '--profile-directory="{0}" --new-window "{1}"' -f $Account.Dir, $script:AffiliateCustomUrl
        $start = New-Object System.Diagnostics.ProcessStartInfo
        $start.FileName = $chrome
        $start.Arguments = $arguments
        $start.UseShellExecute = $true
        [void][Diagnostics.Process]::Start($start)
        Write-RepairLog ("buka ulang {0} ({1})" -f $Account.Name, $Account.Dir)
        Start-Sleep -Seconds 2
    } finally {
        $launcher.ReleaseMutex() | Out-Null
        $launcher.Dispose()
    }
}

$script:DashboardLoginUrl = 'http://127.0.0.1:8788/login'
$script:DashboardEntryCap = 3
$script:ForceDashboard = $false
if ($RecoverNow) {
    # Satu giliran memulihkan semua profil. Gelombang kedua menunggu batas login 8/menit.
    $script:DashboardEntryCap = 10
    $script:ForceDashboard = $true
}

if (-not ('FahraMouse' -as [type])) {
    Add-Type @'
using System;
using System.Runtime.InteropServices;
public class FahraMouse {
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint flags, uint dx, uint dy, uint data, int extra);
}
'@
}

function Get-ProfileLabel([string]$Value) {
    $text = $Value.Trim()
    foreach ($known in $script:AffiliateProfiles) {
        $name = [string]$known.Name
        if ($text.Equals($name, [System.StringComparison]::Ordinal)) { return $name }
        if (-not $text.StartsWith($name, [System.StringComparison]::Ordinal)) { continue }
        $next = $text[$name.Length]
        if ($next -notmatch '[A-Za-z0-9]') { return $name }
    }
    return ''
}

function Test-BridgeReadyValue($Value) {
    if ($Value -is [bool]) { return [bool]$Value }
    return "$Value" -eq 'true'
}

function Test-Due($Map, [string]$Name, [int]$Minutes, $Now) {
    if (-not $Map -or -not $Map.ContainsKey($Name)) { return $true }
    try { $last = [datetime]::Parse([string]$Map[$Name]) } catch { return $true }
    return $last.AddMinutes($Minutes) -le $Now
}

function Set-Stamp($State, [string]$MapName, [string]$Name, $Now) {
    $State[$MapName][$Name] = $Now.ToString('o')
    Write-RepairState $State
}

function Get-Miss($State, [string]$Name) {
    if (-not $State.misses.ContainsKey($Name)) { return 0 }
    return [int]$State.misses[$Name]
}

function Set-Miss($State, [string]$Name, [int]$Count) {
    if ($Count -le 0) {
        if ($State.misses.ContainsKey($Name)) { [void]$State.misses.Remove($Name) }
    } else {
        $State.misses[$Name] = $Count
    }
    Write-RepairState $State
}

function Clear-RepairMarks($State, [string]$Name) {
    $changed = $false
    foreach ($map in @($State.misses, $State.checked)) {
        if ($map.ContainsKey($Name)) {
            [void]$map.Remove($Name)
            $changed = $true
        }
    }
    if ($changed) { Write-RepairState $State }
}

function Read-DashboardCredential {
    $path = Join-Path (Split-Path -Parent $Root) 'repliz-worker-naira-aff\.env'
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    foreach ($line in [System.IO.File]::ReadLines($path)) {
        if ($line.Length -lt 24) { continue }
        if ($line.Substring(0, 24) -ne 'login fahra aff rotator=') { continue }
        $value = $line.Substring(24).Trim().Trim('"').Trim("'")
        $split = $value.IndexOf(' ')
        if ($split -lt 1) { return $null }
        return @{ Email = $value.Substring(0, $split); Password = $value.Substring($split + 1) }
    }
    return $null
}

function Get-BridgeSnapshot($State, [switch]$Quiet) {
    $result = [pscustomobject]@{ Ok = $false; Stale = $true; NotReady = $null; Auth = 'down'; ReadyCount = 0 }
    try {
        $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8788/api/health' -TimeoutSec 8
    } catch {
        return $result
    }
    $result.Ok = $true
    $seen = $null
    $readyCount = 0
    if ($health.bridge) {
        $seen = $health.bridge.last_seen
        if ($null -ne $health.bridge.ready_accounts) { $readyCount = [int]$health.bridge.ready_accounts }
    }
    $stale = $true
    if ($null -ne $seen -and "$seen" -ne '') {
        try {
            $seenAt = ([datetime]'1970-01-01Z').AddSeconds([double]$seen)
            $stale = (([datetime]::UtcNow) - $seenAt).TotalSeconds -gt 20
        } catch {
            $stale = $true
        }
    }
    $result.Stale = [bool]$stale
    $result.ReadyCount = $readyCount
    if (-not $stale -and $readyCount -ge 10) {
        if ($State.bridge) {
            $State.bridge = $null
            Write-RepairState $State
        }
        $result.Auth = 'skipped'
        $result.NotReady = @()
        return $result
    }
    if (-not $stale -and $State.bridge -and [int]$State.bridge.ready_count -eq $readyCount -and $State.bridge.at) {
        try { $cachedAt = [datetime]::Parse([string]$State.bridge.at) } catch { $cachedAt = [datetime]::MinValue }
        if ($cachedAt.AddMinutes(2) -gt (Get-Date)) {
            $result.Auth = 'cached'
            $result.NotReady = @($State.bridge.not_ready)
            return $result
        }
    }
    if ($Quiet) {
        $result.Auth = 'quiet'
        return $result
    }
    $cred = Read-DashboardCredential
    if (-not $cred) {
        $result.Auth = 'missing'
        return $result
    }
    $payload = @{ email = $cred.Email; password = $cred.Password } | ConvertTo-Json -Compress
    $cred.Email = $null
    $cred.Password = $null
    $code = 0
    $session = $null
    try {
        $null = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8788/api/auth/login' -Body $payload -ContentType 'application/json' -TimeoutSec 8 -SessionVariable session
        $code = 200
    } catch {
        if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
    } finally {
        $payload = $null
    }
    if ($code -eq 429) { $result.Auth = 'limited'; return $result }
    if ($code -ne 200 -or -not $session) { $result.Auth = 'rejected'; return $result }
    $body = $null
    try {
        $body = Invoke-RestMethod -Uri 'http://127.0.0.1:8788/api/accounts' -WebSession $session -TimeoutSec 8
    } catch {
        $result.Auth = 'unread'
        return $result
    }
    $pending = New-Object 'System.Collections.Generic.List[string]'
    foreach ($account in $script:AffiliateProfiles) { [void]$pending.Add($account.Name) }
    $rows = @()
    if ($body -and $body.accounts) { $rows = @($body.accounts) }
    foreach ($row in $rows) {
        if (-not (Test-BridgeReadyValue $row.ready)) { continue }
        $label = Get-ProfileLabel ([string]$row.name)
        if ($pending.Contains($label)) { [void]$pending.Remove($label) }
    }
    $names = @($pending.ToArray())
    $body = $null
    $State.bridge = @{ at = (Get-Date).ToString('o'); ready_count = $readyCount; not_ready = $names }
    Write-RepairState $State
    $result.Auth = 'ok'
    $result.NotReady = $names
    return $result
}

function Test-ExactCustomLink([string]$Url) {
    $url = Expand-OmniboxUrl $Url
    return $url -match '^https://affiliate\.shopee\.co\.id/offer/custom_link/?$'
}

function Test-DashboardHome([string]$Url) {
    $url = Expand-OmniboxUrl $Url
    # Setelah login, aplikasi pindah ke / lalu mengganti hash menjadi #dashboard.
    return $url -match '^http://127\.0\.0\.1:8788/?(#|$)[^?]*$'
}

function Test-DashboardLogin([string]$Url) {
    $url = Expand-OmniboxUrl $Url
    return $url -match '^http://127\.0\.0\.1:8788/login/?$'
}

function Test-OptionsPage([string]$Url) {
    $url = Expand-OmniboxUrl $Url
    return $url -match '(?i)^chrome-extension://mgcocjanibcfmfgnbhlgdobencagflpg/options\.html/?$'
}

function Test-StrayDashboardUrl([string]$Url) {
    $url = Expand-OmniboxUrl $Url
    return $url -match '^http://127\.0\.0\.1:8788(/|$)'
}

function Get-OneWindow([Int64]$Hwnd) {
    $matched = @(Get-AffiliateWindows | Where-Object { $_.Hwnd -eq $Hwnd })
    if (-not $matched) { return $null }
    return $matched[0]
}

function Open-TabUrl([Int64]$Hwnd, [string]$Url) {
    if (-not (Send-WindowKeys $Hwnd '^t')) { return $false }
    Start-Sleep -Milliseconds 400
    if (-not (Send-WindowKeys $Hwnd ($Url + '{ENTER}'))) { return $false }
    Start-Sleep -Seconds 2
    return $true
}

function Close-ActiveExtraTab([Int64]$Hwnd) {
    $window = Get-OneWindow $Hwnd
    if (-not $window) { return $false }
    if (@($window.Tabs).Count -le 1) { return $false }
    if (Test-AffiliateCaptchaUrl $window.Url) { return $false }
    return (Send-WindowKeys $Hwnd '^w')
}

function Select-UnselectedTab([Int64]$Hwnd, [string]$Pattern) {
    if (-not (Focus-AffiliateWindow $Hwnd)) { return $false }
    $root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$Hwnd)
    $cond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::TabItem)
    foreach ($tab in @($root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond))) {
        if ([string]$tab.Current.Name -notmatch $Pattern) { continue }
        try {
            $selection = $tab.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern)
            if ($selection.Current.IsSelected) { continue }
            $selection.Select()
            Start-Sleep -Milliseconds 400
            return $true
        } catch {
            return $false
        }
    }
    return $false
}

function Set-StaClipboard([string]$Text) {
    Add-Type -AssemblyName System.Windows.Forms
    $shell = [powershell]::Create()
    $null = $shell.AddScript({
        param($value)
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.Clipboard]::SetText($value)
    }).AddArgument($Text)
    $runspace = [runspacefactory]::CreateRunspace()
    $runspace.ApartmentState = [System.Threading.ApartmentState]::STA
    $runspace.Open()
    $shell.Runspace = $runspace
    try {
        $null = $shell.Invoke()
        return -not $shell.HadErrors
    } finally {
        $shell.Dispose()
        $runspace.Dispose()
    }
}

function Set-ElementValue($Element, [string]$Value) {
    try {
        $pattern = $Element.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
        if ($pattern.Current.IsReadOnly) { return $false }
        $pattern.SetValue($Value)
        return $true
    } catch {
        return $false
    }
}

function Set-SecretValue([Int64]$Hwnd, $Element, [string]$Value, [ref]$Copied) {
    if (Set-ElementValue $Element $Value) { return $true }
    if (-not (Focus-AffiliateWindow $Hwnd)) { return $false }
    try { $Element.SetFocus() } catch { return $false }
    Start-Sleep -Milliseconds 150
    if (-not (Set-StaClipboard $Value)) { return $false }
    $Copied.Value = $true
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.SendKeys]::SendWait('^a')
    [System.Windows.Forms.SendKeys]::SendWait('^v')
    return $true
}

function Find-LoginFields([Int64]$Hwnd) {
    $root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$Hwnd)
    $editCond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Edit)
    $fields = @()
    foreach ($edit in @($root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $editCond))) {
        $name = [string]$edit.Current.Name
        if ($name -eq 'Bilah penelusuran dan alamat') { continue }
        $secret = $false
        try {
            $secret = [bool]$edit.GetCurrentPropertyValue([System.Windows.Automation.AutomationElement]::IsPasswordProperty)
        } catch {}
        $fields += [pscustomobject]@{ Element = $edit; Secret = $secret; Name = $name }
    }
    $password = @($fields | Where-Object { $_.Secret -or $_.Name -eq 'Password' })
    $email = @($fields | Where-Object { -not $_.Secret -and $_.Name -ne 'Password' })
    if ($password.Count -ne 1 -or $email.Count -ne 1) { return $null }
    $buttonCond = New-Object System.Windows.Automation.AndCondition(
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::Button)),
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::NameProperty,
            'Masuk ke dashboard'))
    )
    $button = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $buttonCond)
    if ($null -eq $button) { return $null }
    return [pscustomobject]@{ Email = $email[0].Element; Password = $password[0].Element; Button = $button }
}

function Invoke-Element($Element) {
    try {
        $invoke = $Element.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
        $invoke.Invoke()
        return $true
    } catch {}
    try {
        $rect = $Element.Current.BoundingRectangle
        if ($rect.Width -lt 2 -or $rect.Height -lt 2) { return $false }
        $x = [int]($rect.X + ($rect.Width / 2))
        $y = [int]($rect.Y + ($rect.Height / 2))
        [void][FahraMouse]::SetCursorPos($x, $y)
        Start-Sleep -Milliseconds 80
        [FahraMouse]::mouse_event(2, 0, 0, 0, 0)
        [FahraMouse]::mouse_event(4, 0, 0, 0, 0)
        return $true
    } catch {
        return $false
    }
}

function Read-WindowSignal([Int64]$Hwnd) {
    $root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$Hwnd)
    $cond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Text)
    $login = $false
    $awake = $false
    foreach ($node in @($root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond))) {
        $name = [string]$node.Current.Name
        if ($name -match 'Login diperlukan') { $login = $true }
        if ($name -match 'Bridge berjalan|Bridge sedang|Buka satu tab|Sisakan satu tab|Reload extension') { $awake = $true }
    }
    if ($login) { return 'login' }
    if ($awake) { return 'awake' }
    return 'unknown'
}

function Open-OptionsStatus([Int64]$Hwnd) {
    if (-not (Open-TabUrl $Hwnd 'chrome-extension://mgcocjanibcfmfgnbhlgdobencagflpg/options.html')) { return 'unknown' }
    $window = Get-OneWindow $Hwnd
    if (-not $window) { return 'unknown' }
    if (-not (Test-OptionsPage $window.Url)) {
        if (@($window.Tabs).Count -gt 1) { [void](Send-WindowKeys $Hwnd '^w') }
        return 'unknown'
    }
    $signal = Read-WindowSignal $Hwnd
    if (@($window.Tabs).Count -gt 1) { [void](Send-WindowKeys $Hwnd '^w') }
    return $signal
}

function Close-StrayDashboardTabs([Int64]$Hwnd) {
    $closed = 0
    for ($i = 0; $i -lt 6; $i++) {
        $window = Get-OneWindow $Hwnd
        if (-not $window) { return $closed }
        if (@($window.Tabs).Count -le 1) { return $closed }
        if (Test-StrayDashboardUrl $window.Url) {
            if (-not (Send-WindowKeys $Hwnd '^w')) { return $closed }
            $closed++
            Start-Sleep -Milliseconds 400
            continue
        }
        $picked = Select-UnselectedTab $Hwnd '(?i)^Masuk\b|Fahra Affiliate Rotator'
        if (-not $picked) { return $closed }
        $window = Get-OneWindow $Hwnd
        if (-not $window -or -not (Test-StrayDashboardUrl $window.Url)) { return $closed }
        if (@($window.Tabs).Count -le 1) { return $closed }
        if (-not (Send-WindowKeys $Hwnd '^w')) { return $closed }
        $closed++
        Start-Sleep -Milliseconds 400
    }
    return $closed
}

function Restore-CustomLink([Int64]$Hwnd) {
    [void](Close-StrayDashboardTabs $Hwnd)
    $window = Get-OneWindow $Hwnd
    if ($window -and (Test-ExactCustomLink $window.Url)) { return $true }
    if (-not (Select-WindowTab $Hwnd '^Shopee Affiliate Program')) { return $false }
    $window = Get-OneWindow $Hwnd
    return ($window -and (Test-ExactCustomLink $window.Url))
}

function Enter-Dashboard([Int64]$Hwnd) {
    $cred = $null
    $copied = $false
    $opened = $false
    try {
        $fresh = Get-OneWindow $Hwnd
        if (-not $fresh) { return 'failed' }
        if (Test-DashboardHome $fresh.Url) {
            [void](Restore-CustomLink $Hwnd)
            return 'ready'
        }
        if (-not (Test-DashboardLogin $fresh.Url)) {
            if (-not (Open-TabUrl $Hwnd $script:DashboardLoginUrl)) { return 'failed' }
            $opened = $true
            $fresh = Get-OneWindow $Hwnd
            if (-not $fresh) { return 'failed' }
            if (Test-DashboardHome $fresh.Url) {
                [void](Restore-CustomLink $Hwnd)
                return 'ready'
            }
            if (-not (Test-DashboardLogin $fresh.Url)) {
                [void](Close-ActiveExtraTab $Hwnd)
                return 'failed'
            }
        }
        $cred = Read-DashboardCredential
        if (-not $cred) {
            if ($opened) { [void](Close-ActiveExtraTab $Hwnd) }
            return 'failed'
        }
        $fields = Find-LoginFields $Hwnd
        if (-not $fields) {
            if ($opened) { [void](Close-ActiveExtraTab $Hwnd) }
            return 'failed'
        }
        if (-not (Set-ElementValue $fields.Email $cred.Email)) {
            [void](Close-ActiveExtraTab $Hwnd)
            return 'failed'
        }
        if (-not (Set-SecretValue $Hwnd $fields.Password $cred.Password ([ref]$copied))) {
            [void](Close-ActiveExtraTab $Hwnd)
            return 'failed'
        }
        if (-not (Invoke-Element $fields.Button)) {
            [void](Close-ActiveExtraTab $Hwnd)
            return 'failed'
        }
        $deadline = (Get-Date).AddSeconds(6)
        do {
            Start-Sleep -Milliseconds 500
            $after = Get-OneWindow $Hwnd
            if ($after -and (Test-DashboardHome $after.Url)) {
                [void](Restore-CustomLink $Hwnd)
                return 'entered'
            }
        } while ((Get-Date) -lt $deadline)
        if ($after -and (Test-DashboardLogin $after.Url)) {
            [void](Close-ActiveExtraTab $Hwnd)
        } elseif ($opened) {
            [void](Close-ActiveExtraTab $Hwnd)
        }
        return 'failed'
    } finally {
        if ($cred) {
            $cred.Email = $null
            $cred.Password = $null
        }
        if ($copied) { [void](Set-StaClipboard ' ') }
    }
}

function Close-DuplicateCustomLink([Int64]$Hwnd) {
    if (-not (Select-UnselectedTab $Hwnd '^Shopee Affiliate Program')) { return $false }
    $window = Get-OneWindow $Hwnd
    if (-not $window -or -not (Test-ExactCustomLink $window.Url)) { return $false }
    if (@($window.Tabs).Count -le 1) { return $false }
    return (Send-WindowKeys $Hwnd '^w')
}

function Repair-BridgeAccount($Account, $Window, $State, $Now, [bool]$Stale) {
    $hwnd = [int64]$Window.Hwnd
    $affiliateTabs = @($Window.Tabs | Where-Object { [string]$_ -like 'Shopee Affiliate Program*' })
    if (-not $script:CollapsedDuplicate -and $affiliateTabs.Count -ge 2 -and (Test-ExactCustomLink $Window.Url)) {
        if (Close-DuplicateCustomLink $hwnd) {
            $script:CollapsedDuplicate = $true
            Write-RepairLog ("{0}: tab Custom Link ganda ditutup" -f $Account.Name)
            return
        }
    }
    $gap = 10
    if ($Stale -or $script:ForceDashboard) { $gap = 2 }
    $onLogin = Test-DashboardLogin ([string]$Window.Url)
    $sessionDue = $script:ForceDashboard -or $onLogin -or (Test-Due $State.checked $Account.Name $gap $Now)
    if ($sessionDue -and -not $script:DashboardStop -and $script:BridgeVisits -lt $script:DashboardEntryCap -and $script:DashboardEntries -lt $script:DashboardEntryCap) {
        $script:BridgeVisits++
        $entered = 'awake'
        if ($onLogin -or $script:ForceDashboard) {
            $entered = Enter-Dashboard $hwnd
        } else {
            $signal = Open-OptionsStatus $hwnd
            if ($signal -ne 'awake') { $entered = Enter-Dashboard $hwnd }
        }
        if ($entered -eq 'entered') { $script:DashboardEntries++ }
        if ($entered -eq 'failed' -and -not $script:ForceDashboard) { $script:DashboardStop = $true }
        Set-Stamp $State 'checked' $Account.Name $Now
        if ($entered -eq 'entered') {
            Write-RepairLog ("{0}: dashboard profil dimasuki ulang" -f $Account.Name)
        } elseif ($entered -eq 'ready' -or $entered -eq 'awake') {
            Write-RepairLog ("{0}: dashboard profil masih masuk" -f $Account.Name)
        } else {
            Write-RepairLog ("{0}: dashboard profil belum masuk" -f $Account.Name)
        }
        if ((Get-Miss $State $Account.Name) -lt 1) { Set-Miss $State $Account.Name 1 }
        return
    }
    $miss = Get-Miss $State $Account.Name
    $fresh = Get-OneWindow $hwnd
    $url = [string]$Window.Url
    if ($fresh) { $url = [string]$fresh.Url }
    if ($miss -ge 1 -and (Test-Due $State.reloads $Account.Name 10 $Now) -and (Test-ExactCustomLink $url)) {
        if (Send-WindowKeys $hwnd '{F5}') {
            Set-Stamp $State 'reloads' $Account.Name $Now
            Set-Miss $State $Account.Name 0
            Write-RepairLog ("{0}: tab Custom Link dimuat ulang" -f $Account.Name)
            return
        }
    }
    Set-Miss $State $Account.Name ($miss + 1)
    Write-RepairLog ("{0}: bridge belum siap" -f $Account.Name)
}

$mutex = New-Object System.Threading.Mutex($false, 'Local\FahraAffiliateProfileRepair')
$repairOwned = $false
$mutexWaitMs = 0
if ($RecoverNow) { $mutexWaitMs = 180000 }
try {
    $repairOwned = $mutex.WaitOne($mutexWaitMs)
} catch [System.Threading.AbandonedMutexException] {
    $repairOwned = $true
}
if (-not $repairOwned) {
    Write-RepairLog 'lewati: pemulihan lain masih berjalan'
    exit 0
}

$repairTimer = $null
try {
    # Jangan panggil Get-ScheduledTask di sini. Panggilan itu menahan mutex
    # sampai Task Scheduler macet, lalu setiap menit berikutnya ikut terlewati.
    $repairProcessId = $PID
    if ($RecoverNow) {
        Write-RepairLog 'pulihkan cookie dashboard dimulai'
    } else {
    $repairTimer = New-Object System.Threading.Timer(
        ([System.Threading.TimerCallback]{
            param($state)
            try { [System.Diagnostics.Process]::GetProcessById([int]$state).Kill() } catch {}
        }),
        $repairProcessId,
        150000,
        [System.Threading.Timeout]::Infinite)
    }
    $state = Read-RepairState
    $healthScript = Join-Path $Root 'ensure-worker-health.ps1'
    if ((Test-Path -LiteralPath $healthScript) -and -not $Check) {
        # Mengembalikan izin Local Service dan proses yang mati sebelum Chrome disentuh.
        & $healthScript
        if ($LASTEXITCODE -eq 2) { Write-RepairLog 'worker atau bot dipulihkan otomatis' }
    }
    $script:CollapsedDuplicate = $false
    $script:DashboardEntries = 0
    $script:DashboardStop = $false
    $script:BridgeVisits = 0
    $script:DashboardSlept = $false
    $snapshot = Get-BridgeSnapshot $state -Quiet:$Check
    if ($null -ne $snapshot.NotReady -and @($snapshot.NotReady).Count -ge 10 -and [int]$snapshot.ReadyCount -gt 0) {
        Write-RepairLog 'nama akun bridge tidak cocok, profil tidak disentuh'
        $snapshot.NotReady = $null
    }
    if ($RecoverNow -and $snapshot.Ok -and [int]$snapshot.ReadyCount -lt 10 -and $null -eq $snapshot.NotReady) {
        $snapshot.NotReady = @($script:AffiliateProfiles | ForEach-Object { [string]$_.Name })
    }
    if (-not $Check) {
        if ($snapshot.Auth -eq 'limited') { Write-RepairLog 'login dashboard dibatasi, lanjut menit berikutnya' }
        elseif ($snapshot.Auth -eq 'rejected') { Write-RepairLog 'login dashboard ditolak' }
        elseif ($snapshot.Auth -eq 'missing') { Write-RepairLog 'kunci login dashboard tidak terbaca' }
        elseif ($snapshot.Auth -eq 'unread') { Write-RepairLog 'daftar akun bridge tidak terbaca' }
        elseif ($snapshot.Auth -eq 'down') { Write-RepairLog 'dashboard tidak terjangkau, profil tidak disentuh' }
        elseif ($snapshot.Stale -and $null -ne $snapshot.NotReady) { Write-RepairLog 'bridge tidak terlihat, pemulihan membangunkan extension' }
    }
    $bridgeHealthy = $snapshot.Ok -and -not $snapshot.Stale -and [int]$snapshot.ReadyCount -ge 10 -and @($snapshot.NotReady).Count -eq 0
    if ($bridgeHealthy) {
        Write-RepairLog 'semua siap'
    } elseif (-not $Check -and $snapshot.Auth -eq 'down') {
        # Dashboard mati. Jangan pindai pohon UI Chrome; pemindaian itulah yang membekukan jendela.
    } else {
    $windows = @(Get-AffiliateWindows)
    $now = Get-Date
    foreach ($account in $script:AffiliateProfiles) {
        $window = $windows | Where-Object { $_.Name -eq $account.Name } | Select-Object -First 1
        $hold = Get-HoldUntil $state $account.Name
        $captcha = $window -and (Test-CaptchaWindow $window)
        if ($captcha) {
            if ($Check) {
                Write-RepairLog ("cek {0}: captcha, tidak ditutup karena mode cek" -f $account.Name)
                continue
            }
            $closed = Close-ShopeePages $window.Hwnd
            if ($closed -and (-not $hold -or $hold -le $now)) {
                $until = $now.AddMinutes($HoldMinutes)
                Set-Hold $state $account.Name $until
                Write-RepairLog ("{0}: halaman Shopee ditutup, Custom Link dibuka lagi {1}" -f $account.Name, $until.ToString('HH:mm:ss'))
            } elseif ($closed) {
                Write-RepairLog ("{0}: captcha masih ada, jeda tetap sampai {1}" -f $account.Name, $hold.ToString('HH:mm:ss'))
            } else {
                Write-RepairLog ("{0}: captcha terlihat, halaman belum tertutup" -f $account.Name)
            }
            continue
        }
        if ($hold -and $hold -gt $now) {
            Write-RepairLog ("{0}: menunggu sampai {1}" -f $account.Name, $hold.ToString('HH:mm:ss'))
            continue
        }
        if ($hold) { Clear-Hold $state $account.Name }
        if (-not $window) {
            if ($Check) {
                Write-RepairLog ("cek {0}: jendela tidak ada" -f $account.Name)
            } else {
                Start-MissingProfile $account
            }
            continue
        }
        $bridgeKnown = $snapshot.Ok -and $null -ne $snapshot.NotReady
        $bridgeReady = $bridgeKnown -and -not ($snapshot.NotReady -contains $account.Name)
        if ($bridgeReady) {
            if (-not $Check) { Clear-RepairMarks $state $account.Name }
            $strayName = @($window.Tabs | Where-Object { [string]$_ -match '(?i)^Masuk\b|Fahra Affiliate Rotator' })
            if ((-not $Check) -and ($strayName.Count -gt 0 -or (Test-StrayDashboardUrl $window.Url))) {
                [void](Restore-CustomLink $window.Hwnd)
            }
            Write-RepairLog ("{0}: siap" -f $account.Name)
            continue
        }
        if (-not (Test-CustomLinkReady $window)) {
            if ($Check) {
                Write-RepairLog ("cek {0}: Custom Link belum ada" -f $account.Name)
                continue
            }
            if (Open-CustomLink $window.Hwnd) {
                Write-RepairLog ("{0}: Custom Link dibuka" -f $account.Name)
            } else {
                Write-RepairLog ("{0}: Custom Link gagal dibuka" -f $account.Name)
            }
            continue
        }
        if (-not $bridgeKnown) {
            Write-RepairLog ("{0}: siap" -f $account.Name)
            continue
        }
        if ($Check) {
            Write-RepairLog ("cek {0}: bridge belum siap" -f $account.Name)
            continue
        }
        if ($RecoverNow -and $script:BridgeVisits -ge 4 -and -not $script:DashboardSlept) {
            Write-RepairLog 'jeda login dashboard supaya tidak kena batas percobaan'
            Start-Sleep -Seconds 65
            $script:BridgeVisits = 0
            $script:DashboardEntries = 0
            $script:DashboardStop = $false
            $script:DashboardSlept = $true
        }
        Repair-BridgeAccount $account $window $state $now ([bool]$snapshot.Stale)
    }
    }
} catch {
    $detail = $_.Exception.GetType().Name
    $text = [regex]::Replace([string]$_.Exception.Message, '(?i)[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}', 'email')
    if ($text -match '(?i)password|fahra_session|login fahra') { $text = '' }
    if ($text.Length -gt 120) { $text = $text.Substring(0, 120) }
    if ($text) { $detail = '{0}: {1}' -f $detail, $text }
    Write-RepairLog ("gagal: {0}" -f $detail)
    throw $detail
} finally {
    if ($repairTimer) { $repairTimer.Dispose() }
    $mutex.ReleaseMutex() | Out-Null
    $mutex.Dispose()
}
