# ManyClaude installer for Windows.
# ManyClaude runs on macOS and Linux, so on Windows it is installed inside WSL.
#   irm https://raw.githubusercontent.com/NZ-Linix/manyclaude/main/install.ps1 | iex
$ErrorActionPreference = 'Stop'

$Repo = if ($env:MANYCLAUDE_REPO) { $env:MANYCLAUDE_REPO } else { 'NZ-Linix/manyclaude' }
$Ref = if ($env:MANYCLAUDE_REF) { $env:MANYCLAUDE_REF } else { 'main' }
$Distro = $env:MANYCLAUDE_WSL_DISTRO

if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    throw 'WSL is required. Install it with: wsl --install  (then restart and rerun this installer)'
}
$wslArgs = @()
if ($Distro) { $wslArgs += @('-d', $Distro) }
& wsl.exe @wslArgs -e true 2>$null
if ($LASTEXITCODE -ne 0) {
    throw 'No working WSL distribution found. Run: wsl --install -d Ubuntu  (or set MANYCLAUDE_WSL_DISTRO)'
}

$token = $env:GITHUB_TOKEN
if (-not $token -and (Get-Command gh -ErrorAction SilentlyContinue)) {
    $token = (& gh auth token 2>$null)
}
$env:MANYCLAUDE_REPO = $Repo
$env:MANYCLAUDE_REF = $Ref
$env:WSLENV = (@($env:WSLENV, 'MANYCLAUDE_REPO', 'MANYCLAUDE_REF') | Where-Object { $_ }) -join ':'
if ($token) {
    $env:GITHUB_TOKEN = $token
    $env:WSLENV += ':GITHUB_TOKEN'
}

# The bootstrap in install.sh downloads the repository and installs it inside WSL.
$script = @'
set -eu
command -v curl >/dev/null || { echo 'curl is required inside WSL (sudo apt install curl).' >&2; exit 1; }
command -v python3 >/dev/null || { echo 'Python 3 is required inside WSL (sudo apt install python3).' >&2; exit 1; }
url="https://api.github.com/repos/$MANYCLAUDE_REPO/contents/install.sh?ref=$MANYCLAUDE_REF"
if [ -n "${GITHUB_TOKEN:-}" ]; then
    curl -fsSL --proto '=https' -H "Authorization: Bearer $GITHUB_TOKEN" -H 'Accept: application/vnd.github.raw' "$url" | sh
else
    curl -fsSL --proto '=https' -H 'Accept: application/vnd.github.raw' "$url" | sh
fi
'@
& wsl.exe @wslArgs -e sh -c ($script -replace "`r", '')
if ($LASTEXITCODE -ne 0) { throw "Installation inside WSL failed (exit $LASTEXITCODE)." }
Write-Host 'ManyClaude is installed inside WSL. Open your WSL shell and run: manyclaude'
