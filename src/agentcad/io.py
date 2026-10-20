# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""File boundaries: strict DXF reads and atomic, opt-in overwrites."""

import math
import os
import tempfile
from contextlib import contextmanager
from enum import Enum
from pathlib import Path

import ezdxf
from ezdxf.lldxf.tagwriter import TagCollector

from .errors import CadError


def read_document(path):
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise CadError("FILE_NOT_FOUND", "Drawing does not exist.", {"path": str(source)})
    if source.suffix.lower() != ".dxf":
        raise CadError("UNSUPPORTED_FORMAT", "The DXF engine requires a .dxf file.")
    try:
        return ezdxf.readfile(source)
    except (OSError, ezdxf.DXFError, UnicodeError, ValueError) as exc:
        raise CadError("INVALID_DXF", "Cannot read the DXF drawing.", {"reason": str(exc)}) from exc


@contextmanager
def atomic_output(output, overwrite=False):
    """Publish only a complete file; never silently replace an existing destination."""
    target = Path(output).expanduser().resolve()
    if target.exists() and not overwrite:
        raise CadError(
            "OUTPUT_EXISTS", "Output exists; set overwrite explicitly.", {"path": str(target)}
        )
    if not target.parent.is_dir():
        raise CadError(
            "DIRECTORY_NOT_FOUND",
            "Output parent directory does not exist.",
            {"path": str(target.parent)},
        )
    descriptor, name = tempfile.mkstemp(prefix=".tcad-", suffix=target.suffix, dir=target.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        yield temporary
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise CadError("EMPTY_OUTPUT", "The operation did not produce a nonempty file.")
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, target)
        else:
            # Hard-link creation is atomic and fails if the target appears concurrently.
            try:
                os.link(temporary, target)
            except FileExistsError as exc:
                raise CadError(
                    "OUTPUT_EXISTS", "Output was created concurrently.", {"path": str(target)}
                ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def save_document(doc, output, overwrite=False):
    if Path(output).suffix.lower() != ".dxf":
        raise CadError("UNSUPPORTED_FORMAT", "Drawing output must have a .dxf extension.")
    with atomic_output(output, overwrite=overwrite) as temporary:
        for block in doc.blocks:
            for entity in block:
                validate_finite_entity(entity, doc.dxfversion)
        expected = _entity_signatures(doc)
        doc.saveas(temporary)
        # Verify the serialized artifact before publishing it.
        reopened = read_document(temporary)
        actual = _entity_signatures(reopened)
        if expected != actual:
            raise CadError(
                "SERIALIZATION_MISMATCH",
                "DXF serialization changed the entity inventory; output was not published.",
                {
                    "missing": sorted(expected - actual)[:20],
                    "unexpected": sorted(actual - expected)[:20],
                },
            )
    return str(Path(output).expanduser().resolve())


def _entity_signatures(doc):
    # Include block definitions, not only visible Model/Paper layouts.
    signatures = set()
    for block in doc.blocks:
        for entity in block:
            signatures.add((block.name, entity.dxf.handle, entity.dxftype()))
            for child in _linked_entities(entity):
                signatures.add((block.name, child.dxf.handle, child.dxftype()))
    return signatures


def _linked_entities(entity):
    if entity.dxftype() == "INSERT":
        return entity.attribs
    if entity.dxftype() == "POLYLINE":
        return entity.vertices
    return ()


def validate_finite_entity(entity, dxf_version):
    """Check exported numeric tags, including compound geometry and linked children."""
    for item in (entity, *_linked_entities(entity)):
        for tag in TagCollector.dxftags(item, dxfversion=dxf_version):
            if not isinstance(tag.value, bytes):
                jsonable(tag.value)


def jsonable(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CadError("NON_FINITE_RESULT", "Geometry contains a non-finite coordinate.")
        return value
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return jsonable(value.value)
    if hasattr(value, "tolist"):
        return jsonable(value.tolist())
    try:
        return [jsonable(item) for item in value]
    except TypeError as exc:
        raise CadError(
            "SERIALIZATION_ERROR", f"Unsupported result type: {type(value).__name__}"
        ) from exc
