# Run the role-backend API test suite.
# Prereqs: all 5 backends running (backends\run_all.ps1 or apps\run_all.ps1).
# Loads DATABASE_URL from the super-admin backend .env so DB-verify tests work.
# Usage:  powershell -ExecutionPolicy Bypass -File backends\tests\run_tests.ps1
$ErrorActionPreference = "Continue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$saEnv = Join-Path $here "..\super-admin\backend\.env"

if (Test-Path $saEnv) {
  Get-Content $saEnv | ForEach-Object {
    if ($_ -match '^\s*(DATABASE_URL|OTP_DEV_ECHO)\s*=\s*(.*)$') {
      [Environment]::SetEnvironmentVariable($matches[1], $matches[2].Trim('"'), "Process")
    }
  }
}

python -m pip install -q requests psycopg2-binary pytest 2>$null
Push-Location $here
python -m pytest
$code = $LASTEXITCODE
Pop-Location
exit $code
