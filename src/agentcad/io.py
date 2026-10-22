# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""File boundaries: strict DXF reads and atomic, opt-in overwrites."""

import math
import os
import tempfile
import hashlib
import time
from contextvars import ContextVar
from contextlib import contextmanager
from enum import Enum
from pathlib import Path


from .errors import CadError

_HELD_LOCKS = ContextVar("tcad_held_destination_locks", default=frozenset())
_REVISION_CHECKS = ContextVar("tcad_revision_checks", default={})


def check_input_size(source):
    """Reject oversized files before allocating the editable document model."""
    try:
        maximum = int(os.environ.get("TCAD_MAX_INPUT_MB", "128")) * 1024 * 1024
    except ValueError as exc:
        raise CadError("INVALID_CONFIGURATION", "TCAD_MAX_INPUT_MB must be an integer.") from exc
    if maximum <= 0:
        raise CadError("INVALID_CONFIGURATION", "TCAD_MAX_INPUT_MB must be positive.")
    size = Path(source).stat().st_size
    if size > maximum:
        raise CadError("INPUT_TOO_LARGE", "Drawing exceeds the configured input file budget.",
                       {"bytes": size, "max_bytes": maximum})


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _revision(path):
    return file_sha256(path) if Path(path).is_file() else "missing"


@contextmanager
def destination_lock(output, timeout=10):
    """Serialize cooperating TCAD writers; OS releases the lock if a worker dies."""
    target = str(Path(output).expanduser().resolve())
    held = _HELD_LOCKS.get()
    if target in held:
        yield
        return
    base = Path(os.environ.get("TCAD_CACHE_DIR") or
                (Path(os.environ.get("LOCALAPPDATA", Path.home() / ".cache")) /
                 "tcad-agent" / "cache")) / "locks"
    base.mkdir(parents=True, exist_ok=True)
    lock = base / (hashlib.sha256(os.path.normcase(target).encode()).hexdigest() + ".lock")
    if lock.is_symlink():
        raise CadError("UNSAFE_PATH", "Destination lock must be an ordinary file.")
    with lock.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        started = time.monotonic()
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() - started >= timeout:
                    raise CadError("WRITE_LOCKED", "Another TCAD writer holds this destination.",
                                   {"output": target}) from exc
                time.sleep(0.025)
        token = _HELD_LOCKS.set(held | {target})
        try:
            yield
        finally:
            _HELD_LOCKS.reset(token)
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def revision_guard(input_path, output, expected_source_sha256=None, expected_output_sha256=None):
    """Hold the destination lock from initial read through atomic publication."""
    target = str(Path(output).expanduser().resolve())
    if target in _REVISION_CHECKS.get():
        yield
        return
    source = Path(input_path).expanduser().resolve() if input_path else None
    with destination_lock(target):
        source_revision = _revision(source) if source else None
        target_revision = _revision(target)
        if expected_source_sha256 is not None and source_revision != expected_source_sha256:
            raise CadError("REVISION_CONFLICT", "The input drawing no longer has the expected revision.",
                           {"expected": expected_source_sha256, "actual": source_revision})
        if expected_output_sha256 is not None and target_revision != expected_output_sha256:
            raise CadError("REVISION_CONFLICT", "The output no longer has the expected revision.",
                           {"expected": expected_output_sha256, "actual": target_revision})
        checks = dict(_REVISION_CHECKS.get())
        checks[target] = (source, source_revision, target_revision)
        token = _REVISION_CHECKS.set(checks)
        try:
            yield
        finally:
            _REVISION_CHECKS.reset(token)


def _check_publish_revision(target):
    check = _REVISION_CHECKS.get().get(str(target))
    if check:
        source, source_revision, target_revision = check
        if ((source and _revision(source) != source_revision) or
                _revision(target) != target_revision):
            raise CadError("REVISION_CONFLICT", "A drawing changed while this edit was being prepared.")


def read_document(path):
    import ezdxf
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise CadError("FILE_NOT_FOUND", "Drawing does not exist.", {"path": str(source)})
    if source.suffix.lower() != ".dxf":
        raise CadError("UNSUPPORTED_FORMAT", "The DXF engine requires a .dxf file.")
    check_input_size(source)
    try:
        return ezdxf.readfile(source)
    except (OSError, ezdxf.DXFError, UnicodeError, ValueError) as exc:
        raise CadError("INVALID_DXF", "Cannot read the DXF drawing.", {"reason": str(exc)}) from exc


@contextmanager
def atomic_output(output, overwrite=False):
    """Publish only a complete file; never silently replace an existing destination."""
    target = Path(output).expanduser().resolve()
    with destination_lock(target):
        with _atomic_output_locked(target, overwrite=overwrite) as temporary:
            yield temporary


@contextmanager
def _atomic_output_locked(target, overwrite=False):
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
    operation = os.environ.get("TCAD_OPERATION_ID", "")
    prefix = f".tcad-{operation}-" if operation and all(c in "0123456789abcdef" for c in operation) else ".tcad-"
    descriptor, name = tempfile.mkstemp(prefix=prefix, suffix=target.suffix, dir=target.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        yield temporary
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise CadError("EMPTY_OUTPUT", "The operation did not produce a nonempty file.")
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        _check_publish_revision(target)
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
    from ezdxf.lldxf.tagwriter import TagCollector
    for item in (entity, *_linked_entities(entity)):
        for tag in TagCollector.dxftags(item, dxfversion=dxf_version):
            if not isinstance(tag.value, bytes):
                jsonable(tag.value)


def jsonable(value):
    """Convert CAD results to finite JSON, retaining undecodable text evidence.

    ezdxf uses surrogateescape for undecodable legacy DXF bytes. Such codepoints
    cannot be encoded as UTF-8/SQLite text. Expose each as a literal ``\\udcXX``
    escape, with bounded provenance on a dictionary result. Strict request JSON
    validation remains separate and continues to reject all surrogates.
    """
    issues = []
    issue_count = 0
    issue_limit = 4

    def safe_text(text, path):
        nonlocal issue_count
        if not any(0xD800 <= ord(character) <= 0xDFFF for character in text):
            return text
        chunks = []
        escaped_index = 0
        for index, character in enumerate(text):
            codepoint = ord(character)
            if 0xD800 <= codepoint <= 0xDFFF:
                escaped = f"\\u{codepoint:04x}"
                issue_count += 1
                if len(issues) < issue_limit:
                    issue = {
                        "path": path[:128] or "/", "path_complete": len(path) <= 128,
                        "original_character_index": index, "escaped_character_index": escaped_index,
                        "codepoint": f"U+{codepoint:04X}", "escape": escaped,
                    }
                    if 0xDC80 <= codepoint <= 0xDCFF:
                        issue["byte_escape"] = f"\\x{codepoint - 0xDC00:02x}"
                    issues.append(issue)
                chunks.append(escaped)
                escaped_index += len(escaped)
            else:
                chunks.append(character)
                escaped_index += 1
        return "".join(chunks)

    def convert(item, path):
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, str):
            return safe_text(item, path)
        if isinstance(item, float):
            if not math.isfinite(item):
                raise CadError("NON_FINITE_RESULT", "Geometry contains a non-finite coordinate.")
            return item
        if isinstance(item, dict):
            result = {}
            for key, child in item.items():
                key = str(key)
                # JSON Pointer paths escape ~ and /; invalid key codepoints are
                # represented literally before they can enter diagnostics.
                pointer = key.encode("utf-8", "backslashreplace").decode("utf-8").replace("~", "~0").replace("/", "~1")
                field_path = path + "/" + pointer
                safe_key = safe_text(key, field_path + "/@key")
                if safe_key in result:
                    raise CadError("TEXT_ENCODING_KEY_COLLISION", "Escaping undecodable CAD text would merge distinct object keys.")
                result[safe_key] = convert(child, field_path)
            return result
        if isinstance(item, (list, tuple)):
            return [convert(child, path + "/" + str(index)) for index, child in enumerate(item)]
        if isinstance(item, Path):
            return safe_text(str(item), path)
        if isinstance(item, Enum):
            return convert(item.value, path)
        if hasattr(item, "tolist"):
            return convert(item.tolist(), path)
        try:
            return [convert(child, path + "/" + str(index)) for index, child in enumerate(item)]
        except TypeError as exc:
            raise CadError("SERIALIZATION_ERROR", f"Unsupported result type: {type(item).__name__}") from exc

    result = convert(value, "")
    if issues and isinstance(result, dict):
        metadata = {
            "encoding_complete": False, "representation": "literal_backslash_u_escape",
            "issue_count": issue_count, "issues_returned": len(issues),
            "issues_complete": issue_count == len(issues), "issues": issues,
            "warning": "Undecodable CAD text is escaped, not decoded; literal escape text is distinguished by these field paths and character indices. Source bytes are unchanged.",
        }
        if "text_encoding" in result:
            metadata["previous_metadata"] = result["text_encoding"]
        result["text_encoding"] = metadata
    return result
