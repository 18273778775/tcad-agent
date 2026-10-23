# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

#Requires -Version 5.1
<#
.SYNOPSIS
Install the pinned TCAD CLI into an isolated user environment, without cloning.
.DESCRIPTION
Requires CPython 3.11-3.14 (or uv). No administrator privileges are required.
Installs a user tcad.cmd shim, updates this process PATH and the user PATH.
PackageSource and InstallRoot allow explicit offline/local-wheel installation.
Python selects an explicit interpreter for the pip installation path. By default,
the Python on PATH is preferred over the Windows launcher, which can select a
different installation even inside an activated environment.
#>
[CmdletBinding()]
param(
    [string]$PackageSource = 'https://codeload.github.com/18273778775/tcad-agent/zip/refs/tags/v0.4.0',
    [string]$InstallRoot = '',
    [string]$Python = '',
    [switch]$UsePip,
    [switch]$NoModifyUserPath
)

$ErrorActionPreference = 'Stop'
if (-not $InstallRoot) {
    $InstallRoot = Join-Path $env:LOCALAPPDATA 'tcad-agent-cli'
}
$root = [IO.Path]::GetFullPath($InstallRoot)
$bin = Join-Path $root 'bin'
$environments = Join-Path $root 'venvs'
$environment = Join-Path $environments ('0.4.0-' + [Guid]::NewGuid().ToString('N'))
foreach ($directory in @($root, $bin, $environments)) {
    $ancestor = $directory
    while ($ancestor) {
        if (Test-Path -LiteralPath $ancestor) {
            $item = Get-Item -LiteralPath $ancestor -Force
            if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw "Install directories must be ordinary directories: $ancestor"
            }
        }
        $ancestor = Split-Path -Parent $ancestor
    }
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}

$downloadRoot = $null
try {
    $sourceUri = $null
    if ([Uri]::TryCreate($PackageSource, [UriKind]::Absolute, [ref]$sourceUri) -and
        $sourceUri.Scheme -in @('http', 'https') -and
        $sourceUri.AbsolutePath -notmatch '(?i)\.(zip|whl|tar\.gz|tar\.bz2|tar\.xz|tgz)$') {
        # codeload has no filename suffix; uv requires a local archive extension.
        $downloadRoot = Join-Path ([IO.Path]::GetTempPath()) ('tcad-install-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $downloadRoot | Out-Null
        $downloadZip = Join-Path $downloadRoot 'package.zip'
        $previousProgress = $ProgressPreference
        try {
            $ProgressPreference = 'SilentlyContinue'
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -Uri $PackageSource -OutFile $downloadZip -UseBasicParsing -TimeoutSec 60
        } finally {
            $ProgressPreference = $previousProgress
        }
        $PackageSource = $downloadZip
    }

if (-not $UsePip -and -not $Python -and (Get-Command uv -ErrorAction SilentlyContinue)) {
    & uv venv --python 3.12 $environment
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create the isolated CLI environment.' }
    $environmentPython = Join-Path $environment 'Scripts\python.exe'
    & uv pip install --python $environmentPython $PackageSource
} else {
    $pythonCommand = $null
    $candidates = @()
    if ($Python) {
        $candidates = @(@{ Command = $Python; Arguments = @() })
    } else {
        # setup-python and activated environments put the intended Python on
        # PATH. `py -3` ignores that selection and may choose an unrelated,
        # newer interpreter without compatible dependency wheels.
        foreach ($candidate in @('python', 'python3')) {
            $candidates += @{ Command = $candidate; Arguments = @() }
        }
        foreach ($selector in @('-3.12', '-3.13', '-3.11', '-3.14', '-3')) {
            $candidates += @{ Command = 'py'; Arguments = @($selector) }
        }
    }
    foreach ($candidate in $candidates) {
        $command = Get-Command $candidate.Command -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        $candidateArguments = $candidate.Arguments
        try {
            $probe = & $command @candidateArguments -I -c 'import json, sys; print(json.dumps(dict(executable=sys.executable, major=sys.version_info.major, minor=sys.version_info.minor, implementation=sys.implementation.name)))' 2>$null
        } catch { continue }
        if ($LASTEXITCODE -eq 0 -and $probe) {
            try {
                $pythonInfo = $probe | Select-Object -Last 1 | ConvertFrom-Json
            } catch { continue }
            if ($pythonInfo.implementation -ne 'cpython' -or
                $pythonInfo.major -ne 3 -or $pythonInfo.minor -lt 11 -or $pythonInfo.minor -gt 14) {
                continue
            }
            # Use the resolved interpreter for the whole installation, rather
            # than asking a launcher or alias to select Python a second time.
            $resolvedPython = [string]$pythonInfo.executable
            if (Test-Path -LiteralPath $resolvedPython -PathType Leaf) {
                $pythonCommand = $resolvedPython
                break
            }
        }
    }
    if (-not $pythonCommand -and $Python) {
        throw "The selected interpreter must be CPython 3.11-3.14: $Python. TCAD's pinned native dependencies do not provide Python 3.15 wheels. Select Python 3.12 with -Python <path>, or install uv and rerun without -UsePip."
    }
    if (-not $pythonCommand) {
        throw 'CPython 3.11-3.14 or uv is required. TCAD''s pinned native dependencies do not provide Python 3.15 wheels. Install Python 3.12 and select it with -Python <path>, or install uv and rerun without -UsePip.'
    }
    & $pythonCommand -m venv $environment
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create the isolated CLI environment.' }
    $environmentPython = Join-Path $environment 'Scripts\python.exe'
    & $environmentPython -m pip install --disable-pip-version-check --no-input --timeout 60 --retries 2 $PackageSource
}
if ($LASTEXITCODE -ne 0) { throw 'TCAD installation failed. The previous command shim remains active.' }
$executable = Join-Path $environment 'Scripts\tcad.exe'
& $executable --version
if ($LASTEXITCODE -ne 0) { throw 'Installed TCAD failed its launch check.' }

# Publish the command only after package installation and launch both succeed.
$shim = Join-Path $bin 'tcad.cmd'
$temporaryShim = Join-Path $bin ([Guid]::NewGuid().ToString('N') + '.cmd')
if (Test-Path -LiteralPath $shim) {
    $existingShim = Get-Item -LiteralPath $shim -Force
    if ($existingShim.PSIsContainer -or ($existingShim.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Command shim must be an ordinary file: $shim"
    }
}
$relativeExecutable = '..\venvs\' + (Split-Path -Leaf $environment) + '\Scripts\tcad.exe'
@('@echo off', ('"%~dp0' + $relativeExecutable + '" %*'), 'exit /b %errorlevel%') | Set-Content -LiteralPath $temporaryShim -Encoding ASCII
Move-Item -LiteralPath $temporaryShim -Destination $shim -Force
if (($env:Path -split ';') -notcontains $bin) { $env:Path = $bin + ';' + $env:Path }
if (-not $NoModifyUserPath) {
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (($userPath -split ';') -notcontains $bin) {
        [Environment]::SetEnvironmentVariable('Path', ($bin + ';' + $userPath), 'User')
    }
}
Write-Host "TCAD installed. Run: tcad capabilities"
Write-Host "DWG support is optional: tcad setup-dwg"
Write-Host "Install location: $root. New terminals inherit the user PATH; this shell is ready."
} finally {
    # Only this invocation's private download is removed; local sources stay intact.
    if ($downloadRoot) {
        $downloadZip = Join-Path $downloadRoot 'package.zip'
        if (Test-Path -LiteralPath $downloadZip -PathType Leaf) {
            Remove-Item -LiteralPath $downloadZip -Force -ErrorAction SilentlyContinue
        }
        if (Test-Path -LiteralPath $downloadRoot -PathType Container) {
            Remove-Item -LiteralPath $downloadRoot -ErrorAction SilentlyContinue
        }
    }
}
