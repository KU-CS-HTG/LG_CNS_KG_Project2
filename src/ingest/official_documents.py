"""A-1 CLI. Registration, explicit acquisition, as-of inspection and service links only."""
import argparse
import json
from pathlib import Path

from src.ingest.document_catalog import Catalog, CatalogPaths
from src.ingest.document_acquire import acquire, Limits
from src.ingest.coverage import readonly

REGISTRY = Path(__file__).with_name("official_sources.json")


def inspect_services(path, query):
    """Existing read-only source API; no source DB initialization or fuzzy automatic linking."""
    if not Path(path).is_file():
        return {"path": str(Path(path).resolve()), "state": "service_database_missing", "candidates": []}
    with readonly(path) as con:
        rows = []
        for row in con.execute("SELECT service_id,payload,status,reason,version FROM services ORDER BY service_id"):
            value = json.loads(row["payload"])
            if query in (value.get("service_name") or ""):
                rows.append({**{k: value.get(k) for k in ("service_id", "service_name", "agency", "detail_url")},
                             "status": row["status"], "reason": row["reason"], "version": row["version"]})
    return {"path": str(Path(path).resolve()), "state": "readonly_candidates_not_verified_links", "candidates": rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Official documents A-1; no API ingestion, extraction, model or index writes")
    parser.add_argument("--catalog-db", required=True, type=Path)
    parser.add_argument("--original-dir", required=True, type=Path)
    parser.add_argument("--diagnostics-dir", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    register = sub.add_parser("register")
    register.add_argument("--registry", type=Path, default=REGISTRY)
    register.add_argument("--reason", default="explicit registry registration")
    collect = sub.add_parser("collect")
    collect.add_argument("--source-ids", nargs="+", required=True)
    collect.add_argument("--max-mib", type=int, default=64)
    collect.add_argument("--attempts", type=int, default=3)
    local = sub.add_parser("import-local")
    local.add_argument("--source-id", required=True)
    local.add_argument("--file", type=Path, required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--as-of", required=True)
    sub.add_parser("links")
    services = sub.add_parser("services")
    services.add_argument("--service-db", required=True, type=Path)
    services.add_argument("--query", default="주거급여")
    link = sub.add_parser("link")
    link.add_argument("--document-id", required=True)
    link.add_argument("--version-id")
    link.add_argument("--topic", required=True)
    link.add_argument("--evidence", type=Path, required=True)
    link.add_argument("--service-db", type=Path)
    link.add_argument("--service-id")
    link.add_argument("--expected-name")
    link.add_argument("--expected-agency")
    link.add_argument("--unresolved-reason")
    correct = sub.add_parser("correct-metadata")
    correct.add_argument("--version-id", required=True)
    correct.add_argument("--facts", type=Path, required=True)
    correct.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    try:
        catalog = Catalog(CatalogPaths(args.catalog_db, args.original_dir, args.diagnostics_dir))
        if args.command == "register":
            registry = json.loads(args.registry.read_text(encoding="utf-8-sig"))
            if registry["format_version"] != 1:
                raise ValueError("unsupported_registry_format")
            result = catalog.register(registry["sources"], registry.get("relations", []), reason=args.reason)
        elif args.command == "collect":
            if not 1 <= len(set(args.source_ids)) == len(args.source_ids) <= 5:
                raise ValueError("select_1_to_5_unique_registered_sources")
            # Validate the whole requested selection BEFORE network or writes.
            for sid in args.source_ids:
                catalog.source(sid)
            result = []
            limits = Limits(max_bytes=args.max_mib * 1024 * 1024, attempts=args.attempts).checked()
            for sid in args.source_ids:
                print("원본 수집 단계: " + sid, flush=True)
                result.append(acquire(catalog, sid, limits=limits))
        elif args.command == "import-local":
            result = acquire(catalog, args.source_id, local_file=args.file)
        elif args.command == "inspect":
            result = catalog.inspect(args.as_of)
        elif args.command == "services":
            result = inspect_services(args.service_db, args.query)
        elif args.command == "links":
            if not catalog.paths.db.is_file():
                result = {"state": "catalog_missing", "links": []}
            else:
                with readonly(catalog.paths.db) as con:
                    result = {"links": [dict(r) for r in con.execute("SELECT * FROM service_links ORDER BY link_id")]}
        elif args.command == "correct-metadata":
            catalog.correct_metadata(args.version_id, json.loads(args.facts.read_text(encoding="utf-8-sig")), reason=args.reason)
            result = {"corrected": args.version_id}
        else:
            result = catalog.link(args.document_id, args.topic, json.loads(args.evidence.read_text(encoding="utf-8-sig")),
                version_id=args.version_id, service_db=args.service_db, service_id=args.service_id,
                expected_name=args.expected_name, expected_agency=args.expected_agency, unresolved_reason=args.unresolved_reason)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        outcomes = result if isinstance(result, list) else [result]
        return 1 if any(isinstance(r, dict) and r.get("status") == "failed" for r in outcomes) else 0
    except Exception as exc:
        # Never print request exception representations, response bodies, credentials or authenticated URLs.
        code = str(exc) if isinstance(exc, ValueError) and str(exc).isascii() and " " not in str(exc) else type(exc).__name__
        print(json.dumps({"error": code, "catalog": str(args.catalog_db.resolve())}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
