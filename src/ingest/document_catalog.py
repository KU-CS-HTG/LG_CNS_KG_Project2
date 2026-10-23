"""A-1 isolated document identities, evidence, observations and auditable catalog.

No service queue, Chunk, model or index imports. Dates are facts, not legal opinions.
"""
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import parse_qsl, urlsplit
import uuid

from src.common import config
from src.ingest.coverage import readonly
from src.ingest.locking import file_lock

SCHEMA_VERSION = 1
DDL = """
CREATE TABLE IF NOT EXISTS catalog_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents(document_id TEXT PRIMARY KEY,identity_key TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS sources(source_id TEXT PRIMARY KEY,document_id TEXT NOT NULL REFERENCES documents,
 publication_identity TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifacts(sha256 TEXT PRIMARY KEY,relative_path TEXT UNIQUE NOT NULL,
 size INTEGER NOT NULL,format TEXT NOT NULL,quality TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS versions(version_id TEXT PRIMARY KEY,document_id TEXT NOT NULL REFERENCES documents,
 publication_identity TEXT NOT NULL,artifact_sha256 TEXT NOT NULL REFERENCES artifacts,facts TEXT NOT NULL,
 UNIQUE(document_id,publication_identity,artifact_sha256));
CREATE TABLE IF NOT EXISTS attempts(attempt_id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES sources,
 started_at TEXT NOT NULL,finished_at TEXT,status TEXT NOT NULL,detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS observations(attempt_id TEXT PRIMARY KEY REFERENCES attempts,
 version_id TEXT NOT NULL REFERENCES versions,detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS relations(relation_id TEXT PRIMARY KEY,from_document_id TEXT NOT NULL REFERENCES documents,
 from_version_id TEXT REFERENCES versions,to_document_id TEXT NOT NULL REFERENCES documents,
 to_version_id TEXT REFERENCES versions,kind TEXT NOT NULL,scope TEXT NOT NULL,evidence TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS service_links(link_id TEXT PRIMARY KEY,version_id TEXT REFERENCES versions,
 document_id TEXT NOT NULL REFERENCES documents,service_id TEXT,topic TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS catalog_audit(audit_id INTEGER PRIMARY KEY,at TEXT NOT NULL,
 entity TEXT NOT NULL,entity_id TEXT NOT NULL,before_json TEXT,after_json TEXT NOT NULL,reason TEXT NOT NULL);
"""


def dumps(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def identity(prefix, *values):
    return prefix + "-" + hashlib.sha256(dumps(values).encode()).hexdigest()[:32]


def now():
    return datetime.now(timezone.utc).isoformat()


def safe_url(value):
    u = urlsplit(value)
    if (u.scheme != "https" or not u.hostname or u.username or u.password or u.port not in (None, 443)
            or u.fragment or any(k.lower() in {"servicekey", "api_key", "token", "password", "authorization", "x-amz-signature"}
                                 for k, _ in parse_qsl(u.query))):
        raise ValueError("public_https_source_url_required")
    return value


@dataclass(frozen=True)
class CatalogPaths:
    db: Path
    originals: Path
    diagnostics: Path

    def checked(self):
        p = CatalogPaths(*(Path(v).resolve() for v in (self.db, self.originals, self.diagnostics)))
        protected_dirs = (Path(config.RAW_DIR).resolve(), Path(config.VECTORSTORE_DIR).resolve())
        protected_db = Path(config.DB_PATH).resolve()
        files = [p.db, *(Path(str(p.db) + s) for s in ("-wal", "-shm", "-journal", ".catalog.lock"))]
        for value in [*files, p.originals, p.diagnostics]:
            if any(value == d or value.is_relative_to(d) or (value in (p.originals, p.diagnostics) and d.is_relative_to(value))
                   for d in protected_dirs):
                raise ValueError("document_storage_overlaps_api_or_index")
            if (value == protected_db or str(value).startswith(str(protected_db) + ".")
                    or str(value).startswith(str(protected_db) + "-")
                    or (value.exists() and protected_db.exists() and value.samefile(protected_db))
                    or (value in (p.originals, p.diagnostics) and protected_db.is_relative_to(value))):
                raise ValueError("document_storage_overlaps_api_database")
        if (p.originals == p.diagnostics or p.originals.is_relative_to(p.diagnostics)
                or p.diagnostics.is_relative_to(p.originals)
                or any(f == d or f.is_relative_to(d) for f in files for d in (p.originals, p.diagnostics))):
            raise ValueError("document_destinations_must_be_separate")
        if p.db.is_file():
            with readonly(p.db) as con:
                tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                allowed = {"catalog_meta", "documents", "sources", "artifacts", "versions", "attempts", "observations", "relations", "service_links", "catalog_audit"}
                if tables - allowed or (tables and "catalog_meta" not in tables):
                    raise ValueError("selected_database_is_not_document_catalog")
        return p


def validate_evidence(value):
    if (not isinstance(value, dict) or not value.get("quote") or not value.get("location")
            or value.get("verification") not in ("official_evidence_verified", "candidate_unverified")
            or value.get("review_status") not in ("human_review_pending", "human_review_completed")):
        raise ValueError("evidence_and_review_status_required")
    safe_url(value["url"])
    if value["review_status"] == "human_review_completed" and not (
            value.get("reviewer_type") == "human" and value.get("reviewer") and value.get("reviewed_at")):
        raise ValueError("human_review_receipt_required")


def validate_facts(facts):
    if not all(isinstance(facts.get(k), str) and facts[k].strip() for k in ("title", "publisher", "document_type")):
        raise ValueError("document_identity_facts_required")
    if facts.get("publication_state") not in ("final", "proposal", "unknown"):
        raise ValueError("invalid_publication_state")
    validate_evidence(facts["identity_evidence"])
    for key in ("posting_date", "issuance_date", "effective_start", "effective_end"):
        fact = facts.get(key, {})
        value, precision = fact.get("value"), fact.get("precision")
        if value is None:
            if not fact.get("unresolved_reason"):
                raise ValueError("unknown_date_requires_reason")
        else:
            formats = {"day": "%Y-%m-%d", "month": "%Y-%m", "year": "%Y"}
            if precision not in formats or datetime.strptime(value, formats[precision]).strftime(formats[precision]) != value:
                raise ValueError("invalid_date_precision")
            validate_evidence(fact["evidence"])
    start, end = facts["effective_start"], facts["effective_end"]
    if (start.get("value") and end.get("value") and start.get("precision") == end.get("precision") == "day"
            and start["value"] > end["value"]):
        raise ValueError("reversed_effective_interval")


def temporal_status(facts, as_of):
    """Inclusive day interval [start,end]; open end is uncertainty, never eternity."""
    date.fromisoformat(as_of)
    warnings = []
    start, end = facts["effective_start"], facts["effective_end"]
    # Timing remains visible even when publication finality is unresolved.
    timing = ("future" if start.get("precision") == "day" and start.get("value") and start["value"] > as_of else
              "historical" if end.get("precision") == "day" and end.get("value") and end["value"] < as_of else "unresolved_or_started")
    if facts["publication_state"] == "proposal":
        return {"state": "proposal_not_authoritative", "timing": timing, "warnings": ["Proposal is not a current rule."]}
    if facts["publication_state"] != "final" or facts["identity_evidence"]["verification"] != "official_evidence_verified":
        return {"state": "unresolved_publication", "timing": timing, "warnings": ["Publication/finality not verified."]}
    def known(item):
        return (item.get("value") and item.get("precision") == "day"
                and item.get("evidence", {}).get("verification") == "official_evidence_verified")
    if known(end) and end["value"] < as_of:
        return {"state": "historical", "warnings": ["Known effective end passed; no repeal inferred from download status."]}
    if not known(start):
        return {"state": "unresolved_effectivity", "warnings": ["Edition year/posting date does not establish effectivity."]}
    if start["value"] > as_of:
        return {"state": "future_effective", "warnings": []}
    if not known(end):
        warnings.append("Effective end unresolved; this is not proof of perpetual validity.")
    return {"state": "supported_applicable_candidate", "warnings": warnings + ["Substantive conflicts and scoped amendments require review."]}


class Catalog:
    def __init__(self, paths):
        self.paths = paths.checked()

    def mutate(self, operation):
        # Consistent originals→catalog order; OS handle closure releases crash locks.
        with file_lock(self.paths.originals / ".catalog.lock", "document_originals_busy"):
            with file_lock(Path(str(self.paths.db) + ".catalog.lock"), "document_catalog_busy"):
                self.paths.db.parent.mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect(self.paths.db, timeout=10)) as con:
                    con.row_factory = sqlite3.Row
                    con.execute("PRAGMA foreign_keys=ON")
                    con.executescript(DDL)
                    con.execute("BEGIN IMMEDIATE")
                    binding = dumps({"format": SCHEMA_VERSION, "originals": str(self.paths.originals), "diagnostics": str(self.paths.diagnostics)})
                    old = con.execute("SELECT value FROM catalog_meta WHERE key='binding'").fetchone()
                    if old and old[0] != binding:
                        raise ValueError("catalog_path_or_format_binding_mismatch")
                    con.execute("INSERT OR IGNORE INTO catalog_meta VALUES ('binding',?)", (binding,))
                    result = operation(con)
                    con.commit()
                    return result

    @staticmethod
    def audit(con, entity, key, before, after, reason):
        if before == after:
            return
        if not reason.strip():
            raise ValueError("audit_reason_required")
        con.execute("INSERT INTO catalog_audit(at,entity,entity_id,before_json,after_json,reason) VALUES (?,?,?,?,?,?)",
                    (now(), entity, key, dumps(before) if before is not None else None, dumps(after), reason))

    def register(self, entries, relations=(), *, reason="source registry registration"):
        for entry in entries:
            validate_facts(entry["facts"])
            safe_url(entry["posting_url"])
            if entry.get("attachment_url"):
                safe_url(entry["attachment_url"])
                validate_evidence(entry["attachment_evidence"])
                if entry["attachment_evidence"]["url"] != entry["posting_url"]:
                    raise ValueError("attachment_requires_posting_relationship_evidence")
            if not all(isinstance(entry.get(k), str) and entry[k].strip() for k in ("source_id", "logical_identity", "publication_identity")):
                raise ValueError("independent_document_and_publication_identity_required")
        def write(con):
            ids = []
            for entry in entries:
                doc_id = identity("doc", entry["logical_identity"])
                con.execute("INSERT OR IGNORE INTO documents VALUES (?,?)", (doc_id, entry["logical_identity"]))
                old = con.execute("SELECT payload,document_id,publication_identity FROM sources WHERE source_id=?", (entry["source_id"],)).fetchone()
                if old and (old[1] != doc_id or old[2] != entry["publication_identity"]):
                    raise ValueError("source_identity_reassignment_forbidden")
                self.audit(con, "source", entry["source_id"], json.loads(old[0]) if old else None, entry, reason)
                con.execute("INSERT INTO sources VALUES (?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET payload=excluded.payload",
                            (entry["source_id"], doc_id, entry["publication_identity"], dumps(entry)))
                ids.append({"source_id": entry["source_id"], "document_id": doc_id})
            for relation in relations:
                validate_evidence(relation["evidence"])
                if relation["kind"] not in ("corrects", "amends", "supersedes", "supplements", "refers-to") or not relation["scope"]:
                    raise ValueError("invalid_document_relation")
                for endpoint in ("from", "to"):
                    version = relation.get(endpoint + "_version_id")
                    if version and not con.execute("SELECT 1 FROM versions WHERE version_id=? AND document_id=?",
                            (version, identity("doc", relation[endpoint + "_identity"]))).fetchone():
                        raise ValueError("relation_version_document_mismatch")
                rid = identity("rel", relation)
                values = (rid, identity("doc", relation["from_identity"]), relation.get("from_version_id"),
                          identity("doc", relation["to_identity"]), relation.get("to_version_id"),
                          relation["kind"], relation["scope"], dumps(relation["evidence"]))
                con.execute("INSERT OR IGNORE INTO relations VALUES (?,?,?,?,?,?,?,?)", values)
            return ids
        return self.mutate(write)

    def source(self, source_id):
        with readonly(self.paths.db) as con:
            row = con.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
            if row is None:
                raise ValueError("unregistered_source")
            return {**dict(row), "payload": json.loads(row["payload"])}

    def start_attempt(self, source, origin):
        aid = uuid.uuid4().hex
        self.mutate(lambda con: con.execute("INSERT INTO attempts VALUES (?,?,?,NULL,'running',?)",
                    (aid, source["source_id"], now(), dumps({"source_snapshot": source["payload"], "origin": origin}))))
        return aid

    def finish_attempt(self, aid, status, detail, source, artifact=None, promote=None):
        def write(con):
            row = con.execute("SELECT detail,status FROM attempts WHERE attempt_id=?", (aid,)).fetchone()
            if row is None or row[1] != "running":
                raise ValueError("attempt_not_running")
            observation = {**json.loads(row[0]), **detail}
            version_id = None
            if artifact:
                if promote:
                    promote()
                con.execute("INSERT OR IGNORE INTO artifacts VALUES (?,?,?,?,?)", (artifact["sha256"], artifact["relative_path"],
                    artifact["size"], artifact["format"], dumps(artifact["quality"])))
                version_id = identity("ver", source["document_id"], source["publication_identity"], artifact["sha256"])
                facts = source["payload"]["facts"]
                new = con.execute("INSERT OR IGNORE INTO versions VALUES (?,?,?,?,?)", (version_id, source["document_id"],
                    source["publication_identity"], artifact["sha256"], dumps(facts))).rowcount
                if new:
                    self.audit(con, "version", version_id, None, facts, "exact-byte publication observation")
                con.execute("INSERT INTO observations VALUES (?,?,?)", (aid, version_id, dumps(observation)))
            con.execute("UPDATE attempts SET finished_at=?,status=?,detail=? WHERE attempt_id=?",
                        (now(), status, dumps(observation), aid))
            return {"attempt_id": aid, "status": status, "version_id": version_id, **detail,
                    "artifact": artifact}
        return self.mutate(write)

    def correct_metadata(self, version_id, facts, *, reason):
        validate_facts(facts)
        def write(con):
            row = con.execute("SELECT facts FROM versions WHERE version_id=?", (version_id,)).fetchone()
            if row is None:
                raise ValueError("unknown_version")
            self.audit(con, "version", version_id, json.loads(row[0]), facts, reason)
            con.execute("UPDATE versions SET facts=? WHERE version_id=?", (dumps(facts), version_id))
        self.mutate(write)

    def inspect(self, as_of):
        date.fromisoformat(as_of)
        if not self.paths.db.is_file():
            return {"as_of": as_of, "state": "catalog_missing", "versions": [], "sources": []}
        with readonly(self.paths.db) as con:
            result = {"as_of": as_of, "interval_convention": "inclusive precise calendar days [start,end]",
                      "paths": {k: str(v) for k, v in vars(self.paths).items()}, "versions": []}
            for table in ("sources", "relations", "service_links", "attempts", "artifacts", "observations"):
                result[table] = [dict(r) for r in con.execute("SELECT * FROM " + table)]
            for source in result["sources"]:
                source["payload"] = json.loads(source["payload"])
                source["computed_from_posting_facts"] = temporal_status(source["payload"]["facts"], as_of)
                source["computed_from_posting_facts"]["warnings"].append("Source registration alone does not verify an acquired artifact.")
            for row in con.execute("SELECT * FROM versions ORDER BY version_id"):
                value = {**dict(row), "facts": json.loads(row["facts"])}
                value["computed"] = temporal_status(value["facts"], as_of)
                value["computed"]["warnings"] += ["Related/scoped document dependency requires review."
                    for r in result["relations"] if row["document_id"] in (r["from_document_id"], r["to_document_id"])]
                value["artifact_assurance"] = "official_endpoint_observed" if any(
                    o["version_id"] == row["version_id"] and json.loads(o["detail"]).get("assurance") == "official_endpoint_observed"
                    for o in result["observations"]) else "local_or_unverified"
                if value["artifact_assurance"] == "local_or_unverified":
                    value["computed"]["warnings"].append("Local bytes have not been matched to an official acquisition; not authoritative evidence.")
                result["versions"].append(value)
            for value in result["versions"]:
                overlapping = [v["version_id"] for v in result["versions"] if v["version_id"] != value["version_id"]
                    and v["document_id"] == value["document_id"] and v["computed"]["state"] == value["computed"]["state"] == "supported_applicable_candidate"]
                if overlapping:
                    value["computed"]["warnings"].append("Unresolved competing versions: " + ",".join(overlapping))
            return result

    def link(self, document_id, topic, evidence, *, version_id=None, service_db=None, service_id=None,
             expected_name=None, expected_agency=None, unresolved_reason=None):
        validate_evidence(evidence)
        if not topic.strip():
            raise ValueError("link_topic_required")
        payload = {"evidence": evidence, "review_status": "human_review_pending", "limitations": "Topic-specific relationship; not entitlement."}
        if service_id is None:
            if not unresolved_reason:
                raise ValueError("unresolved_link_reason_required")
            payload.update(state="unresolved", reason=unresolved_reason)
        else:
            if not service_db or not Path(service_db).is_file():
                raise ValueError("service_database_unavailable")
            with readonly(service_db) as con:
                row = con.execute("SELECT * FROM services WHERE service_id=?", (service_id,)).fetchone()
                if row is None:
                    raise ValueError("service_id_not_observed")
                record = json.loads(row["payload"])
                if (not expected_name or not expected_agency or record.get("service_name") != expected_name
                        or record.get("agency") != expected_agency):
                    raise ValueError("service_identity_confirmation_required")
                payload.update(state="registry_identity_matched_topic_review_pending", source_db=str(Path(service_db).resolve()),
                    service_snapshot={k: record.get(k) for k in ("service_id", "service_name", "agency", "detail_url")},
                    service_version=row["version"], service_status=row["status"], observed_at=now())
        key = identity("link", document_id, version_id, service_id, topic)
        def write(con):
            if version_id and not con.execute("SELECT 1 FROM versions WHERE version_id=? AND document_id=?", (version_id, document_id)).fetchone():
                raise ValueError("version_document_mismatch")
            old = con.execute("SELECT payload FROM service_links WHERE link_id=?", (key,)).fetchone()
            self.audit(con, "link", key, json.loads(old[0]) if old else None, payload, "explicit service/topic association")
            con.execute("INSERT INTO service_links VALUES (?,?,?,?,?,?) ON CONFLICT(link_id) DO UPDATE SET payload=excluded.payload",
                        (key, version_id, document_id, service_id, topic, dumps(payload)))
            return {"link_id": key, **payload}
        return self.mutate(write)
