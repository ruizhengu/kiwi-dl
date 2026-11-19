# Install kiwi-dl on Windows:
#
#   irm https://raw.githubusercontent.com/ruizhengu/kiwi-dl/main/install.ps1 | iex
#
# A branch or tag instead of main:   $env:KIWI_DL_REF = "v0.2.0"; irm ... | iex
# pipx even though uv is installed:  $env:KIWI_DL_INSTALLER = "pipx"; irm ... | iex
# A local checkout (what CI does):   $env:KIWI_DL_SOURCE = "C:\path\to\kiwi-dl"; .\install.ps1
#
# kiwi-dl is installed as an isolated tool with uv, or pipx if uv is missing,
# so it never touches your system Python. Update with `kiwi-dl update`, remove
# with `kiwi-dl remove`.

param(
    [string]$Ref = ""
)

$ErrorActionPreference = "Stop"

$Repo = "https://github.com/ruizhengu/kiwi-dl"
if (-not $Ref) { $Ref = if ($env:KIWI_DL_REF) { $env:KIWI_DL_REF } else { "main" } }
$Source = if ($env:KIWI_DL_SOURCE) { $env:KIWI_DL_SOURCE } else { "git+$Repo@$Ref" }
$Installer = $env:KIWI_DL_INSTALLER

function Ok([string]$Message) { Write-Host "OK  $Message" -ForegroundColor Green }
function Step([string]$Message) { Write-Host ">>  $Message" -ForegroundColor Cyan }
function Note([string]$Message) { Write-Host "    $Message" -ForegroundColor DarkGray }

function Fail([string]$Message) {
    Write-Host "XX  $Message" -ForegroundColor Red
    exit 1
}

Write-Host "kiwi-dl installer"
Note ("Windows {0} · {1}" -f [Environment]::OSVersion.Version.ToString(3), $env:PROCESSOR_ARCHITECTURE)
if (-not $env:KIWI_DL_SOURCE) {
    if ($Ref -eq "main") {
        Note "Installing the latest main; set KIWI_DL_REF=v0.2.0 to pin a release."
    } else {
        Note "Installing $Ref."
    }
}
Write-Host ""

if ($Installer -and $Installer -notin @("uv", "pipx")) {
    Fail "KIWI_DL_INSTALLER must be uv or pipx, not '$Installer'."
}
if ($Installer -and -not (Get-Command $Installer -ErrorAction SilentlyContinue)) {
    Fail "KIWI_DL_INSTALLER=$Installer, but $Installer is not on your PATH."
}

if ($Installer -ne "pipx" -and (Get-Command uv -ErrorAction SilentlyContinue)) {
    Step "Installing with uv from $Source"
    uv tool install --force --reinstall $Source
    $Hint = "uv tool update-shell"
} elseif (Get-Command pipx -ErrorAction SilentlyContinue) {
    Step "Installing with pipx from $Source"
    pipx install --force $Source
    $Hint = "pipx ensurepath"
} else {
    Write-Host "XX  kiwi-dl installs with uv (or pipx), and neither is on your PATH." -ForegroundColor Red
    Write-Host "    Install uv, then run this again:"
    Write-Host "      winget install astral-sh.uv                     # Windows"
    Write-Host "      irm https://astral.sh/uv/install.ps1 | iex      # Windows"
    exit 1
}

Write-Host ""
# A fresh install is not on PATH in this session yet, so the shim folder is
# checked directly before falling back to the PATH hint.
$ShimDir = Join-Path $env:USERPROFILE ".local\bin"
$OnPath = Get-Command kiwi-dl -ErrorAction SilentlyContinue
if ($OnPath) {
    Ok "Installed $(kiwi-dl --version)"
} elseif (Test-Path (Join-Path $ShimDir "kiwi-dl.exe")) {
    Ok "Installed"
    Note "The shim folder is not on your PATH in this session: it will be in a new one."
    Note "  $ShimDir"
} else {
    Ok "Installed"
    Note "The install folder is not on your PATH yet: run ``$Hint``, then open a new terminal."
}
Note "Run kiwi-dl for the menu, or kdl <link> to download straight away."
