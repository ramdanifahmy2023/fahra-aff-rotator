# Fungsi bersama untuk sepuluh profil Shopee Affiliate.
# Dipakai task login dan skrip pemulihan. Jangan menambah profil di luar daftar ini.

$script:AffiliateChromeRoot = $PSScriptRoot
$script:AffiliateCustomUrl = 'https://affiliate.shopee.co.id/offer/custom_link'
$script:AffiliateProfiles = @(
    @{ Dir = 'Profile 12'; Name = 'Trubella' },
    @{ Dir = 'Profile 23'; Name = 'arjuno' },
    @{ Dir = 'Profile 33'; Name = 'Adinda' },
    @{ Dir = 'Profile 11'; Name = 'Benicia' },
    @{ Dir = 'Profile 31'; Name = 'bigboss' },
    @{ Dir = 'Profile 29'; Name = 'ciremai' },
    @{ Dir = 'Profile 24'; Name = 'gaspoll' },
    @{ Dir = 'Profile 46'; Name = 'Jejaka' },
    @{ Dir = 'Profile 41'; Name = 'catalog' },
    @{ Dir = 'Profile 38'; Name = 'Dewi' }
)

if (-not ('FahraChromeWin' -as [type])) {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class FahraChromeWin {
  public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern int GetClassName(IntPtr hWnd, StringBuilder cls, int max);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int max);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint procId);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
  [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool attach);
}
"@
}

function Get-AffiliateChromePath {
    $configPath = Join-Path $script:AffiliateChromeRoot 'data\runtime-config.json'
    if (Test-Path -LiteralPath $configPath) {
        $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($config.chrome -and (Test-Path -LiteralPath $config.chrome)) { return [string]$config.chrome }
    }
    $candidate = Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'
    if (Test-Path -LiteralPath $candidate) { return $candidate }
    throw 'chrome.exe tidak ditemukan.'
}

function Test-AffiliateCaptchaUrl([string]$Url) {
    if (-not $Url) { return $false }
    return $Url -match '(?i)(verify/captcha|anti[_-]bot)'
}

function Expand-OmniboxUrl([string]$Url) {
    # ValuePattern bilah alamat Chrome menyembunyikan skema http dan https.
    $url = ([string]$Url).Trim()
    if (-not $url) { return '' }
    if ($url -match '^[a-zA-Z][a-zA-Z0-9+.-]*://') { return $url }
    if ($url -match '^(?i)(127\.0\.0\.1|localhost)(:|/|$)') { return 'http://' + $url }
    if ($url -match '^(?i)([a-z0-9.-]+\.)?shopee\.co\.id(/|$)') { return 'https://' + $url }
    if ($url -match '^(?i)mgcocjanibcfmfgnbhlgdobencagflpg/') { return 'chrome-extension://' + $url }
    return $url
}

function Get-AffiliateWindows {
    $script:FahraChromeHwnds = @()
    $callback = [FahraChromeWin+EnumProc]{
        param($hWnd, $lParam)
        if (-not [FahraChromeWin]::IsWindowVisible($hWnd)) { return $true }
        $cls = New-Object System.Text.StringBuilder 256
        [FahraChromeWin]::GetClassName($hWnd, $cls, $cls.Capacity) | Out-Null
        if ($cls.ToString() -ne 'Chrome_WidgetWin_1') { return $true }
        $title = New-Object System.Text.StringBuilder 240
        [FahraChromeWin]::GetWindowText($hWnd, $title, $title.Capacity) | Out-Null
        if ($title.ToString() -notmatch 'Chrome') { return $true }
        $script:FahraChromeHwnds += [int64]$hWnd
        return $true
    }
    [FahraChromeWin]::EnumWindows($callback, [IntPtr]::Zero) | Out-Null
    $names = @($script:AffiliateProfiles | ForEach-Object { $_.Name })
    $buttonCond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Button)
    $tabCond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::TabItem)
    $editCond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Edit)
    $found = @()
    foreach ($hwnd in $script:FahraChromeHwnds) {
        $root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$hwnd)
        $avatar = ''
        foreach ($button in @($root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $buttonCond))) {
            $buttonName = [string]$button.Current.Name
            if ($names -contains $buttonName) { $avatar = $buttonName; break }
        }
        if (-not $avatar) { continue }
        $tabNames = New-Object System.Collections.Generic.List[string]
        foreach ($tab in @($root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $tabCond))) {
            $tabName = [string]$tab.Current.Name
            if ($tabName) { $tabNames.Add($tabName) | Out-Null }
        }
        $url = ''
        foreach ($edit in @($root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $editCond))) {
            if ($edit.Current.Name -ne 'Bilah penelusuran dan alamat') { continue }
            try {
                $pattern = $edit.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
                $url = Expand-OmniboxUrl ([string]$pattern.Current.Value)
            } catch {}
            break
        }
        $found += [pscustomobject]@{
            Hwnd = $hwnd
            Name = $avatar
            Url = $url
            Tabs = $tabNames
        }
    }
    return $found
}

function Focus-AffiliateWindow([Int64]$Hwnd) {
    $handle = [IntPtr]$Hwnd
    [FahraChromeWin]::ShowWindow($handle, 9) | Out-Null
    $unused = [uint32]0
    $foreground = [FahraChromeWin]::GetForegroundWindow()
    $foregroundThread = [FahraChromeWin]::GetWindowThreadProcessId($foreground, [ref]$unused)
    $targetThread = [FahraChromeWin]::GetWindowThreadProcessId($handle, [ref]$unused)
    $currentThread = [FahraChromeWin]::GetCurrentThreadId()
    [FahraChromeWin]::AttachThreadInput($currentThread, $foregroundThread, $true) | Out-Null
    [FahraChromeWin]::AttachThreadInput($currentThread, $targetThread, $true) | Out-Null
    [FahraChromeWin]::SetForegroundWindow($handle) | Out-Null
    Start-Sleep -Milliseconds 200
    $front = [int64][FahraChromeWin]::GetForegroundWindow()
    [FahraChromeWin]::AttachThreadInput($currentThread, $targetThread, $false) | Out-Null
    [FahraChromeWin]::AttachThreadInput($currentThread, $foregroundThread, $false) | Out-Null
    return ($front -eq $Hwnd)
}
