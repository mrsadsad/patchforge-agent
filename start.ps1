param([string]$EnvFile = '.env', [int]$Port = 8771, [string]$Runs = 'runs/v02')
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    python -m forge serve --port $Port --runs $Runs --env-file $EnvFile
} finally {
    Pop-Location
}
