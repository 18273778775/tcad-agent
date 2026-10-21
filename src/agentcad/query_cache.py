# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Bounded SQLite query snapshots. Cache data is JSON and never executable objects.

A new source/projection is prepared once; keyset pages only read SQLite. Native
entity selection is indexed before geometry, bounds, and vertex formatting.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from .errors import CadError

CACHE_VERSION = 2
MAX_CACHE_BYTES = 512 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 128 * 1024 * 1024
MAX_CACHE_FILES = 16
MAX_VIEWS = 32


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _cache_root():
    configured = os.environ.get("TCAD_CACHE_DIR")
    root = Path(configured).expanduser() if configured else (
        Path(os.environ.get("LOCALAPPDATA", Path.home() / ".cache")) / "tcad-agent" / "queries"
    )
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root.resolve()


def _source(path):
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise CadError("FILE_NOT_FOUND", "Drawing does not exist.", {"path": str(source)})
    if source.suffix.lower() != ".dxf":
        raise CadError("UNSUPPORTED_FORMAT", "Query snapshots require DXF input.")
    stat = source.stat()
    # ctime changes even if another process restores mtime and file size. This is
    # a local version token, not a cryptographic attestation of unmodified bytes.
    identity = [str(source), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
    revision = "stat-v1:" + hashlib.sha256(_json(identity).encode()).hexdigest()
    return source, revision


def _prune(root, keep):
    files = sorted(root.glob("query-*.sqlite3"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    if keep in files:
        files.remove(keep)
        files.insert(0, keep)
    total = 0
    retained = 0
    for path in files:
        size = path.stat().st_size
        if path == keep or (retained < MAX_CACHE_FILES and total + size <= MAX_CACHE_BYTES):
            total += size
            retained += 1
            continue
        try:
            path.unlink()
        except OSError:
            pass  # An active process can retain its open snapshot.


def _decode(cursor):
    try:
        if len(cursor) > 2048:
            raise ValueError()
        payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if (not isinstance(payload, dict) or set(payload) != {"v", "r", "q", "a"}
                or payload["v"] != CACHE_VERSION or type(payload["a"]) is not int
                or payload["a"] < 0):
            raise ValueError()
        return payload
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise CadError("INVALID_CURSOR", "Cursor is malformed or unsupported.") from exc


def _encode(revision, view, ordinal):
    raw = _json({"v": CACHE_VERSION, "r": revision, "q": view, "a": ordinal}).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS entities (
            layout TEXT NOT NULL, ordinal INTEGER NOT NULL, handle TEXT NOT NULL,
            type TEXT NOT NULL, layer TEXT NOT NULL, text TEXT,
            PRIMARY KEY(layout,ordinal)
        );
        CREATE INDEX IF NOT EXISTS by_type ON entities(layout,type,ordinal);
        CREATE INDEX IF NOT EXISTS by_layer ON entities(layout,layer,ordinal);
        CREATE INDEX IF NOT EXISTS by_layer_type ON entities(layout,layer,type,ordinal);
        CREATE INDEX IF NOT EXISTS by_handle ON entities(layout,handle);
        CREATE TABLE IF NOT EXISTS views (
            key TEXT PRIMARY KEY, total INTEGER NOT NULL, unbounded INTEGER NOT NULL,
            touched REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS records (
            view TEXT NOT NULL, ordinal INTEGER NOT NULL, record TEXT NOT NULL,
            PRIMARY KEY(view,ordinal)
        );
    """)


def _ensure_index(db, source, revision):
    if db.execute("SELECT value FROM meta WHERE key='revision'").fetchone():
        return None
    from . import engine
    from .io import file_sha256
    doc = engine.read_document(source)
    layouts = list(doc.layouts.names())
    for name in layouts:
        batch = []
        for ordinal, entity in enumerate(doc.layouts.get(name), 1):
            kind = entity.dxftype()
            text = entity.plain_text() if kind in {"TEXT", "MTEXT"} else None
            batch.append((name, ordinal, entity.dxf.handle, kind, entity.dxf.get("layer", "0"), text))
            if len(batch) >= 512:
                db.executemany("INSERT INTO entities VALUES (?,?,?,?,?,?)", batch)
                batch.clear()
        if batch:
            db.executemany("INSERT INTO entities VALUES (?,?,?,?,?,?)", batch)
    if _source(source)[1] != revision:
        raise CadError("SOURCE_CHANGED", "Source changed while its query index was being built.")
    metadata = {"revision": revision, "units": _json(engine._units(doc)), "layouts": _json(layouts),
                "source_sha256": file_sha256(source)}
    db.executemany("INSERT INTO meta VALUES (?,?)", metadata.items())
    return doc


def _selection(layout, types, layer, text, handles):
    terms, values = ["layout=?"], [layout]
    if types is not None:
        terms.append("type IN (" + ",".join("?" for _ in types) + ")" if types else "0")
        values.extend(types)
    if layer is not None:
        terms.append("layer=?")
        values.append(layer)
    if text is not None:
        terms.append("text IS NOT NULL AND instr(text,?)>0")
        values.append(text)
    if handles is not None:
        terms.append("handle IN (" + ",".join("?" for _ in handles) + ")" if handles else "0")
        values.extend(handles)
    return " AND ".join(terms), values


def _prepare_view(db, source, revision, key, doc, options):
    from . import engine

    layouts = json.loads(db.execute("SELECT value FROM meta WHERE key='layouts'").fetchone()[0])
    if options["layout"] not in layouts:
        raise CadError("INVALID_ARGUMENT", "Layout does not exist.", {"available": layouts})
    where, args = _selection(**{k: options[k] for k in ("layout", "types", "layer", "text", "handles")})
    candidates = db.execute("SELECT ordinal,handle FROM entities WHERE " + where + " ORDER BY ordinal", args)
    total = unbounded = 0
    for ordinal, handle in candidates:
        if doc is None:
            doc = engine.read_document(source)
        entity = doc.entitydb.get(handle)
        if entity is None:
            raise CadError("SOURCE_CHANGED", "Indexed handle disappeared from source.")
        bbox = options["bbox"]
        if bbox is not None:
            extent = None if entity.dxftype() == "INSERT" else engine._bounds(entity)
            if extent is None:
                unbounded += 1
                continue
            if (extent["max"][0] < bbox[0] or extent["min"][0] > bbox[2]
                    or extent["max"][1] < bbox[1] or extent["min"][1] > bbox[3]):
                continue
        record = engine._record(entity, fields=options["fields"], detail=options["detail"],
                                vertex_offset=options["vertex_offset"], vertex_limit=options["vertex_limit"])
        db.execute("INSERT INTO records VALUES (?,?,?)", (key, ordinal, _json(record)))
        total += 1
    if _source(source)[1] != revision:
        raise CadError("SOURCE_CHANGED", "Source changed while query results were being prepared.")
    db.execute("INSERT INTO views VALUES (?,?,?,?)", (key, total, unbounded, time.time()))
    obsolete = db.execute("SELECT key FROM views WHERE key<>? ORDER BY touched DESC LIMIT -1 OFFSET ?",
                          (key, MAX_VIEWS - 1)).fetchall()
    for (old,) in obsolete:
        db.execute("DELETE FROM records WHERE view=?", (old,))
        db.execute("DELETE FROM views WHERE key=?", (old,))


def _fit_record(record, budget):
    """Only shrink the first oversized row; never silently advance past a row."""
    result = json.loads(_json(record))
    while len(_json(result).encode()) > budget:
        arrays = [key for key in ("points", "vertices", "bulges") if isinstance(result.get(key), list)
                  and len(result[key]) > 1]
        if arrays:
            count = max(1, min(len(result[key]) for key in arrays) // 2)
            for key in arrays:
                original = len(result[key])
                result[key] = result[key][:count]
                result.setdefault("truncation", {})[key] = {"complete": False, "reason": "byte_budget",
                                                            "returned": count, "available_in_page": original}
            if "geometry_page" in result:
                page = result["geometry_page"]
                page.update(returned=count, has_more=page["offset"] + count < page["total"],
                            next_offset=page["offset"] + count)
            continue
        strings = [key for key, value in result.items() if isinstance(value, str) and len(value) > 64]
        if strings:
            key = max(strings, key=lambda k: len(result[k]))
            result[key] = result[key][:max(64, len(result[key]) // 2)]
            result.setdefault("truncation", {})[key] = {"complete": False, "reason": "byte_budget"}
            continue
        raise CadError("OUTPUT_BUDGET_TOO_SMALL", "A single projected entity cannot fit the output budget.",
                       {"max_entity_bytes": budget, "hint": "Use fields projection or a larger max_output_bytes."})
    return result


def query_snapshot(*, path, offset, limit, cursor, max_output_bytes, **options):
    source, revision = _source(path)
    view = hashlib.sha256(_json({"version": CACHE_VERSION, **options}).encode()).hexdigest()
    after = None
    if cursor is not None:
        decoded = _decode(cursor)
        if decoded["r"] != revision:
            raise CadError("STALE_CURSOR", "Source revision changed; restart query without cursor.")
        if decoded["q"] != view:
            raise CadError("CURSOR_MISMATCH", "Cursor belongs to different filters or fields.")
        after = decoded["a"]
    root = _cache_root()
    cache = root / (f"query-v{CACHE_VERSION}-" + revision.split(":")[1] + ".sqlite3")
    if cursor is not None and not cache.exists():
        raise CadError("CURSOR_EXPIRED", "Query snapshot was evicted; restart without cursor.")
    _prune(root, cache)
    db = sqlite3.connect(cache, timeout=30)
    try:
        db.execute("PRAGMA max_page_count=" + str(MAX_SNAPSHOT_BYTES // 4096))
        _schema(db)
        db.execute("BEGIN IMMEDIATE")
        doc = _ensure_index(db, source, revision)
        header = db.execute("SELECT total,unbounded FROM views WHERE key=?", (view,)).fetchone()
        cold = header is None
        if cold:
            if cursor is not None:
                raise CadError("CURSOR_EXPIRED", "Query projection expired; restart without cursor.")
            _prepare_view(db, source, revision, view, doc, options)
            header = db.execute("SELECT total,unbounded FROM views WHERE key=?", (view,)).fetchone()
        del doc
        db.execute("UPDATE views SET touched=? WHERE key=?", (time.time(), view))
        db.commit()
        total, unbounded = header
        where = "view=?" + (" AND ordinal>?" if after is not None else "")
        values = [view] + ([after] if after is not None else [])
        rows = db.execute("SELECT ordinal,record FROM records WHERE " + where + " ORDER BY ordinal LIMIT ? OFFSET ?",
                          [*values, limit + 1, 0 if after is not None else offset])
        entities, last, remaining = [], after or 0, False
        used = 0
        # Reserve room for protocol envelope, revision/cursor, warnings, and metadata.
        budget = max_output_bytes - 2048
        for ordinal, raw in rows:
            if len(entities) >= limit:
                remaining = True
                break
            size = len(raw.encode()) + 1
            if entities and used + size > budget:
                remaining = True
                break
            record = json.loads(raw)
            if not entities and size > budget:
                record = _fit_record(record, budget - 1)
                size = len(_json(record).encode()) + 1
            entities.append(record)
            used += size
            last = ordinal
        warnings = []
        if options["bbox"] is not None or options["detail"] == "full" and options["fields"] is None:
            warnings.append("Bounds are approximate; query does not expand INSERT entities.")
        if unbounded:
            warnings.append(f"Bounding-box filtering excluded {unbounded} entities without extents.")
        units = json.loads(db.execute("SELECT value FROM meta WHERE key='units'").fetchone()[0])
        result = {"entities": entities, "total": total, "returned": len(entities), "offset": offset,
                  "limit": limit, "layout": options["layout"], "units": units, "warnings": warnings,
                  "source_revision": revision, "has_more": remaining,
                  "source_sha256": db.execute("SELECT value FROM meta WHERE key='source_sha256'").fetchone()[0],
                  "next_cursor": _encode(revision, view, last) if remaining else None,
                  "cache": {"hit": not cold, "storage": "sqlite", "native_entities": True}}
        if _source(source)[1] != revision:
            raise CadError("SOURCE_CHANGED", "Source changed while its query page was being read.")
        if any(e.get("truncation") or e.get("geometry_page", {}).get("has_more") for e in entities):
            result["warnings"].append("Entity detail is incomplete; inspect truncation/geometry_page and request the next vertex page by handle.")
        # This is also checked at the response boundary; no page cursor advances for omitted rows.
        if len(_json(result).encode()) > max_output_bytes - 512:
            raise CadError("OUTPUT_BUDGET_TOO_SMALL", "Query metadata exceeds the available output budget.")
        return result
    except sqlite3.Error as exc:
        db.rollback()
        raise CadError("QUERY_CACHE_ERROR", "Cannot prepare bounded query cache.",
                       {"reason": str(exc)[:200], "max_snapshot_bytes": MAX_SNAPSHOT_BYTES,
                        "hint": "Narrow fields/filters or choose a writable TCAD_CACHE_DIR."}) from exc
    finally:
        db.close()
        if cache.exists():
            os.utime(cache, None)
        _prune(root, cache)
