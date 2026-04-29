param(
  [Parameter(Mandatory=$true)]
  [ValidateSet("conservative","sniper_paper","sniper_live_canary")]
  [string]$Profile
)

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
  $Python = "python"
}
& $Python (Join-Path $Root "scripts\apply_profile.py") $Profile
