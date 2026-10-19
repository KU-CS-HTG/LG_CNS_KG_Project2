"""[A. 데이터] 원본(raw) → services.jsonl + SQLite(services.db: 서비스 + 청크)

사용법:
    python scripts/build_services.py                                      # 실제 데이터
    python scripts/build_services.py --sample data/sample/services_sample.jsonl   # 샘플

목록(serviceList)과 지원조건(supportConditions)을 서비스ID로 합친다.
필드명은 2026-10-16 실제 응답으로 확인함.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from src import db  # noqa: E402
from src.chunking import chunk_service  # noqa: E402
from src.config import PROCESSED_DIR, RAW_DIR  # noqa: E402
from src.regions import region_of_agency  # noqa: E402

FIELD = {
    "service_id": "서비스ID", "name": "서비스명", "summary": "서비스목적요약",
    "agency": "소관기관명", "category": "서비스분야", "support_type": "지원유형",
    "target": "지원대상", "criteria": "선정기준", "content": "지원내용",
    "how_to_apply": "신청방법", "deadline": "신청기한", "reception": "접수기관",
    "contact": "전화문의", "url": "상세조회URL",
}
AGENCY_TYPE_FIELD = "소관기관유형"
AGE_MIN_FIELD, AGE_MAX_FIELD = "JA0110", "JA0111"
USER_TYPE_FIELD = "사용자구분"
PERSONAL_TYPES = ("개인", "가구")   # 1단계: 개인 대상만


def clean_text(value) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def to_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def from_api() -> list[dict]:
    services_raw = json.loads((RAW_DIR / "serviceList.json").read_text(encoding="utf-8"))
    cond_path = RAW_DIR / "supportConditions.json"
    conditions = json.loads(cond_path.read_text(encoding="utf-8")) if cond_path.exists() else []
    cond_by_id = {c.get(FIELD["service_id"]): c for c in conditions}

    has_type_field = any(USER_TYPE_FIELD in raw for raw in services_raw)
    if not has_type_field:
        print(f"⚠️ '{USER_TYPE_FIELD}' 필드가 없습니다. 대상 필터 없이 전체를 사용합니다.")

    services, skipped, no_cond = [], 0, 0
    for raw in services_raw:
        sid = raw.get(FIELD["service_id"])
        if not sid:
            continue
        user_type = clean_text(raw.get(USER_TYPE_FIELD))
        if has_type_field and not any(t in user_type for t in PERSONAL_TYPES):
            skipped += 1
            continue
        cond = cond_by_id.get(sid)
        if cond is None:
            no_cond += 1
            cond = {}
        s = {k: clean_text(raw.get(v)) for k, v in FIELD.items()}
        s["region"] = region_of_agency(s["agency"], clean_text(raw.get(AGENCY_TYPE_FIELD)))
        s["min_age"] = to_int(cond.get(AGE_MIN_FIELD), 0)
        s["max_age"] = to_int(cond.get(AGE_MAX_FIELD), 200)
        s["user_type"] = user_type
        s["source"] = "gov24"
        services.append(s)

    print(f"개인 대상 {len(services)}건 (제외 {skipped}건)")
    if no_cond:
        print(f"ℹ️ 지원조건 없는 서비스 {no_cond}건 → 나이 제한 없음(0~200)으로 저장")
    return services


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", help="API 대신 샘플 jsonl 사용")
    args = parser.parse_args()

    if args.sample:
        services = [json.loads(l) for l in Path(args.sample).read_text(encoding="utf-8").splitlines() if l.strip()]
    else:
        services = from_api()

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    with (PROCESSED_DIR / "services.jsonl").open("w", encoding="utf-8") as f:
        for s in services:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    chunks = [c for s in services for c in chunk_service(s)]
    db.create(services, chunks)
    lengths = [len(s.get("criteria", "")) + len(s.get("content", "")) for s in services]
    avg = sum(lengths) / len(lengths) if lengths else 0
    print(f"서비스 {len(services)}건 → 청크 {len(chunks)}개 → {db.DB_PATH}")
    print(f"선정기준+지원내용 평균 {avg:.0f}자 (기획서 7번 '문서 평균 길이'에 사용)")


if __name__ == "__main__":
    main()
