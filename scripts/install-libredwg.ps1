# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

#Requires -Version 5.1
<#
.SYNOPSIS
Install the pinned, portable LibreDWG Windows tools inside this workspace.
.DESCRIPTION
Downloads only the official LibreDWG 0.14 win64 release. Its SHA-256 is checked
before any extraction or execution. No PATH or system configuration is changed.
The single success-stream result is the absolute dwg2dxf.exe path, so callers can
use: $env:TCAD_DWG2DXF = & ./scripts/install-libredwg.ps1
.PARAMETER WorkspaceRoot
An existing workspace directory. Defaults to the parent of this scripts folder.
.PARAMETER IncludeTestFixture
Also provision the pinned upstream DWG integration fixture and its license under
artifacts/dwg-fixtures. Tests never download files on their own.
#>
[CmdletBinding()]
param(
    [string]$WorkspaceRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$IncludeTestFixture
)

$ErrorActionPreference = 'Stop'
$version = '0.14'
$archiveName = "libredwg-$version-win64.zip"
$downloadUri = "https://github.com/LibreDWG/libredwg/releases/download/$version/$archiveName"
$expectedSha256 = '1ad7e15344d20b3426c3435b078d82fb84b35062815946b2cca9c5fc9810fea8'
$upstreamCommit = 'd9468ae948b8f07a08efa756c19f8916052358c0'
$licenseUri = "https://raw.githubusercontent.com/LibreDWG/libredwg/$upstreamCommit/COPYING"
$licenseSha256 = '8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903'

function Get-VerifiedDownload([string]$Uri, [string]$Destination, [string]$Sha256) {
    if (Test-Path -LiteralPath $Destination) {
        $existing = Get-Item -LiteralPath $Destination -Force
        if ($existing.PSIsContainer -or ($existing.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Download destinations must be ordinary files: $Destination"
        }
    } else {
        $partialPath = $Destination + '.' + [Guid]::NewGuid().ToString('N') + '.part'
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Write-Verbose "Downloading $Uri"
            Invoke-WebRequest -Uri $Uri -OutFile $partialPath -UseBasicParsing -TimeoutSec 300
            $downloadSha256 = (Get-FileHash -LiteralPath $partialPath -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($downloadSha256 -ne $Sha256) {
                throw "Download SHA-256 mismatch. Expected $Sha256; got $downloadSha256."
            }
            Move-Item -LiteralPath $partialPath -Destination $Destination
        } finally {
            if (Test-Path -LiteralPath $partialPath -PathType Leaf) {
                Remove-Item -LiteralPath $partialPath
            }
        }
    }
    if ((Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Sha256) {
        throw "Cached download SHA-256 mismatch: $Destination"
    }
}

function Copy-License([string]$Source, [string]$Destination) {
    if (Test-Path -LiteralPath $Destination) {
        $existing = Get-Item -LiteralPath $Destination -Force
        if ($existing.PSIsContainer -or ($existing.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "License destinations must be ordinary files: $Destination"
        }
    }
    Copy-Item -LiteralPath $Source -Destination $Destination -Force
}

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or -not [Environment]::Is64BitOperatingSystem) {
    throw 'This installer provisions the Windows 64-bit release only.'
}
$workspace = (Resolve-Path -LiteralPath $WorkspaceRoot).ProviderPath
if (-not (Test-Path -LiteralPath $workspace -PathType Container)) {
    throw 'WorkspaceRoot must be an existing directory.'
}
$toolsRoot = [IO.Path]::GetFullPath((Join-Path $workspace '.tools'))
$downloadRoot = Join-Path $toolsRoot 'downloads'
$installRoot = Join-Path $toolsRoot "libredwg-$version"
$archivePath = Join-Path $downloadRoot $archiveName

# Every write stays in the explicitly selected workspace's .tools directory.
$workspacePrefix = $workspace.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
if (-not $toolsRoot.StartsWith($workspacePrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Refusing to write tools outside the selected workspace.'
}
foreach ($directory in @($toolsRoot, $downloadRoot, $installRoot)) {
    if (Test-Path -LiteralPath $directory) {
        $item = Get-Item -LiteralPath $directory -Force
        if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Tool directories must be ordinary directories: $directory"
        }
    } else {
        New-Item -ItemType Directory -Path $directory | Out-Null
    }
}

Get-VerifiedDownload $downloadUri $archivePath $expectedSha256
$actualSha256 = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()

# Check every ZIP destination before extraction. Keep upstream licensing files.
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [IO.Compression.ZipFile]::OpenRead($archivePath)
$installPrefix = $installRoot.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
try {
    foreach ($entry in $zip.Entries) {
        $entryPath = [IO.Path]::GetFullPath((Join-Path $installRoot $entry.FullName))
        if (-not $entryPath.StartsWith($installPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Archive entry escapes the install directory: $($entry.FullName)"
        }
        # Do not follow a pre-existing junction/symlink inside the install tree.
        $ancestor = $entryPath
        while ($ancestor -and $ancestor.StartsWith($installPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            if (Test-Path -LiteralPath $ancestor) {
                $item = Get-Item -LiteralPath $ancestor -Force
                if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                    throw "Refusing to extract through a reparse point: $ancestor"
                }
            }
            $ancestor = Split-Path -Parent $ancestor
        }
    }
} finally {
    $zip.Dispose()
}

# Re-extract the verified release on every invocation. This repairs modified
# binaries while reusing the cached download; no recursive deletion is used.
Expand-Archive -LiteralPath $archivePath -DestinationPath $installRoot -Force
$licensePath = Join-Path $downloadRoot "libredwg-$version-COPYING"
Get-VerifiedDownload $licenseUri $licensePath $licenseSha256
Copy-License $licensePath (Join-Path $installRoot 'COPYING')
$executables = @(Get-ChildItem -LiteralPath $installRoot -Filter 'dwg2dxf.exe' -Recurse -File)
if ($executables.Count -ne 1) {
    throw "Expected exactly one dwg2dxf.exe in $installRoot; found $($executables.Count)."
}
$record = [ordered]@{
    version = $version
    source = $downloadUri
    sha256 = $actualSha256
    executable = $executables[0].FullName
    license = 'GPL-3.0-or-later; see COPYING'
    license_source = $licenseUri
    license_sha256 = $licenseSha256
}
$record | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $installRoot 'tcad-install.json') -Encoding UTF8

if ($IncludeTestFixture) {
    $artifactRoot = Join-Path $workspace 'artifacts'
    $fixtureRoot = Join-Path $artifactRoot 'dwg-fixtures'
    foreach ($directory in @($artifactRoot, $fixtureRoot)) {
        if (Test-Path -LiteralPath $directory) {
            $item = Get-Item -LiteralPath $directory -Force
            if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw "Fixture directories must be ordinary directories: $directory"
            }
        } else {
            New-Item -ItemType Directory -Path $directory | Out-Null
        }
    }
    $fixtureUri = "https://raw.githubusercontent.com/LibreDWG/libredwg/$upstreamCommit/test/test-data/sample_2000.dwg"
    $fixtureSha256 = '4b8d9b1396a6d0decaa328dfc7cf17a09f2afa716ff8f40e9ca62ad1f1504ffa'
    Get-VerifiedDownload $fixtureUri (Join-Path $fixtureRoot 'sample_2000.dwg') $fixtureSha256
    Copy-License $licensePath (Join-Path $fixtureRoot 'COPYING')
    [ordered]@{
        source = $fixtureUri
        upstream_commit = $upstreamCommit
        sha256 = $fixtureSha256
        license = 'GPL-3.0-or-later; see COPYING'
        expected_modelspace = @{ CIRCLE = 1; TEXT = 1; LWPOLYLINE = 1; LINE = 3 }
        expected_circle_radius = 20
        expected_line_lengths = @(100, 100, 141.4213562373095)
    } | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $fixtureRoot 'PROVENANCE.json') -Encoding UTF8
}
Write-Output $executables[0].FullName
