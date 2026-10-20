# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

# Run from any working directory, without activating the virtual environment.
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    Write-Output '{"schema_version":"1.0","id":null,"ok":false,"error":{"code":"ENVIRONMENT_MISSING","message":"Run scripts/bootstrap.ps1 first."}}'
    exit 2
}
$previousPythonPath = $env:PYTHONPATH
$previousConverter = $env:TCAD_DWG2DXF
try {
    $env:PYTHONPATH = (Join-Path $projectRoot 'src')
    if (-not $env:TCAD_DWG2DXF) {
        $toolsPath = Join-Path $projectRoot '.tools'
        if (Test-Path -LiteralPath $toolsPath) {
            $converter = Get-ChildItem -LiteralPath $toolsPath -Recurse -File -Filter 'dwg2dxf.exe' |
                Sort-Object FullName | Select-Object -First 1
            if ($converter) { $env:TCAD_DWG2DXF = $converter.FullName }
        }
    }
    & $pythonExe -m agentcad @args
    $resultCode = $LASTEXITCODE
} finally {
    $env:PYTHONPATH = $previousPythonPath
    $env:TCAD_DWG2DXF = $previousConverter
}
exit $resultCode
