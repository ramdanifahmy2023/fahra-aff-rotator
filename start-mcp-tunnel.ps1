param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+$')]
    [string]$SshTarget,
    [string]$KeyFile = (Join-Path $env:USERPROFILE '.ssh\fahra_mcp_ed25519'),
    [ValidateRange(1024, 65535)][int]$RemotePort = 18790,
    [ValidateRange(1024, 65535)][int]$LocalPort = 8790
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $KeyFile)) { throw 'SSH key tidak ditemukan.' }
$ssh = (Get-Command ssh.exe -ErrorAction Stop).Source
$mutex = [Threading.Mutex]::new($false, "Local\FahraMcpTunnel-$SshTarget-$RemotePort")
$ownsMutex = $false
try {
    try { $ownsMutex = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $ownsMutex = $true }
    if (-not $ownsMutex) {
        Write-Output 'Tunnel MCP sudah berjalan.'
        return
    }
    while ($true) {
        Write-Output ("{0:u} Menghubungkan MCP ke {1}, loopback VPS:{2}." -f (Get-Date), $SshTarget, $RemotePort)
        & $ssh -N -T -i $KeyFile -o IdentitiesOnly=yes -o BatchMode=yes `
            -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes `
            -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=3 `
            -R "127.0.0.1:${RemotePort}:127.0.0.1:${LocalPort}" $SshTarget
        Write-Output ("{0:u} Tunnel terputus (exit {1}); mencoba lagi dalam 5 detik." -f (Get-Date), $LASTEXITCODE)
        Start-Sleep -Seconds 5
    }
} finally {
    if ($ownsMutex) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
