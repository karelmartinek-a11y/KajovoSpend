param(
  [string]$TaskName = "KajovoSpendService"
)

$ErrorActionPreference = "Stop"

Write-Host "Removing Scheduled Task '$TaskName'..."
schtasks /Delete /F /TN $TaskName | Out-Null
Write-Host "Done."
