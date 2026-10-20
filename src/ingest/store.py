"""SQLite 정규 레코드, 실행 이력 및 버전별 durable A→B 인계."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from src.common.schema import Chunk
from src.ingest.storage import StoragePaths, resolve_storage
from src.ingest.locking import file_lock
from src.ingest.normalize import (  # public compatibility
    build_metadata, clean_service, content_hash, is_youth_service, youth_inclusion_reasons,
)

DDL = """
CREATE TABLE IF NOT EXISTS services (
    service_id TEXT PRIMARY KEY, payload TEXT NOT NULL, content_hash TEXT NOT NULL,
    status TEXT NOT NULL, reason TEXT, version INTEGER NOT NULL, last_run TEXT
);
CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id TEXT PRIMARY KEY, status TEXT NOT NULL, started_at TEXT NOT NULL,
    finished_at TEXT, summary TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_changes (
    service_id TEXT PRIMARY KEY, version INTEGER NOT NULL, event_id TEXT NOT NULL,
    action TEXT NOT NULL, reason TEXT, chunks TEXT NOT NULL
);
"""


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connection(*, storage: StoragePaths | None = None):
    paths = storage.validated() if storage is not None else resolve_storage()
    paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(paths.db_path, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.executescript(DDL)
        con.execute("BEGIN IMMEDIATE")
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


@contextmanager
def ingestion_lock(*, storage: StoragePaths | None = None):
    """OS file lock; process death releases it, so a stale file never blocks recovery."""
    paths = storage.validated() if storage is not None else resolve_storage()
    path = Path(str(paths.db_path) + ".ingest.lock")
    with file_lock(path, "ingestion_already_running"):
        yield


def start_run(run_id: str, *, storage: StoragePaths | None = None) -> None:
    """Call only under ingestion_lock; previous running runs were interrupted."""
    with connection(storage=storage) as con:
        con.execute(
            "UPDATE ingestion_runs SET status='interrupted', finished_at=? WHERE status='running'", (_now(),)
        )
        con.execute("INSERT INTO ingestion_runs VALUES (?, 'running', ?, NULL, '{}')", (run_id, _now()))


def finish_run(run_id: str, status: str, summary: dict, *, storage: StoragePaths | None = None) -> None:
    with connection(storage=storage) as con:
        con.execute("UPDATE ingestion_runs SET status=?, finished_at=?, summary=? WHERE run_id=?",
                    (status, _now(), _json(summary), run_id))


def get_run(run_id: str, *, storage: StoragePaths | None = None) -> dict | None:
    with connection(storage=storage) as con:
        row = con.execute("SELECT * FROM ingestion_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["summary"] = json.loads(result["summary"])
        return result


def get_record(service_id: str, *, storage: StoragePaths | None = None) -> dict | None:
    """A audit access; includes inactive records, unlike load_services."""
    with connection(storage=storage) as con:
        row = con.execute("SELECT * FROM services WHERE service_id=?", (service_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result


def _enqueue(con, service_id: str, version: int, action: str, reason, chunks: list[Chunk]) -> None:
    con.execute(
        """INSERT INTO pending_changes VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(service_id) DO UPDATE SET version=excluded.version,
        event_id=excluded.event_id, action=excluded.action, reason=excluded.reason, chunks=excluded.chunks""",
        (service_id, version, uuid.uuid4().hex, action, reason, _json([c.to_dict() for c in chunks])),
    )


def _status(service: dict, today: str) -> tuple[str, str | None]:
    if not is_youth_service(service):
        return "inactive", "youth_scope_exit"
    if service.get("deadline") and service["deadline"] < today:
        return "inactive", "expired"
    return "active", None


def _apply(con, services: list[dict], run_id: str | None, today: str) -> tuple[list[str], dict]:
    from src.ingest.chunker import chunk_service

    changed_ids = []
    counts = dict(new=0, changed=0, unchanged=0, deferred=0, included=0, excluded=0,
                  removals={}, status_reasons={})
    seen = set()
    for incoming in services:
        service = dict(incoming)
        sid = service["service_id"]
        if sid in seen:
            raise ValueError("duplicate_service_input")
        seen.add(sid)
        old = con.execute("SELECT * FROM services WHERE service_id=?", (sid,)).fetchone()
        included = is_youth_service(service)
        counts["included" if included else "excluded"] += 1
        status, reason = _status(service, today)
        if reason:
            counts["status_reasons"][reason] = counts["status_reasons"].get(reason, 0) + 1
        # Missing auxiliary observations are never fresh evidence of a scope exit
        # or permission to reopen an API-disappeared service from retained text.
        incomplete = service.get("_inclusion_complete", True) is False
        if old and incomplete and (
            reason == "youth_scope_exit" or old["reason"] in ("api_disappeared", "youth_scope_exit")
        ):
            counts["deferred"] += 1
            continue
        digest = content_hash(service)
        service["content_hash"] = digest
        service["inclusion_reasons"] = youth_inclusion_reasons(service)
        version = (old["version"] if old else 0)
        changed = not old or old["content_hash"] != digest or old["status"] != status or old["reason"] != reason
        if changed:
            version += 1
            counts["changed" if old else "new"] += 1
        else:
            counts["unchanged"] += 1
        if reason == "youth_scope_exit":
            # Do not retain non-youth section text/source snapshots. Minimal audit only.
            payload = {key: service.get(key) for key in
                       ("service_id", "service_name", "agency", "detail_url", "inclusion_reasons", "content_hash", "data_quality")}
            payload["_field_provenance"] = {key: value for key, value in service.get("_field_provenance", {}).items()
                                             if key in ("service_name", "agency", "field", "application_period", "detail_url")}
        else:
            payload = service
        con.execute(
            """INSERT INTO services VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(service_id) DO UPDATE SET payload=excluded.payload,
            content_hash=excluded.content_hash, status=excluded.status, reason=excluded.reason,
            version=excluded.version, last_run=excluded.last_run""",
            (sid, _json(payload), digest, status, reason, version, run_id),
        )
        if changed and status == "active":
            chunks = chunk_service(service)
            _enqueue(con, sid, version, "replace", None, chunks)
            changed_ids.append(sid)
        elif changed and old and (old["status"] == "active" or con.execute(
                "SELECT 1 FROM pending_changes WHERE service_id=?", (sid,)).fetchone()):
            _enqueue(con, sid, version, "remove", reason, [])
            counts["removals"][reason] = counts["removals"].get(reason, 0) + 1
    return changed_ids, counts


def upsert_services(services: list[dict], *, storage: StoragePaths | None = None) -> list[str]:
    """Normalized input; enforce provisional youth scope; return new/changed active IDs."""
    from src.ingest.pipeline import seoul_today
    with ingestion_lock(storage=storage):
        with connection(storage=storage) as con:
            changed, _ = _apply(con, services, None, seoul_today())
            return changed


def mark_closed() -> list[str]:
    """Compatibility fail-closed: no implicit snapshot/run globals, no disappearance."""
    return []


def _close_missing(con, listing, run_id: str) -> list[str]:
    if not listing.can_close_missing:
        return []
    seen = {row["서비스ID"] for row in listing.rows}
    removed = []
    for old in con.execute("SELECT * FROM services WHERE status='active'").fetchall():
        sid = old["service_id"]
        if sid not in seen:
            version = old["version"] + 1
            con.execute(
                "UPDATE services SET status='inactive', reason='api_disappeared', version=?, last_run=? WHERE service_id=?",
                (version, run_id, sid),
            )
            _enqueue(con, sid, version, "remove", "api_disappeared", [])
            removed.append(sid)
    return removed


def apply_run(run_id: str, services: list[dict], listing, *, full: bool, today: str,
              summary: dict, storage: StoragePaths | None = None) -> dict:
    """Public mutation participates in the source DB/consumer lock protocol."""
    with ingestion_lock(storage=storage):
        return _apply_run_locked(run_id, services, listing, full=full, today=today,
                                 summary=summary, storage=storage)


def _apply_run_locked(run_id: str, services: list[dict], listing, *, full: bool, today: str,
                      summary: dict, storage: StoragePaths | None = None) -> dict:
    """DB records, events and completed run summary commit in a single transaction."""
    from src.ingest.chunker import chunk_service
    with connection(storage=storage) as con:
        _, counts = _apply(con, services, run_id, today)
        counts["deferred"] += summary.get("diagnostics", {}).get("invalid_records", 0)
        removed = _close_missing(con, listing, run_id)
        if removed:
            counts["removals"]["api_disappeared"] = len(removed)
        # Known persisted deadlines can expire even when API collection fails.
        for old in con.execute("SELECT * FROM services WHERE status='active'").fetchall():
            data = json.loads(old["payload"])
            if data.get("deadline") and data["deadline"] < today:
                version = old["version"] + 1
                con.execute(
                    "UPDATE services SET status='inactive', reason='expired', version=?, last_run=? WHERE service_id=?",
                    (version, run_id, old["service_id"]),
                )
                _enqueue(con, old["service_id"], version, "remove", "expired", [])
                counts["removals"]["expired"] = counts["removals"].get("expired", 0) + 1
        if full:
            for row in con.execute("SELECT * FROM services WHERE status='active'").fetchall():
                chunks = chunk_service(json.loads(row["payload"]))
                _enqueue(con, row["service_id"], row["version"], "replace", None, chunks)
        pending = con.execute("SELECT action, chunks FROM pending_changes").fetchall()
        result = {**summary, **counts,
                  "pending_changes": len(pending),
                  "pending_removals": sum(r["action"] == "remove" for r in pending),
                  "chunks": sum(len(json.loads(r["chunks"])) for r in pending)}
        con.execute(
            "UPDATE ingestion_runs SET status=?, finished_at=?, summary=? WHERE run_id=?",
            (result["status"], _now(), _json(result), run_id),
        )
        return result


def load_services(service_ids: list[str] | None = None, *, storage: StoragePaths | None = None) -> list[dict]:
    """Active youth records only, ordered by ID. [] means no IDs."""
    if service_ids == []:
        return []
    with connection(storage=storage) as con:
        rows = con.execute("SELECT * FROM services WHERE status='active' ORDER BY service_id").fetchall()
        wanted = set(service_ids) if service_ids is not None else None
        return [json.loads(row["payload"]) for row in rows if wanted is None or row["service_id"] in wanted]


def sql_keyword_search(keywords: list[str], k: int, *, storage: StoragePaths | None = None) -> list[dict]:
    """Active records, literal case-insensitive OR LIKE, deterministic ID order, no ranking."""
    if type(k) is not int or k < 0:
        raise ValueError("k must be a nonnegative integer")
    terms = list(dict.fromkeys(term.strip() for term in keywords if isinstance(term, str) and term.strip()))
    if not terms or k == 0:
        return []
    # Search only evidence fields, not JSON keys or operational/raw snapshots.
    fields = ("service_name", "agency", "support_target", "selection_criteria", "support_content", "how_to_apply")
    expression = " || ' ' || ".join(f"coalesce(json_extract(payload, '$.{f}'), '')" for f in fields)
    clauses, params = [], []
    for term in terms:
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append(f"({expression}) LIKE ? ESCAPE '\\'")
        params.append("%" + escaped + "%")
    with connection(storage=storage) as con:
        rows = con.execute(
            "SELECT payload FROM services WHERE status='active' AND (" + " OR ".join(clauses)
            + ") ORDER BY service_id LIMIT ?", (*params, k),
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]


def get_pending_changes(*, storage: StoragePaths | None = None) -> list[dict]:
    """Durable complete-service replacements/removals; reading never acknowledges."""
    with connection(storage=storage) as con:
        rows = con.execute("SELECT * FROM pending_changes ORDER BY service_id").fetchall()
        return [{**dict(row), "chunks": json.loads(row["chunks"])} for row in rows]


def pending_chunks(*, storage: StoragePaths | None = None) -> list[Chunk]:
    return [Chunk(**chunk) for event in get_pending_changes(storage=storage) for chunk in event["chunks"]]


def pending_removal_ids(*, storage: StoragePaths | None = None) -> list[str]:
    return [event["service_id"] for event in get_pending_changes(storage=storage) if event["action"] == "remove"]


def acknowledge_changes(tokens: list[dict], *, storage: StoragePaths | None = None) -> int:
    """Acknowledge only after BOTH indexes durably applied this exact event/version."""
    count = 0
    with connection(storage=storage) as con:
        for token in tokens:
            count += con.execute(
                "DELETE FROM pending_changes WHERE service_id=? AND version=? AND event_id=?",
                (token["service_id"], token["version"], token["event_id"]),
            ).rowcount
    return count
