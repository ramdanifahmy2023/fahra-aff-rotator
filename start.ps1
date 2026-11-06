param(
    [switch]$BootstrapAdmin,
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    if (Test-Path -LiteralPath '.env') {
        foreach ($line in Get-Content -LiteralPath '.env') {
            $setting = $line.Trim()
            if (-not $setting -or $setting.StartsWith('#')) { continue }
            if ($setting -notmatch '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
                throw 'Format .env tidak valid. Gunakan NAMA=nilai.'
            }
            $settingName = $Matches[1]
            $settingValue = $Matches[2].Trim()
            if ($settingValue.Length -ge 2 -and (
                ($settingValue.StartsWith('"') -and $settingValue.EndsWith('"')) -or
                ($settingValue.StartsWith("'") -and $settingValue.EndsWith("'"))
            )) {
                $settingValue = $settingValue.Substring(1, $settingValue.Length - 2)
            }
            [Environment]::SetEnvironmentVariable($settingName, $settingValue, 'Process')
        }
    }

    if ($BootstrapAdmin) {
        & $Python (Join-Path $PSScriptRoot 'bootstrap_admin.py')
        if ($LASTEXITCODE -ne 0) { throw 'Bootstrap admin tidak selesai.' }
        return
    }

    $listenPort = if ($env:PORT) { [int]$env:PORT } else { 8788 }
    $appUrl = "http://127.0.0.1:$listenPort"
    $existing = $null
    try {
        $existing = Invoke-WebRequest -UseBasicParsing -Uri "$appUrl/api/health" -TimeoutSec 2
    } catch {
        # Let Python report bind errors if a different service occupies the port.
    }
    if ($existing -and $existing.Headers['Server'] -like 'FahraAffiliateRotator/*') {
        Write-Output "Fahra Affiliate Rotator sudah berjalan di $appUrl"
        return
    }
    & $Python -u app.py
    if ($LASTEXITCODE -ne 0) { throw 'Server berhenti dengan error.' }
} finally {
    Pop-Location
}
