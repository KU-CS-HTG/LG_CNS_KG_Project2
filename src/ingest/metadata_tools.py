"""Selected source→index tracing and explicit correction; no implicit ingestion or model load."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import re
import subprocess

from src.common import config
from src.common.regions import normalize_region
from src.ingest import handoff, store
from src.ingest.chunker import chunk_service
from src.ingest.coverage import readonly
from src.ingest.normalize import refresh_service
from src.index import vector_store as index
from src.retrieve.filters import agency_region


def persistent_documents(index_dir, model):
    """Inspected Chroma SQLite schema only. Never constructs a client/collection/HNSW."""
    name = "chunks__" + re.sub(r"[^A-Za-z0-9._-]+", "_", model).strip("_.-")[:50]
    path = Path(index_dir) / "chroma.sqlite3"
    result = {"path": str(path.resolve()), "collection": name, "state": "index_database_missing", "documents": {}}
    if not path.is_file():
        return result
    with readonly(path) as con:
        required = {"collections": {"id", "name", "dimension", "config_json_str"}, "segments": {"id", "scope", "collection"},
                    "embeddings": {"id", "embedding_id", "segment_id"},
                    "embedding_metadata": {"id", "key", "string_value", "int_value", "float_value", "bool_value"}}
        for table, fields in required.items():
            if not fields <= {r["name"] for r in con.execute("PRAGMA table_info(" + table + ")")}:
                raise ValueError("unsupported_chroma_metadata_schema")
        collections = con.execute("SELECT * FROM collections").fetchall()
        result["available_collections"] = [r["name"] for r in collections]
        collection = next((r for r in collections if r["name"] == name), None)
        if collection is None:
            result["state"] = "selected_collection_missing"
            return result
        result.update(state="persistent_documents_only", dimension=collection["dimension"],
                      configuration=json.loads(collection["config_json_str"] or "{}"))
        for row in con.execute("""SELECT e.embedding_id,m.key,m.string_value,m.int_value,m.float_value,m.bool_value
            FROM embeddings e JOIN segments s ON e.segment_id=s.id LEFT JOIN embedding_metadata m ON m.id=e.id
            WHERE s.collection=? AND s.scope='METADATA' ORDER BY e.embedding_id,m.key""", (collection["id"],)):
            entry = result["documents"].setdefault(row[0], {"text": None, "metadata": {}})
            value = next((row[k] for k in range(2, 6) if row[k] is not None), None)
            if row[1] == "chroma:document":
                entry["text"] = value
            elif row[1] is not None:
                entry["metadata"][row[1]] = value
    return result


def _records(db_path, service_ids):
    if not Path(db_path).is_file():
        return {}, {}, "source_database_missing"
    records, pending = {}, {}
    with readonly(db_path) as con:
        for sid in service_ids:
            row = con.execute("SELECT * FROM services WHERE service_id=?", (sid,)).fetchone()
            if row:
                records[sid] = {**dict(row), "payload": json.loads(row["payload"])}
            row = con.execute("SELECT * FROM pending_changes WHERE service_id=?", (sid,)).fetchone()
            if row:
                pending[sid] = {**dict(row), "chunks": json.loads(row["chunks"])}
    return records, pending, "observed_readonly"


def _raw(service_ids, raw_run=None, raw_list=None):
    result = {}
    if raw_run:
        root = Path(raw_run)
        manifest = root / "run-summary.json"
        if not manifest.is_file():
            raise ValueError("selected_raw_run_summary_missing")
        summary = json.loads(manifest.read_text(encoding="utf-8"))
        if summary["run_id"] != root.name:
            raise ValueError("selected_raw_run_identity_mismatch")
        files = [(p, p.name.split("-")[0], summary["run_id"]) for endpoint in ("serviceList", "serviceDetail", "supportConditions")
                 for p in sorted(root.glob(endpoint + "-[0-9]*.json"))]
    elif raw_list:
        files = [(Path(raw_list), "standalone_serviceList_not_replay_contract", None)]
    else:
        return result
    for path, endpoint, run_id in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data if isinstance(data, list) else data.get("data")
        if not isinstance(rows, list):
            raise ValueError("selected_raw_data_shape_unsupported")
        for row in rows:
            sid = row.get("서비스ID")
            if sid in service_ids:
                result.setdefault(sid, []).append({"endpoint": endpoint, "run_id": run_id,
                    "agency": row.get("소관기관명"), "source_path": str(path.resolve()),
                    "residence_text": {k: row.get(k) for k in ("지원대상", "선정기준") if k in row}})
    return result


def trace(settings, service_ids, *, raw_run=None, raw_list=None, today=None):
    if not service_ids or len(service_ids) > 50:
        raise ValueError("trace_requires_1_to_50_ids")
    observed = persistent_documents(settings.index_dir, settings.model)
    records, pending, source_state = _records(settings.db_path, service_ids)
    raw = _raw(set(service_ids), raw_run, raw_list)
    rows = []
    from src.ingest.pipeline import seoul_today
    for sid in service_ids:
        record = records.get(sid)
        normalized = refresh_service(record["payload"]) if record and record["status"] == "active" else None
        current = chunk_service(normalized) if normalized else []
        status, reason = store._status(normalized, today or seoul_today()) if normalized else ("skip", record["reason"] if record else "source_missing")
        expected = {c.chunk_id: index._to_store_metadata(c) for c in current}
        actual = {cid: d for cid, d in observed["documents"].items() if d["metadata"].get("service_id") == sid}
        classifications = set()
        for cid, document in actual.items():
            md = document["metadata"]
            want = expected.get(cid)
            if not md.get("region"):
                if want and want["region"] == "전국":
                    classification = "explicit_nationwide_represented_incorrectly"
                elif want and want.get("region_provenance") == "explicit_eligibility":
                    classification = "recoverable_explicit_eligibility"
                elif agency_region((want or md).get("agency")):
                    classification = "stale_blank_recoverable_agency_inference"
                elif not md.get("agency"):
                    classification = "missing_agency_or_source"
                else:
                    classification = "genuinely_unknown_from_available_evidence"
            elif want and md != want:
                classification = "stale_or_inconsistent_metadata"
            else:
                classification = "unresolved_source_history" if not record else "consistent_current_metadata"
            classifications.add(classification)
        rows.append({"service_id": sid, "raw": raw.get(sid, []), "record": record,
                     "correction_plan": {"operation": "replace" if status == "active" else "remove" if status == "inactive" else "skip", "reason": reason},
                     "current_chunks": [c.to_dict() for c in current], "pending": pending.get(sid),
                     "expected": expected, "actual": actual,
                     "decoded": {cid: index._chunk_from_store(cid, d["text"] or "", d["metadata"]).to_dict() for cid, d in actual.items()},
                     "classifications": sorted(classifications) or ["selected_service_not_in_selected_collection"]})
    docs = observed["documents"]
    blanks = [d for d in docs.values() if not d["metadata"].get("region")]
    manifests = {}
    for path in handoff._bindings(settings):
        if path.is_file():
            value = json.loads(path.read_text(encoding="utf-8"))
            manifests[str(path)] = {k: value[k] for k in ("format", "source_db", "index_dir", "model", "dimension",
                "max_seq_length", "space", "embedding_policy", "index_build_revision") if k in value}
        else:
            manifests[str(path)] = "missing"
    try:
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=config.ROOT, text=True).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=config.ROOT))
    except subprocess.CalledProcessError:
        revision = "unavailable"
        dirty = None
    return {"settings": asdict(settings), "source_state": source_state,
            "index": {k: v for k, v in observed.items() if k != "documents"}, "manifests": manifests,
            "runtime_entrypoint": "src.ingest.metadata_tools trace", "current_revision": revision, "current_worktree_dirty": dirty,
            "index_build_revision": next((v["index_build_revision"] for v in manifests.values()
                if isinstance(v, dict) and v.get("index_build_revision")), "not_recorded"),
            "counts": {"chunks": len(docs), "services": len({d["metadata"].get("service_id") for d in docs.values()}),
                       "blank_region_chunks": len(blanks), "services_with_blank_chunks": len({d["metadata"].get("service_id") for d in blanks}),
                       "agency_recoverable_blank_chunks": sum(bool(agency_region(d["metadata"].get("agency"))) for d in blanks)}, "rows": rows}


def _show(result):
    for key in ("settings", "source_state", "index", "manifests", "runtime_entrypoint", "current_revision", "current_worktree_dirty", "index_build_revision", "counts"):
        print(key + ": " + json.dumps(result[key], ensure_ascii=False))
    print("service_id | stage/chunk_id | agency | raw region → canonical | provenance | run/version/pending")
    for item in result["rows"]:
        sid = item["service_id"]
        def line(stage, agency, raw, provenance, token=""):
            normalized = {normalize_region(v) for v in raw} if isinstance(raw, list) else {normalize_region(raw)}
            canonical = next(iter(normalized)) if len(normalized) == 1 else None
            print(f"{sid} | {stage} | {agency or '-'} | {raw or '-'} → {canonical or '-'} | {provenance or '-'} | {token}")
        for raw in item["raw"]:
            line("raw:" + raw["endpoint"], raw.get("agency"), None, "source_text_only", raw.get("run_id"))
            for field, phrase in raw["residence_text"].items():
                snippet = " ".join(str(phrase or "").split())[:120]
                print(f"  {field}: {snippet}")
        record = item["record"]
        if record:
            payload, eligibility = record["payload"], record["payload"].get("eligibility", {})
            line("SQLite:" + record["status"], payload.get("agency"), eligibility.get("region_raw") or eligibility.get("region"), eligibility.get("region_provenance"),
                 f"{record['last_run']}/v{record['version']}")
            print("  region audit: " + json.dumps(eligibility.get("residence_evidence", []), ensure_ascii=False))
        else:
            print(sid + " | SQLite | selected source record unavailable")
        for chunk in item["current_chunks"]:
            md = chunk["metadata"]
            line("Chunk:" + chunk["chunk_id"], md.get("agency"), md.get("region"), md.get("region_provenance"))
            expected = item["expected"][chunk["chunk_id"]]
            line("expected B:" + chunk["chunk_id"], expected.get("agency"), expected.get("region"), expected.get("region_provenance"))
        event = item["pending"]
        if event:
            for chunk in event["chunks"]:
                md = chunk["metadata"]
                line("pending:" + chunk["chunk_id"], md.get("agency"), md.get("region"), md.get("region_provenance"),
                     f"v{event['version']}/{event['event_id']}")
        for cid, actual in item["actual"].items():
            md = actual["metadata"]
            if cid not in item["expected"]:
                line("expected B (index input only):" + cid, md.get("agency"), agency_region(md.get("agency")), "agency_inference_or_unknown; source unavailable")
            line("Chroma:" + cid, md.get("agency"), md.get("region"), md.get("region_provenance") or ("legacy_unknown" if md.get("region") else "unknown"))
            decoded = item["decoded"][cid]["metadata"]
            line("decoded:" + cid, decoded.get("agency"), decoded.get("region"), decoded.get("region_provenance"))
        print("  classification: " + ", ".join(item["classifications"]))
        print("  correction plan: " + str(item["correction_plan"]))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Selected metadata trace/correction; default is inspection only")
    parser.add_argument("command", choices=("trace", "correct"))
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--index-dir", type=Path, required=True)
    parser.add_argument("--model", default=config.EMBEDDING_MODEL)
    parser.add_argument("--service-ids", nargs="+", required=True)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--raw-run", type=Path)
    source.add_argument("--raw-list", type=Path, help="Diagnostic listing only; not an A replay source")
    parser.add_argument("--validation", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--verify-every", type=int, default=1)
    parser.add_argument("--today")
    args = parser.parse_args(argv)
    try:
        settings = handoff.Settings.resolve(db_path=args.db_path, index_dir=args.index_dir, model=args.model,
            validation=args.validation, service_ids=args.service_ids, verify_every=args.verify_every)
        if args.command == "trace" and args.apply:
            raise ValueError("trace_never_applies")
        if args.command == "correct" and args.apply:
            handoff._check_binding(settings)
            if not Path(settings.db_path).is_file():
                raise ValueError("source_database_missing_no_correction")
            from src.ingest.pipeline import seoul_today
            queued = store.reconcile_services(args.service_ids, storage=settings.storage(), today=args.today or seoul_today())
            print("producer: " + json.dumps(queued, ensure_ascii=False))
            result = handoff.consume(settings, apply=True)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1 if result["failed"] else 0
        result = trace(settings, args.service_ids, raw_run=args.raw_run, raw_list=args.raw_list, today=args.today)
        result["runtime_entrypoint"] = "src.ingest.metadata_tools " + args.command + " (inspection)"
        _show(result)
        return 0
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__, "code": handoff._error_code(exc),
                          "selected_db": str(args.db_path.resolve()), "selected_index": str(args.index_dir.resolve()),
                          "selected_raw": str(args.raw_run or args.raw_list) if args.raw_run or args.raw_list else None}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
