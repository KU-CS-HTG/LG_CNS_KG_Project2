"""Stored-event consumer. Default: read-only inspection; --apply: isolated worker.

The worker owns BOTH OS locks until verification and acknowledgment finish.
Killing its launcher cannot leave an unlocked, still-writing index subprocess.
No ingestion, model download, or caller-global configuration mutation occurs here.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict, dataclass, replace
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

from src.common import config
from src.common.schema import Chunk, SECTIONS, Metadata
from src.ingest import store
from src.ingest.locking import file_lock
from src.ingest.storage import StoragePaths


@dataclass(frozen=True)
class Settings:
    db_path: str
    index_dir: str
    model: str
    max_seq_length: int
    device: str | None = None
    validation: bool = False
    adopt_existing: bool = False

    @classmethod
    def resolve(cls, *, db_path=None, index_dir=None, model=None, validation=False,
                adopt_existing=False):
        if validation and (db_path is None or index_dir is None):
            raise ValueError("validation_requires_explicit_db_and_index")
        db = Path(db_path or config.DB_PATH).resolve()
        index = Path(index_dir or config.VECTORSTORE_DIR).resolve()
        if db.exists() and db.stat().st_nlink > 1:
            # A second hardlink would give the same queue a different lock/manifest.
            raise ValueError("source_db_hardlinks_not_supported")
        # Never put a database (or its sidecars) inside the target index/raw tree.
        raw = Path(config.RAW_DIR).resolve()
        if db == raw or db.is_relative_to(raw):
            raise ValueError("source_overlaps_live_raw")
        if db == index or db.is_relative_to(index) or index.is_relative_to(db):
            raise ValueError("source_index_paths_overlap")
        source_family = {Path(str(db) + suffix) for suffix in
                         ("", "-wal", "-shm", "-journal", ".ingest.lock", ".handoff.json", ".handoff.json.tmp")}
        if index in source_family or Path(str(index) + ".handoff.lock") in source_family:
            raise ValueError("source_index_runtime_paths_overlap")
        if index == raw or index.is_relative_to(raw) or raw.is_relative_to(index):
            raise ValueError("index_overlaps_live_raw")
        if validation:
            live_db = Path(config.DB_PATH).resolve()
            live_index = Path(config.VECTORSTORE_DIR).resolve()
            family = [Path(str(live_db) + suffix) for suffix in
                      ("", "-wal", "-shm", "-journal", ".ingest.lock", ".handoff.json")]
            candidates = [db, Path(str(db) + ".ingest.lock"), Path(str(db) + ".handoff.json"),
                          index, Path(str(index) + ".handoff.lock")]
            for candidate in candidates:
                for protected in [*family, live_index, raw]:
                    if (candidate == protected or candidate.is_relative_to(protected)
                            or protected.is_relative_to(candidate)
                            or (candidate.exists() and protected.exists() and candidate.samefile(protected))):
                        raise ValueError("validation_destination_overlaps_live_storage")
        return cls(str(db), str(index), model or config.EMBEDDING_MODEL,
                   config.EMBEDDING_MAX_SEQ_LENGTH, config.EMBEDDING_DEVICE,
                   validation, adopt_existing)

    def checked(self):
        resolved = self.resolve(db_path=self.db_path, index_dir=self.index_dir, model=self.model,
                                validation=self.validation, adopt_existing=self.adopt_existing)
        if (not isinstance(self.model, str) or not self.model.strip()
                or type(self.max_seq_length) is not int or self.max_seq_length < 1):
            raise ValueError("invalid_model_settings")
        return replace(resolved, max_seq_length=self.max_seq_length, device=self.device)

    def storage(self):
        # Consumer never reads/writes raw responses. Only DB and DB lock are used.
        return StoragePaths(Path(self.db_path), Path(self.db_path).parent / ".handoff-unused-raw", "live")


def read_pending(settings: Settings) -> list[dict]:
    """Read-only: no schema initialization, directory creation or acknowledgment."""
    db = Path(settings.db_path)
    if not db.is_file():
        raise ValueError("source_db_missing")
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT * FROM pending_changes ORDER BY service_id").fetchall()
        return [{**dict(row), "chunks": json.loads(row["chunks"])} for row in rows]


def _check_source_mode(settings: Settings):
    with closing(sqlite3.connect(Path(settings.db_path).as_uri() + "?mode=ro", uri=True)) as con:
        row = con.execute("SELECT 1 FROM ingestion_runs WHERE json_extract(summary, '$.mode') "
                          "IN ('offline', 'test', 'validation') LIMIT 1").fetchone()
        if row and not settings.validation:
            raise ValueError("isolated_source_requires_validation_mode")


def _error_code(exc):
    # Never echo arbitrary backend exception text (URLs, credentials, documents).
    return (str(exc) if type(exc) in (ValueError, RuntimeError)
            and re.fullmatch(r"[a-z][a-z0-9_]*", str(exc)) else type(exc).__name__)


def validate_event(event: dict) -> list[Chunk]:
    """Missing chunks is malformed, whereas an explicit [] is a valid replacement."""
    if not isinstance(event, dict):
        raise ValueError("invalid_event")
    for key in ("service_id", "event_id"):
        if not isinstance(event.get(key), str) or not event[key].strip():
            raise ValueError("invalid_event_token")
    if type(event.get("version")) is not int or event["version"] < 1:
        raise ValueError("invalid_event_version")
    if event.get("action") not in ("replace", "remove") or type(event.get("chunks")) is not list:
        raise ValueError("invalid_event_payload")
    if event["action"] == "remove" and event["chunks"]:
        raise ValueError("remove_must_have_empty_chunks")
    chunks, ids = [], set()
    for raw in event["chunks"]:
        try:
            chunk = Chunk(**raw)
        except (TypeError, ValueError):
            raise ValueError("invalid_chunk_payload") from None
        if (chunk.service_id != event["service_id"] or chunk.section not in SECTIONS
                or any(not isinstance(v, str) or not v.strip() for v in
                       (chunk.chunk_id, chunk.service_name, chunk.text))
                or re.fullmatch(re.escape(chunk.service_id + "#" + chunk.section)
                                + r"(?:-[1-9][0-9]*)?", chunk.chunk_id) is None
                or chunk.chunk_id in ids or not isinstance(chunk.detail_url, str)
                or not isinstance(chunk.metadata, dict)
                or set(chunk.metadata) - set(Metadata.__annotations__)):
            raise ValueError("invalid_chunk_contract")
        for key, value in chunk.metadata.items():
            if key in ("age_min", "age_max"):
                valid = value is None or type(value) is int
            elif key == "target_tags":
                valid = value is None or (isinstance(value, list) and all(isinstance(v, str) for v in value))
            else:
                valid = value is None or isinstance(value, str)
            if not valid:
                raise ValueError("invalid_chunk_metadata")
        ids.add(chunk.chunk_id)
        chunks.append(chunk)
    return chunks


def _association(settings: Settings, dimension: int) -> dict:
    return {"format": 1, "source_db": settings.db_path, "index_dir": settings.index_dir,
            "model": settings.model, "max_seq_length": settings.max_seq_length,
            "dimension": dimension, "space": "cosine", "embedding_policy": "B/passage-query/v1"}


def _bindings(settings: Settings):
    return [Path(settings.db_path + ".handoff.json"), Path(settings.index_dir) / "handoff_manifest.json"]


def _check_binding(settings: Settings, dimension=None):
    """Validate both sides before loading a model or initializing any collection."""
    for path in _bindings(settings):
        if path.exists():
            binding = json.loads(path.read_text(encoding="utf-8"))
            expected = _association(settings, dimension if dimension is not None else binding.get("dimension"))
            if binding != expected:
                raise ValueError("incompatible_source_index_model_association")


def _bind(settings: Settings, dimension: int):
    _check_binding(settings, dimension)
    for path in _bindings(settings):
        # Either half can be recovered after a crash; mismatch always fails closed.
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(_association(settings, dimension), handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)


def apply_pending(settings: Settings, adapter) -> dict:
    """Worker core; adapter uses real B code. Stop at the first failed event.

    Lock order: target index, then source DB. Ingestion takes only source DB.
    Nonblocking acquisition fails immediately (bounded wait = zero seconds).
    """
    report = {"mode": "apply", "settings": asdict(settings), "applied": 0, "acknowledged": 0,
              "failed": [], "remaining": None, "events": []}
    stage, event = "source", None
    try:
        _check_source_mode(settings)
        stage = "lock"
        with file_lock(Path(settings.index_dir + ".handoff.lock"), "index_consumer_already_running"):
            with store.ingestion_lock(storage=settings.storage()):
                # Recheck after acquiring the lock: collection may just have finished.
                stage = "source"
                _check_source_mode(settings)
                stage = "read"
                events = read_pending(settings)
                # Validate the complete selected queue before touching an index.
                stage = "validate"
                snapshots = [(event, validate_event(event)) for event in events]
                stage = "association"
                _check_binding(settings)
                if not snapshots:
                    report["remaining"] = 0
                    return report
                stage = "compatibility"
                dimension = adapter.prepare(settings, bound=any(p.exists() for p in _bindings(settings)))
                _bind(settings, dimension)
                for event, chunks in snapshots:
                    stage = "revalidate"
                    current = {row["service_id"]: row for row in read_pending(settings)}.get(event["service_id"])
                    if current != event:
                        raise RuntimeError("event_superseded_or_modified")
                    stage = "apply"
                    before = adapter.capture(event)
                    adapter.apply(event, chunks)
                    report["applied"] += 1
                    stage = "verify"
                    adapter.verify(event, chunks, before)
                    stage = "acknowledge"
                    if store.acknowledge_changes([event], storage=settings.storage()) != 1:
                        raise RuntimeError("exact_event_not_acknowledged")
                    report["acknowledged"] += 1
                    report["events"].append({key: event[key] for key in ("service_id", "version", "event_id", "action")})
                report["remaining"] = len(read_pending(settings))
    except Exception as exc:
        # Do not expose exception URLs/source text. Event tokens contain no credentials.
        report["failed"].append({"stage": stage, "error": type(exc).__name__,
                                 "code": _error_code(exc),
                                 "token": {key: event.get(key) for key in ("service_id", "version", "event_id")}
                                 if isinstance(event, dict) else None})
        try:
            report["remaining"] = len(read_pending(settings))
        except Exception:
            pass
    return report


def consume(settings: Settings, *, apply=False) -> dict:
    """Public API: application runs in a child that owns config AND all locks."""
    settings = settings.checked()
    events = read_pending(settings)
    for event in events:
        validate_event(event)
    _check_binding(settings)
    if not apply:
        return {"mode": "dry-run", "settings": asdict(settings), "remaining": len(events),
                "events": [{**{k: e[k] for k in ("service_id", "version", "event_id", "action")},
                            "chunks": len(e["chunks"])} for e in events]}
    _check_source_mode(settings)
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run([sys.executable, "-m", "src.ingest.handoff_worker"],
                            input=json.dumps(asdict(settings)), capture_output=True, text=True,
                            encoding="utf-8", env=env)
    # Worker stdout is a single JSON report; backend diagnostics remain on stderr.
    if result.returncode not in (0, 1):
        raise RuntimeError("handoff_worker_failed")
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError):
        raise RuntimeError("invalid_worker_report") from None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Stored A events → B index → verification → exact acknowledgment")
    parser.add_argument("--db-path", type=Path)
    parser.add_argument("--index-dir", type=Path)
    parser.add_argument("--model", help="Must match the target association; cached models only (no downloads)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Default; no model/index initialization")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--validation", action="store_true", help="Reject live DB/index/raw destinations")
    parser.add_argument("--adopt-existing", action="store_true", help="Explicit first binding of an existing nonempty B collection")
    args = parser.parse_args(argv)
    try:
        settings = Settings.resolve(db_path=args.db_path, index_dir=args.index_dir, model=args.model,
                                    validation=args.validation, adopt_existing=args.adopt_existing)
        report = consume(settings, apply=args.apply)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report.get("failed") else 0
    except Exception as exc:
        print(json.dumps({"failed": [{"stage": "inspection", "error": type(exc).__name__,
                                     "code": _error_code(exc)}]}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
