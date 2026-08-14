# Deploy palette to both machines and prove it landed.
#
# The corpus lives on the server and the media lives here, so a change can be
# half-deployed: that is how the server spent a morning stamping mojibake
# titles onto clips while this machine ran the fix. The check at the end is
# the point - it fails loudly unless both machines report the same commit.
#
#   .\deploy.ps1              full run
#   .\deploy.ps1 -SkipTests   when you have just run them
#   .\deploy.ps1 -Check       verify only, change nothing

param(
    [switch]$SkipTests,
    [switch]$Check,
    [string]$Server = "torrey@100.102.79.115",
    [int]$ServerPort = 7862,
    [int]$LocalPort = 7861
)

# Deliberately not "Stop": git, ssh and scp all write ordinary progress to
# stderr, and under Stop PowerShell 5.1 turns each such line into a
# terminating error - aborting a deploy that is going fine. Every native
# call below is followed by an explicit $LASTEXITCODE check instead.
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repo

$py = "C:\Users\torre\AppData\Local\Python\pythoncore-3.14-64\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

function Step($text) { Write-Host "`n=== $text ===" -ForegroundColor Cyan }
function Ok($text)   { Write-Host "  OK   $text" -ForegroundColor Green }
function Warn($text) { Write-Host "  WARN $text" -ForegroundColor Yellow }
function Die($text)  { Write-Host "  FAIL $text" -ForegroundColor Red; exit 1 }

# ── 1. tests ─────────────────────────────────────────────────────────────────
if (-not $Check -and -not $SkipTests) {
    Step "tests"
    & $py -m pytest --no-header -q
    if ($LASTEXITCODE -ne 0) { Die "tests failed - nothing deployed" }
    Ok "suite passed"
}

# ── 2. working trees ─────────────────────────────────────────────────────────
# Matching commits are not the same as matching code. A modified file - or
# just a changed mode bit, which is how server-app.sh drifted after a chmod -
# means what runs is not what is committed, and HEAD says nothing about it.
# Checked on both machines and in both modes, since proving that is the
# entire job of this script.
Step "working tree"
$dirty = git status --porcelain
if ($dirty) { Die "this machine's tree is dirty; commit or stash first`n$dirty" }
Ok "clean here"

# ── 3. push ──────────────────────────────────────────────────────────────────
if (-not $Check) {
    Step "push"
    # No 2>&1 here: git writes progress to stderr, and redirecting a native
    # command's stderr in PowerShell 5.1 turns every line into an error
    # record - which aborts this script even on a successful push.
    git push origin main
    if ($LASTEXITCODE -ne 0) { Die "git push failed" }
    Ok "pushed"
}

$localHead = (git rev-parse --short HEAD).Trim()
Write-Host "  local HEAD  $localHead"

# ── 3. server ────────────────────────────────────────────────────────────────
Step "server"
# Pull, report HEAD, and say whether the app happens to be running. It is
# started on demand, so "not running" is a normal outcome, not a failure.
#
# Shipped as a file rather than piped: PowerShell prepends a BOM when it
# pipes to a native command, and bash reads that as part of the first word.
$pullCmd = if ($Check) { "true" } else { "git pull --ff-only --quiet" }
$remoteScript = @"
set -e
cd ~/palette
$pullCmd
echo "HEAD=`$(git rev-parse --short HEAD)"
echo "DIRTY<<EOF"
git status --porcelain
echo "EOF"
if ss -tln 2>/dev/null | grep -q ":$ServerPort"; then
  echo "RUNNING=yes"
else
  echo "RUNNING=no"
fi
"@

$tmp = Join-Path $env:TEMP "palette-deploy-remote.sh"
[IO.File]::WriteAllText($tmp, ($remoteScript -replace "`r`n", "`n"),
                        (New-Object System.Text.UTF8Encoding($false)))
scp -o BatchMode=yes $tmp "${Server}:/tmp/palette-deploy-remote.sh" | Out-Null
if ($LASTEXITCODE -ne 0) { Die "could not copy the deploy step to $Server" }

$out = ssh -o BatchMode=yes $Server "bash /tmp/palette-deploy-remote.sh"
if ($LASTEXITCODE -ne 0) { Die "could not reach or update $Server" }

$serverHead = ($out | Select-String "^HEAD=").ToString().Split("=")[1].Trim()
$serverRunning = ($out | Select-String "^RUNNING=").ToString().Split("=")[1].Trim() -eq "yes"
Write-Host "  server HEAD $serverHead"

# Everything between the DIRTY markers is git status output from the server.
$lines = @($out)
$from = [array]::IndexOf($lines, "DIRTY<<EOF")
$to = [array]::IndexOf($lines, "EOF")
if ($from -ge 0 -and $to -gt $from) {
    # Guard the slice: with a clean tree the markers are adjacent, and
    # PowerShell silently reverses a range whose start exceeds its end,
    # which hands back the markers themselves as though they were changes.
    if (($to - $from) -gt 1) {
        $serverDirty = $lines[($from + 1)..($to - 1)] | Where-Object { $_.Trim() }
    } else {
        $serverDirty = @()
    }
    if ($serverDirty) {
        Die "the server's tree is dirty - same commit, different code:`n  $($serverDirty -join "`n  ")"
    }
    Ok "clean there"
}

if ($serverHead -ne $localHead) {
    Die "server is on $serverHead, this machine is on $localHead"
}
Ok "both machines on $localHead"

# ── 4. the running processes ─────────────────────────────────────────────────
# Deployed code and running code are different things: a process started
# before the pull keeps serving the old module until it is restarted.
Step "running processes"

if (-not $serverRunning) {
    Ok "server app is not running (started on demand - nothing stale to serve)"
} else {
    $status = $null
    try {
        $status = Invoke-RestMethod "http://$($Server.Split('@')[1]):$ServerPort/api/qs/status" -TimeoutSec 30
    } catch { Warn "server app is listening but did not answer: $($_.Exception.Message)" }

    if ($status) {
        $running = $status.palette.version
        if ($running -eq $localHead) {
            Ok "server app running $running"
        } else {
            Warn "server app is running $running but $localHead is deployed - restart it to pick up the change"
        }
    }
}

$localUp = (Test-NetConnection -ComputerName 127.0.0.1 -Port $LocalPort -WarningAction SilentlyContinue).TcpTestSucceeded
if (-not $localUp) {
    Ok "local app is not running"
} else {
    $lstatus = $null
    try {
        $lstatus = Invoke-RestMethod "http://127.0.0.1:$LocalPort/api/qs/status" -TimeoutSec 30
    } catch { Warn "local app is listening but did not answer" }
    if ($lstatus) {
        $lrunning = $lstatus.palette.version
        if ($lrunning -eq $localHead) {
            Ok "local app running $lrunning"
        } else {
            Warn "local app is running $lrunning but $localHead is deployed - restart launch.bat"
        }
    }
}

Write-Host "`ndone." -ForegroundColor Green
