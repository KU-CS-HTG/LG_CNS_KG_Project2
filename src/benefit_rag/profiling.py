"""보존 raw의 선택 run을 읽는 오프라인 프로파일. 원본/현재 프로그램은 변경하지 않는다."""

import hashlib
import json
import math
from collections import Counter
from statistics import mean, median

from sqlalchemy import select
from sqlalchemy.orm import Session

from benefit_rag.db import IngestionRun, ProgramVersion, RawResponse
from benefit_rag.domain import DatasetKind
from benefit_rag.gov24 import ENDPOINT, TEXT_FIELDS, SourceError

PROFILE_VERSION = "stored-run-profile-v1"
PROFILE_FIELDS = {"source_record_id": "서비스ID", **TEXT_FIELDS, "official_url": "상세조회URL"}


def _missing_reason(record: dict, field: str) -> str | None:
    if field not in record:
        return "absent"
    value = record[field]
    if value is None:
        return "null"
    if isinstance(value, str) and not value.strip():
        return "empty_or_whitespace"
    return None


def profile_run(engine, run_id: str, *, dataset_kind: DatasetKind, source: str = "gov24") -> dict:
    """분모는 HTTP 200/JSON data 배열에서 관측한 object 행. 적재 성공 건수와 다를 수 있다."""
    with Session(engine) as session:
        run = session.get(IngestionRun, run_id)
        if run is None:
            raise SourceError("run_not_found")
        if run.dataset_kind != dataset_kind or run.source != source:
            raise SourceError("profile_namespace_mismatch")
        if run.status == "running":
            raise SourceError("profile_run_not_finished")
        pages = list(
            session.scalars(
                select(RawResponse)
                .where(RawResponse.run_id == run_id)
                .order_by(RawResponse.page_number)
            )
        )
        # 최초 버전 생성 시 저장한 상태만 사용한다. 다른 run의 최신 URL 상태를 소급하지 않는다.
        versions = list(
            session.scalars(
                select(ProgramVersion).where(
                    ProgramVersion.raw_response_id.in_([p.id for p in pages])
                )
            )
        )
        stored_status = {
            (v.raw_response_id, v.record_index): v.normalized.get("url_status", "not_stored")
            for v in versions
        }
        rows, statuses, unprofiled, raw_refs = [], Counter(), [], []
        entries = malformed_records = 0
        for page in pages:
            raw_refs.append(
                {
                    "raw_response_id": page.id,
                    "page": page.page_number,
                    "sha256": page.sha256,
                    "http_status": page.http_status,
                    "endpoint": page.endpoint,
                    "collected_at": page.collected_at.isoformat(),
                }
            )
            try:
                payload = (
                    json.loads(page.body)
                    if page.http_status == 200 and page.endpoint == ENDPOINT
                    else None
                )
            except (ValueError, UnicodeError):
                payload = None
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                unprofiled.append(page.page_number)
                continue
            entries += len(payload["data"])
            for index, record in enumerate(payload["data"]):
                if not isinstance(record, dict):
                    malformed_records += 1
                    continue
                rows.append(record)
                statuses[stored_status.get((page.id, index), "not_stored_for_observation")] += 1
        fields = {}
        for field, source_field in PROFILE_FIELDS.items():
            missing = Counter(
                reason for row in rows if (reason := _missing_reason(row, source_field))
            )
            lengths = sorted(
                len(row[source_field])
                for row in rows
                if isinstance(row.get(source_field), str) and row[source_field].strip()
            )
            invalid = sum(
                not isinstance(row[source_field], str)
                for row in rows
                if _missing_reason(row, source_field) is None
            )
            fields[field] = {
                "source_field": source_field,
                "denominator": len(rows),
                "missing_count": sum(missing.values()),
                "missing_reasons": dict(sorted(missing.items())),
                "invalid_type_count": invalid,
                "text_length_chars": {
                    "count": len(lengths),
                    "min": min(lengths) if lengths else None,
                    "max": max(lengths) if lengths else None,
                    "mean": mean(lengths) if lengths else None,
                    "median": median(lengths) if lengths else None,
                    "p95_nearest_rank": lengths[math.ceil(len(lengths) * 0.95) - 1]
                    if lengths
                    else None,
                },
            }
        distributions = {}
        for field in ("category", "audience_type"):
            values = Counter(
                row[PROFILE_FIELDS[field]]
                for row in rows
                if isinstance(row.get(PROFILE_FIELDS[field]), str)
                and row[PROFILE_FIELDS[field]].strip()
            )
            distributions[field] = {
                "denominator": len(rows),
                "nonempty_string_count": sum(values.values()),
                "values": dict(sorted(values.items())),
            }
        types = {}
        for key in sorted({key for row in rows for key in row}):
            counts = Counter(type(row[key]).__name__ for row in rows if key in row)
            types[key] = {
                "present_count": sum(counts.values()),
                "types": dict(sorted(counts.items())),
            }
        identifiers = Counter(
            row["서비스ID"]
            for row in rows
            if isinstance(row.get("서비스ID"), str) and row["서비스ID"].strip()
        )
        manifest = {
            "profile_version": PROFILE_VERSION,
            "run_id": run.id,
            "source": source,
            "dataset_kind": dataset_kind,
            "raw_responses": raw_refs,
            "stored_url_statuses": dict(sorted(statuses.items())),
        }
        snapshot_id = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        limitations = [
            "선택 run의 저장 raw 표본이며 전체 혜택 수·인구·지역 분포를 나타내지 않음",
            "필드 분모는 관측 object 행이며 중복·적재 거부·롤백된 페이지의 행도 포함",
            "목록·상세·지원조건의 의미와 자격을 통계로 검증하지 않음",
            "URL 상태는 이 raw에서 생성된 버전의 저장값만 사용; 없으면 미저장",
            "포털 갱신주기·프로젝트 수집주기·개별 정책 현행성은 별개",
        ]
        if dataset_kind == "fixture":
            limitations.append("합성 fixture 통계: 실제 정책 corpus·사용자에게 일반화 금지")
        else:
            limitations.append("live namespace 표시는 인증 성공·데이터 완전성을 보증하지 않음")
        if not run.collection_complete:
            limitations.append(
                "미완료 수집: 누락 페이지·표본 제한·오류로 전체 corpus를 대표하지 않음"
            )
        return {
            "profile_version": PROFILE_VERSION,
            "scope": "stored_run_raw_observations",
            "source": source,
            "dataset_kind": dataset_kind,
            "data_label": "synthetic_fixture" if dataset_kind == "fixture" else "live_namespace",
            "snapshot_id": snapshot_id,
            "run_id": run.id,
            "collection": {
                "status": run.status,
                "complete": run.collection_complete,
                "started_at": run.started_at.isoformat(),
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                "expected_count": run.expected_count,
                "loaded_unique_count": run.collected_count,
                "rejected_count": run.rejected_count,
                "pages_received": run.pages_received,
                "pages_loaded": run.pages_loaded,
                "page_size": run.page_size,
                "max_pages": run.max_pages,
                "issues": run.issues,
            },
            "observed": {
                "array_entries": entries,
                "object_records": len(rows),
                "malformed_records": malformed_records,
                "unprofiled_pages": unprofiled,
                "valid_string_identifier_count": sum(identifiers.values()),
                "distinct_identifier_count": len(identifiers),
                "duplicate_identifier_occurrences": sum(n - 1 for n in identifiers.values()),
            },
            "fields": fields,
            "observed_values": distributions,
            "observed_field_types": types,
            "urls": {
                "denominator": len(rows),
                "nonempty_string_count": fields["official_url"]["text_length_chars"]["count"],
                "stored_status_counts": dict(sorted(statuses.items())),
            },
            "manifest": manifest,
            "limitations": limitations,
        }
