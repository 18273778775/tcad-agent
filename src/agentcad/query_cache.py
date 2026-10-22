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

CACHE_VERSION = 7
MAX_CACHE_BYTES = 512 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 128 * 1024 * 1024
MAX_CACHE_FILES = 16
MAX_VIEWS = 32
MAX_CACHE_AGE_SECONDS = 30 * 24 * 3600
MAX_ANNOTATION_VISITS = 1000000


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
        recent = time.time() - path.stat().st_mtime <= MAX_CACHE_AGE_SECONDS
        if path == keep or (recent and retained < MAX_CACHE_FILES and total + size <= MAX_CACHE_BYTES):
            total += size
            retained += 1
            continue
        try:
            path.unlink()
        except OSError:
            pass  # An active process can retain its open snapshot.


def _expire(cache, cursor=None):
    if not cache.exists() or time.time() - cache.stat().st_mtime <= MAX_CACHE_AGE_SECONDS:
        return
    if cursor is not None:
        raise CadError("CURSOR_EXPIRED", "Query snapshot expired; restart without cursor.")
    try:
        cache.unlink()
    except OSError:
        # A concurrent reader can retain the immutable, still-current source
        # snapshot. Other inactive cache files remain subject to count/size caps.
        pass


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
            id INTEGER PRIMARY KEY,
            layout TEXT NOT NULL, ordinal INTEGER NOT NULL, handle TEXT NOT NULL,
            type TEXT NOT NULL, layer TEXT NOT NULL, text TEXT, bounds TEXT,
            base_record TEXT NOT NULL, UNIQUE(layout,ordinal)
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS spatial_bounds USING rtree(id,minx,maxx,miny,maxy);
        CREATE TABLE IF NOT EXISTS annotations (
            id INTEGER PRIMARY KEY, layout TEXT NOT NULL, ordinal INTEGER NOT NULL,
            handle TEXT NOT NULL, type TEXT NOT NULL, layer TEXT NOT NULL,
            text TEXT, root_handle TEXT NOT NULL, entity_key TEXT NOT NULL, record TEXT NOT NULL,
            UNIQUE(layout,entity_key)
        );
        CREATE INDEX IF NOT EXISTS annotation_layout ON annotations(layout,ordinal);
        CREATE INDEX IF NOT EXISTS annotation_layer ON annotations(layout,layer,ordinal);
        CREATE VIRTUAL TABLE IF NOT EXISTS annotation_bounds USING rtree(id,minx,maxx,miny,maxy);
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
    from .io import file_sha256, jsonable
    from .spatial import entity_visible
    doc = engine.read_document(source)
    layout_names = list(doc.layouts.names())
    layouts = [jsonable(name) for name in layout_names]
    for native_name, name in zip(layout_names, layouts):
        native = list(doc.layouts.get(native_name))
        batch = []
        for ordinal, entity in enumerate(native, 1):
            kind = entity.dxftype()
            text = jsonable(entity.plain_text()) if kind in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"} else None
            base = jsonable(engine._base_record(entity))
            extent = None
            # Bounds always use the same conservative WCS selection contract.
            base["bounds"] = ({"min": extent[:2], "max": extent[2:]} if extent else None)
            base["bounds_kind"] = "conservative_wcs_2d" if extent else "unavailable"
            base.update(entity_key=entity.dxf.handle, instance_path=[], root_handle=entity.dxf.handle,
                        scope="layout", world_bbox=extent, visible=entity_visible(entity))
            batch.append((name, ordinal, entity.dxf.handle, kind, base["layer"], text,
                          _json(extent) if extent else None, _json(base)))
            if len(batch) >= 512:
                db.executemany("INSERT INTO entities(layout,ordinal,handle,type,layer,text,bounds,base_record) VALUES (?,?,?,?,?,?,?,?)", batch)
                batch.clear()
        if batch:
            db.executemany("INSERT INTO entities(layout,ordinal,handle,type,layer,text,bounds,base_record) VALUES (?,?,?,?,?,?,?,?)", batch)
        db.execute("INSERT INTO meta VALUES (?,?)", ("bounds:" + name, _json({
            "native_count": len(native), "built": False})))
    if _source(source)[1] != revision:
        raise CadError("SOURCE_CHANGED", "Source changed while its query index was being built.")
    metadata = {"revision": revision, "units": _json(engine._units(doc)), "layouts": _json(layouts),
                "source_sha256": file_sha256(source)}
    db.executemany("INSERT INTO meta VALUES (?,?)", metadata.items())
    return doc


def _ensure_spatial(db, source, revision, doc=None):
    """Bounds are lazy so handle/type projections never traverse all geometry."""
    if db.execute("SELECT value FROM meta WHERE key='spatial_ready'").fetchone():
        return doc
    from . import engine
    from .spatial import entity_bounds
    if doc is None:
        doc = engine.read_document(source)
    for name in doc.layouts.names():
        from .io import jsonable
        safe_name = jsonable(name)
        native = list(doc.layouts.get(name))
        bounded, missing, statistics = entity_bounds(native)
        by_handle = {entity.dxf.handle: extent for entity, extent in bounded}
        for row_id, handle, raw in db.execute("SELECT id,handle,base_record FROM entities WHERE layout=?", (safe_name,)).fetchall():
            extent = by_handle.get(handle)
            base = json.loads(raw)
            base.update(bounds={"min": extent[:2], "max": extent[2:]} if extent else None,
                        world_bbox=extent, bounds_kind="conservative_wcs_2d" if extent else "unavailable")
            db.execute("UPDATE entities SET bounds=?,base_record=? WHERE id=?",
                       (_json(extent) if extent else None, _json(base), row_id))
            if extent:
                db.execute("INSERT INTO spatial_bounds VALUES (?,?,?,?,?)", (row_id, extent[0], extent[2], extent[1], extent[3]))
        db.execute("UPDATE meta SET value=? WHERE key=?", (_json({
            "native_count": len(native), "built": True, "bounded_count": len(bounded),
            "unbounded_count": len(missing), "partial_bounds_count": statistics["partial_bounds_count"],
            "method": statistics["method"]}), "bounds:" + safe_name))
    if _source(source)[1] != revision:
        raise CadError("SOURCE_CHANGED", "Source changed while spatial bounds were prepared.")
    db.execute("INSERT INTO meta VALUES ('spatial_ready','1')")
    return doc


def _selection(layout, types=None, layer=None, text=None, handles=None, layers=None,
               exclude_layers=None, bbox=None, annotation=False):
    terms, values = ["layout=?"], [layout]
    if types is not None:
        terms.append("type IN (" + ",".join("?" for _ in types) + ")" if types else "0")
        values.extend(types)
    if layer is not None:
        terms.append("layer=?")
        values.append(layer)
    for selected, excluded in ((layers, False), (exclude_layers, True)):
        if selected is not None:
            terms.append("layer " + ("NOT IN (" if excluded else "IN (") + ",".join("?" for _ in selected) + ")"
                         if selected else "1" if excluded else "0")
            values.extend(selected)
    if text is not None:
        terms.append("text IS NOT NULL AND instr(text,?)>0")
        values.append(text)
    if handles is not None:
        terms.append("handle IN (" + ",".join("?" for _ in handles) + ")" if handles else "0")
        values.extend(handles)
    if bbox is not None:
        table = "annotation_bounds" if annotation else "spatial_bounds"
        terms.append(f"id IN (SELECT id FROM {table} WHERE minx<=? AND maxx>=? AND miny<=? AND maxy>=?)")
        values.extend([bbox[2], bbox[0], bbox[3], bbox[1]])
        raw = "json_extract(record,'$.world_bbox')" if annotation else "bounds"
        terms.append(f"json_extract({raw},'$[0]')<=? AND json_extract({raw},'$[2]')>=? "
                     f"AND json_extract({raw},'$[1]')<=? AND json_extract({raw},'$[3]')>=?")
        values.extend([bbox[2], bbox[0], bbox[3], bbox[1]])
    return " AND ".join(terms), values


def _ensure_annotations(db, source, revision, doc=None):
    if db.execute("SELECT value FROM meta WHERE key='annotations_ready'").fetchone():
        return doc
    from . import engine
    from .io import jsonable
    from .spatial import iter_world_entities
    if doc is None:
        doc = engine.read_document(source)
    diagnostics = {}
    for name in doc.layouts.names():
        roots = [entity for entity in doc.layouts.get(name) if entity.dxftype() == "INSERT"]
        rows = iter_world_entities(doc, layout=name, roots=roots, diagnostics=diagnostics,
                                  types={"TEXT", "MTEXT", "ATTRIB", "ATTDEF"},
                                  max_entities=MAX_ANNOTATION_VISITS)
        for ordinal, item in enumerate(rows, 1):
            entity = item["entity"]
            record = engine._record(entity, fields=["type", "layer", "color", "text", "plain_text",
                                                     "height", "char_height", "rotation", "tag"], vertex_limit=128)
            record.update({key: item[key] for key in ("handle", "instance_path", "root_handle", "entity_key", "bounds")})
            record["world_bbox"] = item["bounds"]
            record["bounds"] = ({"min": item["bounds"][:2], "max": item["bounds"][2:]} if item["bounds"] else None)
            record["coordinate_system"] = "WCS"
            record["insert"] = item["world_insert"]
            record["bounds_kind"] = "conservative_wcs_2d" if item["bounds"] else "unavailable"
            record["scope"] = "block"
            record["visible"] = item["visible"]
            record = jsonable(record)
            row = db.execute("INSERT INTO annotations(layout,ordinal,handle,type,layer,text,root_handle,entity_key,record) "
                             "VALUES (?,?,?,?,?,?,?,?,?)", (jsonable(name), ordinal, item["handle"], entity.dxftype(),
                             jsonable(item["layer"]), jsonable(entity.plain_text()), item["root_handle"], item["entity_key"], _json(record)))
            if item["bounds"]:
                a, b, c, d = item["bounds"]
                db.execute("INSERT INTO annotation_bounds VALUES (?,?,?,?,?)", (row.lastrowid, a, c, b, d))
    if _source(source)[1] != revision:
        raise CadError("SOURCE_CHANGED", "Source changed while block annotations were indexed.")
    db.execute("INSERT INTO meta VALUES ('annotations_ready',?)", (_json(diagnostics),))
    return doc


def _candidates(db, options):
    scope = options.get("text_scope", "layout")
    selection = {key: options.get(key) for key in
                 ("layout", "types", "layer", "text", "handles", "layers", "exclude_layers", "bbox")}
    if scope != "block":
        where, args = _selection(**selection)
        for ordinal, handle, base in db.execute("SELECT ordinal,handle,base_record FROM entities WHERE " + where + " ORDER BY ordinal", args):
            yield ordinal, handle, json.loads(base), False
    if scope != "layout":
        where, args = _selection(**selection, annotation=True)
        top = db.execute("SELECT coalesce(max(ordinal),0) FROM entities WHERE layout=?", (options["layout"],)).fetchone()[0]
        for ordinal, handle, raw in db.execute("SELECT ordinal,handle,record FROM annotations WHERE " + where + " ORDER BY ordinal", args):
            yield top + ordinal, handle, json.loads(raw), True


def _prepare_view(db, source, revision, key, doc, options):
    from . import engine

    layouts = json.loads(db.execute("SELECT value FROM meta WHERE key='layouts'").fetchone()[0])
    if options["layout"] not in layouts:
        raise CadError("INVALID_ARGUMENT", "Layout does not exist.", {"available": layouts})
    if options.get("text_scope", "layout") != "layout":
        doc = _ensure_annotations(db, source, revision, doc)
    candidates = _candidates(db, options)
    total = unbounded = 0
    if options.get("bbox") is not None:
        where, args = _selection(**{key: options.get(key) for key in
                                   ("layout", "types", "layer", "text", "handles", "layers", "exclude_layers")})
        if options.get("text_scope", "layout") != "block":
            unbounded = db.execute("SELECT count(*) FROM entities WHERE " + where + " AND bounds IS NULL", args).fetchone()[0]
    for ordinal, handle, base, annotation in candidates:
        fields = options["fields"]
        if annotation:
            record = base if fields is None else {key: value for key, value in base.items() if key in fields}
            # An instance is not a native layout handle. Keep this identity even
            # when the projection otherwise requests only the source handle.
            record.update({key: base[key] for key in ("entity_key", "instance_path", "root_handle", "scope")})
        elif fields is not None and set(fields) <= set(base):
            record = {key: base[key] for key in fields}
        else:
            if doc is None:
                doc = engine.read_document(source)
            entity = doc.entitydb.get(handle)
            if entity is None:
                raise CadError("SOURCE_CHANGED", "Indexed handle disappeared from source.")
            record = engine._record(entity, fields=fields, detail=options["detail"],
                                    vertex_offset=options["vertex_offset"], vertex_limit=options["vertex_limit"])
            if fields is None or "bounds" in fields or "bounds_kind" in fields:
                for field_name in ("bounds", "bounds_kind"):
                    if fields is None or field_name in fields:
                        record[field_name] = base[field_name]
            for field_name in ("entity_key", "instance_path", "root_handle", "scope", "world_bbox", "visible"):
                if fields is not None and field_name in fields:
                    record[field_name] = base[field_name]
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
        if isinstance(result.get("block_entities"), list) and result["block_entities"]:
            original = len(result["block_entities"])
            count = original // 2
            result["block_entities"] = result["block_entities"][:count]
            result["block_complete"] = False
            result.setdefault("truncation", {})["block_entities"] = {
                "complete": False, "reason": "byte_budget", "returned": count,
                "total": result.get("block_entity_count", original),
                "hint": "Block child pagination is unavailable; increase the output budget or use bounded export. Nested INSERTs are not expanded.",
            }
            continue
        if isinstance(result.get("attributes"), list) and result["attributes"]:
            original = len(result["attributes"])
            count = original // 2
            result["attributes"] = result["attributes"][:count]
            result["attribute_complete"] = False
            result.setdefault("truncation", {})["attributes"] = {
                "complete": False, "reason": "byte_budget", "returned": count,
                "total": result.get("attribute_count", original),
                "hint": "Attribute index pagination is unavailable; increase the output budget or narrow fields. Export retains the annotation caps.",
            }
            continue
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
    expected = options.pop("expected_source_revision", None)
    if expected is not None and expected != revision:
        raise CadError("STALE_SOURCE", "Source revision changed; restart the spatial request.")
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
    _expire(cache, cursor)
    if cursor is not None and not cache.exists():
        raise CadError("CURSOR_EXPIRED", "Query snapshot was evicted; restart without cursor.")
    _prune(root, cache)
    db = sqlite3.connect(cache, timeout=30)
    try:
        db.execute("PRAGMA max_page_count=" + str(MAX_SNAPSHOT_BYTES // 4096))
        _schema(db)
        db.execute("BEGIN IMMEDIATE")
        doc = _ensure_index(db, source, revision)
        if options["bbox"] is not None or options["fields"] is None or set(options["fields"]) & {"bounds", "bounds_kind", "world_bbox"}:
            doc = _ensure_spatial(db, source, revision, doc)
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
            warnings.append("Selection bounds are conservative WCS 2D extents, including transformed INSERT definitions and attached attributes; text extents depend on fonts.")
        if unbounded:
            warnings.append(f"Bounding-box filtering excluded {unbounded} entities without extents.")
        units = json.loads(db.execute("SELECT value FROM meta WHERE key='units'").fetchone()[0])
        result = {"entities": entities, "total": total, "returned": len(entities), "offset": offset,
                  "limit": limit, "layout": options["layout"], "units": units, "warnings": warnings,
                  "source_revision": revision, "has_more": remaining,
                  "source_sha256": db.execute("SELECT value FROM meta WHERE key='source_sha256'").fetchone()[0],
                  "next_cursor": _encode(revision, view, last) if remaining else None,
                  "cache": {"hit": not cold, "storage": "sqlite-rtree", "native_entities": options.get("text_scope", "layout") == "layout"},
                  "text_scope": options.get("text_scope", "layout")}
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


def _index_request(path, options, expected_source_revision=None, receipt=False):
    """One source snapshot, reusable by spatial commands without CAD parsing."""
    source, revision = _source(path)
    if expected_source_revision is not None and expected_source_revision != revision:
        raise CadError("STALE_SOURCE", "Source revision changed; rebuild the selection.",
                       {"expected_source_revision": expected_source_revision, "source_revision": revision})
    from .spatial import validated_bbox
    if options.get("bbox") is not None:
        options["bbox"] = validated_bbox(options["bbox"])
    if options.get("text_scope", "layout") not in {"layout", "block", "all"}:
        raise CadError("INVALID_ARGUMENT", "text_scope must be layout, block or all.")
    root = _cache_root()
    cache = root / (f"query-v{CACHE_VERSION}-" + revision.split(":")[1] + ".sqlite3")
    _expire(cache)
    _prune(root, cache)
    started = time.perf_counter()
    db = sqlite3.connect(cache, timeout=30)
    try:
        db.execute("PRAGMA max_page_count=" + str(MAX_SNAPSHOT_BYTES // 4096))
        _schema(db)
        db.execute("BEGIN IMMEDIATE")
        hit = (db.execute("SELECT value FROM meta WHERE key='revision'").fetchone() is not None
               and db.execute("SELECT value FROM meta WHERE key='spatial_ready'").fetchone() is not None
               and (options.get("text_scope", "layout") == "layout"
                    or db.execute("SELECT value FROM meta WHERE key='annotations_ready'").fetchone() is not None))
        doc = _ensure_index(db, source, revision)
        doc = _ensure_spatial(db, source, revision, doc)
        layout = options["layout"]
        layouts = json.loads(db.execute("SELECT value FROM meta WHERE key='layouts'").fetchone()[0])
        if layout not in layouts:
            raise CadError("INVALID_ARGUMENT", "Layout does not exist.", {"available": layouts})
        if options.get("text_scope", "layout") != "layout":
            doc = _ensure_annotations(db, source, revision, doc)
        del doc
        bounds = json.loads(db.execute("SELECT value FROM meta WHERE key=?", ("bounds:" + layout,)).fetchone()[0])
        result = {"source_revision": revision,
                  "source_sha256": db.execute("SELECT value FROM meta WHERE key='source_sha256'").fetchone()[0],
                  "layout": layout, "layouts": layouts, "bounds": bounds,
                  "cache": {"hit": hit, "storage": "sqlite-rtree", "version": CACHE_VERSION,
                            "max_files": MAX_CACHE_FILES, "max_total_bytes": MAX_CACHE_BYTES,
                            "max_snapshot_bytes": MAX_SNAPSHOT_BYTES, "max_age_seconds": MAX_CACHE_AGE_SECONDS},
                  "text_scope": options.get("text_scope", "layout")}
        if receipt:
            result["entity_count"] = db.execute("SELECT count(*) FROM entities WHERE layout=?", (layout,)).fetchone()[0]
            result["block_annotation_count"] = db.execute("SELECT count(*) FROM annotations WHERE layout=?", (layout,)).fetchone()[0]
            diagnostics = db.execute("SELECT value FROM meta WHERE key='annotations_ready'").fetchone()
            result["annotation_index"] = json.loads(diagnostics[0]) if diagnostics else {"built": False}
        else:
            rows = list(_candidates(db, options))
            result["handles"] = [handle for _, handle, _, annotation in rows if not annotation]
            result["entities"] = [base for _, _, base, _ in rows]
            result["entity_count"] = len(rows)
            result["unbounded_count"] = bounds["unbounded_count"]
        if _source(source)[1] != revision:
            raise CadError("SOURCE_CHANGED", "Source changed during spatial selection.")
        db.commit()
        result["cache"]["bytes"] = cache.stat().st_size
        result["elapsed_seconds"] = time.perf_counter() - started
        return result
    except sqlite3.Error as exc:
        db.rollback()
        raise CadError("QUERY_CACHE_ERROR", "Cannot prepare bounded spatial cache.",
                       {"reason": str(exc)[:200], "max_snapshot_bytes": MAX_SNAPSHOT_BYTES}) from exc
    finally:
        db.close()
        if cache.exists():
            os.utime(cache, None)
        _prune(root, cache)


def select_indexed(path, **options):
    expected = options.pop("expected_source_revision", None)
    return _index_request(path, options, expected)


def index_source(path, layout="Model", text_scope="all"):
    return _index_request(path, {"layout": layout, "text_scope": text_scope}, receipt=True)
