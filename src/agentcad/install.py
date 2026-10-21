# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Explicit, checksum-verified installation of optional upstream DWG tools."""

import hashlib
import json
import os
import platform
import shutil
import stat
import tempfile
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from .errors import CadError

LIBREDWG_VERSION = "0.14"
LIBREDWG_URL = (
    "https://github.com/LibreDWG/libredwg/releases/download/0.14/libredwg-0.14-win64.zip"
)
LIBREDWG_SHA256 = "1ad7e15344d20b3426c3435b078d82fb84b35062815946b2cca9c5fc9810fea8"
LICENSE_URL = (
    "https://raw.githubusercontent.com/LibreDWG/libredwg/"
    "d9468ae948b8f07a08efa756c19f8916052358c0/COPYING"
)
LICENSE_SHA256 = "8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903"


def user_data_dir():
    """Return a stable user location; never infer tools from the current directory."""
    override = os.environ.get("TCAD_DATA_HOME")
    if override:
        return Path(override).expanduser().absolute()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return (base / "tcad-agent").expanduser().absolute()


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _ordinary_directory(path):
    """Reject links/junctions in every ancestor before writing user tool files."""
    path = Path(path).absolute()
    for ancestor in (*reversed(path.parents), path):
        if ancestor.exists() or ancestor.is_symlink():
            attributes = getattr(ancestor.lstat(), "st_file_attributes", 0)
            if ancestor.is_symlink() or attributes & 0x400 or not ancestor.is_dir():
                raise CadError("UNSAFE_INSTALL_PATH", "Tool directories must be ordinary directories.")
        else:
            ancestor.mkdir()
    return path


def _download(url, target, expected, max_bytes):
    if target.exists():
        if target.is_symlink() or not target.is_file():
            raise CadError("UNSAFE_INSTALL_PATH", "Download cache must contain ordinary files.")
        if _sha256(target) == expected:
            return
    request = urllib.request.Request(url, headers={"User-Agent": "tcad-agent/0.2.0"})
    descriptor, temporary_name = tempfile.mkstemp(prefix=".download-", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output, urllib.request.urlopen(request, timeout=60) as response:
            size = 0
            while block := response.read(1024 * 1024):
                size += len(block)
                if size > max_bytes:
                    raise CadError("DOWNLOAD_TOO_LARGE", "Upstream download exceeds its size budget.")
                output.write(block)
        if _sha256(temporary) != expected:
            raise CadError("CHECKSUM_MISMATCH", "Upstream download failed SHA-256 verification.")
        os.replace(temporary, target)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise CadError("DOWNLOAD_FAILED", "Cannot download the official optional DWG tools.") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _extract_verified_zip(archive, destination):
    """Extract only bounded regular files, rejecting traversal and ZIP symlinks."""
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        if len(members) > 4096 or sum(item.file_size for item in members) > 512 * 1024 * 1024:
            raise CadError("UNSAFE_ARCHIVE", "Optional tool archive exceeds extraction limits.")
        for item in members:
            name = item.filename.replace("\\", "/")
            relative = PurePosixPath(name)
            mode = item.external_attr >> 16
            if relative.is_absolute() or ".." in relative.parts or ":" in name or stat.S_ISLNK(mode):
                raise CadError("UNSAFE_ARCHIVE", "Optional tool archive contains an unsafe path.")
            target = destination.joinpath(*relative.parts)
            if not target.resolve().is_relative_to(destination.resolve()):
                raise CadError("UNSAFE_ARCHIVE", "Archive path escapes the install directory.")
        bundle.extractall(destination)


def find_installed_dwg2dxf(*, data_dir=None):
    """Discover only an intact converter provisioned by explicit setup-dwg."""
    root = Path(data_dir) if data_dir is not None else user_data_dir()
    try:
        record = json.loads((root / "dwg-install.json").read_text(encoding="utf-8"))
        executable = Path(record["executable"])
        if not executable.resolve().is_relative_to((root / "tools").resolve()):
            return None
        if executable.is_file() and _sha256(executable) == record["executable_sha256"]:
            return str(executable.resolve())
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def install_dwg(*, data_dir=None):
    """Install pinned official LibreDWG win64; invoked only by explicit user action."""
    if os.name != "nt" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise CadError(
            "UNSUPPORTED_PLATFORM",
            "The bundled installer supports Windows x64. Install LibreDWG yourself and set TCAD_DWG2DXF.",
        )
    root = _ordinary_directory(Path(data_dir) if data_dir is not None else user_data_dir())
    downloads = _ordinary_directory(root / "downloads")
    tools = _ordinary_directory(root / "tools")
    archive = downloads / "libredwg-0.14-win64.zip"
    license_file = downloads / "libredwg-0.14-COPYING"
    try:
        _download(LIBREDWG_URL, archive, LIBREDWG_SHA256, 64 * 1024 * 1024)
        _download(LICENSE_URL, license_file, LICENSE_SHA256, 2 * 1024 * 1024)
        # A fresh directory avoids mutating a running converter during upgrades.
        # Discovery is published by the final atomic JSON replacement, so an
        # interrupted extraction never becomes the active converter.
        installed = tools / f"libredwg-{LIBREDWG_VERSION}-{uuid.uuid4().hex}"
        installed.mkdir()
        _extract_verified_zip(archive, installed)
        candidates = list(installed.rglob("dwg2dxf.exe"))
        if len(candidates) != 1:
            raise CadError("INVALID_TOOL_ARCHIVE", "Expected exactly one dwg2dxf.exe.")
        shutil.copyfile(license_file, installed / "COPYING")
        executable = candidates[0]
        record = {
            "version": LIBREDWG_VERSION,
            "source": LIBREDWG_URL,
            "sha256": LIBREDWG_SHA256,
            "executable": str(executable),
            "executable_sha256": _sha256(executable),
            "license": "GPL-3.0-or-later",
            "license_path": str(installed / "COPYING"),
            "license_source": LICENSE_URL,
            "license_sha256": LICENSE_SHA256,
        }
        descriptor, name = tempfile.mkstemp(prefix=".install-", suffix=".json", dir=root)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(record, stream, indent=2)
        os.replace(name, root / "dwg-install.json")
        return record
    except (OSError, zipfile.BadZipFile) as exc:
        raise CadError("INSTALL_FAILED", "Optional DWG tools could not be installed.") from exc
