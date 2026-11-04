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

function Get-PythonPath([string]$Exe, [string]$Version) {
    # The full path of a Python 3.11 or later that `$Exe` (with `-$Version` for py) starts, or $null. The path comes
    # from sys.executable, so the venv is made with that interpreter itself and nothing else is passed along.
    if (-not (Get-Command $Exe -ErrorAction SilentlyContinue)) { return $null }
    $probe = "import sys; print(sys.executable if sys.version_info >= (3, 11) else '')"
    try {
        if ($Version) { $found = & $Exe "-$Version" -c $probe 2>$null } else { $found = & $Exe -c $probe 2>$null }
    } catch {
        return $null
    }
    if ($LASTEXITCODE -ne 0 -or -not $found) { return $null }
    $path = "$found".Trim()
    if ($path -and (Test-Path -LiteralPath $path)) { return $path }
    return $null
}

function Find-Python {
    # The launcher's 3.13, 3.12 and 3.11, then its newest (3.14 and later too), then python on the PATH.
    foreach ($version in "3.13", "3.12", "3.11", "3") {
        $path = Get-PythonPath "py" $version
        if ($path) { return $path }
    }
    return Get-PythonPath "python" ""
}

function Update-SessionPath {
    # What winget just added, seen in this window too.
    $env:Path = $env:Path + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}

function Stop-Install([string]$Why) {
    Write-Host "Rafiq didn't install: $Why" -ForegroundColor Red
    exit 1
}

$python = Find-Python
if (-not $python -and (Get-Command winget -ErrorAction SilentlyContinue)) {
    Write-Host "Installing Python 3.13 with winget..."
    & winget install -e --id Python.Python.3.13 --scope user --accept-package-agreements --accept-source-agreements
    Update-SessionPath
    $python = Find-Python
}
if (-not $python) {
    Stop-Install "it needs Python 3.11 or later. Install it from https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH'), then run this again."
}
Write-Host "Python: $python"

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "Creating .venv..."
    & $python -m venv (Join-Path $PSScriptRoot ".venv")
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPython)) {
        Stop-Install "$python couldn't make the environment in .venv (exit code $LASTEXITCODE)."
    }
}

& $venvPython -m pip install --quiet --upgrade pip
if ($LASTEXITCODE -ne 0) { Stop-Install "pip couldn't update itself." }
& $venvPython -m pip install --quiet -e ".[google,windows]"
if ($LASTEXITCODE -ne 0) { Stop-Install "pip couldn't install Rafiq; its messages above say why." }
# Piper's voices need piper-phonemize, which may have no wheel for this Python on Windows: Rafiq runs without them.
& $venvPython -m pip install --quiet -e ".[google,windows,offline-voice]" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Warning "The offline voices didn't install; Rafiq uses Edge or Azure voices instead."
}

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "Installing ffmpeg (for playing answers and reading songs) with winget..."
        & winget install -e --id Gyan.FFmpeg --scope user --accept-package-agreements --accept-source-agreements
        Update-SessionPath
    }
    if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
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
if (-not (Test-Path -LiteralPath $rafiqExe)) { Stop-Install "pip made no $rafiqExe." }
Copy-Item -LiteralPath $rafiqExe -Destination (Join-Path $bin "rafiq.exe") -Force
# The user PATH as stored, %VARIABLES% unexpanded, and written back as such: expanding them would freeze an entry
# like %JAVA_HOME%\bin at today's value.
$key = Get-Item -LiteralPath "HKCU:\Environment"
$userPath = $key.GetValue("Path", "", "DoNotExpandEnvironmentNames")
if (-not (($userPath -split ";") -contains $bin)) {
    $newPath = (@($bin) + ($userPath -split ";" | Where-Object { $_ })) -join ";"
    Set-ItemProperty -LiteralPath "HKCU:\Environment" -Name Path -Value $newPath -Type ExpandString
    Write-Host "Added $bin to your PATH: open a new terminal to use 'rafiq' there."
}
$env:Path = "$bin;$env:Path"

Write-Host "Installed: $rafiqExe"
if (-not $NoSetup) {
    & $rafiqExe setup
    exit $LASTEXITCODE
}
Write-Host "Next: rafiq setup"
