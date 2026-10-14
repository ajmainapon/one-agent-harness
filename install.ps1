# One Agent Harness — Universal Installer for Windows (PowerShell)
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

$PyBin = $null
foreach ($cand in @("python", "python3", "py")) {
    if (Get-Command $cand -ErrorAction SilentlyContinue) {
        $PyBin = $cand
        break
    }
}

if (-not $PyBin) {
    Write-Error "Python 3.9+ is required. Please install Python from https://python.org and re-run .\install.ps1"
    exit 1
}

Write-Host "==> Installing One Agent Harness (Windows) using $PyBin..." -ForegroundColor Cyan
$env:PYTHONPATH = "$RepoRoot;$env:PYTHONPATH"
& $PyBin -m harness install

$LocalBin = Join-Path $HOME ".local\bin"
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($UserPath -notlike "*$LocalBin*") {
    [Environment]::SetEnvironmentVariable("Path", "$LocalBin;$UserPath", "User")
    $env:Path = "$LocalBin;$env:Path"
    Write-Host "==> Added $LocalBin to your Windows User PATH." -ForegroundColor Green
}

Write-Host "==> Starting Auto-Sync Watcher daemon..." -ForegroundColor Cyan
& $PyBin -m harness watch --daemon

Write-Host ""
Write-Host "Done! Run 'harness' or 'harness pick' in PowerShell or CMD." -ForegroundColor Green
