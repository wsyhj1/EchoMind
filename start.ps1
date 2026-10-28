param(
    [ValidateSet('Frontend', 'Backend', 'Databases', 'Evaluator')]
    [string]$Service = 'Frontend',
    [switch]$PullImages
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if ($Service -eq 'Evaluator') { $python = Join-Path $PSScriptRoot '.venv-eval\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $python)) { throw 'Python environment is missing.' }
New-Item -ItemType Directory -Force -Path '.runtime\tmp' | Out-Null
$env:PYTHONUTF8 = '1'
$env:TEMP = Join-Path $PSScriptRoot '.runtime\tmp'
$env:TMP = $env:TEMP
$env:NO_PROXY = 'localhost,127.0.0.1,::1'
if ($Service -eq 'Databases') {
    if ($PullImages) {
        Write-Host 'Downloading PostgreSQL and Redis Stack images. Reusing the local Milvus 2.5.10 image.'
        & docker compose pull postgres redis
        if ($LASTEXITCODE -ne 0) { throw 'Image download failed.' }
    }
    & docker compose up -d --pull never --wait --wait-timeout 420
    if ($LASTEXITCODE -ne 0) { throw 'Database startup failed. Check Docker Desktop and docker compose logs.' }
} elseif ($Service -eq 'Evaluator') {
    & $python -m evaluation.worker
} elseif ($Service -eq 'Backend') {
    & $python -c 'from dotenv import dotenv_values; c=dotenv_values(".env"); k=c.get("DASHSCOPE_API_KEY", ""); raise SystemExit(0 if k and not k.startswith("your_") else "Set DASHSCOPE_API_KEY in .env first.")'
    if ($LASTEXITCODE -ne 0) { throw 'DASHSCOPE_API_KEY is not configured.' }
    Set-Location -LiteralPath (Join-Path $PSScriptRoot 'backend')
    & $python -m uvicorn main:app --host 127.0.0.1 --port 8000
} else {
    Set-Location -LiteralPath (Join-Path $PSScriptRoot 'frontend')
    & $python -m streamlit run app.py --server.address=127.0.0.1 --server.port=8501 --server.headless=true --browser.gatherUsageStats=false
}
exit $LASTEXITCODE
