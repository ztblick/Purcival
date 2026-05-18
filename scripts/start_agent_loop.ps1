$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$python = Join-Path $repoRoot "venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Expected virtualenv python at $python"
}

$logsDir = Join-Path $repoRoot "logs"
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

& $python "scripts\run_agent_loop.py" --persona jo 1>> (Join-Path $logsDir "agent_loop.log") 2>> (Join-Path $logsDir "agent_loop.err.log")
exit $LASTEXITCODE
