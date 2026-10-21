"""Compact read-only ID inspection, separate from production classification and scoring."""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3

from src.common import config


@contextmanager
def readonly(path):
    con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN")
        yield con
    finally:
        con.close()


def inspect_ids(db_path, index_dir, questions, *, source_run=None, model=None):
    from eval.run_eval import missing_gold
    from src.index.vector_store import _to_store_metadata
    records, pending, deferred, selections = {}, set(), {}, {}
    if Path(db_path).is_file():
        with readonly(db_path) as con:
            records = {r["service_id"]: {**dict(r), "payload": json.loads(r["payload"])}
                       for r in con.execute("SELECT * FROM services")}
            pending = {r[0] for r in con.execute("SELECT service_id FROM pending_changes")}
            for row in con.execute("SELECT summary FROM ingestion_runs ORDER BY started_at"):
                summary = json.loads(row[0])
                for sid in summary.get("processed_ids", []):
                    deferred.pop(sid, None)
                for item in summary.get("replay_deferred", []):
                    deferred[item["service_id"]] = item["reason"]
                if summary.get("operation") == "raw_reprocess":
                    selections[summary["source_acquisition"]["run_id"]] = set(summary.get("processed_ids", [])) | {
                        item["service_id"] for item in summary.get("replay_deferred", [])}
    observed, selected = None, None
    if source_run:
        root = Path(source_run)
        summary = json.loads((root / "run-summary.json").read_text(encoding="utf-8"))
        if summary["run_id"] != root.name:
            raise ValueError("source_run_identity_mismatch")
        observed = {r["서비스ID"] for p in sorted(root.glob("serviceList-[0-9]*.json"))
                    for r in json.loads(p.read_text(encoding="utf-8"))["data"]}
        selected = selections.get(summary["run_id"])
    documents, index_state = {}, "not_created"
    index_db = Path(index_dir) / "chroma.sqlite3"
    if index_db.exists():
        name = "chunks__" + re.sub(r"[^A-Za-z0-9._-]+", "_", model or config.EMBEDDING_MODEL).strip("_.-")[:50]
        with readonly(index_db) as con:
            collection = con.execute("SELECT id FROM collections WHERE name=?", (name,)).fetchone()
            index_state = "selected_collection_missing" if collection is None else "persistent_documents_only"
            if collection:
                for r in con.execute("""SELECT e.embedding_id,m.key,m.string_value,m.int_value,m.float_value,m.bool_value
                    FROM embeddings e JOIN segments s ON e.segment_id=s.id
                    JOIN embedding_metadata m ON m.id=e.id WHERE s.collection=? AND s.scope='METADATA'""", (collection[0],)):
                    entry = documents.setdefault(r[0], {"text": None, "metadata": {}})
                    value = next((r[k] for k in range(2, 6) if r[k] is not None), None)
                    if r[1] == "chroma:document":
                        entry["text"] = value
                    else:
                        entry["metadata"][r[1]] = value
    rows = []
    from src.ingest.chunker import chunk_service
    for q in questions:
        for sid in q.get("relevant_service_ids", []):
            old = records.get(sid)
            actual = {cid: d for cid, d in documents.items() if d["metadata"].get("service_id") == sid}
            expected = {c.chunk_id: {"text": c.text, "metadata": _to_store_metadata(c)}
                        for c in chunk_service(old["payload"])} if old and old["status"] == "active" else {}
            reason = (deferred[sid] if sid in deferred and sid not in pending else
                      "not_observed_in_available_sample" if not old and observed is not None and sid not in observed else
                      "observed_outside_replay_selection" if not old and selected is not None and sid in observed and sid not in selected else
                      "unknown" if not old else old["reason"] if old["status"] != "active" else
                      "insufficient_section_content" if not expected else
                      "pending_application" if sid in pending else
                      "index_snapshot_mismatch_or_incompatibility" if actual != expected else "present_in_index")
            decision = old["payload"].get("inclusion_policy", {}) if old else {}
            rows.append({"question": q["id"], "service_id": sid, "state": reason,
                         "observed": sid in observed if observed is not None else None,
                         "status": old["status"] if old else None,
                         "policy_id": decision.get("policy_id"), "inclusion_outcome": decision.get("outcome"),
                         "selected_for_replay": sid in selected if selected is not None else None,
                         "current_chunks": len(expected), "indexed_chunks": len(actual), "pending": sid in pending})
    return {"db_path": str(Path(db_path).resolve()), "index_dir": str(Path(index_dir).resolve()),
            "index_observation": index_state, "rows": rows,
            "missing_gold": missing_gold(questions, lambda sid: any(d["metadata"].get("service_id") == sid for d in documents.values())),
            "limitations": "Sample absence is not invalid gold. Persistent membership is not retrieval quality or eligibility. No model/query/ack."}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Compact DB/index coverage; no API/model/index writes/ack")
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--index-dir", type=Path, required=True)
    parser.add_argument("--source-run", type=Path)
    parser.add_argument("--questions", type=Path, default=config.EVAL_DIR / "questions.jsonl")
    parser.add_argument("--only", default="q15,q16,q23")
    parser.add_argument("--model", default=config.EMBEDDING_MODEL)
    args = parser.parse_args(argv)
    try:
        questions = [json.loads(line) for line in args.questions.read_text(encoding="utf-8").splitlines() if line.strip()]
        selected = set(args.only.split(","))
        result = inspect_ids(args.db_path, args.index_dir, [q for q in questions if q["id"] in selected],
                             source_run=args.source_run, model=args.model)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        from src.ingest.handoff import _error_code
        print(json.dumps({"error": type(exc).__name__, "code": _error_code(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
