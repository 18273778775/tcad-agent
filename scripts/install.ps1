# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

#Requires -Version 5.1
<#
.SYNOPSIS
Install the pinned TCAD CLI into an isolated user environment, without cloning.
.DESCRIPTION
Requires Python 3.11+ (or uv). No administrator privileges are required.
Installs a user tcad.cmd shim, updates this process PATH and the user PATH.
PackageSource and InstallRoot allow explicit offline/local-wheel installation.
#>
[CmdletBinding()]
param(
    [string]$PackageSource = 'https://codeload.github.com/18273778775/tcad-agent/zip/refs/tags/v0.2.0',
    [string]$InstallRoot = '',
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
$environment = Join-Path $environments ('0.2.0-' + [Guid]::NewGuid().ToString('N'))
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

if (-not $UsePip -and (Get-Command uv -ErrorAction SilentlyContinue)) {
    & uv venv --python 3.12 $environment
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create the isolated CLI environment.' }
    $python = Join-Path $environment 'Scripts\python.exe'
    & uv pip install --python $python $PackageSource
} else {
    $pythonCommand = $null
    $pythonArguments = @()
    foreach ($candidate in @('py', 'python', 'python3')) {
        if (Get-Command $candidate -ErrorAction SilentlyContinue) {
            $candidateArguments = @()
            if ($candidate -eq 'py') { $candidateArguments = @('-3') }
            & $candidate @candidateArguments -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>$null
            if ($LASTEXITCODE -eq 0) {
                $pythonCommand = $candidate
                $pythonArguments = $candidateArguments
                break
            }
        }
    }
    if (-not $pythonCommand) {
        throw 'Python 3.11+ or uv is required. Install Python from python.org, then rerun this command.'
    }
    & $pythonCommand @pythonArguments -m venv $environment
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create the isolated CLI environment.' }
    $python = Join-Path $environment 'Scripts\python.exe'
    & $python -m pip install --disable-pip-version-check --no-input --timeout 60 --retries 2 $PackageSource
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
