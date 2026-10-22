"""Bounded historical raw replay (원본 재처리); never fetches API or closes missing IDs."""
import argparse
from datetime import date, datetime, timezone
import hashlib
import json
import re
import uuid
from pathlib import Path

from src.ingest import client, pipeline, store
from src.ingest.storage import StoragePaths, resolve_storage
from src.ingest import policy, normalize

SOURCE_ORDER = "validated_page_row_first_occurrence-v1"


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def implementation_fingerprint():
    from src.common import config
    files = ("src/ingest/normalize.py", "src/ingest/regions.py", "src/common/regions.py",
             "src/ingest/policy.py", "src/ingest/merge.py", "src/ingest/chunker.py", "src/ingest/reprocess.py",
             "src/ingest/store.py", "src/ingest/pipeline.py")
    return _digest({name: hashlib.sha256((config.ROOT / name).read_text(encoding="utf-8").encode()).hexdigest() for name in files})


def _stamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
        return parsed.astimezone(timezone.utc) if parsed and parsed.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def acquisition_time(run_id, summary):
    # A run IDs encode acquisition time, not the provider's content update time.
    try:
        encoded = datetime.strptime(run_id.split("-")[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        encoded = None
    return _stamp(summary.get("started_at")) or encoded


def read_raw_run(source_dir, output_dir, run_id):
    source_dir = Path(source_dir).resolve()
    path = source_dir / "run-summary.json"
    if not path.is_file():
        raise ValueError("requires_A_run_summary_not_label_helper_listing")
    summary = json.loads(path.read_text(encoding="utf-8"))
    source_id = summary.get("run_id")
    if source_id != source_dir.name or not isinstance(summary.get("endpoints"), dict):
        raise ValueError("source_run_identity_mismatch")
    if summary.get("operation") == "raw_reprocess":
        raise ValueError("select_original_acquisition_run_not_replay_output")
    collections, checksums = {}, {}
    for endpoint in client.ENDPOINTS:
        expected = summary["endpoints"].get(endpoint)
        pages = sorted(source_dir.glob(endpoint + "-[0-9]*.json"))
        if expected is None:
            if pages or (source_dir / (endpoint + "-summary.json")).exists():
                raise ValueError("unmanifested_endpoint")
            collections[endpoint] = client.Collection(endpoint, diagnostics=["source_endpoint_unavailable"])
            continue
        endpoint_path = source_dir / (endpoint + "-summary.json")
        if not endpoint_path.is_file():
            raise ValueError("source_endpoint_summary_missing")
        actual = json.loads(endpoint_path.read_text(encoding="utf-8"))
        if actual != expected:
            raise ValueError("source_endpoint_summary_mismatch")
        if len(pages) != expected["pages"] or [p.name for p in pages] != [
                f"{endpoint}-{n:05}.json" for n in range(1, len(pages) + 1)]:
            raise ValueError("source_page_sequence_mismatch")
        payloads = [json.loads(p.read_text(encoding="utf-8")) for p in pages]
        for p in [endpoint_path, *pages]:
            checksums[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
        if not payloads:
            if expected["count"] or expected["status"] != "failed":
                raise ValueError("source_pages_missing")
            collections[endpoint] = client.Collection(endpoint, diagnostics=["source_collection_failed"])
            continue

        def fetch(name, **params):
            n = params["page"]
            if n > len(payloads):
                raise client.CollectionError("source_page_unavailable")
            payload = payloads[n - 1]
            if str(payload.get("code", payload.get("resultCode", ""))) not in ("", "0", "00", "200", "INFO-000"):
                raise client.CollectionError("historical_api_error")
            return payload

        decoded = client.collect_endpoint(endpoint, run_id=run_id, raw_dir=output_dir,
            per_page=payloads[0]["perPage"], max_pages=len(pages) if expected["status"] == "sample" else None,
            fetch_page=fetch)
        for field in ("count", "pages", "total", "status", "conflicts"):
            if decoded.summary()[field] != expected[field]:
                raise ValueError("source_decoding_summary_mismatch")
        decoded.verified = decoded.authoritative = False
        decoded.limited = True
        collections[endpoint] = decoded
        client.write_json(Path(output_dir) / run_id / (endpoint + "-summary.json"), decoded.summary())
    if not collections["serviceList"].rows:
        raise ValueError("source_has_no_usable_listing")
    checksums[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return summary, collections, checksums


def _ordering(old, source_id, acquired, paths):
    if not old:
        return None
    if old["reason"] == "api_disappeared":
        return "historical_replay_cannot_reverse_api_disappearance"
    payload = old["payload"]
    prior = payload.get("_acquisition", {})
    prior_id = prior.get("run_id") or old["last_run"]
    if prior_id == source_id:
        return None
    old_time = _stamp(prior.get("acquired_at"))
    if old_time is None and old["last_run"]:
        old_run = store.get_run(old["last_run"], storage=paths)
        if old_run:
            prior_source = old_run["summary"].get("source_acquisition", {})
            if prior_source.get("run_id") == source_id:
                return None
            old_time = _stamp(prior_source.get("acquired_at")) or acquisition_time(old["last_run"], old_run)
    if acquired is None or old_time is None:
        return "observation_order_unknown_deferred"
    if acquired <= old_time:
        return "older_or_equal_historical_observation_deferred"
    return None


def reprocess(source_dir, *, storage, limit=100, today=None, offset=0, checkpoint=None, resume=False):
    """Replay [offset, offset+limit) in ONE acquisition; optional durable DB checkpoint."""
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("replay_limit_must_be_1_to_1000")
    if type(offset) is not int or offset < 0 or (resume and offset != 0):
        raise ValueError("invalid_replay_offset")
    if checkpoint is not None and (not isinstance(checkpoint, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", checkpoint)):
        raise ValueError("invalid_checkpoint_name")
    if resume and not checkpoint:
        raise ValueError("resume_requires_checkpoint")
    reference = date.fromisoformat(today or pipeline.seoul_today()).isoformat()
    paths = storage.validated()
    source_root = Path(source_dir).resolve()
    if (source_root.is_relative_to(paths.raw_dir) or paths.raw_dir.is_relative_to(source_root)
            or paths.db_path.is_relative_to(source_root)):
        raise ValueError("replay_output_must_not_contain_original_source")
    run_id = client.new_run_id()
    with store.ingestion_lock(storage=paths):
        original, collections, checksums = read_raw_run(source_dir, paths.raw_dir, run_id)
        endpoint_summaries = {k: v.summary() for k, v in collections.items()}
        listing_ids = {r["서비스ID"] for r in collections["serviceList"].rows}
        source_join = {"unmatched_" + name: len({r["서비스ID"] for r in collections[name].rows} - listing_ids)
                       for name in ("serviceDetail", "supportConditions")}
        source_id = original["run_id"]
        acquired = acquisition_time(source_id, original)
        total = len(collections["serviceList"].rows)
        source_conflicts = list(collections["serviceList"].conflicts)
        progress = None
        if checkpoint:
            previous = store.read_state("replay:" + checkpoint, storage=paths)
            identity = store.read_state("database_id", storage=paths)
            from src.common import config
            binding = {"source_run": source_id, "source_fingerprint": _digest(checksums), "order": SOURCE_ORDER,
                       "target_db_id": identity or uuid.uuid4().hex, "target_db_path": str(paths.db_path),
                       "policy": policy.POLICY_ID, "normalization": normalize.NORMALIZATION_VERSION,
                       "implementation": implementation_fingerprint(), "reference_date": reference,
                       "settings": {"max_chunk_chars": config.MAX_CHUNK_CHARS, "youth_age_range": list(config.YOUTH_AGE_RANGE),
                                    "youth_keywords": list(config.YOUTH_KEYWORDS)},
                       "reference_semantics": "Asia/Seoul; deadline day inclusive"}
            if resume:
                if not previous:
                    raise ValueError("checkpoint_missing_restart_initial_segment")
                if previous["binding"] != binding:
                    raise ValueError("checkpoint_binding_changed_use_new_checkpoint")
                offset = previous["next_position"]
            elif previous:
                raise ValueError("checkpoint_exists_use_resume_or_new_name")
            progress = {"name": checkpoint, "binding": binding, "previous_generation": previous["generation"] if previous else 0,
                        "deferred_remaining": dict(previous["deferred_remaining"]) if previous else {}}
            for sid in source_conflicts:
                progress["deferred_remaining"][sid] = {"service_id": sid, "position": None, "reason": "conflicting_source_listing_id"}
        if offset > total:
            raise ValueError("offset_beyond_source_end")
        selected = collections["serviceList"].rows[offset:offset + limit]
        print(f"원본 {source_id} / 전체 {total} / 선택 [{offset}, {offset + len(selected)}) / 쓰기 전 검증 완료")
        deferred, eligible = [], []
        details = {r["서비스ID"] for r in collections["serviceDetail"].rows}
        for row in selected:
            sid = row["서비스ID"]
            old = store.get_record(sid, storage=paths)
            reason = _ordering(old, source_id, acquired, paths)
            if reason is None and sid not in details:
                reason = "historical_detail_unavailable_deferred"
            if reason:
                deferred.append({"service_id": sid, "reason": reason})
            else:
                eligible.append(row)
        collections["serviceList"].rows = eligible
        eligible_ids = {r["서비스ID"] for r in eligible}
        for endpoint in ("serviceDetail", "supportConditions"):
            collections[endpoint].rows = [r for r in collections[endpoint].rows if r["서비스ID"] in eligible_ids]
        services, diagnostics = pipeline._join(collections, storage=paths)
        normalized_ids = {s["service_id"] for s in services}
        deferred.extend({"service_id": row["서비스ID"], "reason": "normalization_failed"}
                        for row in eligible if row["서비스ID"] not in normalized_ids)
        source = {"run_id": source_id, "acquired_at": acquired.isoformat() if acquired else None,
                  "kind": "historical_raw", "checksums": checksums}
        def historical(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key == "observation" and isinstance(child, str) and child.startswith("fresh_"):
                        value[key] = child.replace("fresh_", "historical_", 1)
                    else:
                        historical(child)
            elif isinstance(value, list):
                for child in value:
                    historical(child)

        for service in services:
            sid = service["service_id"]
            old = store.get_record(sid, storage=paths)
            # An auxiliary source absent in this acquisition may be retained, never relabeled fresh.
            for name, state in service["_observations"].items():
                if state == "fresh":
                    service["_observations"][name] = "historical"
            for value in service["_field_provenance"].values():
                if value["observation"].startswith("fresh_"):
                    value["source_acquisition"] = source_id
            historical(service["_field_provenance"])
            historical(service["_merge_conflicts"])
            # Extraction evidence also carries observation provenance; historical replay is never fresh.
            from src.ingest.regions import apply_region_evidence
            apply_region_evidence(service)
            historical(service["eligibility"])
            service["_acquisition"] = source
            # A failed conditions collection cannot establish that a minimal excluded record can reopen.
            if old and not service["_inclusion_complete"] and (old["reason"] in ("youth_scope_exit", "policy_scope_exit")
                                                              or not policy.is_included_service(service)):
                deferred.append({"service_id": sid, "reason": "historical_conditions_unavailable_deferred"})
        blocked = {r["service_id"] for r in deferred}
        services = [s for s in services if s["service_id"] not in blocked]
        diagnostics.pop("field_conflicts", None)
        next_position = offset + len(selected)
        if progress:
            for row in selected:
                progress["deferred_remaining"].pop(row["서비스ID"], None)
            positions = {row["서비스ID"]: offset + n for n, row in enumerate(selected)}
            progress["deferred_remaining"].update({d["service_id"]: {**d, "position": positions[d["service_id"]]} for d in deferred})
            progress.update(next_position=next_position, total_source_records=total, end_of_source=next_position == total)
        store.start_run(run_id, storage=paths)
        try:
            result = store._apply_run_locked(run_id, services, collections["serviceList"], full=False,
                today=reference, storage=paths, checkpoint=progress,
                summary={"run_id": run_id, "status": "sample", **paths.summary(), "operation": "raw_reprocess",
                    "source_acquisition": source, "source_run_status": original["status"],
                    "selected": len(selected), "limit": limit, "replay_deferred": deferred,
                    "offset": offset, "attempted": len(selected), "total_source_records": total,
                    "next_position": next_position, "end_of_source": next_position == total,
                    "source_order": SOURCE_ORDER, "reference_date": reference,
                    "source_conflicting_ids": source_conflicts,
                    "processed_ids": [s["service_id"] for s in services],
                    "source_join": source_join,
                    "diagnostics": diagnostics, "endpoints": endpoint_summaries,
                    "skipped_decisions": ["Historical replay never authorizes disappearance; no live observation."]})
            # Deferred decisions are part of the same durable run transaction above.
            client.write_json(paths.raw_dir / run_id / "run-summary.json", result)
        except BaseException:
            prior = store.get_run(run_id, storage=paths)
            if prior["status"] == "running":
                store.finish_run(run_id, "failed", {"operation": "raw_reprocess"}, storage=paths)
            raise
    pipeline._print_summary(result)
    print(f"원본 재처리: 선택 {len(selected)} / 명시 유보 {len(deferred)} / 원본 실행 {source_id}")
    print(f"처리 {result['processed']} / 동일 {result['unchanged']} / 다음 위치 {next_position} / 원본 끝={result['end_of_source']}"
          f" / 남은 유보 {len(progress['deferred_remaining']) if progress else len(deferred)}")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Historical A raw replay; no API/index/ack")
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--raw-dir", type=Path, required=True, help="Separate replay output directory")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--offset", type=int, default=0, help="Zero-based validated page/row order")
    cursor = parser.add_mutually_exclusive_group()
    cursor.add_argument("--checkpoint", help="Start a named DB checkpoint; never overwrite an existing name")
    cursor.add_argument("--resume", help="Resume a named DB checkpoint with identical bindings")
    parser.add_argument("--today", help="Fixed ISO reference date; default Asia/Seoul today")
    parser.add_argument("--validation", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.today:
            from datetime import date
            date.fromisoformat(args.today)
        paths = resolve_storage("validation" if args.validation else "live", db_path=args.db_path, raw_dir=args.raw_dir)
        reprocess(args.source_run, storage=paths, limit=args.limit, today=args.today, offset=args.offset,
                  checkpoint=args.checkpoint or args.resume, resume=bool(args.resume))
        return 0
    except Exception as exc:
        from src.ingest.handoff import _error_code
        print(json.dumps({"error": type(exc).__name__, "code": _error_code(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
