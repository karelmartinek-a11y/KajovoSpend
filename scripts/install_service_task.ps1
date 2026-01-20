param(
  [Parameter(Mandatory=$true)][string]$ProjectDir,
  [string]$TaskName = "KajovoSpendService",
  [string]$ConfigPath = "config.yaml"
)

$ErrorActionPreference = "Stop"

$proj = (Resolve-Path $ProjectDir).Path
$python = Join-Path $proj ".venv\Scripts\python.exe"
if (!(Test-Path $python)) {
  $python = "python.exe"
}

$config = Join-Path $proj $ConfigPath
if (!(Test-Path $config)) {
  throw "Config file not found: $config"
}

$serviceMain = Join-Path $proj "service_main.py"
if (!(Test-Path $serviceMain)) {
  throw "service_main.py not found in: $proj"
}

$action = "\"$python\" \"$serviceMain\" --config \"$config\""

Write-Host "Creating Scheduled Task '$TaskName'..."
schtasks /Create /F /TN $TaskName /SC ONLOGON /RL LIMITED /TR $action | Out-Null

Write-Host "Starting task..."
schtasks /Run /TN $TaskName | Out-Null

Write-Host "Done. Use: schtasks /Query /TN $TaskName"
