"""A 수집·정제·증분 적재·분할. B 색인 쓰기는 이 모듈에서 실행하지 않는다."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.common import config
from src.common.schema import Chunk
from src.ingest import client, store
from src.ingest.merge import merge_fields
from src.ingest.storage import StoragePaths, resolve_storage


def seoul_today() -> str:
    return datetime.now(timezone(timedelta(hours=9))).date().isoformat()


def _join(collections: dict[str, client.Collection], *, storage: StoragePaths | None = None) -> tuple[list[dict], dict]:
    listing = collections["serviceList"]
    detail = {r["서비스ID"]: r for r in collections["serviceDetail"].rows}
    conditions = {r["서비스ID"]: r for r in collections["supportConditions"].rows}
    listed = {r["서비스ID"] for r in listing.rows}
    diagnostics = {
        "unmatched_detail": len(detail.keys() - listed),
        "unmatched_conditions": len(conditions.keys() - listed),
        "missing_detail": 0, "missing_conditions": 0, "invalid_records": 0,
        "quality_issues": {}, "broad_keyword_examples": [], "broad_age_examples": [],
        "field_conflicts": [],
    }
    services = []
    for row in listing.rows:
        sid = row["서비스ID"]
        old = store.get_record(sid, storage=storage)
        previous = old["payload"].get("_sources", {}) if old else {}
        sources = {
            "serviceList": row,
            "serviceDetail": detail.get(sid, previous.get("serviceDetail")),
            "supportConditions": conditions.get(sid, previous.get("supportConditions")),
        }
        for endpoint, metric in (("serviceDetail", "missing_detail"), ("supportConditions", "missing_conditions")):
            if sid not in (detail if endpoint == "serviceDetail" else conditions):
                diagnostics[metric] += 1
        try:
            merged, provenance, conflicts = merge_fields(row, detail.get(sid), previous,
                previous_provenance=old["payload"].get("_field_provenance", {}) if old else {})
            diagnostics["field_conflicts"].extend({"service_id": sid, **c} for c in conflicts)
            service = store.clean_service(merged, sources["supportConditions"])
        except ValueError:
            diagnostics["invalid_records"] += 1
            continue
        service["_sources"] = sources
        service["_field_provenance"] = provenance
        from src.ingest.regions import apply_region_evidence
        from src.ingest.normalize import content_hash
        apply_region_evidence(service)
        service["content_hash"] = content_hash(service)
        service["_merge_conflicts"] = conflicts
        service["_inclusion_complete"] = sid in detail and sid in conditions
        service["_observations"] = {
            name: "fresh" if sid in records else ("retained" if sources[name] is not None else "unavailable")
            for name, records in (("serviceDetail", detail), ("supportConditions", conditions))
        }
        for issue in service["data_quality"]:
            diagnostics["quality_issues"][issue] = diagnostics["quality_issues"].get(issue, 0) + 1
        reasons = service["inclusion_reasons"]
        if len(diagnostics["broad_age_examples"]) < 3 and any(
                r["reason"] == "verified_age_overlap" and
                (r["range"][0] < config.YOUTH_AGE_RANGE[0] or r["range"][1] > config.YOUTH_AGE_RANGE[1])
                for r in reasons):
            diagnostics["broad_age_examples"].append({"service_id": sid, "reasons": reasons})
        if len(diagnostics["broad_keyword_examples"]) < 3 and any(
                r.get("keyword") in ("구직", "미취업") for r in reasons):
            diagnostics["broad_keyword_examples"].append({"service_id": sid, "reasons": reasons})
        services.append(service)
    return services, diagnostics


def _print_summary(summary: dict) -> None:
    print(f"실행 {summary['run_id']} / 상태: {summary['status']}")
    print(f"모드: {summary['mode']} / DB: {summary['db_path']} / 원본: {summary['raw_dir']}")
    for name, endpoint in summary.get("endpoints", {}).items():
        print(f"수집 {name}: {endpoint['status']} / {endpoint['count']}건 / {endpoint['pages']}페이지"
              f" / 계약 검증={endpoint['verified']} / 진단={endpoint['diagnostics']}")
    print(f"데이터 포함 {summary.get('included', 0)} / 제외 {summary.get('excluded', 0)} / 정책 {summary.get('inclusion_policy_id')}")
    print(f"신규 {summary.get('new', 0)} / 변경 {summary.get('changed', 0)} / "
          f"동일 {summary.get('unchanged', 0)} / 판단 유보 {summary.get('deferred', 0)}")
    print(f"상태·제거 사유: {summary.get('removals', {})}")
    print(f"관측 상태 사유: {summary.get('status_reasons', {})}")
    print(f"반환 조각 {summary.get('chunks', 0)} / 미확인 인계 {summary.get('pending_changes', 0)}"
          f" / 제거 인계 {summary.get('pending_removals', 0)}")
    print(f"실패·진단: {summary.get('diagnostics', {})}")
    print(f"생략한 결정: {summary.get('skipped_decisions', [])}")


def _execute(full: bool = False, *, max_pages: int | None = None, per_page: int = 500,
             fetch_page=None, storage: StoragePaths | None = None,
             mode: str | None = None) -> tuple[list[Chunk], dict]:
    if fetch_page is not None:
        mode = "test" if storage is not None and storage.mode == "test" else "offline"
    else:
        mode = mode or "live"
    if mode in ("offline", "test") and fetch_page is None:
        raise ValueError("offline_mode_requires_fixture_source")
    if mode == "validation" and (type(max_pages) is not int or max_pages < 1):
        raise ValueError("validation_requires_positive_page_limit")
    if type(per_page) is not int or not 1 <= per_page <= 1000:
        raise ValueError("per_page_must_be_1_to_1000")
    if max_pages is not None and (type(max_pages) is not int or max_pages < 1):
        raise ValueError("sample_pages_must_be_positive")
    paths = (StoragePaths(storage.db_path, storage.raw_dir, mode).validated()
             if storage is not None else resolve_storage(mode))
    run_id = client.new_run_id()
    with store.ingestion_lock(storage=paths):
        store.start_run(run_id, storage=paths)
        try:
            collections = {
                endpoint: client.collect_endpoint(endpoint, run_id=run_id, per_page=per_page,
                                                  max_pages=max_pages, fetch_page=fetch_page, raw_dir=paths.raw_dir)
                for endpoint in client.ENDPOINTS
            }
            if mode != "live":
                for collection in collections.values():
                    collection.authoritative = False
                    if mode in ("offline", "test"):
                        collection.verified = False
                    client.write_json(paths.raw_dir / run_id / f"{collection.endpoint}-summary.json", collection.summary())
            services, diagnostics = _join(collections, storage=paths)
            acquisition = {"run_id": run_id, "acquired_at": store.get_run(run_id, storage=paths)["started_at"]}
            for service in services:
                service["_acquisition"] = acquisition
                for value in service.get("_field_provenance", {}).values():
                    if value["observation"].startswith("fresh_"):
                        value["source_acquisition"] = run_id
                from src.ingest.regions import apply_region_evidence
                apply_region_evidence(service)
            conflicts = diagnostics.pop("field_conflicts")
            client.write_json(paths.raw_dir / run_id / "merge-conflicts.json", conflicts)
            diagnostics["field_conflict_count"] = len(conflicts)
            diagnostics["field_conflict_examples"] = [
                {key: c[key] for key in ("service_id", "field", "source", "observation", "chosen_value", "reason")}
                for c in conflicts[:10]
            ]
            states = [c.status for c in collections.values()]
            if all(state == "complete" for state in states):
                status = "complete"
            elif all(state in ("complete", "sample") for state in states):
                status = "sample"
            else:
                status = "partial" if any(c.rows for c in collections.values()) else "failed"
            listing = collections["serviceList"]
            skipped = []
            if not listing.can_close_missing:
                skipped.append("완전성·권위성 미확인: API 소실 판정 생략")
            if diagnostics["missing_detail"] or diagnostics["missing_conditions"]:
                skipped.append("누락 상세·조건은 기존 관측 보존; 근거 없는 범위 이탈·재개 판단 유보")
            if diagnostics["invalid_records"]:
                status = "partial"
            summary = {
                "run_id": run_id, "status": status, "full": full, **paths.summary(),
                "endpoints": {k: v.summary() for k, v in collections.items()},
                "diagnostics": diagnostics, "skipped_decisions": skipped,
            }
            summary = store._apply_run_locked(run_id, services, listing, full=full, today=seoul_today(),
                                      summary=summary, storage=paths)
            # The authoritative summary is committed with DB/events. Failure to write
            # this convenience raw copy cannot undo that transaction.
            client.write_json(paths.raw_dir / run_id / "run-summary.json", summary)
            chunks = store.pending_chunks(storage=paths)
        except BaseException as exc:
            prior = store.get_run(run_id, storage=paths)
            if prior and prior["status"] == "running":
                store.finish_run(run_id, "failed", {
                    "run_id": run_id, "status": "failed", "error": type(exc).__name__, **paths.summary(),
                }, storage=paths)
            raise
    _print_summary(summary)
    return chunks, summary


def run(full: bool = False) -> list[Chunk]:
    """Collect, commit, return all unacknowledged replacement chunks (possibly replayed)."""
    return _execute(full)[0]


def _fixture_fetch(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("synthetic") is not True:
        raise ValueError("offline fixture must be explicitly synthetic")
    endpoints = data["endpoints"]

    def fetch(endpoint, **params):
        try:
            return endpoints[endpoint][params["page"] - 1]
        except (KeyError, IndexError):
            raise client.CollectionError("fixture_page_unavailable") from None

    return fetch


def run_fixture(path: Path, full: bool = False, *, db_path: Path | None = None,
                raw_dir: Path | None = None, max_pages: int | None = None,
                per_page: int = 500) -> tuple[list[Chunk], dict]:
    """Synthetic programmatic entry point; same enforced isolation as the CLI."""
    paths = resolve_storage("offline", db_path=db_path, raw_dir=raw_dir)
    return _execute(full, fetch_page=_fixture_fetch(Path(path)), storage=paths,
                    max_pages=max_pages, per_page=per_page)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="보조금24 A 수집·정제·증분 인계")
    parser.add_argument("--full", action="store_true", help="활성 조각 전체 재생성 (DB·이력 보존)")
    parser.add_argument("--sample-pages", type=int, help="엔드포인트별 최대 페이지, 항상 표본으로 표시")
    parser.add_argument("--per-page", type=int, default=500)
    parser.add_argument("--offline-fixture", type=Path, help="합성 응답으로 실행, 네트워크 호출 없음")
    parser.add_argument("--validation", action="store_true", help="별도 DB·원본 경로의 제한된 라이브 검증")
    parser.add_argument("--db-path", type=Path, help="실행별 SQLite 경로 (기본 live 경로는 유지)")
    parser.add_argument("--raw-dir", type=Path, help="실행별 원본 경로")
    args = parser.parse_args(argv)
    if args.sample_pages is not None and args.sample_pages < 1:
        parser.error("--sample-pages must be positive")
    if not 1 <= args.per_page <= 1000:
        parser.error("--per-page must be 1..1000")
    if args.validation and (args.offline_fixture is not None or args.sample_pages is None):
        parser.error("--validation requires --sample-pages and cannot use --offline-fixture")
    try:
        if args.offline_fixture is not None:
            _, summary = run_fixture(args.offline_fixture, args.full, db_path=args.db_path,
                                      raw_dir=args.raw_dir, max_pages=args.sample_pages, per_page=args.per_page)
        else:
            mode = "validation" if args.validation else "live"
            paths = resolve_storage(mode, db_path=args.db_path, raw_dir=args.raw_dir)
            _, summary = _execute(args.full, max_pages=args.sample_pages, per_page=args.per_page,
                                  storage=paths, mode=mode)
        return 0 if summary["status"] in ("complete", "sample") else 1
    except Exception as exc:
        # Exception representations may include authenticated URLs or source values.
        detail = str(exc) if type(exc) is ValueError and str(exc).isascii() else type(exc).__name__
        print(f"실행 중단: {detail} (저장 경로 및 SQLite 실행 이력 확인)")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
