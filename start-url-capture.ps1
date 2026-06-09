$ErrorActionPreference = "Stop"

$workflowRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$configPath = Join-Path $workflowRoot "config.ps1"

if (-not (Test-Path $configPath)) {
    throw "Missing config file: $configPath"
}

. $configPath

if (-not $env:ZHIPU_API_KEY) {
    Write-Host "Reminder: ZHIPU_API_KEY is missing." -ForegroundColor Yellow
}

if (-not $env:NOTION_API_TOKEN) {
    Write-Host "Reminder: NOTION_API_TOKEN is missing." -ForegroundColor Yellow
}

$port = if ($env:URL_CAPTURE_PORT) { $env:URL_CAPTURE_PORT } else { "8765" }

Write-Host ""
Write-Host "Starting URL capture page" -ForegroundColor Cyan
Write-Host "Workflow Root: $workflowRoot"
Write-Host "Open in browser: http://127.0.0.1:$port"
Write-Host ""

Start-Process "http://127.0.0.1:$port" | Out-Null

python (Join-Path $workflowRoot "url_capture_server.py")
