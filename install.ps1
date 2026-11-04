# Install Rafiq on Windows 10 or 11 (#193): a virtual environment in .venv next to this script, the dependencies
# (Google, the volume through Core Audio, and the offline voices where they install), ffmpeg, then `rafiq setup`.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1             install, then run the setup wizard
#   powershell -ExecutionPolicy Bypass -File install.ps1 -NoSetup    install only (run `rafiq setup` later)
#
# Every program runs with its arguments as a list (the & operator): nothing is built into a command string.

param([switch]$NoSetup)

# No $ErrorActionPreference = "Stop": Windows PowerShell 5.1 would stop at any warning pip writes to stderr. Each
# step's exit code is checked instead.
Set-Location -LiteralPath $PSScriptRoot

# Rafiq runs from this folder at every logon. Outside your profile (C:\rafiq, a second drive) Windows usually lets
# every account change files, and so the code that runs as you.
if (-not $PSScriptRoot.StartsWith($env:USERPROFILE, [StringComparison]::OrdinalIgnoreCase)) {
    Write-Warning "$PSScriptRoot is outside your profile, where other accounts on this PC can usually change files. Clone Rafiq under $env:USERPROFILE instead."
}

function Test-Python([string[]]$Command) {
    # True for a Python 3.11 or later.
    try {
        $exe, $rest = $Command
        & $exe @rest -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Find-Python {
    foreach ($version in "3.13", "3.12", "3.11") {
        if ((Get-Command py -ErrorAction SilentlyContinue) -and (Test-Python @("py", "-$version"))) {
            return @("py", "-$version")
        }
    }
    # The newest Python the launcher knows (3.14 and later too), then python on the PATH.
    if ((Get-Command py -ErrorAction SilentlyContinue) -and (Test-Python @("py", "-3"))) {
        return @("py", "-3")
    }
    if ((Get-Command python -ErrorAction SilentlyContinue) -and (Test-Python @("python"))) {
        return @("python")
    }
    return $null
}

$python = Find-Python
if (-not $python -and (Get-Command winget -ErrorAction SilentlyContinue)) {
    Write-Host "Installing Python 3.13 with winget..."
    & winget install -e --id Python.Python.3.13 --scope user --accept-package-agreements --accept-source-agreements
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + $env:Path
    $python = Find-Python
}
if (-not $python) {
    Write-Error "Rafiq needs Python 3.11 or later: install it from https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH'), then run this again."
    exit 1
}

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "Creating .venv..."
    $exe, $rest = $python
    & $exe @rest -m venv .venv
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -e ".[google,windows]"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
# Piper's voices need piper-phonemize, which may have no wheel for this Python on Windows: Rafiq runs without them.
& $venvPython -m pip install --quiet -e ".[google,windows,offline-voice]" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Warning "The offline voices didn't install; Rafiq uses Edge or Azure voices instead."
}

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "Installing ffmpeg (for playing answers and reading songs) with winget..."
        & winget install -e --id Gyan.FFmpeg --scope user --accept-package-agreements --accept-source-agreements
    }
    if ($LASTEXITCODE -ne 0 -or -not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
        Write-Warning "ffmpeg isn't on the PATH yet: open a new terminal, or install it from https://ffmpeg.org"
    }
}

# The guides say `rafiq doctor`, not `.venv\Scripts\rafiq doctor` (#171): a copy of rafiq.exe in
# %USERPROFILE%\.local\bin, on the user's PATH. pip's launcher names the venv's python by its full path, so the copy
# runs the same Rafiq; a rafiq.cmd would pass every argument through cmd.exe's parser. .venv\Scripts itself stays
# off the PATH, where its python.exe would hide the one the user installed.
$bin = Join-Path $env:USERPROFILE ".local\bin"
New-Item -ItemType Directory -Force -Path $bin | Out-Null
$rafiqExe = Join-Path $PSScriptRoot ".venv\Scripts\rafiq.exe"
Copy-Item -LiteralPath $rafiqExe -Destination (Join-Path $bin "rafiq.exe") -Force
# The user PATH as stored, %VARIABLES% unexpanded, and written back as such: expanding them would freeze an entry
# like %JAVA_HOME%\bin at today's value.
$key = Get-Item -LiteralPath "HKCU:\Environment"
$userPath = $key.GetValue("Path", "", "DoNotExpandEnvironmentNames")
if (-not (($userPath -split ";") -contains $bin)) {
    $newPath = (@($bin) + ($userPath -split ";" | Where-Object { $_ })) -join ";"
    Set-ItemProperty -LiteralPath "HKCU:\Environment" -Name Path -Value $newPath -Type ExpandString
    Write-Host "Added $bin to your PATH: open a new terminal to use `rafiq` there."
}
$env:Path = "$bin;$env:Path"

Write-Host "Installed: $rafiqExe"
if (-not $NoSetup) {
    & $rafiqExe setup
    exit $LASTEXITCODE
}
Write-Host "Next: rafiq setup"
