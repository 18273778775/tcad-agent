# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

param([switch]$WithDwg)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (Get-Command uv -ErrorAction SilentlyContinue) {
    if (-not (Test-Path -LiteralPath $pythonExe)) {
        & uv venv --python 3.12 (Join-Path $projectRoot '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
    }
    & uv sync --project $projectRoot --extra dev --locked
} else {
    if (-not (Test-Path -LiteralPath $pythonExe)) {
        & py -3 -m venv (Join-Path $projectRoot '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
    }
    & $pythonExe -m pip install -e "$projectRoot[dev]"
}
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
if ($WithDwg) { & (Join-Path $PSScriptRoot 'install-libredwg.ps1') }
