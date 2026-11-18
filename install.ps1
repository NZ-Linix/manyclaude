# ManyClaude installer for Windows.
#   irm https://raw.githubusercontent.com/NZ-Linix/manyclaude/main/install.ps1 | iex
# Run from a checkout (.\install.ps1) to install that copy instead of downloading.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Repo = if ($env:MANYCLAUDE_REPO) { $env:MANYCLAUDE_REPO } else { 'NZ-Linix/manyclaude' }
$Ref = if ($env:MANYCLAUDE_REF) { $env:MANYCLAUDE_REF } else { 'main' }
$InstallDir = if ($env:MANYCLAUDE_INSTALL_DIR) { $env:MANYCLAUDE_INSTALL_DIR } else { Join-Path $env:LOCALAPPDATA 'manyclaude' }
$BinDir = if ($env:MANYCLAUDE_BIN_DIR) { $env:MANYCLAUDE_BIN_DIR } else { Join-Path $InstallDir 'bin' }
$Utf8 = New-Object System.Text.UTF8Encoding($false)

function Find-Python {
    foreach ($candidate in @(@('py', '-3'), @('python'), @('python3'))) {
        $command = Get-Command $candidate[0] -ErrorAction SilentlyContinue
        # The WindowsApps python.exe is a Store installer stub, not an interpreter.
        if (-not $command -or $command.Source -like '*\WindowsApps\*') { continue }
        $arguments = @($candidate | Select-Object -Skip 1) + @('-c', 'import sys; print(sys.executable if sys.version_info >= (3, 10) else "")')
        $executable = (& $command.Source @arguments 2>$null | Select-Object -First 1)
        if ($LASTEXITCODE -eq 0 -and $executable) { return $executable.Trim() }
    }
    return $null
}

function Update-SessionPath {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine')
}

$Python = Find-Python
if (-not $Python) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host 'Python 3.10+ not found; installing Python with winget...'
        winget install --id Python.Python.3.13 --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Host
        Update-SessionPath
        $Python = Find-Python
    }
    if (-not $Python) { throw 'Python 3.10+ is required. Install it from https://www.python.org/downloads/windows/ and rerun.' }
}

$Work = Join-Path ([IO.Path]::GetTempPath()) ('manyclaude-' + [Guid]::NewGuid())
New-Item -ItemType Directory -Path $Work | Out-Null
try {
    if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot 'manyclaude'))) {
        $Source = $PSScriptRoot
    } else {
        Write-Host "Downloading $Repo@$Ref..."
        $headers = @{ 'User-Agent' = 'manyclaude-installer' }
        $token = $env:GITHUB_TOKEN
        if (-not $token -and (Get-Command gh -ErrorAction SilentlyContinue)) { $token = (& gh auth token 2>$null) }
        if ($token) { $headers['Authorization'] = "Bearer $token" }
        $archive = Join-Path $Work 'manyclaude.zip'
        Invoke-WebRequest -UseBasicParsing -Headers $headers -OutFile $archive "https://api.github.com/repos/$Repo/zipball/$Ref"
        Expand-Archive -Path $archive -DestinationPath (Join-Path $Work 'src')
        $Source = (Get-ChildItem (Join-Path $Work 'src') -Directory | Select-Object -First 1).FullName
    }
    if ((Resolve-Path $Source).Path.TrimEnd('\') -eq [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')) {
        throw 'Choose an install directory different from the source folder.'
    }

    New-Item -ItemType Directory -Force -Path $InstallDir, $BinDir | Out-Null
    $entry = Join-Path $BinDir 'manyclaude.cmd'
    if ((Test-Path $entry) -and -not ((Get-Content -Raw $entry) -match 'manyclaude global launcher')) {
        throw "Refusing to overwrite an unrelated command: $entry"
    }
    # The install folder also holds account metadata (profiles.json); only program files are replaced.
    Get-ChildItem $Source -File | Where-Object { $_.Name -eq 'manyclaude' -or $_.Name -eq 'README.md' -or $_.Extension -eq '.py' } |
        ForEach-Object { Copy-Item $_.FullName -Destination $InstallDir -Force }
    $cache = Join-Path $InstallDir '__pycache__'
    if (Test-Path $cache) { Remove-Item -Recurse -Force $cache }

    [IO.File]::WriteAllText((Join-Path $InstallDir 'command-bin-dir'), $BinDir + [Environment]::NewLine, $Utf8)
    $script = Join-Path $InstallDir 'manyclaude'
    [IO.File]::WriteAllText($entry, "@echo off`r`nrem manyclaude global launcher`r`n`"$Python`" `"$script`" %*`r`nexit /b %ERRORLEVEL%`r`n", $Utf8)
    [IO.File]::WriteAllText((Join-Path $InstallDir 'manyclaude-acp.cmd'),
        "@echo off`r`nsetlocal`r`nset `"MANYCLAUDE_HOME=$InstallDir`"`r`n`"$Python`" `"$script`" global acp %*`r`n", $Utf8)

    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not (($userPath -split ';') -contains $BinDir)) {
        [Environment]::SetEnvironmentVariable('Path', (@($BinDir, $userPath) | Where-Object { $_ }) -join ';', 'User')
    }
    if (-not (($env:Path -split ';') -contains $BinDir)) { $env:Path = "$BinDir;$env:Path" }

    # Python's curses module is not bundled on Windows; the wizard needs this wheel.
    & $Python -m pip install --user --quiet --disable-pip-version-check windows-curses 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Warning 'Could not install windows-curses; the interactive wizard is unavailable, commands still work.'
    }

    $env:MANYCLAUDE_HOME = $InstallDir
    & $Python $script refresh
    if ($LASTEXITCODE -ne 0) { throw "manyclaude refresh failed (exit $LASTEXITCODE)." }
    Write-Host "Installed: $entry"
    Write-Host 'Open a new terminal, then run: manyclaude'
} finally {
    Remove-Item -Recurse -Force $Work -ErrorAction SilentlyContinue
}
