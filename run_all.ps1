# Run every role backend from the dedicated backends/ folder.
# Each role's FastAPI app boots standalone on its own port, sharing the Neon DB.
# Usage:  powershell -ExecutionPolicy Bypass -File backends\run_all.ps1
$ErrorActionPreference = "Continue"
$Root = "E:\GLIMMORA\educore\GTPROJECT\backends"

$Apps = @(
  @{ role = "mentor";      port = 8101 },
  @{ role = "super-admin"; port = 8102 },
  @{ role = "enterprise";  port = 8103 },
  @{ role = "freelancer";  port = 8104 },
  @{ role = "reviewer";    port = 8105 }
)

foreach ($a in $Apps) {
  $dir = Join-Path $Root "$($a.role)\backend"
  if (-not (Test-Path $dir)) { Write-Host "skip $($a.role) (no folder)"; continue }
  # load this role backend's own .env into the process so the child uvicorn inherits it
  if (Test-Path "$dir\.env") {
    Get-Content "$dir\.env" | ForEach-Object {
      if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
        [Environment]::SetEnvironmentVariable($matches[1], $matches[2].Trim('"'), "Process")
      }
    }
  }
  $env:PYTHONPATH = $dir
  $log = $a.role -replace '-','_'
  Write-Host "Starting $($a.role) backend on :$($a.port)"
  Start-Process -WindowStyle Hidden -FilePath "python" `
    -ArgumentList "-m","uvicorn","app:app","--host","127.0.0.1","--port","$($a.port)" `
    -WorkingDirectory $dir `
    -RedirectStandardOutput "$env:TEMP\glm_be_$($log).out.log" `
    -RedirectStandardError  "$env:TEMP\glm_be_$($log).err.log"
}
Write-Host "All role backends launched from backends/. Logs in %TEMP%\glm_be_*.log"
