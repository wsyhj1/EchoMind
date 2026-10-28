param([switch]$Databases)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
foreach ($name in 'frontend', 'backend', 'evaluator') {
    $python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if ($name -eq 'evaluator') { $python = Join-Path $PSScriptRoot '.venv-eval\Scripts\python.exe' }
    $pidFile = Join-Path $PSScriptRoot ".runtime\$name.pid"
    if (Test-Path -LiteralPath $pidFile) {
        $serviceProcessId = [int](Get-Content -LiteralPath $pidFile)
        $serviceProcess = Get-Process -Id $serviceProcessId -ErrorAction SilentlyContinue
        if ($serviceProcess -and $serviceProcess.Path -eq $python) {
            # Stop the Windows venv launcher and its child interpreter together.
            & taskkill.exe /PID $serviceProcessId /T /F | Out-Null
            Write-Host "Stopped $name."
        }
        Remove-Item -LiteralPath $pidFile
    }
}
if ($Databases) {
    & docker compose stop
    if ($LASTEXITCODE -ne 0) { throw 'Failed to stop database containers.' }
}
