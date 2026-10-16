"""[A. 데이터] 원본(raw) → 팀 공통 형식(services.jsonl)으로 정리

사용법:
    python scripts/build_services.py

목록(serviceList)과 지원조건(supportConditions)을 서비스ID로 합친다.
※ 아래 FIELD / 나이 조건 필드명은 실제 응답을 보고 맞춰야 한다.
"""
import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from src.config import PROCESSED_DIR, RAW_DIR  # noqa: E402

# API 응답 필드명 → 우리 스키마 필드명  (TODO: 실제 응답으로 확인)
FIELD = {
    "service_id": "서비스ID",
    "name": "서비스명",
    "agency": "소관기관명",
    "target": "지원대상",
    "criteria": "선정기준",
    "content": "지원내용",
    "how_to_apply": "신청방법",
    "url": "상세조회URL",
}
# 지원조건의 나이 하한/상한 필드 (TODO: 명세의 코드표로 확인)
AGE_MIN_FIELD = "JA0110"
AGE_MAX_FIELD = "JA0111"


def to_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def guess_region(agency: str) -> str:
    """소관기관명으로 지역 추정. 시·도 이름이 없으면 중앙부처로 보고 '전국'."""
    sido = ["서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종",
            "경기", "강원", "충청북", "충청남", "충북", "충남", "전라북", "전북",
            "전라남", "전남", "경상북", "경북", "경상남", "경남", "제주"]
    for name in sido:
        if agency.startswith(name):
            return agency.split()[0]
    return "전국"


def main():
    services_raw = json.loads((RAW_DIR / "serviceList.json").read_text(encoding="utf-8"))
    cond_path = RAW_DIR / "supportConditions.json"
    conditions = json.loads(cond_path.read_text(encoding="utf-8")) if cond_path.exists() else []
    cond_by_id = {c.get(FIELD["service_id"]): c for c in conditions}

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    out = PROCESSED_DIR / "services.jsonl"
    count = 0
    with out.open("w", encoding="utf-8") as f:
        for raw in services_raw:
            sid = raw.get(FIELD["service_id"])
            if not sid:
                continue
            cond = cond_by_id.get(sid, {})
            service = {k: (raw.get(v) or "").strip() for k, v in FIELD.items()}
            service["region"] = guess_region(service["agency"])
            service["min_age"] = to_int(cond.get(AGE_MIN_FIELD), 0)
            service["max_age"] = to_int(cond.get(AGE_MAX_FIELD), 200)
            f.write(json.dumps(service, ensure_ascii=False) + "\n")
            count += 1
    print(f"{count}건 저장: {out}")


if __name__ == "__main__":
    main()
