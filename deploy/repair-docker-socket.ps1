param([switch]$RestartWsl)
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
$logPath = Join-Path $projectDir '.runtime\logs\docker-repair.log'
try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $isAdmin = ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    "User: $($identity.Name); Administrator: $isAdmin" | Set-Content -LiteralPath $logPath -Encoding UTF8
    $socketDir = 'C:\Users\12818\AppData\Local\docker-secrets-engine'
    $resolvedDir = (Resolve-Path -LiteralPath $socketDir).Path
    if ($resolvedDir -ne $socketDir) { throw 'Unexpected socket directory; refusing to rename it.' }
    Get-Process -Name 'Docker Desktop', 'com.docker.backend', 'docker-ai', 'com.docker.build' -ErrorAction SilentlyContinue | Stop-Process -Force
    if ($RestartWsl) {
        $wslState = (& wsl.exe --list --verbose | Out-String).Replace([string][char]0, '')
        if (-not $wslState.Contains('Stopped') -or $wslState -match '\sRunning\s') {
            throw 'WSL state is not fully stopped; refusing to restart the service.'
        }
        Restart-Service -Name WslService -Force
        'Restarted WSL service; all distributions were stopped.' | Add-Content -LiteralPath $logPath -Encoding UTF8
    }
    $backupName = 'docker-secrets-engine.echomind-backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
    $backupPath = Join-Path (Split-Path -Parent $resolvedDir) $backupName
    [IO.Directory]::Move($resolvedDir, $backupPath)
    "Socket directory backed up: $backupName" | Add-Content -LiteralPath $logPath -Encoding UTF8
    Start-Process -FilePath 'C:\Users\12818\AppData\Local\Programs\DockerDesktop\Docker Desktop.exe' -WindowStyle Hidden
    'Docker Desktop launched. Check docker info after startup.' | Add-Content -LiteralPath $logPath -Encoding UTF8
} catch {
    $_.Exception.Message | Add-Content -LiteralPath $logPath -Encoding UTF8
    throw
}
