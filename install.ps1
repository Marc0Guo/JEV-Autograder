# One-command setup for JEV Autograder.
# Installs uv and Python 3.12 when missing, then syncs MarkItDown and SemIf.
# Does not download the model, ask for a token, or post a grade.

$ErrorActionPreference = "Stop"

function Refresh-Path {
    $env:Path = @(
        "$env:USERPROFILE\.local\bin",
        "$env:USERPROFILE\.cargo\bin",
        "$env:LOCALAPPDATA\uv",
        $env:Path
    ) -join ";"
}

function Find-RepoRoot {
    if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot "pyproject.toml"))) {
        return $PSScriptRoot
    }
    $here = (Get-Location).Path
    $manifest = Join-Path $here "pyproject.toml"
    if (Test-Path $manifest) {
        $text = Get-Content -Raw $manifest
        if ($text -match 'name = "autograde"') {
            return $here
        }
    }
    return $null
}

Refresh-Path
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv..."
    irm https://astral.sh/uv/install.ps1 | iex
    Refresh-Path
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is not on PATH. Open a new terminal and run this script again."
}

$root = Find-RepoRoot
if (-not $root) {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw "git is required to clone https://github.com/Marc0Guo/JEV-Autograder"
    }
    $root = Join-Path (Get-Location) "JEV-Autograder"
    if (Test-Path $root) {
        throw "$root already exists and is not this project. Move it aside or clone somewhere else."
    }
    git clone https://github.com/Marc0Guo/JEV-Autograder.git $root
}

Set-Location $root
Write-Host "Configuring $root"
uv python install 3.12
uv sync
uv run python -c "import autograde, markitdown, semif_phase1; print('ready', semif_phase1.__version__)"
Write-Host ""
Write-Host "Configured. Start the app with:"
Write-Host "  uv run autograde"
Write-Host "Then open http://127.0.0.1:8010"
